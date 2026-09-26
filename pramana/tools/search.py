"""Code search: ripgrep when available, pure-Python fallback otherwise. Results are grouped by
file and capped, so one broad query cannot flood the context window."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .editor import IGNORED_DIRS

MAX_MATCHES = 80
MAX_PER_FILE = 12


def _rg(root: Path, pattern: str, path: Optional[str], glob: Optional[str], fixed: bool, ignore_case: bool) -> Optional[List[Tuple[str, int, str]]]:
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [rg, "--no-heading", "--line-number", "--color", "never", "--max-columns", "300",
           "--max-columns-preview", "--max-count", "200", "--hidden", "-g", "!.git", "-g", "!.pramana"]
    if fixed:
        cmd.append("-F")
    if ignore_case:
        cmd.append("-i")
    if glob:
        cmd += ["-g", glob]
    cmd += ["-e", pattern]
    if path:
        cmd.append(path)
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode not in (0, 1):
        if "regex parse error" in p.stderr and not fixed:
            return _rg(root, pattern, path, glob, True, ignore_case)
        return None
    out = []
    for line in p.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((parts[0], int(parts[1]), parts[2]))
    return out


def _git_grep(root: Path, pattern: str, path: Optional[str], glob: Optional[str], fixed: bool, ignore_case: bool) -> Optional[List[Tuple[str, int, str]]]:
    """Fast fallback wherever git exists: searches tracked + untracked (non-ignored) files."""
    if not shutil.which("git") or not (root / ".git").exists():
        return None
    cmd = ["git", "grep", "-n", "-I", "--untracked", "--no-color", "--full-name"]
    cmd.append("-F" if fixed else "-E")
    if ignore_case:
        cmd.append("-i")
    cmd += ["-e", pattern, "--"]
    if path:
        cmd.append(path)
    if glob:
        cmd.append(glob if "/" in glob or glob.startswith("!") else f"*{glob}" if glob.startswith("*") else glob)
    cmd.append(":(exclude).pramana")
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode not in (0, 1):
        if not fixed and ("regex" in p.stderr.lower() or "invalid" in p.stderr.lower()):
            return _git_grep(root, pattern, path, glob, True, ignore_case)
        return None
    out = []
    for line in p.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((parts[0], int(parts[1]), parts[2][:300]))
    return out


def _py_search(root: Path, pattern: str, path: Optional[str], glob: Optional[str], fixed: bool, ignore_case: bool) -> List[Tuple[str, int, str]]:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(re.escape(pattern) if fixed else pattern, flags)
    except re.error:
        rx = re.compile(re.escape(pattern), flags)
    base = (root / path) if path else root
    files: List[Path] = []
    if base.is_file():
        files = [base]
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for f in filenames:
                if glob and not fnmatch.fnmatch(f, glob) and not fnmatch.fnmatch(os.path.join(dirpath, f), glob):
                    continue
                files.append(Path(dirpath) / f)
    out = []
    for fp in files:
        try:
            if fp.stat().st_size > 2_000_000:
                continue
            with open(fp, "r", encoding="utf-8", errors="strict") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append((str(fp.relative_to(root)), i, line.rstrip("\n")[:300]))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def search(root: Path, pattern: str, path: Optional[str] = None, glob: Optional[str] = None,
           fixed: bool = False, ignore_case: bool = False) -> str:
    if not pattern:
        return "error: pattern is required"
    if path:
        path = path.strip()
        if os.path.isabs(path):
            try:
                path = str(Path(path).resolve().relative_to(root.resolve()))
            except ValueError:
                return f"error: path {path} is outside the repository"
        if not (root / path).exists():
            return f"error: path does not exist: {path}"
    hits = _rg(root, pattern, path, glob, fixed, ignore_case)
    perl_only = (not fixed) and re.search(r"\\[dwsbDWSB]|\(\?", pattern)
    if hits is None and not (glob and glob.startswith("!")) and not perl_only:
        hits = _git_grep(root, pattern, path, glob, fixed, ignore_case)
    if hits is None:
        hits = _py_search(root, pattern, path, glob, fixed, ignore_case)
    if not hits:
        return f"No matches for {pattern!r}" + (f" in {path}" if path else "") + (f" (glob {glob})" if glob else "") + "."
    by_file: Dict[str, List[Tuple[int, str]]] = {}
    for f, ln, text in hits:
        by_file.setdefault(f, []).append((ln, text))
    # non-test source files first, then by match count
    order = sorted(by_file, key=lambda f: ("test" in f.lower(), -len(by_file[f]), f))
    lines: List[str] = [f"{len(hits)} matches in {len(by_file)} files for {pattern!r}:"]
    shown = 0
    shown_files = 0
    for f in order:
        if shown >= MAX_MATCHES:
            break
        entries = by_file[f]
        lines.append(f"{f}  ({len(entries)} match{'es' if len(entries) != 1 else ''})")
        for ln, text in entries[:MAX_PER_FILE]:
            lines.append(f"  {ln}: {text.strip()[:220]}")
            shown += 1
        if len(entries) > MAX_PER_FILE:
            lines.append(f"  ... {len(entries) - MAX_PER_FILE} more in this file")
        shown_files += 1
    rest = order[shown_files:]
    if rest:
        lines.append(f"... results capped; {len(rest)} more files matched: " + ", ".join(rest[:30]))
    return "\n".join(lines)


def find_files(root: Path, pattern: str) -> str:
    rg = shutil.which("rg")
    files: List[str] = []
    if rg:
        try:
            p = subprocess.run([rg, "--files", "-g", pattern, "-g", "!.git"], cwd=str(root), capture_output=True, text=True, timeout=60)
            files = sorted(p.stdout.splitlines())
        except (OSError, subprocess.TimeoutExpired):
            files = []
    if not files:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                if fnmatch.fnmatch(fn, pattern) or fnmatch.fnmatch(rel, pattern):
                    files.append(rel)
        files.sort()
    if not files:
        return f"No files match {pattern!r}."
    more = f"\n... and {len(files) - 100} more" if len(files) > 100 else ""
    return f"{len(files)} files match {pattern!r}:\n" + "\n".join(files[:100]) + more
