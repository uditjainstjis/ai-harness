"""Change tracking against a fixed baseline, without touching the user's index, HEAD or stash.

* Works in a real git checkout (baseline = HEAD, or a snapshot commit if the tree was dirty)
  and in a plain directory (a private "shadow" git dir outside the work tree).
* `patch()` builds the full diff (edits + new files, binary-safe) through a temporary index.
* `baseline()` temporarily reverse-applies the patch so a command can be run against the
  original code, then restores it. This is how every claim gets its before/after evidence.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

SCRATCH_DIRNAME = ".pramana"
JUNK_PATTERNS = ("__pycache__/", ".pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".egg-info/", ".DS_Store",
                 ".coverage", "node_modules/", ".tox/", ".nox/", ".hypothesis/")


class GitError(Exception):
    pass


class GitTracker:
    def __init__(self, root: Path, shadow_dir: Optional[Path] = None) -> None:
        self.root = root.resolve()
        self.env: Dict[str, str] = dict(os.environ)
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(var, None)
        self.env.update({"GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        self.shadow = False
        top = self._try(["git", "rev-parse", "--show-toplevel"])
        if top is None or Path(top).resolve() != self.root:
            if top is not None and Path(top).resolve() != self.root:
                # repo is a subdirectory of a larger checkout: track only this subtree via shadow
                pass
            self._init_shadow(shadow_dir)
        self._ensure_exclude()
        self.initial_untracked = set(self._untracked())
        self.base = self._snapshot_base()

    # ------------------------------------------------------------------ plumbing
    def _run(self, args: List[str], env: Optional[Dict[str, str]] = None, input_bytes: Optional[bytes] = None,
             check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run(args, cwd=str(self.root), env=env or self.env, input=input_bytes,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and p.returncode != 0:
            raise GitError(f"{' '.join(args[:4])} failed: {p.stderr.decode(errors='replace')[:500]}")
        return p

    def _try(self, args: List[str]) -> Optional[str]:
        try:
            p = self._run(args, check=False)
        except OSError:
            return None
        return p.stdout.decode(errors="replace").strip() if p.returncode == 0 else None

    def _git(self, *args: str, env: Optional[Dict[str, str]] = None, input_bytes: Optional[bytes] = None) -> str:
        return self._run(["git", *args], env=env, input_bytes=input_bytes).stdout.decode(errors="replace")

    def _init_shadow(self, shadow_dir: Optional[Path]) -> None:
        d = shadow_dir or Path(tempfile.mkdtemp(prefix="pramana-shadow-"))
        d.mkdir(parents=True, exist_ok=True)
        self.env["GIT_DIR"] = str(d / "git")
        self.env["GIT_WORK_TREE"] = str(self.root)
        self.shadow = True
        if not (d / "git").exists():
            self._git("init", "-q")
        self._git("config", "user.email", "pramana@localhost")
        self._git("config", "user.name", "pramana")
        self._ensure_exclude()
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", "pramana baseline")

    def _ensure_exclude(self) -> None:
        path = self._try(["git", "rev-parse", "--git-path", "info/exclude"])
        if not path:
            return
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text() if p.exists() else ""
        if f"/{SCRATCH_DIRNAME}/" not in existing:
            with open(p, "a") as fh:
                fh.write(f"\n/{SCRATCH_DIRNAME}/\n")

    def _untracked(self) -> List[str]:
        out = self._git("ls-files", "--others", "--exclude-standard", "-z")
        return [f for f in out.split("\0") if f]

    def _snapshot_base(self) -> str:
        head = self._git("rev-parse", "HEAD").strip()
        dirty = self._git("status", "--porcelain", "--untracked-files=no").strip()
        if not dirty:
            return head
        snap = self._git("stash", "create").strip()  # commit object of the dirty tree; does not touch the stash list
        return snap or head

    # ------------------------------------------------------------------ public API
    def _temp_index_env(self) -> Tuple[Dict[str, str], str]:
        fd, idx = tempfile.mkstemp(prefix="pramana-index-")
        os.close(fd)
        os.unlink(idx)
        env = dict(self.env)
        env["GIT_INDEX_FILE"] = idx
        return env, idx

    def _is_junk(self, path: str) -> bool:
        return path.startswith(SCRATCH_DIRNAME + "/") or any(j in path or path.endswith(j.rstrip("/")) for j in JUNK_PATTERNS)

    def patch(self, include_new: bool = True, paths: Optional[List[str]] = None) -> str:
        env, idx = self._temp_index_env()
        try:
            self._git("read-tree", self.base, env=env)
            self._git("add", "-A", "--", ".", env=env)
            # drop files that were already untracked before we started, and junk
            staged = self._git("diff", "--cached", "--name-only", "-z", self.base, env=env).split("\0")
            drop = [f for f in staged if f and (f in self.initial_untracked or self._is_junk(f))]
            if not include_new:
                drop += [f for f in staged if f and f not in drop and self._is_new(f)]
            for i in range(0, len(drop), 200):
                self._git("rm", "-q", "--cached", "--ignore-unmatch", "--", *drop[i : i + 200], env=env)
            args = ["diff", "--cached", "--binary", "--no-color", "--no-ext-diff", self.base]
            if paths:
                args += ["--", *paths]
            return self._git(*args, env=env)
        finally:
            if os.path.exists(idx):
                os.unlink(idx)

    def _is_new(self, path: str) -> bool:
        return self._run(["git", "cat-file", "-e", f"{self.base}:{path}"], check=False).returncode != 0

    def changed_files(self) -> List[Tuple[str, str]]:
        """[(status, path)] with status A/M/D relative to the baseline."""
        env, idx = self._temp_index_env()
        try:
            self._git("read-tree", self.base, env=env)
            self._git("add", "-A", "--", ".", env=env)
            out = self._git("diff", "--cached", "--name-status", "-z", "--no-renames", self.base, env=env)
        finally:
            if os.path.exists(idx):
                os.unlink(idx)
        parts = [p for p in out.split("\0") if p]
        res = []
        for i in range(0, len(parts) - 1, 2):
            status, path = parts[i], parts[i + 1]
            if path in self.initial_untracked or self._is_junk(path):
                continue
            res.append((status[0], path))
        return res

    def apply(self, patch_text: str, reverse: bool = False) -> None:
        if not patch_text.strip():
            return
        args = ["apply", "--binary", "--whitespace=nowarn"]
        if reverse:
            args.append("-R")
        p = self._run(["git", *args], input_bytes=patch_text.encode(), check=False)
        if p.returncode != 0:
            raise GitError(f"git apply{' -R' if reverse else ''} failed: {p.stderr.decode(errors='replace')[:800]}")

    @contextlib.contextmanager
    def baseline(self) -> Iterator[str]:
        """Temporarily restore the original code (scratch files stay). Yields the patch."""
        patch_text = self.patch()
        if not patch_text.strip():
            yield patch_text
            return
        self.apply(patch_text, reverse=True)
        try:
            yield patch_text
        finally:
            self.apply(patch_text)

    def reset_to_base(self) -> str:
        """Remove every change made since the baseline. Returns the removed patch."""
        patch_text = self.patch()
        if patch_text.strip():
            self.apply(patch_text, reverse=True)
        return patch_text

    def restore(self, path: str) -> bool:
        """Put one file back to its baseline content."""
        p = self._run(["git", "show", f"{self.base}:{path}"], check=False)
        if p.returncode != 0:
            return False
        (self.root / path).write_bytes(p.stdout)
        return True

    def diffstat(self, patch_text: str) -> Tuple[int, int, int]:
        files = added = removed = 0
        for line in patch_text.splitlines():
            if line.startswith("diff --git"):
                files += 1
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return files, added, removed

    def cleanup(self) -> None:
        if self.shadow:
            gd = Path(self.env["GIT_DIR"]).parent
            shutil.rmtree(gd, ignore_errors=True)
