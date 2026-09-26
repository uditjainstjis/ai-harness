"""The submit gate: turns the agent's *claims* into *evidence*.

Every verification command is executed twice - on the patched tree and on the original tree
(the patch is reverse-applied, the scratch dir stays) - and classified:

    fixes          fail -> pass   (the proof we want)
    passes_both    pass -> pass   (no regression; not proof of a fix)
    regression     pass -> fail   (reject)
    still_failing  fail -> fail   (reject if it is the agent's own reproduction / acceptance test)
    fails_both     fail -> fail   (pre-existing failure in an unrelated test; noted)

Related existing tests are selected automatically, so the agent cannot "verify" with
commands that avoid the tests its change could break.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..repo.git import GitTracker
from ..repo.localize import is_test_path
from ..tools.editor import syntax_error
from ..tools.shell import CommandResult, run_command


@dataclass
class Outcome:
    exit_code: Optional[int]
    passed: bool
    timed_out: bool
    duration_s: float
    tail: str
    summary: str

    @staticmethod
    def of(res: CommandResult) -> "Outcome":
        lines = res.output.rstrip().splitlines()
        tail = "\n".join(lines[-40:])
        return Outcome(res.exit_code, res.ok, res.timed_out, round(res.duration_s, 2), tail, summarize_output(res.output, res.exit_code, res.timed_out))


@dataclass
class Check:
    command: str
    origin: str  # agent | acceptance | related-tests
    before: Optional[Outcome] = None
    after: Optional[Outcome] = None
    verdict: str = ""

    def classify(self) -> str:
        a, b = self.after, self.before
        if a is None:
            return "error"
        if a.timed_out:
            self.verdict = "timeout"
        elif b is None:
            self.verdict = "passes_after" if a.passed else "fails_after"
        elif not b.passed and a.passed:
            self.verdict = "fixes"
        elif b.passed and a.passed:
            self.verdict = "passes_both"
        elif b.passed and not a.passed:
            self.verdict = "regression" if not b.timed_out else "fails_after"
        else:
            self.verdict = "still_failing" if self.origin != "related-tests" else "fails_both"
        if a.exit_code == 5 and "pytest" in self.command:  # pytest: no tests collected
            self.verdict = "no_tests"
        return self.verdict


@dataclass
class Verification:
    accepted: bool
    feedback: str
    checks: List[Check] = field(default_factory=list)
    syntax_errors: List[str] = field(default_factory=list)
    patch: str = ""
    changed: List[Tuple[str, str]] = field(default_factory=list)
    strength: str = "none"  # strong | weak | none
    score: float = 0.0
    round: int = 0
    summary: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("patch", None)
        return d


PYTEST_SUMMARY_RE = re.compile(r"=+ (.*(?:passed|failed|error|skipped|no tests ran).*) =+\s*$", re.M)


def summarize_output(out: str, code: Optional[int], timed_out: bool) -> str:
    if timed_out:
        return "timed out"
    m = None
    for m in PYTEST_SUMMARY_RE.finditer(out):
        pass
    if m:
        return m.group(1).strip()[:160]
    m2 = re.search(r"Ran (\d+) tests? in [\d.]+s\s*\n+\s*(OK.*|FAILED.*)", out)
    if m2:
        return f"{m2.group(1)} tests: {m2.group(2).strip()}"[:160]
    lines = [l for l in out.strip().splitlines() if l.strip() and not l.startswith("[exit code")]
    last = lines[-1].strip() if lines else ""
    return f"exit {code}" + (f": {last[:120]}" if last else "")


def related_test_commands(changed: List[str], files: List[str], test_file_cmd: str, framework: str, limit: int = 2) -> List[str]:
    if not test_file_cmd:
        return []
    test_files = [f for f in files if is_test_path(f)]
    picks: List[str] = []
    for src in changed:
        if is_test_path(src):
            continue
        p = Path(src)
        stem = p.stem if p.stem != "__init__" else p.parent.name
        names = {f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py", f"tests_{stem}.py",
                 f"{stem}.test.js", f"{stem}.spec.js", f"{stem}.test.ts", f"{stem}.spec.ts", f"{stem}_test.go"}
        exact = [t for t in test_files if Path(t).name in names]
        # prefer tests whose directory mirrors the source directory
        exact.sort(key=lambda t: (-len(set(Path(t).parts) & set(p.parts)), len(t)))
        for t in exact[:1]:
            if t not in picks:
                picks.append(t)
        if len(picks) >= limit:
            break
    if not picks:
        return []
    if "{files}" in test_file_cmd:
        return [test_file_cmd.replace("{files}", " ".join(picks[:limit]))]
    if "{modules}" in test_file_cmd:  # django runtests labels
        mods = []
        for t in picks[:limit]:
            parts = Path(t).with_suffix("").parts
            if parts and parts[0] == "tests":
                parts = parts[1:]
            mods.append(".".join(parts))
        return [test_file_cmd.replace("{modules}", " ".join(mods))]
    if "{packages}" in test_file_cmd:
        pkgs = sorted({"./" + str(Path(t).parent) for t in picks[:limit]})
        return [test_file_cmd.replace("{packages}", " ".join(pkgs))]
    return []


class Gate:
    def __init__(self, root: Path, git: GitTracker, files: List[str], test_file_cmd: str, framework: str,
                 env: Dict[str, str], timeout_s: int = 300, acceptance_cmd: Optional[str] = None,
                 max_rounds: int = 3) -> None:
        self.root = root
        self.git = git
        self.files = files
        self.test_file_cmd = test_file_cmd
        self.framework = framework
        self.env = env
        self.timeout_s = timeout_s
        self.acceptance_cmd = acceptance_cmd
        self.max_rounds = max_rounds
        self.rounds = 0
        self.asked_for_proof = False
        self.history: List[Verification] = []

    def _run(self, cmd: str) -> Outcome:
        env = dict(self.env, PYTHONDONTWRITEBYTECODE="1")
        return Outcome.of(run_command(cmd, self.root, timeout=self.timeout_s, env=env, max_chars=20000))

    def _purge_bytecode(self, paths: List[str]) -> None:
        """Stale .pyc files can mask a revert (same size + same-second mtime): delete them."""
        for rel in paths:
            if not rel.endswith(".py"):
                continue
            cache = (self.root / rel).parent / "__pycache__"
            if cache.is_dir():
                for pyc in cache.glob(Path(rel).stem + ".*.pyc"):
                    try:
                        pyc.unlink()
                    except OSError:
                        pass

    def verify(self, summary: str, commands: List[str], final: bool = False) -> Verification:
        self.rounds += 1
        last_round = final or self.rounds >= self.max_rounds
        patch = self.git.patch()
        changed = self.git.changed_files()
        v = Verification(accepted=False, feedback="", patch=patch, changed=changed, round=self.rounds, summary=summary)
        if not patch.strip():
            v.feedback = ("REJECTED: the repository has no changes. Make the source edit that fixes the issue, "
                          "verify it, then submit again.")
            self.history.append(v)
            return v
        for status, path in changed:
            if status == "D":
                continue
            p = self.root / path
            if p.is_file():
                err = syntax_error(p, p.read_text(encoding="utf-8", errors="replace"))
                if err:
                    v.syntax_errors.append(f"{path}: {err}")
        # commands: acceptance first, then the agent's, then related tests
        checks: List[Check] = []
        seen = set()
        if self.acceptance_cmd:
            checks.append(Check(self.acceptance_cmd, "acceptance"))
            seen.add(self.acceptance_cmd.strip())
        for c in commands[:4]:
            if c.strip() and c.strip() not in seen:
                checks.append(Check(c.strip(), "agent"))
                seen.add(c.strip())
        src_changed = [p for s, p in changed if s != "D"]
        for c in related_test_commands(src_changed, self.files, self.test_file_cmd, self.framework):
            if c.strip() not in seen and not any(c.strip() in s or s in c.strip() for s in seen):
                checks.append(Check(c, "related-tests"))
                seen.add(c.strip())
        changed_paths = [p for _, p in changed]
        self._purge_bytecode(changed_paths)
        for ch in checks:
            ch.after = self._run(ch.command)
        try:
            with self.git.baseline():
                self._purge_bytecode(changed_paths)
                for ch in checks:
                    ch.before = self._run(ch.command)
            self._purge_bytecode(changed_paths)
        except Exception as e:  # noqa: BLE001 - evidence is best effort; never lose the patch
            v.feedback += f"(could not run baseline comparison: {e})\n"
        for ch in checks:
            ch.classify()
        v.checks = checks

        fixes = [c for c in checks if c.verdict == "fixes"]
        regressions = [c for c in checks if c.verdict == "regression"]
        failing_own = [c for c in checks if c.origin in ("agent", "acceptance") and c.verdict in ("still_failing", "fails_after", "regression", "timeout")]
        pre_existing = [c for c in checks if c.verdict == "fails_both"]

        v.score = 3.0 * len(fixes) + 0.5 * sum(1 for c in checks if c.verdict == "passes_both") \
            - 6.0 * len(regressions) - 3.0 * len(failing_own) - 5.0 * len(v.syntax_errors)
        if self.acceptance_cmd:
            acc = checks[0]
            v.score += 10.0 if acc.after and acc.after.passed else -10.0
        v.strength = "strong" if fixes and not regressions and not failing_own and not v.syntax_errors else (
            "weak" if not regressions and not v.syntax_errors and not failing_own else "none")

        problems: List[str] = []
        if v.syntax_errors:
            problems.append("Syntax errors in changed files:\n" + "\n".join(v.syntax_errors))
        for c in regressions:
            problems.append(f"REGRESSION - `{c.command}` passed on the original code but FAILS with your change:\n{c.after.tail if c.after else ''}")
        for c in failing_own:
            if c in regressions:
                continue
            label = "the acceptance test" if c.origin == "acceptance" else "your verification command"
            problems.append(f"{label} `{c.command}` still fails after your change ({c.after.summary if c.after else '?'}):\n{c.after.tail if c.after else ''}")

        table = render_checks(checks)
        if problems and not last_round:
            v.feedback += "REJECTED - the evidence does not support the fix yet.\n\n" + table + "\n\n" + "\n\n".join(problems) + \
                "\n\nFix these problems, then call submit again."
            self.history.append(v)
            return v
        if not fixes and not self.asked_for_proof and not last_round and not problems:
            self.asked_for_proof = True
            v.feedback += ("NOT YET - none of your verification commands fails on the original code, so none of them proves "
                           "that your change fixes the issue.\n\n" + table + "\n\nWrite a reproduction in .pramana/ that exits "
                           "non-zero on the original code because of this issue (assert the expected behaviour) and passes "
                           "with your change, run it, then submit again including it. If the issue truly cannot be "
                           "reproduced by a script, explain why in the summary and submit again.")
            self.history.append(v)
            return v
        v.accepted = True
        verdict = {"strong": "VERIFIED", "weak": "ACCEPTED (weak evidence)", "none": "ACCEPTED WITH UNRESOLVED PROBLEMS"}[v.strength]
        v.feedback += f"{verdict}.\n\n" + table
        if pre_existing:
            v.feedback += "\n\nNote: some related tests also fail on the original code (pre-existing failures)."
        if problems:
            v.feedback += "\n\nUnresolved:\n" + "\n\n".join(p[:1500] for p in problems)
        self.history.append(v)
        return v


VERDICT_ICON = {"fixes": "PROVES FIX", "passes_both": "ok (no regression)", "regression": "REGRESSION",
                "still_failing": "STILL FAILING", "fails_both": "fails before+after (pre-existing)",
                "timeout": "TIMEOUT", "no_tests": "no tests collected", "passes_after": "passes",
                "fails_after": "FAILS", "error": "error"}


def render_checks(checks: List[Check]) -> str:
    if not checks:
        return "(no verification commands)"
    rows = ["| verdict | origin | command | original code | with patch |", "|---|---|---|---|---|"]
    for c in checks:
        b = c.before.summary if c.before else "-"
        a = c.after.summary if c.after else "-"
        rows.append(f"| {VERDICT_ICON.get(c.verdict, c.verdict)} | {c.origin} | `{c.command[:90]}` | {b[:60]} | {a[:60]} |")
    return "\n".join(rows)
