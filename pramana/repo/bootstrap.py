"""Environment bootstrap for repositories the harness cloned itself (never for a user's own
checkout, whose environment we respect). Time-boxed and best-effort: the goal is only that the
project's tests *can run*, because without runnable tests there is no evidence.

Python: create ./.venv (uv if available, else venv), install the project (editable) plus test
extras / requirement files, then pytest. Node: `npm ci`/`npm install` when package.json exists.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from ..tools.shell import build_env, run_command


def _log(cb: Optional[Callable[[str], None]], msg: str) -> None:
    if cb:
        cb(msg)


def bootstrap(repo: Path, log: Optional[Callable[[str], None]] = None, timeout_s: int = 600) -> List[str]:
    notes: List[str] = []
    has_py = any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"))
    if has_py and not (repo / ".venv").exists():
        notes += _bootstrap_python(repo, log, timeout_s)
    if (repo / "package.json").exists() and not (repo / "node_modules").exists() and shutil.which("npm"):
        _log(log, "installing node dependencies (npm)")
        cmd = "npm ci --no-audit --no-fund" if (repo / "package-lock.json").exists() else "npm install --no-audit --no-fund"
        r = run_command(cmd, repo, timeout=timeout_s, env=build_env(repo))
        notes.append(f"node deps: {'ok' if r.ok else 'FAILED (' + r.output.strip().splitlines()[-1][:120] + ')' if r.output.strip() else 'FAILED'}")
    return notes


def _bootstrap_python(repo: Path, log, timeout_s: int) -> List[str]:
    notes: List[str] = []
    venv = repo / ".venv"
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.parent.exists():
        with open(exclude, "a") as fh:
            fh.write("\n/.venv/\n")
    _log(log, "creating .venv for the target repository")
    uv = shutil.which("uv")
    try:
        if uv:
            subprocess.run([uv, "venv", "-q", str(venv)], check=True, cwd=repo, timeout=180)
            pip = [uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python")]
        else:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, cwd=repo, timeout=180)
            pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "-q"]
    except (subprocess.SubprocessError, OSError) as e:
        return [f"could not create a venv: {e}"]

    def pip_install(args: List[str], what: str) -> bool:
        try:
            p = subprocess.run(pip + args, cwd=repo, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            notes.append(f"{what}: timed out")
            return False
        if p.returncode != 0:
            last = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["?"]
            notes.append(f"{what}: failed ({last[0][:140]})")
            return False
        notes.append(f"{what}: ok")
        return True

    _log(log, "installing the project and its test dependencies")
    installed = False
    for extra in ("[test]", "[tests]", "[testing]", "[dev]"):
        if _declares_extra(repo, extra.strip("[]")):
            installed = pip_install(["-e", f".{extra}"], f"pip install -e .{extra}")
            if installed:
                break
    if not installed and any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        installed = pip_install(["-e", "."], "pip install -e .")
    for req in ("requirements.txt", "requirements-dev.txt", "requirements_test.txt", "requirements-test.txt",
                "test-requirements.txt", "requirements/test.txt", "requirements/dev.txt", "requirements/tests.txt"):
        if (repo / req).is_file():
            pip_install(["-r", req], f"pip install -r {req}")
    pip_install(["pytest"], "pytest")
    return notes


def _declares_extra(repo: Path, name: str) -> bool:
    for f in ("pyproject.toml", "setup.cfg", "setup.py"):
        p = repo / f
        if p.is_file():
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if f"{name} =" in text or f'"{name}"' in text or f"'{name}'" in text or f"\n{name}=" in text or f"{name}:" in text:
                return True
    return False
