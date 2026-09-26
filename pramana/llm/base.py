"""Provider-neutral message and response types shared by every model backend.

Internal message format (a plain dict, so trajectories serialise to JSON unchanged):

    {"role": "system",    "content": str}
    {"role": "user",      "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall.as_dict(), ...]}
    {"role": "tool",      "tool_call_id": str, "name": str, "content": str}

Each backend converts this to its own wire format and back.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    """A model call failed in a way retries could not fix."""


class ContextOverflow(LLMError):
    """The request exceeded the model's context window; caller should compact and retry."""


class ToolsUnsupported(LLMError):
    """The endpoint rejected native tool calling; caller should fall back to the text protocol."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON schema (type: object)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "name": self.name, "arguments": self.arguments}
        if self.parse_error:
            d["parse_error"] = self.parse_error
            d["raw_arguments"] = self.raw_arguments
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ToolCall":
        return ToolCall(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments") or {},
            raw_arguments=d.get("raw_arguments", ""),
            parse_error=d.get("parse_error"),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd
        self.calls += other.calls

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class LLMResponse:
    text: str
    tool_calls: List[ToolCall]
    usage: Usage
    stop_reason: str = ""
    latency_s: float = 0.0
    reasoning: str = ""

    def as_message(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.as_dict() for tc in self.tool_calls]
        return msg


def new_call_id(prefix: str = "call") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Tolerant JSON parsing for tool-call arguments. Weaker models emit trailing
# commas, raw newlines inside strings, or wrap the object in prose.
# ---------------------------------------------------------------------------

def _escape_raw_newlines_in_strings(s: str) -> str:
    out = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
                out.append(ch)
                continue
            if ch == "\\":
                esc = True
                out.append(ch)
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def parse_json_args(raw: Any) -> Dict[str, Any]:
    """Parse tool arguments; raises ValueError with a helpful message if hopeless."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    attempts = [s]
    # strip markdown fences
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    if fenced != s:
        attempts.append(fenced)
    # outermost object
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        attempts.append(s[i : j + 1])
    for cand in list(attempts):
        attempts.append(_escape_raw_newlines_in_strings(cand))
        attempts.append(re.sub(r",\s*([}\]])", r"\1", _escape_raw_newlines_in_strings(cand)))
    last_err = None
    for cand in attempts:
        try:
            val = json.loads(cand)
            if isinstance(val, dict):
                return val
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ValueError(f"could not parse tool arguments as a JSON object ({last_err})")
