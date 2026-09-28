"""Bundled end-to-end benchmark (`make test` / `pramana bench`).

Each task in bench/tasks/<name>/ has: repo/ (a small project), issue.md, hidden_tests/ (never
shown to the agent) and task.json with the command that grades the result. The harness runs
through the exact same code path as `make run`; afterwards the hidden tests are copied in and
executed. A task is RESOLVED only if the hidden tests pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from .agent.events import Events
from .agent.orchestrator import Orchestrator
from .config import Config
from .repo.issue import issue_from_file
from .tools.shell import build_env, run_command
from .ui.live import LiveView

console = Console()
ROOT = Path(__file__).resolve().parent.parent


def _python_env(repo: Path, cache: Path) -> None:
    """Give Python task repos a venv with pytest (shared, symlinked as repo/.venv)."""
    venv = cache / "bench-venv"
    if not (venv / "bin" / "python").exists():
        cache.mkdir(parents=True, exist_ok=True)
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", "-q", str(venv)], check=True)
            subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "pytest"], check=True)
        else:
            from .repo.bootstrap import host_python
            subprocess.run([host_python(), "-m", "venv", str(venv)], check=True)
            subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "pytest"], check=True)
    (repo / ".venv").symlink_to(venv, target_is_directory=True)


def prepare(task_dir: Path, cache: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix=f"pramana-bench-{task_dir.name}-"))
    repo = work / "repo"
    shutil.copytree(task_dir / "repo", repo)
    env = dict(os.environ, GIT_AUTHOR_NAME="bench", GIT_AUTHOR_EMAIL="bench@localhost",
               GIT_COMMITTER_NAME="bench", GIT_COMMITTER_EMAIL="bench@localhost")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    with open(repo / ".git" / "info" / "exclude", "a") as fh:
        fh.write("\n.venv\n__pycache__/\n.pytest_cache/\nnode_modules/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, env=env)
    spec = json.loads((task_dir / "task.json").read_text())
    if spec.get("language", "python") == "python":
        _python_env(repo, cache)
    return repo


def grade(task_dir: Path, repo: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    hidden = task_dir / "hidden_tests"
    if hidden.exists():
        shutil.copytree(hidden, repo / "hidden_tests", dirs_exist_ok=True)
    res = run_command(spec["test_cmd"], repo, timeout=600, env=build_env(repo))
    return {"resolved": res.ok, "output_tail": "\n".join(res.output.splitlines()[-15:])}


def run_bench(cfg: Config, suite: str = "mini", only: Optional[List[str]] = None, plain: bool = True) -> int:
    tasks_dir = ROOT / "bench" / "tasks"
    tasks = sorted(d for d in tasks_dir.iterdir() if (d / "task.json").exists()) if tasks_dir.exists() else []
    if suite == "quick" and not only:
        only = ["slugify", "semver-js"]  # one Python, one JavaScript: a fast end-to-end smoke test
    if only:
        tasks = [t for t in tasks if t.name in only]
    if not tasks:
        console.print("[red]no benchmark tasks found[/]")
        return 2
    cache = Path(cfg.runs_dir) / ".bench-cache"
    rows = []
    t_all = time.time()
    for task_dir in tasks:
        spec = json.loads((task_dir / "task.json").read_text())
        console.rule(f"[bold]{task_dir.name}[/]: {spec.get('title', '')}")
        repo = prepare(task_dir, cache)
        issue = issue_from_file(task_dir / "issue.md")
        events = Events()
        view = LiveView(console, plain=plain)
        events.subscribe(view)
        with view:
            res = Orchestrator(cfg, events).solve(repo, issue, acceptance_cmd=spec.get("acceptance_cmd"))
        g = grade(task_dir, repo, spec)
        rows.append({
            "task": task_dir.name, "status": res.status, "resolved": g["resolved"],
            "tokens": res.usage.total_tokens, "calls": res.usage.calls, "seconds": res.elapsed_s,
            "attempts": len(res.attempts), "run_dir": str(res.run_dir), "grade_tail": g["output_tail"],
        })
        console.print(f"hidden tests: {'[green]PASS[/]' if g['resolved'] else '[red]FAIL[/]'}  ({res.status}, {res.usage.total_tokens:,} tokens, {res.elapsed_s:.0f}s)")
        shutil.rmtree(repo.parent, ignore_errors=True)
    t = Table(title=f"Pramana benchmark ({cfg.model.name} via {cfg.resolved_provider})")
    for col in ("task", "harness verdict", "hidden tests", "tokens", "calls", "time"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["task"], r["status"], "[green]PASS[/]" if r["resolved"] else "[red]FAIL[/]", f"{r['tokens']:,}", str(r["calls"]), f"{r['seconds']:.0f}s")
    console.print(t)
    solved = sum(r["resolved"] for r in rows)
    console.print(f"[bold]resolved {solved}/{len(rows)}[/] · {sum(r['tokens'] for r in rows):,} tokens · {time.time() - t_all:.0f}s")
    out = Path(cfg.runs_dir) / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": cfg.model.name, "provider": cfg.resolved_provider, "results": rows}, indent=2))
    console.print(f"results: {out}")
    return 0 if solved == len(rows) else 1
