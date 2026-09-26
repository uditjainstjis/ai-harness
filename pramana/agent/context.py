"""Context-window management.

Strategy: the transcript is append-only (maximises provider prompt caching) until the prompt
crosses a threshold; then old tool observations are elided in ONE pass (head+tail kept, the
call itself stays so the model remembers what it did). The system prompt, the issue, and
every assistant message are never touched. A second, harsher pass exists for hard overflows.
"""
from __future__ import annotations

from typing import Any, Dict, List


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(str(tc.get("arguments") or "")) + 20
    return int(chars / 3.5) + 4 * len(messages)


def _elide(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 200:
        return text
    lines = text.count("\n")
    return (
        text[:head].rstrip()
        + f"\n[... older output elided by the context manager ({lines} lines). Re-run the tool if you need it again ...]\n"
        + text[-tail:].lstrip()
    )


def compact(messages: List[Dict[str, Any]], keep_recent: int = 8, head: int = 400, tail: int = 300) -> int:
    """Elide old tool outputs in place. Returns number of messages changed."""
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    old = tool_idx[:-keep_recent] if keep_recent else tool_idx
    changed = 0
    for i in old:
        c = messages[i].get("content") or ""
        if messages[i].get("_elided") or len(c) <= head + tail + 200:
            continue
        messages[i]["content"] = _elide(c, head, tail)
        messages[i]["_elided"] = True
        changed += 1
    # old harness nudges are noise once acted upon
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user" and m.get("_nudge")]
    for i in user_idx[:-2]:
        if not messages[i].get("_elided"):
            messages[i]["content"] = "[earlier harness note elided]"
            messages[i]["_elided"] = True
            changed += 1
    return changed


def compact_hard(messages: List[Dict[str, Any]], keep_recent: int = 4) -> int:
    changed = compact(messages, keep_recent=keep_recent, head=200, tail=150)
    # long assistant monologues (not tool calls) beyond the recent window
    asst = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    for i in asst[:-keep_recent]:
        c = messages[i].get("content") or ""
        if len(c) > 1500 and not messages[i].get("_elided_text"):
            messages[i]["content"] = c[:700] + "\n[... reasoning elided ...]\n" + c[-400:]
            messages[i]["_elided_text"] = True
            changed += 1
    return changed


def strip_private(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop harness-private keys (prefixed with _) before sending to a backend."""
    out = []
    for m in messages:
        if any(k.startswith("_") for k in m):
            m = {k: v for k, v in m.items() if not k.startswith("_")}
        out.append(m)
    return out
