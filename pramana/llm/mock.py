"""Scripted backend for offline tests: replays responses from PRAMANA_MOCK_SCRIPT (JSON list)
or from a list handed to the constructor. Each item: {"text": str, "tool_calls": [{"name":..,"arguments":{..}}]}."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .base import LLMResponse, ToolCall, ToolSpec, Usage, new_call_id


class MockLLM:
    def __init__(self, script: Optional[List[Dict[str, Any]]] = None) -> None:
        if script is None:
            path = os.environ.get("PRAMANA_MOCK_SCRIPT")
            script = json.load(open(path)) if path else []
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools: Optional[List[ToolSpec]] = None, temperature=None) -> LLMResponse:
        self.calls += 1
        if not self.script:
            item: Dict[str, Any] = {"text": "", "tool_calls": [{"name": "submit", "arguments": {"summary": "mock: out of script"}}]}
        else:
            item = self.script.pop(0)
        calls = [ToolCall(id=new_call_id(), name=c["name"], arguments=c.get("arguments", {})) for c in item.get("tool_calls", [])]
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        return LLMResponse(text=item.get("text", ""), tool_calls=calls, stop_reason=item.get("stop_reason", ""),
                           usage=Usage(input_tokens=chars // 4, output_tokens=50, calls=1))
