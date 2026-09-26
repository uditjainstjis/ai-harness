"""Zero-token reproduction: pull runnable Python out of the issue text and execute it on the
original code before the model's first turn.

Handles fenced blocks (```python / ``` / ```py) and doctest-style sessions (>>> / ...).
A block is used only if it compiles; output-only blocks, tracebacks and shell sessions are
skipped. Snippets run with cwd=.pramana/ (side effects stay out of the repo), the repo on
PYTHONPATH, and a short timeout. The resulting traceback also feeds fault localization.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..tools.shell import format_result, run_command

FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)```", re.S)
SHELL_HINT = re.compile(r"^\s*(\$ |pip |python -m pip|conda |git |cd |ls |export |>>>\s*$)", re.M)


@dataclass
class Snippet:
    code: str
    source: str  # "fenced" | "doctest"


def _from_doctest(block: str) -> Optional[str]:
    lines = block.splitlines()
    if not any(l.lstrip().startswith(">>>") for l in lines):
        return None
    out = []
    for l in lines:
        s = l.lstrip()
        if s.startswith(">>> "):
            out.append(s[4:])
        elif s == ">>>":
            out.append("")
        elif s.startswith("... "):
            out.append(s[4:])
        elif s == "...":
            out.append("")
    # print bare expressions like the REPL would, so their values show up in the output
    fixed = []
    for l in out:
        if l and not l.startswith((" ", "\t")) and _is_bare_expr(l):
            fixed.append(f"print(repr({l}))")
        else:
            fixed.append(l)
    return "\n".join(fixed).strip() or None


def _is_bare_expr(line: str) -> bool:
    try:
        compile(line, "<l>", "eval")
    except SyntaxError:
        return False
    return not re.match(r"^\s*(print|import|from)\b", line)


def _compiles(code: str) -> bool:
    try:
        compile(code, "<snippet>", "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def extract(issue_text: str, limit: int = 2) -> List[Snippet]:
    found: List[Snippet] = []
    blocks = [(m.group(1).lower(), m.group(2)) for m in FENCE_RE.finditer(issue_text)]
    if not blocks and ">>>" in issue_text:
        blocks = [("", issue_text)]
    for lang, body in blocks:
        if lang and lang not in ("python", "py", "python3", "pycon", "ipython", ""):
            continue
        body = body.strip("\n")
        if not body.strip() or body.lstrip().startswith(("Traceback", "$ ", "pip ", "Error", "ERROR")):
            continue
        code = _from_doctest(body)
        source = "doctest" if code else "fenced"
        if code is None:
            if SHELL_HINT.search(body) and "import" not in body:
                continue
            code = body
        if len(code) > 6000 or not _compiles(code):
            continue
        # must look like it exercises code: an import, a call, or an assert
        if not re.search(r"\bimport\b|\w\(|assert\b", code):
            continue
        found.append(Snippet(code, source))
        if len(found) >= limit:
            break
    return found


def run_snippets(root: Path, scratch: Path, env: Dict[str, str], issue_text: str, timeout: int = 45) -> List[Dict[str, str]]:
    results = []
    for i, snip in enumerate(extract(issue_text), 1):
        name = f"issue_snippet_{i}.py"
        path = scratch / name
        path.write_text(snip.code + "\n")
        res = run_command(f"python {name}", scratch, timeout=timeout, env=env, max_chars=4000)
        results.append({"file": f".pramana/{name}", "code": snip.code, "output": format_result(res),
                        "exit_code": "timeout" if res.timed_out else str(res.exit_code)})
    return results


def render(results: List[Dict[str, str]]) -> str:
    if not results:
        return ""
    parts = ["\n<issue_code_run_by_harness>",
             "The harness extracted the code from the issue and ran it on the ORIGINAL code (cwd .pramana/):"]
    for r in results:
        parts.append(f"$ python {r['file']}\n{r['output']}")
    parts.append("Use this as a starting point for your reproduction (the file is already in .pramana/). It may "
                 "not show the bug by itself (e.g. it only prints values) - check it against the expected behaviour.")
    parts.append("</issue_code_run_by_harness>\n")
    return "\n".join(parts)
