"""`str_replace_editor`: view / create / str_replace / insert / undo_edit.

Three reliability features that matter most for weaker models:
1. Tolerant matching - if `old_str` is not found verbatim we retry with (a) line-number
   prefixes copied from `view` output removed, (b) trailing whitespace ignored,
   (c) indentation ignored (the replacement is re-indented to fit). Only a UNIQUE match is
   ever applied.
2. Helpful misses - if nothing matches, we show the closest region of the file with line
   numbers so the model can correct itself in one step.
3. Lint gate - an edit that turns a parseable file into an unparseable one is rejected and
   rolled back, with the exact syntax error, before it can poison later steps.
"""
from __future__ import annotations

import difflib
import json
import warnings
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..repo.symbols import SymbolIndex, file_symbols

VIEW_FULL_MAX_LINES = 400
VIEW_MAX_RANGE = 500
LINE_NO_RE = re.compile(r"^\s*\d+\t")
LINE_NO_LOOSE_RE = re.compile(r"^\s*\d+[:|]?\s{1,4}")
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".venv",
                "venv", "dist", "build", ".eggs", ".idea", ".vscode", "target", ".pramana", ".ruff_cache"}


class EditError(Exception):
    pass


def _indent(s: str) -> str:
    return s[: len(s) - len(s.lstrip(" \t"))]


