"""Anthropic Messages API backend (native tool use + prompt caching)."""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from .base import ContextOverflow, LLMError, LLMResponse, ToolCall, ToolSpec, Usage

_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _clean_id(i: str) -> str:
    return _ID_RE.sub("_", i)[:64] or "call"


class AnthropicLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        temperature: Optional[float] = 0.0,
        max_output_tokens: int = 8192,
        timeout_s: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._drop_temperature = False
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=30.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    @staticmethod
    def _to_wire(messages: List[Dict[str, Any]]):
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []

        def push(role: str, blocks: List[Dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append(m.get("content") or "")
            elif role == "user":
                push("user", [{"type": "text", "text": m.get("content") or "(empty)"}])
            elif role == "tool":
                push(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": _clean_id(m["tool_call_id"]),
                            "content": m.get("content") or "(no output)",
                        }
                    ],
                )
            elif role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append(
                        {"type": "tool_use", "id": _clean_id(tc["id"]), "name": tc["name"], "input": tc.get("arguments") or {}}
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": "(no content)"})
                push("assistant", blocks)
        # tool_result blocks must precede text blocks inside a user turn
        for msg in out:
            if msg["role"] == "user":
                msg["content"].sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "Begin."}]})
        # cache breakpoint on the newest block: every turn re-reads the growing prefix from cache
        if out:
            out[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        system = [{"type": "text", "text": "\n\n".join(system_parts), "cache_control": {"type": "ephemeral"}}] if system_parts else None
        return system, out

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[ToolSpec]] = None, temperature: Optional[float] = None) -> LLMResponse:
        system, wire = self._to_wire(messages)
        payload: Dict[str, Any] = {"model": self.model, "max_tokens": self.max_output_tokens, "messages": wire}
        if system:
            payload["system"] = system
        temp = self.temperature if temperature is None else temperature
        if temp is not None and not self._drop_temperature:
            payload["temperature"] = temp
        if tools:
            payload["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        attempt = 0
        while True:
            t0 = time.time()
            try:
                r = self._client.post(f"{self.base_url}/v1/messages", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if attempt > 6:
                    raise LLMError(f"network error talking to Anthropic: {e}") from e
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            if r.status_code == 200:
                return self._parse(r.json(), time.time() - t0)
            body = r.text[:2000]
            low = body.lower()
            if r.status_code == 400:
                if "prompt is too long" in low or "context" in low and "token" in low:
                    raise ContextOverflow(body)
                if "temperature" in low and "temperature" in payload:
                    self._drop_temperature = True
                    payload.pop("temperature", None)
                    continue
                raise LLMError(f"HTTP 400 from Anthropic: {body}")
            if r.status_code in (401, 403, 404):
                raise LLMError(f"HTTP {r.status_code} from Anthropic: {body[:300]}")
            if r.status_code == 413:
                raise ContextOverflow(body)
            attempt += 1
            if attempt > 8:
                raise LLMError(f"HTTP {r.status_code} after retries: {body[:500]}")
            wait = min(90.0, (2 ** min(attempt, 6)) + random.random())
            ra = r.headers.get("retry-after")
            if ra:
                try:
                    wait = min(120.0, max(wait, float(ra)))
                except ValueError:
                    pass
            time.sleep(wait)

    @staticmethod
    def _parse(data: Dict[str, Any], latency: float) -> LLMResponse:
        texts: List[str] = []
        thinking: List[str] = []
        calls: List[ToolCall] = []
        for block in data.get("content", []):
            t = block.get("type")
            if t == "text":
                texts.append(block.get("text", ""))
            elif t == "thinking":
                thinking.append(block.get("thinking", ""))
            elif t == "tool_use":
                calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}, raw_arguments=json.dumps(block.get("input") or {}))
                )
        u = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cached_tokens=int(u.get("cache_read_input_tokens") or 0),
            calls=1,
        )
        return LLMResponse(
            text="\n".join(texts).strip(),
            tool_calls=calls,
            usage=usage,
            stop_reason=data.get("stop_reason") or "",
            latency_s=latency,
            reasoning="\n".join(thinking),
        )
