"""Fast path: solve an easy issue in ONE model call, then prove it the same way the agent is proven.

The model gets the issue, the likely files in full (within a budget), the project overview, the error the
issue's own code produced, and an example test from the project. It answers once with SEARCH/REPLACE
edits plus a small test that must FAIL on the current code. The harness applies the edits with the same
tolerant editor the agent uses, then the submit gate runs that test and the related existing tests on the
original and on the patched code. Proven (fail -> pass, nothing regressed): done, one call. Otherwise the
agent loop starts, told exactly what the quick attempt tried and why it was not accepted.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..llm.base import Cancelled, ModelUnresponsive, short_error
from ..repo.git import SCRATCH_DIRNAME
from .verify import Verification, render_checks

FAST_PROMPT = """You are fixing a GitHub issue in the repository at {root}. You get ONE reply: no tools, no follow-up questions.

<issue>
{issue}
</issue>

<repository_overview>
{overview}
</repository_overview>
{error_block}
<files>
{files}
</files>
{example_test}
The project's tests run with: {test_command}

Reply in exactly this format, starting directly with "DIAGNOSIS:" (no preamble, no thinking out loud).

DIAGNOSIS: <one or two sentences: the root cause>

Then every source edit, each as (the file path alone on the line above the block, e.g. {example_path}):
{example_path}
<<<<<<< SEARCH
exact lines copied from the file above (enough lines to be unique)
=======
the replacement lines
>>>>>>> REPLACE

Then ONE new test file that FAILS on the current code and PASSES after your edits. Use the project's own test
framework and imports, and test the behaviour the issue describes (including sibling cases of the same bug):
{scratch}/test_issue{ext}
<<<<<<< SEARCH
=======
<the whole test file>
>>>>>>> REPLACE

TEST_COMMAND: <the one command that runs just that test>

