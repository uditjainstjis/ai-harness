"""Text tool-calling protocol: used when a model/endpoint has no native tool calling,
and as a safety net when a model writes tool calls as text in native mode.

Canonical format (raw values, no JSON escaping, which weak models handle far better for code):

    <tool name="str_replace_editor">
    <command>str_replace</command>
    <path>src/pkg/mod.py</path>
    <old_str>    return a+b</old_str>
    <new_str>    return a + b</new_str>
    </tool>

Also accepted (common model habits): Hermes-style <tool_call>{"name":..,"arguments":..}</tool_call>,
Qwen-style <function=name><parameter=x>..</parameter></function>, and fenced JSON objects that
carry "name" + "arguments".
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import ToolCall, ToolSpec, new_call_id, parse_json_args

_TOOL_RE = re.compile(r"<tool\s+name\s*=\s*[\"']?([\w.\-]+)[\"']?\s*>(.*?)(?:</tool>|\Z)", re.S)
_PARAM_RE = re.compile(r"<([A-Za-z_][\w\-]*)>(.*?)</\1>", re.S)
_HERMES_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.S)
_QWEN_RE = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|\Z)", re.S)
_QWEN_PARAM_RE = re.compile(r"<parameter=([\w\-]+)>(.*?)(?:</parameter>|(?=<parameter=)|\Z)", re.S)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_INVOKE_RE = re.compile(r"<invoke\s+name\s*=\s*[\"']?([\w.\-]+)[\"']?\s*>(.*?)(?:</invoke>|\Z)", re.S)
_NAMED_PARAM_RE = re.compile(r"<parameter\s+name\s*=\s*[\"']?([\w\-]+)[\"']?\s*>(.*?)(?:</parameter>|(?=<parameter\s)|\Z)", re.S)
# a model writing its own tool results is hallucinating: everything from here on is discarded
HALLUCINATED_RESULT_RE = re.compile(r"<(result|tool_result|function_results|output|observation)\b[^>]*>|^=== USER ===", re.M)
MAX_TEXT_CALLS_PER_TURN = 5
TEXT_STOP_SEQUENCES = ["<tool_result", "<function_results", "<result>", "=== USER ==="]


def truncate_hallucination(text: str) -> str:
    """Keep the reply only up to the first self-written tool result (if a call precedes it)."""
    m = HALLUCINATED_RESULT_RE.search(text or "")
    if not m:
        return text
    head = text[: m.start()]
    if re.search(r"<tool\s+name|<invoke\s+name|<function=|<tool_call>", head):
        return head.rstrip()
    return text


def render_tools_prompt(tools: List[ToolSpec]) -> str:
    lines = [
        "# How to call tools",
        "You act only through tools. To call a tool, write a block in exactly this format:",
        "",
        '<tool name="TOOL_NAME">',
        "<PARAMETER_NAME>value</PARAMETER_NAME>",
        "</tool>",
        "",
        "Rules: values are raw text (no quotes, no escaping; multi-line is fine). Make ONE tool call per",
        "reply (you may batch a few independent read-only calls such as several views/searches). After your",
        "tool block(s), STOP your reply immediately: the harness runs the tools and sends the real results",
        "in the next message inside <tool_result> blocks. NEVER write <tool_result>/<result> blocks or",
        "guess what a tool returns; anything you write after your tool calls is discarded.",
        "",
        "# Available tools",
    ]
    for t in tools:
        lines.append(f"## {t.name}")
        lines.append(t.description.strip())
        props = t.parameters.get("properties", {})
        req = set(t.parameters.get("required", []))
        if props:
            lines.append("Parameters:")
            for pname, pspec in props.items():
                typ = pspec.get("type", "string")
                if "enum" in pspec:
                    typ += " one of " + "|".join(map(str, pspec["enum"]))
                desc = (pspec.get("description") or "").strip().replace("\n", " ")
                lines.append(f"- {pname} ({typ}{', required' if pname in req else ''}): {desc}")
        lines.append("")
    return "\n".join(lines)


def _strip_one_newline(v: str) -> str:
    if v.startswith("\r\n"):
        v = v[2:]
    elif v.startswith("\n"):
        v = v[1:]
    if v.endswith("\r\n"):
        v = v[:-2]
    elif v.endswith("\n"):
        v = v[:-1]
    return v


def _coerce(value: str, spec: Dict[str, Any]) -> Any:
    typ = spec.get("type")
    v = value.strip()
    try:
        if typ == "integer":
            return int(v)
        if typ == "number":
            return float(v)
        if typ == "boolean":
            return v.lower() in ("true", "1", "yes")
        if typ == "array":
            if v.startswith("["):
                return json.loads(v)
            # models often wrap each element in its own tag: <item>a</item><command>b</command>
            inner = re.findall(r"<([A-Za-z_][\w\-]*)>(.*?)</\1>", v, re.S)
            if inner:
                return [_strip_one_newline(x).strip() for _, x in inner if x.strip()]
            return [x for x in (s.strip() for s in v.splitlines()) if x]
        if typ == "object":
            return json.loads(v)
    except (ValueError, json.JSONDecodeError):
        return v
    return value


def _schema_for(name: str, tools: Optional[List[ToolSpec]]) -> Dict[str, Any]:
    for t in tools or []:
        if t.name == name:
            return t.parameters.get("properties", {})
    return {}


def parse_text_tool_calls(text: str, tools: Optional[List[ToolSpec]] = None) -> Tuple[str, List[ToolCall]]:
    """Return (prose_before_first_call, calls). Empty list if no call found."""
    if not text:
        return "", []
    calls: List[ToolCall] = []
    first_pos: Optional[int] = None

    for m in _TOOL_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        props = _schema_for(name, tools)
        args: Dict[str, Any] = {}
        for pm in _NAMED_PARAM_RE.finditer(body):
            key, val = pm.group(1), _strip_one_newline(pm.group(2))
            args[key] = _coerce(val, props.get(key, {})) if key in props else val
        for pm in _PARAM_RE.finditer(body):
            key, val = pm.group(1), _strip_one_newline(pm.group(2))
            if key == "parameter" or key in args:
                continue
            args[key] = _coerce(val, props.get(key, {})) if key in props else val
        if not args and body.strip().startswith("{"):
            try:
                args = parse_json_args(body)
            except ValueError:
                pass
        calls.append(ToolCall(id=new_call_id("txt"), name=name, arguments=args, raw_arguments=body))
        first_pos = m.start() if first_pos is None else first_pos

    if not calls:
        for m in _INVOKE_RE.finditer(text):
            name, body = m.group(1), m.group(2)
            props = _schema_for(name, tools)
            args = {}
            for pm in _NAMED_PARAM_RE.finditer(body):
                key, val = pm.group(1), _strip_one_newline(pm.group(2))
                args[key] = _coerce(val, props.get(key, {})) if key in props else val
            calls.append(ToolCall(id=new_call_id("txt"), name=name, arguments=args, raw_arguments=body))
            first_pos = m.start() if first_pos is None else first_pos

    if not calls:
        for m in _QWEN_RE.finditer(text):
            name, body = m.group(1), m.group(2)
            props = _schema_for(name, tools)
            args = {}
            for pm in _QWEN_PARAM_RE.finditer(body):
                key, val = pm.group(1), _strip_one_newline(pm.group(2))
                args[key] = _coerce(val, props.get(key, {})) if key in props else val
            calls.append(ToolCall(id=new_call_id("txt"), name=name, arguments=args, raw_arguments=body))
            first_pos = m.start() if first_pos is None else first_pos

    if not calls:
        known = {t.name for t in tools or []}
        for rx in (_HERMES_RE, _FENCED_JSON_RE):
            for m in rx.finditer(text):
                try:
                    obj = parse_json_args(m.group(1))
                except ValueError:
                    continue
                name = obj.get("name") or obj.get("tool") or ""
                if known and name not in known:
                    continue
                args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
                if isinstance(args, str):
                    try:
                        args = parse_json_args(args)
                    except ValueError:
                        args = {}
                calls.append(ToolCall(id=new_call_id("txt"), name=name, arguments=args or {}, raw_arguments=m.group(1)))
                first_pos = m.start() if first_pos is None else first_pos
            if calls:
                break

    prose = text[:first_pos].strip() if first_pos is not None else text.strip()
    return prose, calls


def render_call(tc: Dict[str, Any]) -> str:
    parts = [f'<tool name="{tc["name"]}">']
    for k, v in (tc.get("arguments") or {}).items():
        if not isinstance(v, str):
            v = json.dumps(v)
        parts.append(f"<{k}>{v}</{k}>")
    parts.append("</tool>")
    return "\n".join(parts)


def to_text_messages(messages: List[Dict[str, Any]], tools: List[ToolSpec]) -> List[Dict[str, Any]]:
    """Flatten tool calls/results into plain user/assistant text for a tool-less endpoint."""
    out: List[Dict[str, Any]] = []
    tools_prompt = render_tools_prompt(tools) if tools else ""

    def push(role: str, content: str) -> None:
        if out and out[-1]["role"] == role and role != "system":
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})

    if tools_prompt and not any(m["role"] == "system" for m in messages):
        out.append({"role": "system", "content": tools_prompt})  # never drop the tool contract

    for m in messages:
        role = m["role"]
        if role == "system":
            push("system", (m.get("content") or "") + ("\n\n" + tools_prompt if tools_prompt else ""))
        elif role == "assistant":
            content = m.get("content") or ""
            tcs = m.get("tool_calls") or []
            if tcs and "<tool name=" not in content:
                content = (content + "\n\n" if content else "") + "\n\n".join(render_call(tc) for tc in tcs)
            push("assistant", content or "(no content)")
        elif role == "tool":
            push("user", f'<tool_result name="{m.get("name", "")}">\n{m.get("content") or "(no output)"}\n</tool_result>')
        else:
            push("user", m.get("content") or "")
    return out
