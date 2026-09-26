"""Deterministic fault localization (zero model tokens).

Signals, strongest first:
  * traceback frames and file paths quoted in the issue,
  * identifiers from the issue (backticked code, dotted names, CamelCase, snake_case, calls)
    resolved through the symbol index,
  * BM25 lexical similarity between the issue and each file (identifiers split into words).
The output is a short ranked list the agent starts from - a hint, never a constraint.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .symbols import CODE_EXTS, SymbolIndex

STOP = set(
    """a an the and or but if then else when while for to of in on at by with from as is are was were be been being it its
    this that these those there here i we you he she they them my our your their me us not no yes do does did done have has
    had can could should would will shall may might must also just only very more most less least so such than too into out
    up down over under again further once all any both each few other some same own what which who whom why how where
    about above after before below between during through until against among because while use used using get gets got
    set sets new old one two three first second last next example expected actual output input result results error errors
    issue bug fix fixes problem work works working worked happen happens code line lines file files function method class
    value values return returns returned call calls called true false none null self cls def import print str int float
    bool list dict tuple type types object objects string strings number test tests see seems seem like want need please
    thanks thank hi hello following follow version versions python run running ran instead however currently now make
    makes made way case cases e.g i.e etc""".split()
)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOTTED_RE = re.compile(r"\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b")
BACKTICK_RE = re.compile(r"`{1,3}([^`]+?)`{1,3}", re.S)
TRACE_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([\w<>]+))?')
JS_TRACE_RE = re.compile(r"at (?:[\w.$<>]+ )?\(?([\w./\\-]+\.(?:js|ts|mjs|cjs|jsx|tsx)):(\d+):\d+\)?")
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|scala|toml|cfg|ini|yaml|yml|json))\b")
CALL_RE = re.compile(r"\b([A-Za-z_][\w]*)\s*\(")


def split_ident(tok: str) -> List[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok).replace("_", " ").lower().split()
    return [p for p in parts if len(p) > 1]


def tokenize(text: str) -> List[str]:
    out = []
    for tok in IDENT_RE.findall(text):
        low = tok.lower()
        if len(low) > 2 and low not in STOP:
            out.append(low)
        if "_" in tok or re.search(r"[a-z][A-Z]", tok):
            out.extend(p for p in split_ident(tok) if p not in STOP and len(p) > 2)
    return out


@dataclass
class Candidate:
    path: str
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)  # rendered "name (line N)"


def extract_identifiers(text: str) -> Tuple[List[str], List[str], List[Tuple[str, int, str]], List[str]]:
    """Return (strong identifiers, dotted names, traceback frames, quoted paths)."""
    strong: List[str] = []
    for m in BACKTICK_RE.finditer(text):
        strong += IDENT_RE.findall(m.group(1))
    for m in CALL_RE.finditer(text):
        strong.append(m.group(1))
    for tok in IDENT_RE.findall(text):
        if "_" in tok.strip("_") or re.search(r"[a-z][A-Z]", tok) or re.fullmatch(r"[A-Z][a-z]+[A-Z]\w*", tok):
            strong.append(tok)
    dotted = DOTTED_RE.findall(text)
    frames: List[Tuple[str, int, str]] = []
    for m in TRACE_RE.finditer(text):
        frames.append((m.group(1), int(m.group(2)), m.group(3) or ""))
    for m in JS_TRACE_RE.finditer(text):
        frames.append((m.group(1), int(m.group(2)), ""))
    paths = PATH_RE.findall(text)
    seen: Set[str] = set()
    uniq = []
    for s in strong:
        if s.lower() in STOP or len(s) < 3 or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:80], list(dict.fromkeys(dotted))[:60], frames[-40:], list(dict.fromkeys(paths))[:40]


def _match_path(ref: str, files: List[str], suffix_index: Dict[str, List[str]]) -> List[str]:
    ref = ref.replace("\\", "/").lstrip("./")
    name = ref.rsplit("/", 1)[-1]
    cands = suffix_index.get(name, [])
    if "/" in ref:
        tail = "/".join(ref.split("/")[-3:])
        exact = [f for f in cands if f.endswith(tail)]
        if exact:
            return exact
        tail2 = "/".join(ref.split("/")[-2:])
        exact = [f for f in cands if f.endswith(tail2)]
        if exact:
            return exact
    return cands if len(cands) <= 3 else []


VENDOR_RE = re.compile(r"(^|/)(vendor|vendored|third_party|thirdparty|node_modules|static|dist|build|_vendor)(/|$)|\.min\.(js|css)$", re.I)


def specificity(name: str) -> float:
    """Plain lowercase words ('default', 'choices', 'save') name dozens of things; snake_case/CamelCase don't."""
    if "_" in name.strip("_") or re.search(r"[a-z][A-Z]", name) or re.match(r"[A-Z][a-z]+[A-Z]", name):
        return 1.0
    if name[:1].isupper():
        return 0.8  # a single capitalised word, e.g. a class name like Session
    return 0.25 if len(name) <= 12 else 0.6