def reindent(new_lines: List[str], old_lines: List[str], file_window: List[str]) -> List[str]:
    """Map the indentation scheme the model used (old_str) onto the file's real one, level by level."""
    mapping: Dict[int, str] = {}
    for o, f in zip(old_lines, file_window):
        if o.strip() and f.strip():
            mapping.setdefault(len(_indent(o).expandtabs(4)), _indent(f))
    keys = sorted(mapping)
    ratio = 1.0
    for k in keys:
        if k > 0 and "\t" not in mapping[k]:
            ratio = len(mapping[k]) / k
            break
    out = []
    for line in new_lines:
        if not line.strip():
            out.append("")
            continue
        w = len(_indent(line).expandtabs(4))
        if w in mapping:
            pre = mapping[w]
        else:
            lower = [k for k in keys if k <= w]
            if lower:
                base = lower[-1]
                extra = int(round((w - base) * ratio))
                pre = mapping[base] + ("\t" * max(1, extra // 4) if "\t" in mapping[base] else " " * extra)
            else:
                pre = _indent(line)
        out.append(pre + line.lstrip(" \t"))
    return out


def number_lines(lines: List[str], start: int = 1) -> str:
    out = []
    for i, line in enumerate(lines, start):
        if len(line) > 500:
            line = line[:500] + " …[line truncated]"
        out.append(f"{i:>6}\t{line}")
    return "\n".join(out)


def syntax_error(path: Path, content: str) -> Optional[str]:
    """Return a human-readable syntax error for supported file types, else None."""
    suf = path.suffix.lower()
    if suf in (".py", ".pyi"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(content, str(path), "exec", dont_inherit=True)
        except SyntaxError as e:
            line = (e.text or "").rstrip("\n")
            caret = " " * max((e.offset or 1) - 1, 0) + "^"
            return f"SyntaxError: {e.msg} (line {e.lineno})\n    {line}\n    {caret}"
        except (ValueError, TypeError) as e:
            return f"{type(e).__name__}: {e}"
        return None
    if suf == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return f"JSON error: {e}"
        return None
    if suf in (".js", ".mjs", ".cjs") and shutil.which("node"):
        with tempfile.NamedTemporaryFile("w", suffix=suf, delete=False) as fh:
            fh.write(content)
            tmp = fh.name
        try:
            p = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=20)
            if p.returncode != 0:
                msg = (p.stderr or p.stdout).replace(tmp, str(path)).strip()
                return "\n".join(msg.splitlines()[:8])
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            os.unlink(tmp)
        return None
    if suf == ".toml":
        try:
            import tomllib  # type: ignore
            tomllib.loads(content)
        except ImportError:
            return None
        except Exception as e:  # noqa: BLE001
            return f"TOML error: {e}"
    return None


class Editor:
    def __init__(self, root: Path, scratch: Path, index: Optional[SymbolIndex] = None) -> None:
        self.root = root.resolve()
        self.scratch = scratch.resolve()
        self.index = index
        self.history: Dict[str, List[Optional[str]]] = {}
        self.touched: List[str] = []  # repo-relative paths written by the agent, in order
        self.created: List[str] = []  # repo-relative paths the agent deliberately created
        self.states: Dict[str, List[str]] = {}  # content hashes per file, to detect back-and-forth edits
        self.oscillations = 0

    # ------------------------------------------------------------------ paths
    def resolve(self, path: str, for_write: bool = False) -> Path:
        if not path:
            raise EditError("path is required")
        p = Path(path.strip().strip("'\"")).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = Path(os.path.normpath(str(p)))
        if for_write:
            ok = str(p).startswith(str(self.root) + os.sep) or str(p).startswith(str(self.scratch) + os.sep) or p == self.root
            if not ok:
                raise EditError(f"refusing to write outside the repository: {p}")
        return p

    def rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.root))
        except ValueError:
            return str(p)

    def _record(self, p: Path, old: Optional[str]) -> None:
        key = str(p)
        self.history.setdefault(key, []).append(old)
        r = self.rel(p)
        if r not in self.touched:
            self.touched.append(r)
        if self.index is not None:
            self.index.invalidate(r)

    # ------------------------------------------------------------------ commands
    def view(self, path: str, view_range: Optional[List[int]] = None) -> str:
        p = self.resolve(path)
        if not p.exists():
            hint = self._suggest_path(path)
            raise EditError(f"path does not exist: {path}" + (f"\nDid you mean: {hint}" if hint else ""))
        if p.is_dir():
            return self._view_dir(p)
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise EditError(f"cannot read {path}: {e}")
        if b"\x00" in raw[:4096]:
            raise EditError(f"{path} looks like a binary file ({len(raw)} bytes); not displaying")
        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        n = len(lines)
        rel = self.rel(p)
        if view_range:
            try:
                start = int(view_range[0])
                end = int(view_range[1]) if len(view_range) > 1 else start + 100
            except (TypeError, ValueError, IndexError):
                raise EditError("view_range must be [start_line, end_line] (1-based, end -1 = end of file)")
            if end == -1 or end > n:
                end = n
            start = max(1, start)
            if start > n:
                raise EditError(f"start line {start} is past the end of {rel} ({n} lines)")
            note = ""
            if end - start + 1 > VIEW_MAX_RANGE:
                end = start + VIEW_MAX_RANGE - 1
                note = f"\n[range capped at {VIEW_MAX_RANGE} lines; request the next range to continue]"
            return f"{rel} (lines {start}-{end} of {n})\n" + number_lines(lines[start - 1 : end], start) + note
        if n <= VIEW_FULL_MAX_LINES:
            return f"{rel} ({n} lines)\n" + number_lines(lines)
        syms = file_symbols(p, rel, text)
        outline = "\n".join(f"  {s.line:>6}  {s.kind} {s.qualname}{s.signature if s.kind != 'class' else ''}" for s in syms[:150])
        more = "" if len(syms) <= 150 else f"\n  ... {len(syms) - 150} more symbols"
        head = number_lines(lines[:120])
        return (
            f"{rel} is long ({n} lines). Outline (line, kind, name):\n{outline or '  (no symbols found)'}{more}\n\n"
            f"First 120 lines:\n{head}\n\n[Use view_range=[start, end] to read a specific region.]"
        )

    def _view_dir(self, p: Path) -> str:
        out: List[str] = []
        base_depth = len(p.parts)
        count = 0
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= 2:
                dirnames[:] = []
            rel = self.rel(Path(dirpath))
            for d in dirnames:
                out.append(f"{rel}/{d}/" if rel != "." else f"{d}/")
            for f in sorted(filenames):
                if f.startswith(".") or f.endswith((".pyc", ".pyo")):
                    continue
                out.append(f"{rel}/{f}" if rel != "." else f)
                count += 1
            if len(out) > 400:
                out.append("... (listing truncated; view a subdirectory)")
                break
        return f"Files and directories up to 2 levels deep in {self.rel(p)}:\n" + "\n".join(out)

    def _suggest_path(self, path: str) -> str:
        name = Path(path).name
        if not name:
            return ""
        hits = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            if name in filenames:
                hits.append(self.rel(Path(dirpath) / name))
                if len(hits) >= 5:
                    break
        return ", ".join(hits)

    def create(self, path: str, file_text: str) -> str:
        p = self.resolve(path, for_write=True)
        in_scratch = str(p).startswith(str(self.scratch) + os.sep)
        if p.exists() and in_scratch and p.is_file():
            # scratch files are throwaway: overwriting them is harmless and saves a step
            old = p.read_text(encoding="utf-8", errors="replace")
            p.write_text(file_text or "", encoding="utf-8")
            self._record(p, old)
            return f"Overwrote scratch file {self.rel(p)} ({(file_text or '').count(chr(10)) + 1} lines)."
        if p.exists():
            raise EditError(
                f"{self.rel(p)} already exists. Use command=str_replace to change it "
                "(create never overwrites, so existing content cannot be lost by accident)."
            )
        if file_text is None:
            raise EditError("file_text is required for create")
        p.parent.mkdir(parents=True, exist_ok=True)
        err = syntax_error(p, file_text)
        p.write_text(file_text, encoding="utf-8")
        self._record(p, None)
        if self.rel(p) not in self.created:
            self.created.append(self.rel(p))
        n = file_text.count("\n") + (0 if file_text.endswith("\n") else 1)
        warn = f"\nWARNING: the new file has a syntax problem:\n{err}" if err else ""
        return f"Created {self.rel(p)} ({n} lines).{warn}"

    def str_replace(self, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        p = self.resolve(path, for_write=True)
        if not p.is_file():
            hint = self._suggest_path(path)
            raise EditError(f"file does not exist: {path}" + (f"\nDid you mean: {hint}" if hint else ""))
        if old_str is None or old_str == "":
            raise EditError("old_str must be non-empty (to add text use command=insert, to make a new file use command=create)")
        if new_str is None:
            new_str = ""
        content = p.read_text(encoding="utf-8", errors="replace")
        if old_str == new_str:
            raise EditError("old_str and new_str are identical; nothing to change")

        count = content.count(old_str)
        note = ""
        if count == 0:
            new_content, note = self._tolerant_replace(content, old_str, new_str)
            if new_content is None:
                raise EditError(self._miss_report(content, old_str, self.rel(p)))
        elif count > 1 and not replace_all:
            lines_at = []
            start = 0
            for _ in range(count):
                idx = content.find(old_str, start)
                lines_at.append(content.count("\n", 0, idx) + 1)
                start = idx + 1
            raise EditError(
                f"old_str occurs {count} times in {self.rel(p)} (starting at lines {lines_at[:10]}). "
                "Include more surrounding lines to make it unique, or pass replace_all=true to change every occurrence."
            )
        else:
            new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
        return self._commit(p, content, new_content, new_str, note)

    def _commit(self, p: Path, before: str, after: str, new_str: str, note: str = "") -> str:
        before_err = syntax_error(p, before)
        after_err = syntax_error(p, after)
        if after_err and not before_err:
            raise EditError(
                "Edit NOT applied: it would make the file unparseable.\n"
                f"{after_err}\nFix the replacement text (check indentation, brackets, quotes) and try again."
            )
        p.write_text(after, encoding="utf-8")
        self._record(p, before)
        import hashlib

        hist = self.states.setdefault(str(p), [hashlib.sha1(before.encode()).hexdigest()])
        h = hashlib.sha1(after.encode()).hexdigest()
        revisit = h in hist[:-1]
        hist.append(h)
        # snippet around the change
        a_lines, b_lines = before.split("\n"), after.split("\n")
        sm = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
        first, last = None, None
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                first = j1 if first is None else first
                last = max(j2, j1 + 1)
        if first is None:
            first, last = 0, 1
        lo, hi = max(0, first - 4), min(len(b_lines), last + 4)
        snippet = number_lines(b_lines[lo:hi], lo + 1)
        added = sum(1 for l in difflib.ndiff(a_lines, b_lines) if l.startswith("+ "))
        removed = sum(1 for l in difflib.ndiff(a_lines, b_lines) if l.startswith("- "))
        msg = f"Edited {self.rel(p)} (+{added} -{removed} lines){' ' + note if note else ''}. Result:\n{snippet}"
        if after_err and before_err:
            msg += f"\nNote: the file was already unparseable before this edit:\n{after_err}"
        if revisit:
            self.oscillations += 1
            msg += ("\nNOTE: this edit returns the file to a version it already had earlier - you are going back and "
                    "forth. Stop editing and re-think: re-read the issue, and use `compare` to check whether the failure "
                    "you are chasing already existed before your changes (then it is not yours to fix).")
        return msg

    def _tolerant_replace(self, content: str, old_str: str, new_str: str) -> Tuple[Optional[str], str]:
        # (a) line-number prefixes pasted from `view` output
        old_lines = old_str.split("\n")
        if all(LINE_NO_RE.match(l) or not l.strip() for l in old_lines) and any(LINE_NO_RE.match(l) for l in old_lines):
            stripped_old = "\n".join(LINE_NO_RE.sub("", l, count=1) for l in old_lines)
            new_lines = new_str.split("\n")
            stripped_new = (
                "\n".join(LINE_NO_RE.sub("", l, count=1) for l in new_lines)
                if any(LINE_NO_RE.match(l) for l in new_lines) else new_str
            )
            if content.count(stripped_old) == 1:
                return content.replace(stripped_old, stripped_new, 1), "(matched after removing pasted line numbers)"
            old_str, new_str = stripped_old, stripped_new
            old_lines = old_str.split("\n")

        file_lines = content.split("\n")
        # trim blank edge lines in old_str: they are rarely intentional
        while old_lines and not old_lines[0].strip():
            old_lines = old_lines[1:]
        while old_lines and not old_lines[-1].strip():
            old_lines = old_lines[:-1]
        if not old_lines:
            return None, ""
        k = len(old_lines)

        def windows(norm):
            target = [norm(l) for l in old_lines]
            hits = []
            for i in range(0, len(file_lines) - k + 1):
                if norm(file_lines[i]) != target[0]:
                    continue
                if [norm(l) for l in file_lines[i : i + k]] == target:
                    hits.append(i)
            return hits

        # (b) trailing whitespace
        hits = windows(lambda s: s.rstrip())
        if len(hits) == 1:
            i = hits[0]
            new_block = new_str.split("\n")
            out = file_lines[:i] + new_block + file_lines[i + k :]
            return "\n".join(out), "(matched ignoring trailing whitespace)"
        # (c) indentation-insensitive, re-indent the replacement
        hits = windows(lambda s: s.strip())
        if len(hits) == 1:
            i = hits[0]
            new_block = reindent(new_str.split("\n"), old_lines, file_lines[i : i + k])
            out = file_lines[:i] + new_block + file_lines[i + k :]
            return "\n".join(out), "(matched ignoring indentation; replacement re-indented to the file's style)"
        return None, ""

    def _miss_report(self, content: str, old_str: str, rel: str) -> str:
        file_lines = content.split("\n")
        old_lines = [l for l in old_str.split("\n")]
        k = max(1, len(old_lines))
        best, best_i = 0.0, -1
        target = "\n".join(l.strip() for l in old_lines)
        first_tok = old_lines[0].strip()[:12] if old_lines and old_lines[0].strip() else ""
        for i in range(0, max(1, len(file_lines) - k + 1)):
            window = "\n".join(l.strip() for l in file_lines[i : i + k])
            sm = difflib.SequenceMatcher(None, target, window, autojunk=False)
            if sm.real_quick_ratio() < best or sm.quick_ratio() < best:
                continue
            r = sm.ratio()
            if first_tok and first_tok in window:
                r += 0.02
            if r > best:
                best, best_i = r, i
        msg = f"old_str was not found in {rel} (not even ignoring whitespace/indentation)."
        if best_i >= 0 and best > 0.45:
            lo, hi = max(0, best_i - 2), min(len(file_lines), best_i + k + 2)
            msg += (
                f"\nThe most similar region ({best:.0%} similar) is lines {best_i + 1}-{best_i + k}:\n"
                + number_lines(file_lines[lo:hi], lo + 1)
                + "\nCopy the exact current text from the file (without the line-number column) into old_str."
            )
        else:
            msg += " View the file again to copy the exact current text."
        return msg

    def insert(self, path: str, insert_line: int, new_str: str) -> str:
        p = self.resolve(path, for_write=True)
        if not p.is_file():
            raise EditError(f"file does not exist: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        try:
            at = int(insert_line)
        except (TypeError, ValueError):
            raise EditError("insert_line must be an integer (0 = top of file; N = after line N)")
        if at < 0 or at > len(lines):
            raise EditError(f"insert_line {at} is out of range (file has {len(lines)} lines)")
        new_lines = lines[:at] + (new_str or "").split("\n") + lines[at:]
        return self._commit(p, content, "\n".join(new_lines), new_str or "")

    def undo_edit(self, path: str) -> str:
        p = self.resolve(path, for_write=True)
        stack = self.history.get(str(p))
        if not stack:
            raise EditError(f"no edit history for {self.rel(p)}")
        prev = stack.pop()
        if prev is None:
            if p.exists():
                p.unlink()
            return f"Undid creation of {self.rel(p)} (file removed)."
        p.write_text(prev, encoding="utf-8")
        if self.index is not None:
            self.index.invalidate(self.rel(p))
        return f"Reverted the last edit to {self.rel(p)}."
