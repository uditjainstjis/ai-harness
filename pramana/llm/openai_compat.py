"""OpenAI-compatible Chat Completions backend.

Covers OpenAI, Gemini (OpenAI endpoint), OpenRouter, Groq, xAI, DeepSeek, Mistral,
Together, Fireworks, Cerebras, NVIDIA, Hugging Face router, Ollama, vLLM, ...

Robustness features:
* parameter negotiation - if an endpoint rejects a parameter (temperature on
  reasoning models, max_tokens vs max_completion_tokens, parallel_tool_calls,
  reasoning_effort) we drop/rename it and retry, and remember for the session;
* retries with exponential backoff and Retry-After on 429/5xx/timeouts;
* context-overflow and tools-unsupported errors are surfaced as typed exceptions.
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

import httpx

from .base import (
    ContextOverflow,
    LLMError,
    LLMResponse,
    ToolCall,
    ToolSpec,
    ToolsUnsupported,
    Usage,
    new_call_id,
    parse_json_args,
)

_OVERFLOW_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "input is too long",
    "context window",
    "exceeds the limit",
    "request too large",
)
_TOOLS_UNSUPPORTED_HINTS = (
    "does not support tools",
    "tools is not supported",
    "tool use is not supported",
    "function calling is not",
    "does not support function",
    "tools are not supported",
    "not support tool",
)


def _debug_dump(payload: Dict[str, Any]) -> None:
    d = os.environ.get("PRAMANA_DEBUG_DIR")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"req-{int(time.time() * 1000)}.json"), "w") as fh:
            json.dump(payload, fh, indent=1)
    except OSError:
        pass


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: Optional[float] = 0.0,
        max_output_tokens: int = 8192,
        reasoning_effort: str = "",
        timeout_s: float = 300.0,
        provider: str = "openai",
        seed: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.provider = provider
        self.seed = seed
        self._dropped: set = set()
        self._use_max_completion = model.startswith(("o1", "o3", "o4", "gpt-5")) and provider == "openai"
        # reasoning models trained to see their own earlier analysis inside a tool-calling loop
        self.reasoning_window = 6 if "gpt-oss" in model.lower() else 0
        self.text_mode_stops: List[str] = []  # set by ChatModel when it drives this backend in text mode
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/pramana-harness"
            headers["X-Title"] = "Pramana coding harness"
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=30.0), headers=headers)

    # ------------------------------------------------------------------ wire format
    @staticmethod
    def _to_wire(messages: List[Dict[str, Any]], tool_names: bool = True, reasoning_window: int = 0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        asst_idx = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
        keep_reasoning = set(asst_idx[-reasoning_window:]) if reasoning_window > 0 else set()
        for i, m in enumerate(messages):
            role = m["role"]
            if role == "assistant":
                wm: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if i in keep_reasoning and m.get("reasoning"):
                    wm["reasoning"] = m["reasoning"]
                tcs = m.get("tool_calls") or []
                if tcs:
                    wm["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc.get("raw_arguments") if tc.get("parse_error") else json.dumps(tc.get("arguments") or {}),
                            },
                        }
                        for tc in tcs
                    ]
                    if not wm["content"]:
                        wm["content"] = None
                out.append(wm)
            elif role == "tool":
                tm = {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content") or "(no output)"}
                if tool_names and m.get("name"):
                    tm["name"] = m["name"]  # Gemini/Groq-style endpoints map results by function name
                out.append(tm)
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    @staticmethod
    def _tools_to_wire(tools: List[ToolSpec]) -> List[Dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

    # ------------------------------------------------------------------ request
    def _payload(self, messages, tools, temperature) -> Dict[str, Any]:
        p: Dict[str, Any] = {"model": self.model, "messages": self._to_wire(
            messages, tool_names=self.provider != "openai", reasoning_window=self.reasoning_window)}
        if tools:
            p["tools"] = self._tools_to_wire(tools)
            if "parallel_tool_calls" not in self._dropped and self.provider in ("openai", "openrouter", "groq", "together", "fireworks"):
                p["parallel_tool_calls"] = True
        temp = self.temperature if temperature is None else temperature
        if temp is not None and "temperature" not in self._dropped:
            p["temperature"] = temp
        if self.max_output_tokens:
            if self._use_max_completion or "max_tokens" in self._dropped:
                if "max_completion_tokens" not in self._dropped:
                    p["max_completion_tokens"] = self.max_output_tokens
            else:
                p["max_tokens"] = self.max_output_tokens
        if self.reasoning_effort and "reasoning_effort" not in self._dropped:
            p["reasoning_effort"] = self.reasoning_effort
        if self.seed is not None and "seed" not in self._dropped and self.provider in ("openai", "ollama", "together", "fireworks"):
            p["seed"] = self.seed
        if self.provider == "openrouter":
            p["usage"] = {"include": True}
        if not tools and self.text_mode_stops and "stop" not in self._dropped:
            p["stop"] = self.text_mode_stops
        return p

    def _negotiate(self, err_text: str, payload: Dict[str, Any]) -> bool:
        """Drop/rename a parameter the endpoint complained about. True if we changed something."""
        low = err_text.lower()
        changed = False
        if "reasoning" in low and self.reasoning_window and "reasoning_effort" not in low:
            self.reasoning_window = 0  # endpoint rejects passed-back reasoning: stop sending it
            changed = True
        for param in ("temperature", "parallel_tool_calls", "reasoning_effort", "seed", "top_p", "stop"):
            if param in low and param in payload and param not in self._dropped:
                self._dropped.add(param)
                changed = True
        if "max_tokens" in low and "max_completion_tokens" in low and "max_tokens" in payload:
            self._dropped.add("max_tokens")
            changed = True
        elif "max_completion_tokens" in low and "max_completion_tokens" in payload:
            self._dropped.add("max_completion_tokens")
            changed = True
        elif "max_tokens" in low and "max_tokens" in payload and ("unsupported" in low or "not supported" in low):
            self._dropped.add("max_tokens")
            changed = True
        return changed

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[ToolSpec]] = None, temperature: Optional[float] = None) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        negotiations = 0
        while True:
            payload = self._payload(messages, tools, temperature)
            _debug_dump(payload)
            t0 = time.time()
            try:
                r = self._client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if attempt > 6:
                    raise LLMError(f"network error talking to {self.base_url}: {e}") from e
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            latency = time.time() - t0
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    attempt += 1
                    if attempt > 6:
                        raise LLMError(f"non-JSON response: {r.text[:300]}")
                    time.sleep(min(30, 2 ** attempt))
                    continue
                if "choices" not in data or not data["choices"]:
                    err = json.dumps(data.get("error") or data)[:500]
                    low = err.lower()
                    if any(h in low for h in _OVERFLOW_HINTS):
                        raise ContextOverflow(err)
                    attempt += 1
                    if attempt > 6:
                        raise LLMError(f"empty choices from model: {err}")
                    time.sleep(min(30, 2 ** attempt))
                    continue
                return self._parse(data, latency)
            body = r.text[:2000]
            low = body.lower()
            if r.status_code in (400, 404, 422):
                if any(h in low for h in _OVERFLOW_HINTS):
                    raise ContextOverflow(body)
                if tools and any(h in low for h in _TOOLS_UNSUPPORTED_HINTS):
                    raise ToolsUnsupported(body)
                if negotiations < 6 and self._negotiate(body, payload):
                    negotiations += 1
                    continue
                raise LLMError(f"HTTP {r.status_code} from {self.base_url}: {body}")
            if r.status_code in (401, 403):
                raise LLMError(f"authentication failed (HTTP {r.status_code}) at {self.base_url}: {body[:300]}")
            if r.status_code == 413:
                raise ContextOverflow(body)
            # 408/409/425/429/5xx: retry with backoff (rate limits get more patience than server errors)
            attempt += 1
            limit = 8 if r.status_code == 429 else 4
            if attempt > limit:
                raise LLMError(f"HTTP {r.status_code} after retries: {body[:500]}")
            wait = min(60.0, (2 ** min(attempt, 6)) + random.random()) if r.status_code == 429 else 1.5 * attempt + random.random()
            ra = r.headers.get("retry-after")
            if ra:
                try:
                    wait = min(120.0, max(wait, float(ra)))
                except ValueError:
                    pass
            time.sleep(wait)

    def _parse(self, data: Dict[str, Any], latency: float) -> LLMResponse:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if isinstance(reasoning, (dict, list)):
            reasoning = json.dumps(reasoning)[:4000]
        calls: List[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            name = fn.get("name") or ""
            cid = tc.get("id") or new_call_id()
            try:
                args = parse_json_args(raw)
                calls.append(ToolCall(id=cid, name=name, arguments=args, raw_arguments=raw if isinstance(raw, str) else json.dumps(raw)))
            except ValueError as e:
                calls.append(ToolCall(id=cid, name=name, arguments={}, raw_arguments=str(raw), parse_error=str(e)))
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        cdetails = u.get("completion_tokens_details") or {}
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0),
            reasoning_tokens=int(cdetails.get("reasoning_tokens") or 0),
            cost_usd=float(u.get("cost") or 0.0),
            calls=1,
        )
        return LLMResponse(
            text=text.strip() if isinstance(text, str) else "",
            tool_calls=calls,
            usage=usage,
            stop_reason=choice.get("finish_reason") or "",
            latency_s=latency,
            reasoning=reasoning if isinstance(reasoning, str) else "",
        )

    def list_models(self) -> List[str]:
        try:
            r = self._client.get(f"{self.base_url}/models")
            if r.status_code == 200:
                return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            pass
        return []
