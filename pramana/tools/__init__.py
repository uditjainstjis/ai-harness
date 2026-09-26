"""The agent-computer interface: tool schemas + dispatch."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..llm.base import ToolCall, ToolSpec
from ..repo.symbols import SymbolIndex
from .editor import EditError, Editor
from .search import find_files, search
from .shell import build_env, check_denylist, ensure_python_shim, format_result, run_command

TOOL_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="bash",
        description=(
            "Run a shell command (bash) in the repository root and return exit code + output. Use it to run "
            "scripts and tests, inspect git history, list files, or install a missing dependency. Stdin is closed "
            "(no interactive programs). Long output is truncated in the middle; narrow it with grep/head/tail or "
            "test selectors (e.g. pytest path::test -x -q)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 180, max 900)."},
            },
            "required": ["command"],
        },
    ),
    ToolSpec(
        name="str_replace_editor",
        description=(
            "View and edit files.\n"
            "* view: show a file with line numbers (long files show an outline + the first lines; pass view_range "
            "[start, end] for a region, end=-1 for EOF) or list a directory.\n"
            "* str_replace: replace old_str with new_str in path. old_str must match the file exactly and uniquely "
            "(include 2-3 lines of context); copy it from the file WITHOUT the line-number column. Edits that would "
            "make the file unparseable are rejected with the syntax error.\n"
            "* insert: insert new_str after line insert_line (0 = top of file).\n"
            "* create: create a NEW file with file_text (never overwrites).\n"
            "* undo_edit: revert the last edit to path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["view", "str_replace", "insert", "create", "undo_edit"], "description": "Operation to perform."},
                "path": {"type": "string", "description": "File or directory path (relative to the repository root, or absolute)."},
                "old_str": {"type": "string", "description": "str_replace: exact existing text to replace."},
                "new_str": {"type": "string", "description": "str_replace/insert: the new text."},
                "file_text": {"type": "string", "description": "create: full content of the new file."},
                "insert_line": {"type": "integer", "description": "insert: line number after which to insert."},
                "view_range": {"type": "array", "items": {"type": "integer"}, "description": "view: [start_line, end_line]."},
                "replace_all": {"type": "boolean", "description": "str_replace: replace every occurrence instead of requiring a unique match."},
            },
            "required": ["command", "path"],
        },
    ),
    ToolSpec(
        name="search",
        description=(
            "Search file contents across the repository (ripgrep). Returns matching lines grouped by file, source "
            "files first. pattern is a regex (falls back to literal if invalid); set literal=true to search exact "
            "text containing regex characters like ( [ . *."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex or literal text to find."},
                "path": {"type": "string", "description": "Optional file or directory to limit the search."},
                "glob": {"type": "string", "description": "Optional file glob filter, e.g. '*.py' or '!**/tests/**'."},
                "literal": {"type": "boolean", "description": "Treat pattern as exact text."},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search."},
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="find_definition",
        description=(
            "Locate where a class/function/method/constant is defined. Accepts a bare name ('parse_url'), a "
            "qualified name ('Session.send') or a dotted path ('requests.sessions.Session'). Returns file:line "
            "and signatures."
        ),
        parameters={
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Name to look up."}},
            "required": ["symbol"],
        },
    ),
    ToolSpec(
        name="find_files",
        description="List repository files whose name or path matches a glob, e.g. '*config*.py' or 'tests/**/test_*url*'.",
        parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Glob pattern."}},
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="compare",
        description=(
            "Run a command on the ORIGINAL code and on your CURRENT code and compare (your changes are "
            "temporarily reverted for the first run, then restored). Use it whenever a test fails, to see whether "
            "the failure is caused by your change or already existed; and to show your reproduction goes from "
            "failing to passing. Pre-existing failures unrelated to the issue are not yours to fix."
        ),
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run in both states (keep it targeted)."}},
            "required": ["command"],
        },
    ),
    ToolSpec(
        name="submit",
        description=(
            "Finish the task. The harness then VERIFIES your claims: it runs each verification command on the "
            "original code and on your patched code. A reproduction that fails before and passes after is proof "
            "of the fix; any command that passed before but fails after is a regression and your submission is "
            "returned to you. Provide: (1) your reproduction command, (2) the most relevant existing test "
            "command(s)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Root cause and what you changed, 2-6 sentences."},
                "verification_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell commands (run from the repo root) that demonstrate the fix, e.g. 'python .pramana/repro.py', 'python -m pytest tests/test_x.py -q'.",
                },
            },
            "required": ["summary", "verification_commands"],
        },
    ),
]

TOOL_NAMES = {t.name for t in TOOL_SPECS}
FULL_SUITE_RE = re.compile(r"^(python3? -m )?(pytest|py\.test)(\s+(-q|-qq|-x|-v|-vv|-rA|-ra|-s|--tb=\w+))*\s*$|^(npm|yarn|pnpm) (run )?test\s*$|^go test \./\.\.\.\s*$|^cargo test\s*$")
ALIASES = {
    "execute_bash": "bash", "run": "bash", "shell": "bash", "run_command": "bash", "terminal": "bash",
    "editor": "str_replace_editor", "edit": "str_replace_editor", "str_replace_based_edit_tool": "str_replace_editor",
    "text_editor": "str_replace_editor", "view": "str_replace_editor", "read_file": "str_replace_editor",
    "grep": "search", "search_code": "search", "rg": "search",
    "finish": "submit", "done": "submit", "complete": "submit",
    "find_symbol": "find_definition", "goto_definition": "find_definition",
    "glob": "find_files",
    "diff_run": "compare", "compare_runs": "compare", "run_on_original": "compare",
}


EDITOR_SUBCOMMANDS = {
    "create": "create", "create_file": "create", "write_file": "create", "new_file": "create", "write": "create",
    "str_replace": "str_replace", "replace": "str_replace", "edit_file": "str_replace", "replace_in_file": "str_replace",
    "apply_edit": "str_replace", "insert": "insert", "insert_lines": "insert", "undo_edit": "undo_edit", "undo": "undo_edit",
    "view": "view", "read_file": "view", "open_file": "view", "view_file": "view", "cat": "view",
}
NAMESPACE_PREFIXES = ("functions.", "function.", "tools.", "tool.", "tool:", "default_api.")


def _infer_from_args(args: Dict[str, Any]) -> Optional[str]:
    """Unknown tool name: infer the intended tool from the argument shape."""
    keys = set(args)
    if "old_str" in keys or "new_str" in keys or "file_text" in keys or ({"path", "content"} <= keys):
        return "str_replace_editor"
    if "command" in keys or "cmd" in keys:
        return "bash"
    if "pattern" in keys or "query" in keys or "regex" in keys:
        return "search"
    if "symbol" in keys:
        return "find_definition"
    if "verification_commands" in keys or "summary" in keys:
        return "submit"
    if keys and keys <= {"path", "file_path", "view_range", "start_line", "end_line"}:
        return "str_replace_editor"
    return None


RANGE_START_KEYS = ("line_start", "start_line", "start", "from_line", "line_from", "begin", "first_line", "lineno", "line")
RANGE_END_KEYS = ("line_end", "end_line", "end", "to_line", "line_to", "last_line", "stop")


def _extract_range(args: Dict[str, Any]) -> Optional[List[int]]:
    """Pull a line range out of whatever spelling the model used, removing the consumed keys."""
    def as_int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None
    for key in ("lines", "line_range", "range"):
        if key in args:
            nums = [int(x) for x in re.findall(r"(?<!\d)-?\d+", str(args.pop(key)))]
            if nums:
                return nums[:2] if len(nums) > 1 else [nums[0], nums[0] + 100]
    if "offset" in args or "limit" in args:  # Claude-Code style: offset (0/1-based) + limit
        off = as_int(args.pop("offset", 1)) or 1
        lim = as_int(args.pop("limit", 200)) or 200
        return [max(1, off), max(1, off) + lim - 1]
    start = next((as_int(args.pop(k)) for k in RANGE_START_KEYS if k in args), None)
    end = next((as_int(args.pop(k)) for k in RANGE_END_KEYS if k in args), None)
    if start is not None:
        return [start, end if end is not None else start + 150]
    if end is not None:
        return [max(1, end - 150), end]
    return None


def canonicalize(call: ToolCall) -> Optional[ToolCall]:
    """Map alias tool names/argument spellings onto the declared schema, so the transcript only
    ever contains declared tools (some providers 500 on histories with undeclared tool names).
    Returns None for a call we cannot map."""
    raw_name = (call.name or "").strip()
    base = raw_name
    for prefix in NAMESPACE_PREFIXES:  # e.g. "functions.bash"; built-ins like "browser.open" stay unknown
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    sub = EDITOR_SUBCOMMANDS.get(base)
    name = ALIASES.get(base, base)
    if sub and base not in TOOL_NAMES:
        name = "str_replace_editor"
    if call.parse_error:
        return ToolCall(call.id, name, {}, call.raw_arguments, call.parse_error) if name in TOOL_NAMES else None
    args = dict(call.arguments or {})
    if name not in TOOL_NAMES:
        name = _infer_from_args(args) or ""
        if name not in TOOL_NAMES:
            return None
    for alt in ("file_path", "file", "filename"):
        if alt in args and "path" not in args:
            args["path"] = args.pop(alt)
    if name in ("bash", "compare"):
        for alt in ("cmd", "script", "code"):
            if alt in args and "command" not in args:
                args["command"] = args.pop(alt)
    elif name == "search":
        for alt in ("query", "regex", "text"):
            if alt in args and "pattern" not in args:
                args["pattern"] = args.pop(alt)
    elif name == "find_definition":
        for alt in ("name", "query"):
            if alt in args and "symbol" not in args:
                args["symbol"] = args.pop(alt)
    if name == "str_replace_editor" and sub and not args.get("command"):
        args["command"] = sub
    if name == "str_replace_editor" and not args.get("command"):
        if raw_name in ("view", "read_file"):
            args["command"] = "view"
        elif args.get("old_str") is not None:
            args["command"] = "str_replace"
        elif args.get("file_text") is not None or args.get("content") is not None:
            args["command"] = "create"
        elif args.get("insert_line") is not None:
            args["command"] = "insert"
        else:
            args["command"] = "view"
    for alt in ("content", "text"):
        if name == "str_replace_editor" and args.get("command") == "create" and alt in args and "file_text" not in args:
            args["file_text"] = args.pop(alt)
    if name == "str_replace_editor" and "view_range" not in args:
        vr = _extract_range(args)
        if vr:
            args["view_range"] = vr
    if name == "str_replace_editor" and isinstance(args.get("view_range"), str):
        nums = [int(x) for x in re.findall(r"(?<!\d)-?\d+", args["view_range"])]
        if nums:
            args["view_range"] = nums[:2] if len(nums) > 1 else [nums[0], nums[0] + 100]
    return ToolCall(call.id, name, args, json.dumps(args))


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


class Toolbox:
    def __init__(self, root: Path, scratch: Path, index: SymbolIndex, command_timeout: int = 180, git=None) -> None:
        self.git = git
        self.root = root
        self.scratch = scratch
        self.index = index
        self.editor = Editor(root, scratch, index)
        self.command_timeout = command_timeout
        self.env = ensure_python_shim(build_env(root, extra={"PRAMANA_SCRATCH": str(scratch)}), scratch / ".bin")
        self.commands: List[Dict[str, Any]] = []
        self.on_submit: Optional[Callable[[Dict[str, Any]], ToolResult]] = None

    def specs(self) -> List[ToolSpec]:
        return TOOL_SPECS

    def execute(self, call: ToolCall) -> ToolResult:
        canon = canonicalize(call)
        if canon is not None:
            call = canon
        name = ALIASES.get(call.name, call.name)
        args = call.arguments or {}
        if call.parse_error:
            return ToolResult(
                f"Your call to `{call.name}` had malformed arguments ({call.parse_error}). Raw arguments were:\n"
                f"{call.raw_arguments[:1500]}\nResend the call with a valid JSON object for the arguments.",
                is_error=True,
            )
        if name not in TOOL_NAMES:
            return ToolResult(f"Unknown tool `{call.name}`. Available tools: {', '.join(sorted(TOOL_NAMES))}.", is_error=True)
        # model habit: calling view/read_file directly
        if call.name in ("view", "read_file") and "command" not in args:
            args = {"command": "view", **args}
            if "file_path" in args and "path" not in args:
                args["path"] = args.pop("file_path")
        spec = next((t for t in TOOL_SPECS if t.name == name), None)
        allowed = set((spec.parameters.get("properties") or {}).keys()) if spec else set()
        tolerated = {"file_path", "cmd", "query", "content", "file", "text", "name"}
        unknown = sorted(k for k in args if k not in allowed and k not in tolerated)
        try:
            res = getattr(self, f"_t_{name}")(args)
        except EditError as e:
            res = ToolResult(str(e), is_error=True)
        except Exception as e:  # noqa: BLE001 - a tool bug must never crash the run
            res = ToolResult(f"Tool `{name}` failed internally: {type(e).__name__}: {e}", is_error=True)
        if unknown:  # never ignore an argument silently: the model would keep retrying it
            res.output += (f"\n[harness note] Ignored unknown argument(s) for `{name}`: {', '.join(unknown)}. "
                           f"Valid arguments: {', '.join(sorted(allowed))}.")
        return res

    # ------------------------------------------------------------------ tools
    def _t_bash(self, a: Dict[str, Any]) -> ToolResult:
        cmd = a.get("command") or a.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        if not str(cmd).strip():
            return ToolResult("command is empty", is_error=True)
        why = check_denylist(cmd)
        if why:
            return ToolResult(f"Command blocked: {why}.", is_error=True)
        try:
            timeout = int(a.get("timeout") or self.command_timeout)
        except (TypeError, ValueError):
            timeout = self.command_timeout
        timeout = max(5, min(timeout, 900))
        res = run_command(cmd, self.root, timeout=timeout, env=self.env)
        self.commands.append({"command": cmd, "exit_code": res.exit_code, "timed_out": res.timed_out, "duration_s": round(res.duration_s, 2)})
        out = format_result(res)
        if not res.ok and FULL_SUITE_RE.match(cmd.strip()):
            out += ("\n\n[harness note] You ran the ENTIRE test suite. Failures here are often pre-existing or "
                    "environment-specific. Before acting on one, run that specific test with `compare` to see if it "
                    "also fails on the original code; only regressions you caused need fixing.")
        return ToolResult(out, is_error=not res.ok, meta={"exit_code": res.exit_code, "timed_out": res.timed_out})

    def _t_str_replace_editor(self, a: Dict[str, Any]) -> ToolResult:
        cmd = (a.get("command") or "").strip()
        path = a.get("path") or a.get("file_path") or a.get("file") or ""
        ed = self.editor
        if not cmd:  # infer the obvious intent instead of bouncing the call
            if a.get("old_str") is not None:
                cmd = "str_replace"
            elif a.get("file_text") is not None:
                cmd = "create"
            elif a.get("insert_line") is not None:
                cmd = "insert"
            else:
                cmd = "view"
        if cmd == "view":
            vr = a.get("view_range")
            if isinstance(vr, str):
                try:
                    vr = json.loads(vr)
                except ValueError:
                    vr = [int(x) for x in vr.replace(",", " ").split() if x.lstrip("-").isdigit()]
            return ToolResult(ed.view(path, vr))
        if cmd == "create":
            text = a.get("file_text")
            if text is None:
                text = a.get("content", a.get("new_str"))
            out = ed.create(path, text if text is not None else "")
            return ToolResult(out, meta={"edited": ed.rel(ed.resolve(path))})
        if cmd == "str_replace":
            out = ed.str_replace(path, a.get("old_str", ""), a.get("new_str", ""), bool(a.get("replace_all")))
            return ToolResult(out, meta={"edited": ed.rel(ed.resolve(path))})
        if cmd == "insert":
            out = ed.insert(path, a.get("insert_line", 0), a.get("new_str", a.get("text", "")))
            return ToolResult(out, meta={"edited": ed.rel(ed.resolve(path))})
        if cmd == "undo_edit":
            return ToolResult(ed.undo_edit(path), meta={"edited": ed.rel(ed.resolve(path))})
        return ToolResult(f"Unknown command {cmd!r}; use one of view, str_replace, insert, create, undo_edit.", is_error=True)

    def _t_search(self, a: Dict[str, Any]) -> ToolResult:
        out = search(self.root, a.get("pattern") or a.get("query") or "", a.get("path"), a.get("glob"),
                     bool(a.get("literal")), bool(a.get("ignore_case")))
        return ToolResult(out, is_error=out.startswith("error:"))

    def _t_find_definition(self, a: Dict[str, Any]) -> ToolResult:
        sym = a.get("symbol") or a.get("name") or ""
        hits = self.index.lookup(sym)
        if not hits:
            return ToolResult(f"No definition found for {sym!r}. Try `search` with a regex like 'def {sym.split('.')[-1]}|class {sym.split('.')[-1]}'.")
        return ToolResult(f"{len(hits)} definition(s) for {sym!r}:\n" + "\n".join(h.render() for h in hits))

    def _t_find_files(self, a: Dict[str, Any]) -> ToolResult:
        return ToolResult(find_files(self.root, a.get("pattern") or "*"))

    def _t_compare(self, a: Dict[str, Any]) -> ToolResult:
        cmd = str(a.get("command") or "").strip()
        if not cmd:
            return ToolResult("command is empty", is_error=True)
        why = check_denylist(cmd)
        if why:
            return ToolResult(f"Command blocked: {why}.", is_error=True)
        if self.git is None:
            return self._t_bash({"command": cmd})
        from ..agent.verify import summarize_output

        timeout = max(5, min(int(a.get("timeout") or self.command_timeout), 900))
        mine = run_command(cmd, self.root, timeout=timeout, env=dict(self.env, PYTHONDONTWRITEBYTECODE="1"))
        if not self.git.patch().strip():
            return ToolResult("(You have not changed anything yet, so both states are identical.)\n" + format_result(mine),
                              is_error=not mine.ok)
        try:
            with self.git.baseline():
                orig = run_command(cmd, self.root, timeout=timeout, env=dict(self.env, PYTHONDONTWRITEBYTECODE="1"))
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Could not run on the original code ({e}).\n" + format_result(mine), is_error=True)
        o_ok, m_ok = orig.ok, mine.ok
        if not o_ok and m_ok:
            verdict = "FIXED by your change: fails on the original code, passes on yours."
        elif o_ok and not m_ok:
            verdict = "REGRESSION: passes on the original code but FAILS with your change. Your change caused this."
        elif o_ok and m_ok:
            verdict = "Passes on both (your change does not affect this result)."
        else:
            verdict = ("Fails on BOTH: this failure already existed before your change. If it is unrelated to the "
                       "issue, ignore it - do not try to fix pre-existing failures.")
        body = (
            f"ORIGINAL code: {summarize_output(orig.output, orig.exit_code, orig.timed_out)}\n"
            f"YOUR code:     {summarize_output(mine.output, mine.exit_code, mine.timed_out)}\n=> {verdict}\n\n"
            f"Output with your code (tail):\n" + "\n".join(mine.output.rstrip().splitlines()[-30:])
        )
        if o_ok != m_ok or (not o_ok and not m_ok):
            body += "\n\nOutput with the original code (tail):\n" + "\n".join(orig.output.rstrip().splitlines()[-15:])
        return ToolResult(body, is_error=False, meta={"compare": verdict.split(":")[0]})

    def _t_submit(self, a: Dict[str, Any]) -> ToolResult:
        cmds = a.get("verification_commands") or a.get("commands") or []
        if isinstance(cmds, str):
            try:
                parsed = json.loads(cmds)
                cmds = parsed if isinstance(parsed, list) else [cmds]
            except ValueError:
                cmds = [c for c in cmds.splitlines() if c.strip()]
        cleaned = []
        for c in cmds:
            c = str(c).strip()
            m = re.fullmatch(r"<([A-Za-z_][\w\-]*)>(.*)</\1>", c, re.S)  # stray wrapper tag from text-mode models
            if m:
                c = m.group(2).strip()
            if c.startswith("$ "):
                c = c[2:]
            if c:
                cleaned.append(c)
        payload = {"summary": a.get("summary") or a.get("message") or "", "verification_commands": cleaned}
        if self.on_submit is None:
            return ToolResult("Submitted.", meta={"submitted": True, **payload})
        return self.on_submit(payload)
