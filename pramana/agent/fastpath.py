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

from ..llm.base import short_error
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

Reply in exactly this format.

DIAGNOSIS: <one or two sentences: the root cause>

Then every source edit, each as:
path/relative/to/the/repo/file.ext
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

BLOCK_RE = re.compile(r"<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE", re.S)


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


def parse_reply(text: str) -> Tuple[str, List[Tuple[str, str, str]], str]:
    """-> (diagnosis, [(path, search, replace)], test_command)."""
    text = re.sub(r"```[\w+-]*\n?", "", text or "")
    diag = ""
    m = re.search(r"DIAGNOSIS:\s*(.+?)(?:\n\s*\n|\n(?=\S+\n<<<<<<<))", text, re.S)
    if m:
        diag = " ".join(m.group(1).split())
    edits = []
    for bm in BLOCK_RE.finditer(text):
        before = text[:bm.start()].rstrip("\n").splitlines()
        path = before[-1].strip() if before else ""
        path = re.sub(r"^(?:File|Path|path|file):\s*", "", path).strip().strip("`*'\" ")
        if path.startswith("./"):
            path = path[2:]
        edits.append((path, bm.group(1), bm.group(2)))
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
            test_command=self.info.test_command or "(unknown)", scratch=SCRATCH_DIRNAME, ext=ext)
        msgs = [{"role": "system", "content": "You are an expert software engineer. Answer in the exact format requested."},
                {"role": "user", "content": prompt}]
        resp = None
        for tries in range(3):   # a busy endpoint is not a hard issue: wait it out rather than escalate
            try:
                resp = self.model.chat(msgs, tools=None, temperature=0.0)
                break
            except Exception as e:  # noqa: BLE001 - the agent loop is the fallback
                busy = any(k in str(e).lower() for k in ("429", "rate", "busy", "timed out", "timeout", "overloaded"))
                if busy and tries < 2:
                    self.events.emit("log", level="warn", message="fast path: endpoint busy, waiting before retrying the same call")
                    time.sleep(15 * (tries + 1))
                    continue
                res.stage, res.reason = "no-reply", short_error(e, 160)
                return self._done(res, t0)
        text = resp.text or ""
        self.events.emit("llm", text=text[:1500], total_tokens=self.model.usage.total_tokens, cached_tokens=0)
        res.diagnosis, res.edits, res.test_command = parse_reply(text)
        if not res.edits:
            res.stage, res.reason = "parse", "the reply contained no SEARCH/REPLACE edits"
            return self._done(res, t0)
        # apply
        for path, search, replace in res.edits:
            self.events.emit("tool_call", name="str_replace_editor", brief=f"{'create' if not search.strip() else 'edit'} {path}")
            try:
                if not search.strip():
                    target = self.root / path
                    if target.exists() and not path.startswith(SCRATCH_DIRNAME + "/"):
                        raise RuntimeError(f"{path} already exists; an empty SEARCH only creates new files")
                    self.toolbox.editor.create(path, replace + ("\n" if not replace.endswith("\n") else ""))
                    if path.startswith(SCRATCH_DIRNAME + "/") and not res.test_path:
                        res.test_path = path
                else:
                    self.toolbox.editor.str_replace(path, search, replace)
                self.events.emit("tool_result", name="str_replace_editor", output=f"applied to {path}", is_error=False,
                                 meta={"edited": True})
            except Exception as e:  # noqa: BLE001
                self.events.emit("tool_result", name="str_replace_editor", output=str(e)[:300], is_error=True)
                res.stage, res.reason = "apply", f"edit to {path} could not be applied: {short_error(e, 200)}"
                res.patch = self.git.patch()
                self.git.reset_to_base()
                return self._done(res, t0)
        res.patch = self.git.patch()
        # prove: the gate runs the test (and related existing tests) on the original and on the patched code
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
            res.stage = "proof"
            res.reason = ("its test did not fail on the original code, so nothing proves the fix"
                          if any(c.verdict == "passes_both" for c in v.checks) and not any(c.verdict == "fixes" for c in v.checks)
                          else (v.feedback or "the checks did not pass")[:600])
            self.git.reset_to_base()
            return self._done(res, t0)
        # one cheap second opinion; a real concern sends the (kept) fix on to the agent loop to complete
        if self.reviewer is not None:
            self.events.emit("phase", name="review")
            review = self.reviewer(v)
            res.review = review
            self.events.emit("review", attempt=0, **(review or {}))
            if review and review.get("verdict") == "revise" and review.get("concerns"):
                res.stage, res.reason = "review", "the reviewer asked for changes"
                return self._done(res, t0)
        res.ok, res.stage, res.reason = True, "accepted", "proven in one call"
        return self._done(res, t0)

    def _done(self, res: FastResult, t0: float) -> FastResult:
        res.elapsed_s = round(time.time() - t0, 1)
        self.events.emit("log", level="info" if res.ok else "warn",
                         message=f"fast path {'accepted' if res.ok else 'not accepted'} ({res.stage}): {res.reason} · {res.elapsed_s}s")
        return res