def is_test_path(p: str) -> bool:
    low = p.lower()
    return bool(re.search(r"(^|/)(tests?|testing|spec|__tests__)(/|$)", low) or re.search(r"(^|/)(test_[^/]*|[^/]*_test\.\w+|[^/]*\.(test|spec)\.\w+)$", low))


class Localizer:
    def __init__(self, root: Path, files: List[str], index: SymbolIndex) -> None:
        self.root = root
        self.files = [f for f in files if Path(f).suffix in CODE_EXTS and not VENDOR_RE.search(f)]
        self.index = index
        self.suffix_index: Dict[str, List[str]] = defaultdict(list)
        for f in self.files:
            self.suffix_index[f.rsplit("/", 1)[-1]].append(f)

    def bm25(self, query_tokens: List[str], max_files: int = 12000) -> Dict[str, float]:
        q = Counter(query_tokens)
        if not q:
            return {}
        docs: Dict[str, Counter] = {}
        df: Counter = Counter()
        total_len = 0
        for rel in self.files[:max_files]:
            p = self.root / rel
            try:
                if p.stat().st_size > 400_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            toks = tokenize(rel.replace("/", " ") + " " + text)
            c = Counter(t for t in toks if t in q)
            total_len += len(toks)
            docs[rel] = c
            docs[rel]["__len__"] = len(toks)
            for t in c:
                if t != "__len__":
                    df[t] += 1
        n = len(docs) or 1
        avg = total_len / n if n else 1.0
        k1, b = 1.2, 0.75
        scores: Dict[str, float] = {}
        for rel, c in docs.items():
            dl = c["__len__"] or 1
            s = 0.0
            for t, qf in q.items():
                tf = c.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg)) * (1 + math.log(qf))
            if s > 0:
                scores[rel] = s
        return scores

    def localize(self, issue_text: str, top_k: int = 8) -> Tuple[List[Candidate], List[Candidate]]:
        strong, dotted, frames, paths = extract_identifiers(issue_text)
        cands: Dict[str, Candidate] = {}

        def bump(path: str, score: float, reason: str, sym: Optional[str] = None) -> None:
            c = cands.setdefault(path, Candidate(path))
            c.score += score
            if reason and reason not in c.reasons and len(c.reasons) < 4:
                c.reasons.append(reason)
            if sym and sym not in c.symbols and len(c.symbols) < 6:
                c.symbols.append(sym)

        for i, (fpath, line, func) in enumerate(frames):
            for rel in _match_path(fpath, self.files, self.suffix_index):
                last = i == len(frames) - 1
                bump(rel, 6.0 + (3.0 if last else 0.0), "in traceback", f"{func or 'frame'} (line {line})")
        for ref in paths:
            for rel in _match_path(ref, self.files, self.suffix_index):
                bump(rel, 5.0, "path mentioned in issue")
        for name in dotted:
            parts = name.split(".")
            # module path -> file
            for cut in range(len(parts), 0, -1):
                mod = "/".join(parts[:cut])
                hits = [f for f in self.files if f.endswith(mod + ".py") or f.endswith(mod + "/__init__.py")]
                if hits and len(hits) <= 3:
                    for h in hits:
                        bump(h, 3.0, f"module `{'.'.join(parts[:cut])}`")
                    break
            leaf = parts[-1]
            for s in self.index.lookup(".".join(parts[-2:]) if len(parts) > 1 else leaf, limit=6):
                if s.name == leaf and not VENDOR_RE.search(s.path):
                    bump(s.path, 3.5 * max(specificity(leaf), 0.5), f"defines `{s.qualname}`", f"{s.qualname} (line {s.line})")
        for name in strong:
            defs = [d for d in self.index.by_name.get(name, []) if not VENDOR_RE.search(d.path)]
            if not defs or len(defs) > 25:
                continue
            w = 4.0 * specificity(name) / math.sqrt(len(defs))
            for s in defs[:10]:
                bump(s.path, w, f"defines `{s.qualname}`", f"{s.qualname} (line {s.line})")

        lex = self.bm25(tokenize(issue_text))
        if lex:
            mx = max(lex.values())
            for rel, s in sorted(lex.items(), key=lambda kv: -kv[1])[:40]:
                bump(rel, 4.0 * s / mx, "", None)
                if rel in cands and not cands[rel].reasons:
                    cands[rel].reasons.append("text similarity")

        # Second hop: the tests that best match the issue import the code they exercise.
        ranked = sorted(cands.values(), key=lambda c: -c.score)
        test_cands = [c for c in ranked if is_test_path(c.path)][:3]
        if test_cands:
            top_test = test_cands[0].score or 1.0
            for tc in test_cands:
                imported = [m for m in self._imported_sources(tc.path) if not is_test_path(m) and not m.endswith("__init__.py")]
                test_dirs = {d for d in Path(tc.path).parent.parts if d not in ("tests", "test", "testing", "src")}
                test_stem = Path(tc.path).stem.replace("test_", "").replace("_test", "")
                for mod_file in imported:
                    # a test's specific imports (not package roots) are strong evidence of what it exercises;
                    # test layouts mirror the code (tests/migrations/test_writer.py <-> db/migrations/writer.py)
                    affinity = 1.0
                    mod_parts = set(Path(mod_file).parent.parts)
                    if test_dirs & mod_parts:
                        affinity += 0.6
                    if Path(mod_file).stem == test_stem:
                        affinity += 0.6
                    bump(mod_file, (2.0 + 2.0 * tc.score / top_test) * affinity,
                         f"imported by related test {tc.path.rsplit('/', 1)[-1]}")

        ranked = sorted(cands.values(), key=lambda c: -c.score)
        src = [c for c in ranked if not is_test_path(c.path)][:top_k]
        tests = [c for c in ranked if is_test_path(c.path)][:4]
        return src, tests

    def _imported_sources(self, test_rel: str) -> List[str]:
        """Repo source files imported by a test file (Python absolute imports, JS/TS relative imports)."""
        p = self.root / test_rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            return []
        out: List[str] = []
        fileset = set(self.files)
        if test_rel.endswith(".py"):
            mods = re.findall(r"^\s*from\s+([\w.]+)\s+import|^\s*import\s+([\w.]+)", text, re.M)
            for a, b in mods:
                mod = (a or b).strip(".")
                if not mod or mod.startswith(("os", "sys", "re", "unittest", "pytest", "typing")):
                    continue
                parts = mod.split(".")
                for cut in range(len(parts), 0, -1):
                    base = "/".join(parts[:cut])
                    for cand in (base + ".py", base + "/__init__.py", "src/" + base + ".py", "src/" + base + "/__init__.py"):
                        if cand in fileset:
                            out.append(cand)
                            break
                    else:
                        continue
                    break
        else:
            for spec in re.findall(r"""(?:require\(\s*|from\s+)['"](\.{1,2}/[^'"]+)['"]""", text):
                base = str((Path(test_rel).parent / spec)).replace("\\", "/")
                base = str(Path(base))  # normalise ../
                for ext in ("", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", "/index.js", "/index.ts"):
                    if base + ext in fileset:
                        out.append(base + ext)
                        break
        return list(dict.fromkeys(out))[:12]

    @staticmethod
    def render(src: List[Candidate], tests: List[Candidate]) -> str:
        if not src and not tests:
            return "(no strong signals; start with search/find_definition)"
        lines = []
        for i, c in enumerate(src, 1):
            why = "; ".join(c.reasons) or "text similarity"
            sym = (" - " + ", ".join(c.symbols[:4])) if c.symbols else ""
            lines.append(f"{i}. {c.path}  [{why}]{sym}")
        if tests:
            lines.append("Related tests: " + ", ".join(c.path for c in tests))
        return "\n".join(lines)