Rules: fix the root cause in the source code; never edit existing tests; keep the change as small as the issue
allows; if the same bug appears in several places, include every edit.
"""

FINISH_NOW = ("You ran out of space while thinking. Stop analysing now. Reply with ONLY the final answer in the exact "
              "format: DIAGNOSIS:, then each edit (file path line, SEARCH text copied exactly from the files you were shown, "
              "REPLACE), then the test file, then TEST_COMMAND. No explanation.")

BLOCK_RE = re.compile(r"<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE", re.S)


def near_miss_apply(text: str, search: str, replace: str, min_ratio: float = 0.9) -> Optional[Tuple[str, int]]:
    """Apply a SEARCH/REPLACE whose SEARCH is a slightly misremembered copy of the file (measured on real repos:
    5 of 13 edits were 82-95% similar to the real code). Only when one region is >= min_ratio similar AND every
    line the edit changes matches the file exactly: the real text is kept for the misremembered context lines.
    Returns (new_text, first_line) or None. The proof still has to pass afterwards."""
    import difflib

    lines = text.splitlines(keepends=True)
    s_lines, r_lines = search.splitlines(), replace.splitlines()
    n = len(s_lines)
    if n < 2 or len(lines) > 8000:
        return None
    norm = [l.strip() for l in s_lines]
    real = [l.strip() for l in lines]
    target = "\n".join(norm)
    scored = []
    for size in sorted({max(1, n + d) for d in (-2, -1, 0, 1, 2)}):
        for st in range(0, len(lines) - size + 1):
            sm = difflib.SequenceMatcher(None, target, "\n".join(real[st:st + size]), autojunk=False)   # character level
            if sm.real_quick_ratio() >= min_ratio and sm.quick_ratio() >= min_ratio:
                r = sm.ratio()
                if r >= min_ratio:
                    scored.append((r, st, size))
    if not scored:
        return None
    scored.sort(reverse=True)
    r0, st, size = scored[0]
    if any(st2 + size2 <= st or st2 >= st + size for _, st2, size2 in scored[1:]):
        return None                                      # a second, separate region matches as well: ambiguous
    window = real[st:st + size]
    exact = {}                                           # SEARCH line index -> file line index, exact matches only
    for a, b, k in difflib.SequenceMatcher(None, norm, window, autojunk=False).get_matching_blocks():
        for t in range(k):
            exact[a + t] = st + b + t
    edits = [op for op in difflib.SequenceMatcher(None, s_lines, r_lines, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not edits:
        return None
    out = list(lines)
    for tag, i1, i2, j1, j2 in reversed(edits):
        new = [l + "\n" for l in r_lines[j1:j2]]
        if tag == "insert":
            if i1 in exact:
                at = exact[i1]
            elif i1 - 1 in exact:
                at = exact[i1 - 1] + 1
            else:
                return None
            out[at:at] = new
            continue
        idx = [exact.get(i) for i in range(i1, i2)]
        if None in idx or idx != list(range(idx[0], idx[0] + len(idx))):
            return None                                  # the lines being changed are themselves misremembered
        out[idx[0]:idx[-1] + 1] = new
    return "".join(out), st + 1


@dataclass
class FastResult:
    ok: bool = False
    stage: str = ""                 # where it stopped: no-reply | parse | apply | proof | review | accepted
    reason: str = ""
    diagnosis: str = ""
    patch: str = ""
    test_path: str = ""
    test_command: str = ""
    verification: Optional[Verification] = None
    review: Optional[Dict[str, Any]] = None
    elapsed_s: float = 0.0
    edits: List[Tuple[str, str, str]] = field(default_factory=list)

    def lessons(self) -> str:
        """What the agent loop is told when the fast path is not enough."""
        out = [f"- A one-shot attempt ({self.stage}) was not accepted: {self.reason}"]
        if self.diagnosis:
            out.append(f"- Its diagnosis: {self.diagnosis[:600]}")
        if self.patch.strip():
            p = self.patch if len(self.patch) < 3500 else self.patch[:3500] + "\n[... truncated ...]"
            out.append(f"- Its patch:\n```diff\n{p}\n```")
        if self.verification is not None:
            out.append("- Proof result:\n" + render_checks(self.verification.checks))
        if self.review and self.review.get("concerns"):
            out.append("- Reviewer concerns:\n" + "\n".join(f"  - {c}" for c in self.review["concerns"][:5]))
        return "\n".join(out)


def _numbered_block(path: str, text: str, limit: int) -> str:
    body = text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more characters not shown ...]"
    return f'<file path="{path}">\n{body}\n</file>'


def gather_files(root: Path, candidates: List[str], budget: int = 60000, per_file: int = 24000) -> str:
    parts, used = [], 0
    for rel in candidates:
        f = root / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        chunk = _numbered_block(rel, text, min(per_file, max(2000, budget - used)))
        if used + len(chunk) > budget and parts:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


PATHISH = re.compile(r"^[\w@.+-][\w@.+/-]*\.[A-Za-z0-9]{1,8}$|^[\w@.+-]+(?:/[\w@.+-]+)+$")


def _path_above(before: str) -> str:
    """The file path written above an edit block, tolerating blank lines, markdown and labels."""
    for line in reversed(before.splitlines()[-6:]):
        raw = line.strip()
        if not raw or raw.startswith("```"):
            continue
        cand = re.sub(r"^(?:#+\s*|[-*]\s+|\d+[.)]\s+)", "", raw)
        cand = cand.strip("*_` ")
        cand = re.sub(r"^(?:file(?:name)?|path)\s*[:=]\s*", "", cand, flags=re.I).strip("*_` ")
        cand = cand.rstrip(":").strip("*_` ")
        if cand.startswith("./"):
            cand = cand[2:]
        if PATHISH.match(cand):
            return cand
        if len(raw.split()) > 3:          # a sentence: the path is not above this block
            return ""
    return ""


def parse_reply(text: str) -> Tuple[str, List[Tuple[str, str, str]], str]:
    """-> (diagnosis, [(path, search, replace)], test_command)."""
    text = re.sub(r"```[\w+-]*\n?", "", text or "")
    diag = ""
    m = re.search(r"DIAGNOSIS:\s*(.+?)(?:\n\s*\n|\n(?=\S+\n<<<<<<<))", text, re.S)
    if m:
        diag = " ".join(m.group(1).split())
    edits = []
    for bm in BLOCK_RE.finditer(text):
        edits.append((_path_above(text[:bm.start()]), bm.group(1), bm.group(2)))
    cmd = ""
    mc = re.search(r"TEST_COMMAND:\s*`?([^\n`]+)`?", text)
    if mc:
        cmd = mc.group(1).strip()
    return diag, edits, cmd


class FastPath:
    def __init__(self, model, root: Path, info, git, toolbox, gate, events, reviewer=None) -> None:
        self.model, self.root, self.info, self.git, self.toolbox, self.gate, self.events = model, root, info, git, toolbox, gate, events
        self.reviewer = reviewer

    def _example_test(self, tests: List[str]) -> Tuple[str, str]:
        for rel in tests[:3]:
            f = self.root / rel
            if f.is_file():
                txt = f.read_text(errors="replace")
                return f'<example_test path="{rel}">\n{txt[:3500]}\n</example_test>\n', Path(rel).suffix or ".py"
        return "", ".py" if (self.info.primary_language or "").lower() == "python" else ".js"

    def run(self, issue_text: str, snippet_block: str, src: List[str], tests: List[str]) -> FastResult:
        t0 = time.time()
        res = FastResult()
        self.events.emit("phase", name="fix")
        self.events.emit("log", level="info", message="fast path: one call with the likely files, then prove it")
        example, ext = self._example_test(tests)
        if ext not in (".py", ".js", ".ts", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java"):
            ext = ".py"
        prompt = FAST_PROMPT.format(
            root=self.root, issue=issue_text[:14000], overview=(self.info.overview or "")[:5000],
            error_block=(f"\n<what_happens_when_the_issue_code_runs>\n{snippet_block[:4000]}\n</what_happens_when_the_issue_code_runs>\n" if snippet_block else ""),
            files=gather_files(self.root, src), example_test=example,
            test_command=self.info.test_command or "(unknown)", scratch=SCRATCH_DIRNAME, ext=ext,
            example_path=src[0] if src else f"src/module{ext}")   # a real path: weak models copy placeholders verbatim
        msgs = [{"role": "system", "content": "You are an expert software engineer. Answer in the exact format requested."},
                {"role": "user", "content": prompt}]
        for rnd in (1, 2):             # a second round gets the exact reason the first one was not accepted
            reply = self._ask(msgs, res)
            if reply is None:
                return self._done(res, t0)
            if "<<<<<<< SEARCH" not in reply and (self._stop in ("length", "max_tokens") or len(reply) > 12000):
                # measured on real repos: a model that reasons in its answer ran out of room before any edit
                # (6 of 13 first replies). Its reasoning is kept; one short call turns it into the answer.
                self._keep(reply, rnd * 10)
                self.events.emit("log", level="info", message="fast path: the reply ran out of room while reasoning; "
                                 "asking for the final answer")
                done = self._ask(msgs + [{"role": "assistant", "content": reply},
                                         {"role": "user", "content": FINISH_NOW}], res)
                if done is None:
                    return self._done(res, t0)
                reply = done
            self._keep(reply, rnd)
            feedback = self._attempt(reply, res)
            if res.ok or rnd == 2 or not feedback:
                return self._done(res, t0)
            self.events.emit("log", level="info", message=f"fast path round 2 ({res.stage}): {res.reason[:140]}")
            msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": feedback}]
            res.stage, res.reason, res.test_path = "", "", ""
        return self._done(res, t0)

    def _ask(self, msgs, res: FastResult) -> Optional[str]:
        self._stop = ""
        for tries in range(3):   # a busy endpoint is not a hard issue: wait it out rather than escalate
            try:
                resp = self.model.chat(msgs, tools=None, temperature=0.0)
                text = resp.text or ""
                self._stop = (resp.stop_reason or "").lower()
                self.events.emit("llm", text=text[:1500], total_tokens=self.model.usage.total_tokens, cached_tokens=0)
                return text
            except (ModelUnresponsive, Cancelled):
                raise                                  # no point escalating to more calls on a dead endpoint
            except Exception as e:  # noqa: BLE001 - the agent loop is the fallback
                busy = any(k in str(e).lower() for k in ("429", "rate", "busy", "timed out", "timeout", "overloaded"))
                if busy and tries < 2:
                    self.events.emit("log", level="warn", message="fast path: endpoint busy, waiting before retrying the same call")
                    time.sleep(15 * (tries + 1))
                    continue
                res.stage, res.reason = "no-reply", short_error(e, 160)
                return None
        return None

    def _keep(self, reply: str, rnd: int) -> None:
        """The full reply goes into the run's scratch folder (moved into the evidence bundle)."""
        try:
            d = self.root / SCRATCH_DIRNAME
            d.mkdir(exist_ok=True)
            (d / f"fastpath_reply_{rnd}.txt").write_text(reply)
        except OSError:
            pass

    def _resolve_path(self, search: str) -> str:
        """No usable path given: the SEARCH text is copied from a file, so find the file that contains it."""
        body = search.strip()
        if not body:
            return ""
        first = body.splitlines()[0].strip()
        for rel in list(getattr(self.info, "files", []) or [])[:5000]:
            f = self.root / rel
            try:
                if not f.is_file() or f.stat().st_size > 500_000:
                    continue
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if first in text and (body in text or len(body.splitlines()) == 1):
                return rel
        return ""

    def _contains(self, rel: str, search: str) -> bool:
        try:
            return search.strip() in (self.root / rel).read_text(errors="replace")
        except OSError:
            return False

    def _attempt(self, reply: str, res: FastResult) -> str:
        """Apply and prove one reply. '' when accepted (or not worth a retry), else the feedback for round 2."""
        res.diagnosis, res.edits, res.test_command = parse_reply(reply)
        if not res.edits:
            res.stage, res.reason = "parse", "the reply contained no SEARCH/REPLACE edits"
            return ("Your reply contained no edits in the required format. Reply again: DIAGNOSIS:, then each edit as a "
                    "file path line directly followed by a <<<<<<< SEARCH / ======= / >>>>>>> REPLACE block, then TEST_COMMAND:.")
        fixed = []
        for path, search, replace in res.edits:
            if path and search.strip() and not self._contains(path, search):
                path = self._resolve_path(search) or path     # wrong or invented path, but the text exists elsewhere
            if not path:
                path = self._resolve_path(search) if search.strip() else ""
                if not path and not search.strip():
                    ext = ".py" if (self.info.primary_language or "").lower() == "python" else ".js"
                    path = f"{SCRATCH_DIRNAME}/test_issue{ext}"
            fixed.append((path, search, replace))
        # measured on real repos: 5 of 13 replies repeated an edit verbatim; the copy failed and sank the whole answer
        res.edits = [e for i, e in enumerate(fixed) if e not in fixed[:i]]
        for path, search, replace in res.edits:
            self.events.emit("tool_call", name="str_replace_editor", brief=f"{'create' if not search.strip() else 'edit'} {path}")
            try:
                if not path:
                    raise RuntimeError("no file path was given for this edit and its SEARCH text is not in any file")
                if not search.strip():
                    target = self.root / path
                    if target.exists() and not path.startswith(SCRATCH_DIRNAME + "/"):
                        raise RuntimeError(f"{path} already exists; an empty SEARCH only creates new files")
                    self.toolbox.editor.create(path, replace + ("\n" if not replace.endswith("\n") else ""))
                    if path.startswith(SCRATCH_DIRNAME + "/") and not res.test_path:
                        res.test_path = path
                else:
                    try:
                        self.toolbox.editor.str_replace(path, search, replace)
                    except Exception as miss:  # noqa: BLE001
                        f = self.root / path
                        near = near_miss_apply(f.read_text(errors="replace"), search, replace) if f.is_file() else None
                        if near is None:
                            raise miss
                        f.write_text(near[0])
                        self.events.emit("log", level="info", message=f"edit to {path}: SEARCH text was a near miss; "
                                         f"applied to the matching region at line {near[1]} (real text kept)")
                self.events.emit("tool_result", name="str_replace_editor", output=f"applied to {path}", is_error=False,
                                 meta={"edited": True})
            except Exception as e:  # noqa: BLE001
                self.events.emit("tool_result", name="str_replace_editor", output=str(e)[:300], is_error=True)
                res.stage, res.reason = "apply", f"edit to {path or '(no path)'} could not be applied: {short_error(e, 200)}"
                res.patch = self.git.patch()
                self.git.reset_to_base()
                return (f"This edit could not be applied to {path or '(no path given)'}:\n{str(e)[:1500]}\n\n"
                        "Nothing was changed. Reply again with ALL edits (SEARCH text copied exactly from the current file, "
                        "the file path on the line directly above each block), the test file, and TEST_COMMAND.")
        res.patch = self.git.patch()
        cmds = [res.test_command] if res.test_command else []
        if not cmds and res.test_path:
            cmds = [f"python -m pytest -q {res.test_path}" if res.test_path.endswith(".py") else f"node {res.test_path}"]
        self.events.emit("phase", name="verify")
        v = self.gate.verify(res.diagnosis or "fast path", cmds, dry=True)
        res.verification = v
        self.events.emit("verify", attempt=0, accepted=v.strength == "strong", strength=v.strength, round=0,
                         checks=[{"command": c.command, "verdict": c.verdict, "origin": c.origin} for c in v.checks])
        bad = [c for c in v.checks if c.verdict in ("regression", "still_failing", "fails_after", "timeout", "fails_both")]
        if not (v.strength == "strong" and not bad):
            no_fail = any(c.verdict == "passes_both" for c in v.checks) and not any(c.verdict == "fixes" for c in v.checks)
            res.stage = "proof"
            res.reason = ("its test did not fail on the original code, so nothing proves the fix" if no_fail
                          else (v.feedback or "the checks did not pass")[:600])
            self.git.reset_to_base()
            if no_fail:
                return ("Your test PASSED on the original, unfixed code, so it does not show the bug. The edits were "
                        "reverted. Write a test that FAILS on the current code for exactly the reason in the issue (check the "
                        "concrete behaviour the issue describes), then give the source edits, the test and TEST_COMMAND again.")
            broken = [c for c in v.checks if c.verdict in ("still_failing", "fails_both") and c.origin != "related-tests"]
            if broken and not any(c.verdict == "fixes" for c in v.checks):
                # measured: a model re-sent the same broken test when this was buried under the output table
                why = broken[0].after.summary if broken[0].after else ""
                return ("Your test FAILS EVEN WITH YOUR FIX, so the TEST ITSELF is most likely broken: it errors before it "
                        f"reaches the behaviour. First error: {why}\nCopy how the project's own tests set things up (their "
                        "imports, fixtures, helpers that start servers or build objects) and write the test the same way. "
                        "The edits were reverted: reply again with the source edits, the corrected test and TEST_COMMAND.\n\n"
                        "Full output:\n" + (v.feedback or "")[-2500:])
            return ("Your change was run on the original and on the patched code and was not accepted:\n"
                    + (v.feedback or "")[-2500:] + "\n\nThe edits were reverted. Reply again with corrected edits, the test and TEST_COMMAND.")
        if self.reviewer is not None:
            self.events.emit("phase", name="review")
            review = self.reviewer(v)
            res.review = review
            self.events.emit("review", attempt=0, **(review or {}))
            if review and review.get("verdict") == "revise" and review.get("concerns"):
                res.stage, res.reason = "review", "the reviewer asked for changes"
                return ""
        res.ok, res.stage, res.reason = True, "accepted", "proven"
        return ""

    def _done(self, res: FastResult, t0: float) -> FastResult:
        res.elapsed_s = round(time.time() - t0, 1)
        self.events.emit("log", level="info" if res.ok else "warn",
                         message=f"fast path {'accepted' if res.ok else 'not accepted'} ({res.stage}): {res.reason} · {res.elapsed_s}s")
        return res
