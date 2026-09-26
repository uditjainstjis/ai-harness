"""Language-aware symbol extraction: Python via `ast`, everything else via universal-ctags when
installed, with a regex fallback so the harness never depends on an external binary."""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts",
    ".scala", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".swift", ".m",
    ".mm", ".lua", ".pl", ".pm", ".sh", ".bash", ".zsh", ".ex", ".exs", ".erl", ".hs", ".ml", ".r", ".R",
    ".jl", ".dart", ".vue", ".svelte", ".sql", ".groovy", ".clj", ".cljs",
}


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    path: str  # repo-relative
    line: int
    end_line: Optional[int] = None
    signature: str = ""

    def render(self) -> str:
        span = f"{self.line}" + (f"-{self.end_line}" if self.end_line and self.end_line != self.line else "")
        sig = f"  {self.signature}" if self.signature else ""
        return f"{self.path}:{span}  {self.kind} {self.qualname}{sig}"


def _py_signature(node: ast.AST) -> str:
    try:
        args = node.args  # type: ignore[attr-defined]
        names = [a.arg for a in getattr(args, "posonlyargs", [])] + [a.arg for a in args.args]
        if args.vararg:
            names.append("*" + args.vararg.arg)
        names += [a.arg for a in args.kwonlyargs]
        if args.kwarg:
            names.append("**" + args.kwarg.arg)
        return "(" + ", ".join(names) + ")"
    except Exception:  # noqa: BLE001
        return ""


def python_symbols(source: str, rel: str) -> List[Symbol]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return regex_symbols(source, rel)
    out: List[Symbol] = []

    def visit(node: ast.AST, prefix: str, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if prefix else "def")
                q = f"{prefix}.{child.name}" if prefix else child.name
                sig = "" if kind == "class" else _py_signature(child)
                if kind == "class" and child.bases:
                    try:
                        sig = "(" + ", ".join(ast.unparse(b) for b in child.bases) + ")"  # py>=3.9
                    except Exception:  # noqa: BLE001
                        sig = ""
                out.append(Symbol(child.name, q, kind, rel, child.lineno, getattr(child, "end_lineno", None), sig))
                if depth < 3:
                    visit(child, q, depth + 1)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and depth == 0:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id.isupper() or t.id[0].isupper()):
                        out.append(Symbol(t.id, t.id, "var", rel, child.lineno, None, ""))
            elif isinstance(child, (ast.If, ast.Try)) and depth == 0:
                visit(child, prefix, depth)

    visit(tree, "", 0)
    return out


_REGEX_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
    (re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("), "func"),
    (re.compile(r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)"), "type"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"), "fn"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)"), "type"),
    (re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)"), "def"),
    (re.compile(r"^\s*(?:class|module)\s+([A-Z]\w*)"), "class"),
    (re.compile(r"^\s*(?:public|private|protected|internal|static|final|abstract|override|virtual|async|synchronized|\s)+[\w<>\[\],.? ]+\s+([A-Za-z_]\w*)\s*\([^;]*$"), "method"),
    (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?function\s+&?([A-Za-z_]\w*)\s*\("), "function"),
]


def regex_symbols(source: str, rel: str) -> List[Symbol]:
    out: List[Symbol] = []
    for i, line in enumerate(source.splitlines(), 1):
        if len(line) > 400:
            continue
        for rx, kind in _REGEX_PATTERNS:
            m = rx.match(line)
            if m:
                name = m.group(1)
                if name in ("if", "for", "while", "switch", "return", "catch", "new"):
                    break
                out.append(Symbol(name, name, kind, rel, i, None, line.strip()[:120]))
                break
    return out


def file_symbols(path: Path, rel: str, source: Optional[str] = None) -> List[Symbol]:
    if source is None:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
    if path.suffix in (".py", ".pyi"):
        return python_symbols(source, rel)
    return regex_symbols(source, rel)


class SymbolIndex:
    """Lazily built index over the repository's code files."""

    def __init__(self, root: Path, files: Iterable[str]) -> None:
        self.root = root
        self.files = [f for f in files if Path(f).suffix in CODE_EXTS]
        self._by_name: Optional[Dict[str, List[Symbol]]] = None
        self._by_file: Dict[str, List[Symbol]] = {}

    def _build(self) -> None:
        by_name: Dict[str, List[Symbol]] = {}
        non_py = [f for f in self.files if not f.endswith((".py", ".pyi"))]
        ctags_done = set()
        if non_py and shutil.which("ctags"):
            ctags_done = self._ctags(non_py, by_name)
        for rel in self.files:
            if rel in ctags_done:
                continue
            p = self.root / rel
            try:
                if p.stat().st_size > 1_500_000:
                    continue
            except OSError:
                continue
            syms = file_symbols(p, rel)
            self._by_file[rel] = syms
            for s in syms:
                by_name.setdefault(s.name, []).append(s)
        self._by_name = by_name

    def _ctags(self, files: List[str], by_name: Dict[str, List[Symbol]]) -> set:
        done = set()
        try:
            proc = subprocess.run(
                ["ctags", "--output-format=json", "--fields=+nKSe", "-f", "-", "-L", "-"],
                input="\n".join(files), capture_output=True, text=True, cwd=str(self.root), timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return done
        if proc.returncode != 0:
            return done
        for line in proc.stdout.splitlines():
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if t.get("_type") != "tag":
                continue
            kind = t.get("kind", "")
            if kind in ("variable", "local", "parameter", "member", "field", "label", "enumerator", "import", "package"):
                continue
            scope = t.get("scope")
            q = f"{scope}.{t['name']}" if scope else t["name"]
            s = Symbol(t["name"], q, kind, t["path"], int(t.get("line", 0)), t.get("end"), t.get("signature", "") or "")
            self._by_file.setdefault(t["path"], []).append(s)
            by_name.setdefault(s.name, []).append(s)
            done.add(t["path"])
        return done

    @property
    def by_name(self) -> Dict[str, List[Symbol]]:
        if self._by_name is None:
            self._build()
        return self._by_name  # type: ignore[return-value]

    def symbols_in(self, rel: str) -> List[Symbol]:
        if self._by_name is None:
            self._build()
        if rel not in self._by_file:
            p = self.root / rel
            self._by_file[rel] = file_symbols(p, rel) if p.exists() else []
        return self._by_file[rel]

    def lookup(self, query: str, limit: int = 20) -> List[Symbol]:
        query = query.strip().strip("`'\"()")
        if not query:
            return []
        parts = re.split(r"[.:#]+", query)
        leaf = parts[-1]
        cands = list(self.by_name.get(leaf, []))
        if len(parts) > 1:
            suffix = ".".join(parts[-2:])
            narrowed = [s for s in cands if s.qualname.endswith(suffix)]
            path_hint = "/".join(parts[:-1])
            by_path = [s for s in cands if path_hint.replace(".", "/") in s.path.replace(".py", "")]
            cands = narrowed or by_path or cands
        if not cands:
            low = leaf.lower()
            for name, syms in self.by_name.items():
                if name.lower() == low:
                    cands.extend(syms)
        def rank(s: Symbol):
            is_test = "test" in s.path.lower()
            return (is_test, s.kind not in ("class", "def", "function", "func", "fn", "method"), len(s.path))
        cands.sort(key=rank)
        return cands[:limit]

    def invalidate(self, rel: str) -> None:
        self._by_file.pop(rel, None)
