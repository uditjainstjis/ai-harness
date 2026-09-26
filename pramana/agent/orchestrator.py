"""Run orchestration: intake -> localize -> attempt(s) -> select -> evidence bundle.

Adaptive compute: a second attempt (fresh context, reset tree, lessons from the first) is only
spent when the first one did not end with *proof* (a fail->pass check and no regressions).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..llm import ChatModel, LLMError, build_model
from ..llm.base import Usage
from ..repo.git import SCRATCH_DIRNAME, GitTracker
from ..repo.issue import Issue
from ..repo.localize import Localizer
from ..repo.symbols import SymbolIndex
from ..repo.workspace import RepoInfo, inspect_repo
from ..tools import Toolbox
from . import prompts
from .events import Events
from .loop import Attempt, AttemptResult, FatalModelError
from .verify import Gate, Verification, render_checks

LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock"}
SCRATCH_LIKE = re.compile(r"^(repro|reproduce|reproduction|debug|scratch|tmp|temp|test_repro|test_issue|check_)[\w.-]*\.(py|js|ts|sh|rb|go)$", re.I)


@dataclass
class RunResult:
    status: str  # verified | patched | no_patch | error
    patch: str = ""
    attempts: List[AttemptResult] = field(default_factory=list)
    best: Optional[int] = None
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0
    run_dir: Optional[Path] = None
    verification: Optional[Verification] = None
    error: str = ""
    repo: Optional[RepoInfo] = None
    issue: Optional[Issue] = None
    hints: str = ""
    model: str = ""
    provider: str = ""
    summary: str = ""


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:n] or "issue"


class Orchestrator:
    def __init__(self, cfg: Config, events: Optional[Events] = None, model: Optional[ChatModel] = None) -> None:
        self.cfg = cfg
        self.events = events or Events()
        self.model = model

    def _predict_criteria(self, issue_text: str) -> str:
        self.events.emit("phase", name="localize")
        try:
            resp = self.model.chat([{"role": "system", "content": "You are a meticulous senior maintainer."},
                                    {"role": "user", "content": prompts.CRITERIA_PROMPT.format(issue=issue_text[:12000])}],
                                   tools=None, temperature=0.0)
        except Exception as e:  # noqa: BLE001 - optional step
            from ..llm.base import short_error

            self.events.emit("log", level="warn", message=f"criteria prediction skipped: {short_error(e, 90)}")
            return ""
        lines = [l.strip() for l in (resp.text or "").splitlines() if re.match(r"^\s*\d+[.)]\s+\S", l)]
        text = "\n".join(lines[:8])[:2000]
        if text:
            self.events.emit("criteria", text=text)
        return text

    def _independent(self, issue_text: str, criteria: str, info: RepoInfo, related: str, git: GitTracker, gate: Gate,
                     toolbox: Toolbox):
        def run() -> Optional[Dict[str, Any]]:
            from .testwriter import TestWriter
            from .verify import Check

            before_patch = git.patch()
            out = TestWriter(self.model, toolbox, self.events).run(issue_text, criteria, info.summary(), related)
            if git.patch() != before_patch:  # the writer may only add scratch files: restore the fix exactly
                git.reset_to_base()
                git.apply(before_patch)
                self.events.emit("log", level="warn", message="independent test writer touched source files; patch restored")
            if not out:
                return None
            chk = Check(out["command"], "independent")
            chk.after = gate._run(chk.command)
            try:
                with git.baseline():
                    chk.before = gate._run(chk.command)
            except Exception:  # noqa: BLE001
                chk.before = None
            chk.classify()
            content = ""
            for tok in chk.command.split():
                if tok.startswith(".pramana/") and (info.root / tok.split("::")[0]).is_file():
                    content = (info.root / tok.split("::")[0]).read_text(errors="replace")
                    break
            self.events.emit("independent_test", status="ran", command=chk.command, verdict=chk.verdict)
            return {"check": chk, "file_content": content}
        return run

    def _reviewer(self, issue_text: str):
        def review(v: Verification) -> Optional[Dict[str, Any]]:
            try:  # wide context: lets the reviewer see parallel code the patch left untouched
                wide = self._git.patch(context=25) if getattr(self, "_git", None) else v.patch
            except Exception:  # noqa: BLE001
                wide = v.patch
            patch = wide if 0 < len(wide) < 20000 else v.patch
            patch = patch if len(patch) < 20000 else patch[:20000] + "\n[... patch truncated ...]"
            crit = getattr(self, "_criteria", "")
            prompt = prompts.REVIEW_PROMPT.format(issue=issue_text[:10000], patch=patch, evidence=render_checks(v.checks),
                                                  criteria=(f"<predicted_acceptance_criteria>\n{crit}\n</predicted_acceptance_criteria>\n" if crit else ""))
            try:
                resp = self.model.chat([{"role": "system", "content": "You are a meticulous code reviewer."},
                                        {"role": "user", "content": prompt}], tools=None, temperature=0.0)
            except Exception as e:  # noqa: BLE001
                return {"verdict": "skipped", "concerns": [], "error": str(e)[:200]}
            m = re.search(r"\{.*\}", resp.text or "", re.S)
            if not m:
                return {"verdict": "approve", "concerns": [], "raw": (resp.text or "")[:500]}
            try:
                d = json.loads(m.group(0))
            except ValueError:
                return {"verdict": "approve", "concerns": [], "raw": (resp.text or "")[:500]}
            concerns = [str(c) for c in (d.get("concerns") or []) if str(c).strip()]
            return {"verdict": str(d.get("verdict", "approve")).lower(), "concerns": concerns}
        return review

    def solve(self, repo_path: Path, issue: Issue, acceptance_cmd: Optional[str] = None) -> RunResult:
        t_start = time.time()
        cfg = self.cfg
        ev = self.events
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(issue.title, 32)
        run_dir = Path(cfg.runs_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = RunResult(status="error", run_dir=run_dir, issue=issue)
        recorder = TrajectoryRecorder(run_dir / "trajectory.jsonl")
        ev.subscribe(recorder)
        try:
            if self.model is None:
                self.model = build_model(cfg)
            result.model, result.provider = self.model.name, self.model.provider
            ev.emit("run", status="start", run_dir=str(run_dir), model=self.model.name, provider=self.model.provider,
                    issue=issue.title, repo=str(repo_path))

            # ---------------------------------------------------------- intake
            ev.emit("phase", name="intake")
            root = repo_path.resolve()
            git = GitTracker(root)
            self._git = git
            scratch = root / SCRATCH_DIRNAME
            scratch.mkdir(exist_ok=True)
            info = inspect_repo(root)
            result.repo = info
            ev.emit("intake", summary=info.summary(), files=len(info.files), language=info.primary_language,
                    test_command=info.test_command, notes=info.env_notes)

            # ---------------------------------------------------------- zero-token reproduction
            issue_text = issue.render()
            snippet_block, snippet_out = "", ""
            if info.primary_language == "Python":
                from ..repo import snippets
                from ..tools.shell import build_env

                ev.emit("phase", name="reproduce")
                from ..tools.shell import ensure_python_shim

                runs = snippets.run_snippets(root, scratch, ensure_python_shim(build_env(root), scratch / ".bin"), issue_text)
                if runs:
                    snippet_block = snippets.render(runs)
                    snippet_out = "\n".join(r["output"] for r in runs)
                    for r in runs:
                        last = [l for l in r["output"].splitlines() if l.strip()][-1:] or [""]
                        ev.emit("snippet", file=r["file"], exit_code=r["exit_code"], last_line=last[0][:160])

            # ---------------------------------------------------------- localize
            ev.emit("phase", name="localize")
            index = SymbolIndex(root, info.files)
            t0 = time.time()
            src, tests = Localizer(root, info.files, index).localize(issue_text + "\n" + snippet_out)
            hints = Localizer.render(src, tests)
            result.hints = hints
            ev.emit("localized", hints=hints, seconds=round(time.time() - t0, 2), top=[c.path for c in src[:5]])

            criteria = self._predict_criteria(issue_text) if cfg.agent.criteria else ""
            self._criteria = criteria
            system = prompts.SYSTEM_PROMPT.format(root=root, repo_summary=info.summary().replace("\n", "\n- "))
            lessons: Optional[str] = None
            budget = cfg.agent.token_budget

            def budget_left() -> int:
                return budget - self.model.usage.total_tokens

            for n in range(1, max(1, cfg.agent.max_attempts) + 1):
                if n > 1:
                    if budget_left() < budget * 0.25:
                        ev.emit("log", level="info", message="skipping another attempt: token budget mostly spent")
                        break
                    git.reset_to_base()
                    ev.emit("log", level="info", message=f"attempt {n}: repository reset to baseline; retrying with lessons from attempt {n - 1}")
                toolbox = Toolbox(root, scratch, index, command_timeout=cfg.agent.command_timeout_s, git=git)
                gate = Gate(root, git, info.files, info.test_file_command, info.test_framework, toolbox.env,
                            timeout_s=cfg.agent.verify_timeout_s, acceptance_cmd=acceptance_cmd,
                            max_rounds=cfg.agent.max_gate_rejections)
                initial = prompts.build_initial(issue_text, info.overview, hints, acceptance_cmd, lessons, snippet_block, criteria)
                temp = cfg.model.temperature if n == 1 else max(cfg.model.temperature, 0.6)
                related = ", ".join(c.path for c in tests[:3])
                attempt = Attempt(
                    n, self.model, toolbox, gate, ev, system, initial, cfg.agent.max_steps, budget_left,
                    compact_at_tokens=cfg.agent.compact_at_tokens, keep_recent=cfg.agent.keep_recent_observations,
                    temperature=temp, reviewer=self._reviewer(issue_text) if cfg.agent.review else None,
                    independent=(self._independent(issue_text, criteria, info, related, git, gate, toolbox)
                                 if cfg.agent.independent_tests else None),
                )
                res = attempt.run()
                result.attempts.append(res)
                if res.strength == "strong":
                    break
                lessons = self._lessons(res)

            # ---------------------------------------------------------- select
            best = pick_best(result.attempts)
            current = git.patch()
            if best is not None:
                if best.patch != current:
                    git.reset_to_base()
                    git.apply(best.patch)
                result.best = best.number
                result.patch = best.patch
                result.verification = best.verification
                result.summary = best.summary
                result.status = "verified" if best.strength == "strong" else "patched"
            else:
                if current.strip():
                    git.reset_to_base()
                result.status = "no_patch"
            self._tidy(root, git, run_dir, result)
        except FatalModelError as e:
            result.status, result.error = "error", f"model unavailable: {e}"
            ev.emit("log", level="error", message=result.error)
        except LLMError as e:
            result.status, result.error = "error", str(e)
            ev.emit("log", level="error", message=result.error)
        except Exception as e:  # noqa: BLE001
            import traceback

            result.status, result.error = "error", f"{type(e).__name__}: {e}"
            ev.emit("log", level="error", message=result.error + "\n" + traceback.format_exc()[-2000:])
        finally:
            if self.model is not None:
                result.usage = self.model.usage
            result.elapsed_s = round(time.time() - t_start, 1)
            try:
                from ..report.evidence import write_bundle

                write_bundle(result, self.cfg)
            except Exception as e:  # noqa: BLE001
                ev.emit("log", level="error", message=f"could not write evidence bundle: {e}")
            ev.emit("run", status="end", result=result.status, run_dir=str(run_dir), tokens=result.usage.total_tokens,
                    elapsed_s=result.elapsed_s)
            recorder.close()
        return result

    @staticmethod
    def _lessons(res: AttemptResult) -> str:
        parts = [f"- It stopped because: {res.stop_reason} after {res.steps} steps."]
        if res.summary:
            parts.append(f"- Its own summary: {res.summary[:800]}")
        if res.patch.strip():
            p = res.patch if len(res.patch) < 3500 else res.patch[:3500] + "\n[... truncated ...]"
            parts.append(f"- Its patch (now reverted):\n```diff\n{p}\n```")
        else:
            parts.append("- It produced no patch.")
        if res.verification:
            parts.append("- Verification result:\n" + render_checks(res.verification.checks))
            fb = res.verification.feedback
            if "REJECTED" in fb or "STILL" in fb or "REGRESSION" in fb:
                parts.append("- Gate feedback: " + fb[-1500:])
        if res.review and res.review.get("concerns"):
            parts.append("- Reviewer concerns: " + "; ".join(res.review["concerns"][:4]))
        return "\n".join(parts)

    def _tidy(self, root: Path, git: GitTracker, run_dir: Path, result: RunResult) -> None:
        """Move scratch artefacts into the evidence bundle; leave the repo with only the fix."""
        scratch = root / SCRATCH_DIRNAME
        if scratch.exists():
            dest = run_dir / "scratch"
            try:
                shutil.copytree(scratch, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".bin", "__pycache__"))
            except Exception:  # noqa: BLE001
                pass
        # Patch hygiene. A new file belongs to the fix only if the agent deliberately created it with the
        # editor (and it is not a repro/debug script); files that merely appeared while commands ran
        # (data files, logs, caches) are moved into the bundle. Lockfiles rewritten by installs are restored.
        best = next((a for a in result.attempts if a.number == result.best), None)
        created = set(best.created_files) if best else set()
        touched = set(best.touched_files) if best else set()
        moved, restored = [], []
        stubs = set((result.verification.shadow_stubs if result.verification else []) or [])
        for status, path in git.changed_files():
            if status == "A" and path in stubs:
                dest = run_dir / "scratch" / "artifacts" / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root / path), str(dest))
                moved.append(path)
                self.events.emit("log", level="warn",
                                 message=f"removed a third-party dependency stub from the patch: {path} (kept in the evidence bundle)")
                continue
            name = path.rsplit("/", 1)[-1]
            if status == "A" and (path not in created or ("/" not in path and SCRATCH_LIKE.match(name))):
                dest = run_dir / "scratch" / "artifacts" / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root / path), str(dest))
                moved.append(path)
            elif status == "M" and path not in touched and name in LOCKFILES:
                if git.restore(path):
                    restored.append(path)
        if moved or restored:
            result.patch = git.patch()
            if moved:
                self.events.emit("log", level="info", message=f"kept out of the patch (side-effect/scratch files): {', '.join(moved[:8])}")
            if restored:
                self.events.emit("log", level="info", message=f"restored lockfiles changed by installs: {', '.join(restored)}")
        shutil.rmtree(scratch, ignore_errors=True)


def pick_best(attempts: List[AttemptResult]) -> Optional[AttemptResult]:
    cands = [a for a in attempts if a.patch.strip()]
    if not cands:
        return None
    rank = {"strong": 2, "weak": 1, "none": 0}

    def key(a: AttemptResult):
        v = a.verification
        return (rank.get(a.strength, 0), v.score if v else -99.0, -len(a.patch))

    return max(cands, key=key)


class TrajectoryRecorder:
    def __init__(self, path: Path) -> None:
        self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, kind: str, data: Dict[str, Any]) -> None:
        try:
            self.fh.write(json.dumps({"event": kind, **data}, default=str) + "\n")
            self.fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass
