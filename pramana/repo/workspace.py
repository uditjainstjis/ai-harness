"""Repository intake: file inventory, language + test-runner detection, environment probe,
and a compact tree overview for the model's first message."""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..tools.editor import IGNORED_DIRS
from ..tools.shell import build_env, find_repo_venv, run_command

LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".cs": "C#", ".swift": "Swift", ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir", ".lua": "Lua",
}


@dataclass
class RepoInfo:
    root: Path
    files: List[str]
    languages: List[str]
    primary_language: str
    test_command: str
    test_file_command: str  # template with {files}
    test_framework: str
    env_notes: List[str] = field(default_factory=list)
    python: str = ""
    overview: str = ""
    package_dirs: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Root: {self.root}",
            f"Languages: {', '.join(self.languages[:4]) or 'unknown'} ({len(self.files)} files)",
            f"Tests: {self.test_framework} - run: `{self.test_command}`"
            + (f"  (specific files: `{self.test_file_command}`)" if self.test_file_command else ""),
        ]
        if self.python:
            lines.append(f"Python: {self.python}")
        lines += [f"Note: {n}" for n in self.env_notes]
        return "\n".join(lines)


def list_files(root: Path, limit: int = 60000) -> List[str]:
    try:
        p = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           cwd=str(root), capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            files = [f for f in p.stdout.decode(errors="replace").split("\0") if f]
            files = [f for f in files if not f.startswith(".pramana/") and os.path.isfile(os.path.join(root, f))]
            return files[:limit]
    except (OSError, subprocess.TimeoutExpired):
        pass
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), root))
            if len(out) >= limit:
                return out
    return out


def _read(p: Path, limit: int = 200000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def detect_tests(root: Path, files: List[str], lang: str) -> (str, str, str):
    """Return (framework, full test command, per-file command template)."""
    has = set(files)
    if lang == "Python" or any(f.endswith(".py") for f in files[:2000]):
        if "tests/runtests.py" in has and any(f.startswith("django/") for f in files[:5000]):
            return ("django runtests", "python tests/runtests.py --parallel 1", "python tests/runtests.py --parallel 1 {modules}")
        if "bin/test" in has and any(f.startswith("sympy/") for f in files[:5000]):
            return ("sympy (pytest-compatible)", "python -m pytest -q", "python -m pytest -q {files}")
        return ("pytest", "python -m pytest -q", "python -m pytest -q {files}")
    if "package.json" in has:
        try:
            pkg = json.loads(_read(root / "package.json"))
        except ValueError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        test_script = (pkg.get("scripts") or {}).get("test", "")
        runner = "npm"
        if (root / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        elif (root / "yarn.lock").exists():
            runner = "yarn"
        full = f"{runner} test" if test_script else "node --test"
        if "vitest" in deps or "vitest" in test_script:
            return ("vitest", full, "npx vitest run {files}")
        if "jest" in deps or "jest" in test_script:
            return ("jest", full, "npx jest {files}")
        if "mocha" in deps or "mocha" in test_script:
            return ("mocha", full, "npx mocha {files}")
        if "node --test" in test_script or not test_script:
            return ("node:test", full, "node --test {files}")
        return ("npm test", full, "")
    if "go.mod" in has:
        return ("go test", "go test ./...", "go test {packages}")
    if "Cargo.toml" in has:
        return ("cargo test", "cargo test", "cargo test {filter}")
    if "pom.xml" in has:
        return ("maven", "mvn -q test", "mvn -q test -Dtest={classes}")
    if any(f in has for f in ("build.gradle", "build.gradle.kts")):
        return ("gradle", "./gradlew test" if "gradlew" in has else "gradle test", "")
    if "Gemfile" in has:
        if any(f.startswith("spec/") for f in files):
            return ("rspec", "bundle exec rspec", "bundle exec rspec {files}")
        return ("rake", "bundle exec rake test", "")
    if "Makefile" in has and re.search(r"^test:", _read(root / "Makefile"), re.M):
        return ("make", "make test", "")
    return ("unknown", "", "")


def overview(root: Path, files: List[str], max_lines: int = 45) -> str:
    top: Counter = Counter()
    sub: Dict[str, Counter] = {}
    top_files: List[str] = []
    for f in files:
        parts = f.split("/")
        if len(parts) == 1:
            top_files.append(f)
            continue
        top[parts[0]] += 1
        if len(parts) > 2:
            sub.setdefault(parts[0], Counter())[parts[1]] += 1
    lines = []
    for d, n in sorted(top.items(), key=lambda kv: -kv[1]):
        lines.append(f"{d}/  ({n} files)")
        kids = sub.get(d)
        if kids and len(lines) < max_lines - 5:
            for k, c in kids.most_common(8):
                lines.append(f"    {k}/  ({c})")
        if len(lines) >= max_lines:
            lines.append("...")
            break
    if top_files:
        lines.append("top-level files: " + ", ".join(sorted(top_files)[:30]) + (" ..." if len(top_files) > 30 else ""))
    return "\n".join(lines)


def probe_python(root: Path, files: List[str], env: Dict[str, str]) -> (str, List[str], List[str]):
    notes: List[str] = []
    # candidate import names: top-level packages that exist in the repo
    pkgs = []
    for f in files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[-1] == "__init__.py":
            if len(parts) == 2 or (len(parts) == 3 and parts[0] == "src"):
                name = parts[-2]
                if name not in ("tests", "test", "docs", "examples", "scripts", "benchmarks") and name.isidentifier():
                    pkgs.append(name)
    pkgs = sorted(set(pkgs))[:3]
    code = (
        "import sys, importlib\n"
        "print('PY', sys.version.split()[0], sys.executable)\n"
        "try:\n import pytest; print('PYTEST', pytest.__version__)\nexcept Exception as e: print('PYTEST-MISSING', type(e).__name__)\n"
        f"for m in {pkgs!r}:\n"
        "  try:\n    mod = importlib.import_module(m); print('IMPORT-OK', m, getattr(mod, '__file__', ''))\n"
        "  except Exception as e: print('IMPORT-FAIL', m, type(e).__name__, str(e)[:160])\n"
    )
    res = run_command(f"python - <<'PYEOF'\n{code}\nPYEOF", root, timeout=60, env=env)
    pyver = ""
    for line in res.output.splitlines():
        if line.startswith("PY "):
            pyver = line[3:]
        elif line.startswith("PYTEST-MISSING"):
            notes.append("pytest is not installed in this environment (pip install pytest if you need it).")
        elif line.startswith("IMPORT-FAIL"):
            notes.append(f"importing the package fails: {line[12:]} - dependencies may be missing; install what you need with pip.")
        elif line.startswith("IMPORT-OK"):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[2] and str(root) not in parts[2]:
                notes.append(f"`import {parts[1]}` resolves OUTSIDE the repo ({parts[2]}); the harness puts the repo first on PYTHONPATH.")
    if not pyver:
        notes.append("`python` was not found on PATH; try python3.")
    return pyver, notes, pkgs


def inspect_repo(root: Path) -> RepoInfo:
    root = root.resolve()
    files = list_files(root)
    counts: Counter = Counter()
    for f in files:
        lang = LANG_BY_EXT.get(Path(f).suffix.lower())
        if lang:
            counts[lang] += 1
    langs = [l for l, _ in counts.most_common()]
    primary = langs[0] if langs else "unknown"
    framework, full, per_file = detect_tests(root, files, primary)
    info = RepoInfo(root=root, files=files, languages=langs, primary_language=primary, test_command=full,
                    test_file_command=per_file, test_framework=framework, overview=overview(root, files))
    if primary == "Python" or framework.startswith(("pytest", "django", "sympy")):
        env = build_env(root)
        venv = find_repo_venv(root)
        pyver, notes, pkgs = probe_python(root, files, env)
        info.python = pyver + (f" (venv {venv})" if venv else "")
        info.env_notes += notes
        info.package_dirs = pkgs
    return info
