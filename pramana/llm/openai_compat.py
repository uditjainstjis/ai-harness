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
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from .base import (
    Cancelled,
    ModelUnresponsive,
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


class _Throttle:
    """Process-wide, per-endpoint concurrency that adapts to rate limits (AIMD) and serves callers in
    arrival order (no run starves while others keep calling). Starts at 6 requests in flight (cap 12, floor 2).
    A burst of 429s is ONE signal: the limit is halved at most once per back-off window, and everyone pauses
    together. It climbs back by one after every 5 clean replies, or every 15 s without a 429 while callers
    wait, and an endpoint idle for a minute starts fresh, so one batch never inherits another's collapse."""

    def __init__(self, start: int = 6, cap: int = 12, floor: int = 2) -> None:
        import collections
        self.start, self.limit, self.cap, self.floor = start, start, cap, floor
        self.in_flight, self.ok, self.cool_until = 0, 0, 0.0
        self.last_cut, self.last_grow = 0.0, time.time()
        self.last_used = self.last_grow
        self.queue = collections.deque()
        self.cond = threading.Condition()

    def _heal(self, now: float) -> None:
        if not self.in_flight and now - self.last_used > 60:
            self.limit = max(self.limit, self.start)
        elif self.queue and self.limit < self.cap and now - max(self.last_cut, self.last_grow) >= 15:
            self.limit, self.last_grow = self.limit + 1, now

    def acquire(self) -> None:
        me = object()
        with self.cond:
            self.queue.append(me)
            while True:
                now = time.time()
                self._heal(now)
                if now < self.cool_until:
                    self.cond.wait(timeout=self.cool_until - now)
                elif self.queue[0] is me and self.in_flight < self.limit:
                    self.queue.popleft()
                    self.in_flight += 1
                    self.last_used = now
                    self.cond.notify_all()
                    return
                else:
                    self.cond.wait(timeout=0.5)

    def release(self, status: int, pause: float = 0.0) -> None:
        with self.cond:
            now = time.time()
            self.in_flight = max(0, self.in_flight - 1)
            self.last_used = now
            if status == 429:
                if now - self.last_cut >= max(pause, 10.0):     # the rest of a burst is the same signal
                    self.limit, self.last_cut = max(self.floor, self.limit // 2), now
                self.ok = 0
                self.cool_until = max(self.cool_until, now + pause)
            elif 0 < status < 400:
                self.ok += 1
                if self.ok >= 5 and self.limit < self.cap:
                    self.limit, self.ok, self.last_grow = self.limit + 1, 0, now
            self.cond.notify_all()


_THROTTLES: Dict[str, _Throttle] = {}
_THROTTLES_LOCK = threading.Lock()


def throttle_for(url: str) -> _Throttle:
    with _THROTTLES_LOCK:
        return _THROTTLES.setdefault(url.split("?")[0], _Throttle())


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
        # DeepSeek V4 (thinking mode, the default) rejects a tool-carrying request with HTTP 400 unless
        # EVERY earlier assistant message carries its reasoning_content. Learned from the first reply
        # that has one, or from that 400 itself.
        self.reasoning_field = ""
        self.passback_all = False
        self.text_mode_stops: List[str] = []  # set by ChatModel when it drives this backend in text mode
        self.on_wait = None       # callback(message): tells the UI when the endpoint makes us wait (retries)
        self.on_heartbeat = None  # callback(seconds): a call is in flight and has not answered yet
        self.cancel = threading.Event()  # set by the app's Stop button
        self.timeout_s = timeout_s
        headers = {"Content-Type": "application/json"}
        self.api_version = os.environ.get("AI_API_VERSION", "").strip()
        self.is_azure = provider == "azure" or ".azure.com" in self.base_url or "azure-api.net" in self.base_url
        if api_key:
            if self.is_azure:
                headers["api-key"] = api_key  # Azure OpenAI authenticates with this header, not Bearer
                if not self.api_version:
                    self.api_version = "2024-10-21"
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/pramana-harness"
            headers["X-Title"] = "Pramana coding harness"
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=30.0), headers=headers)

    # ------------------------------------------------------------------ wire format
    @staticmethod
    def _to_wire(messages: List[Dict[str, Any]], tool_names: bool = True, reasoning_window: int = 0,
                 reasoning_field: str = "", passback_all: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        asst_idx = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
        keep_reasoning = set(asst_idx[-reasoning_window:]) if reasoning_window > 0 else set()
        for i, m in enumerate(messages):
            role = m["role"]
            if role == "assistant":
                wm: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if passback_all:
                    wm[reasoning_field or "reasoning_content"] = m.get("reasoning") or ""
                elif i in keep_reasoning and m.get("reasoning"):
                    wm[reasoning_field or "reasoning"] = m["reasoning"]
                if m.get("reasoning_details"):
                    wm["reasoning_details"] = m["reasoning_details"]
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
            messages, tool_names=self.provider != "openai", reasoning_window=self.reasoning_window,
            reasoning_field=self.reasoning_field, passback_all=self.passback_all)}
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
        if "reasoning_content" in low and "missing" in low:
            if not self.passback_all:  # DeepSeek thinking mode: every assistant turn needs its reasoning back
                self.passback_all, self.reasoning_field = True, "reasoning_content"
                changed = True
        elif "reasoning" in low and (self.reasoning_window or self.passback_all) and "reasoning_effort" not in low:
            self.reasoning_window = 0  # endpoint rejects passed-back reasoning: stop sending it
            self.passback_all = False
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
        if self.is_azure and "/deployments/" not in url and self.model:
            url = f"{self.base_url}/openai/deployments/{self.model}/chat/completions"
        if self.api_version:
            url += ("&" if "?" in url else "?") + f"api-version={self.api_version}"
        attempt = 0
        negotiations = 0
        timeouts = 0
        while True:
            payload = self._payload(messages, tools, temperature)
            _debug_dump(payload)
            t0 = time.time()
            try:
                thr = throttle_for(url)
                thr.acquire()
                status = 0
                try:
                    r = self._post_watched(url, payload)
                    status = r.status_code
                finally:
                    ra = 0.0
                    if status == 429:
                        try:
                            ra = float(r.headers.get("retry-after") or 0)
                        except (ValueError, UnboundLocalError):
                            ra = 0.0
                    thr.release(status, pause=max(ra, 2.0 ** min(attempt + 1, 4)) if status == 429 else 0.0)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if isinstance(e, httpx.TimeoutException):
                    timeouts += 1
                    if timeouts >= 2:
                        raise ModelUnresponsive(
                            f"{self.model} did not answer within {int(self.timeout_s)}s, twice. The endpoint is probably "
                            f"overloaded (common on free tiers): pick another model in the Model menu and run again.") from e
                if attempt > 6:
                    raise LLMError(f"network error talking to {self.base_url}: {e}") from e
                wait = min(20, 2 ** attempt + random.random())
                self._tell(f"model endpoint did not answer ({type(e).__name__}); retrying in {wait:.0f}s")
                time.sleep(wait)
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
            wait = min(20.0, (2 ** min(attempt, 5)) + random.random()) if r.status_code == 429 else 1.5 * attempt + random.random()
            ra = r.headers.get("retry-after")
            if ra:
                try:
                    wait = min(60.0, max(wait, float(ra)))
                except ValueError:
                    pass
            self._tell(f"model endpoint busy (HTTP {r.status_code}{', rate limit' if r.status_code == 429 else ''}); retrying in {wait:.0f}s")
            time.sleep(wait)

    def _post_watched(self, url: str, payload: Dict[str, Any]):
        """POST on a worker thread; meanwhile report a heartbeat every 5s and honour Stop at once."""
        import concurrent.futures as cf
        if self.cancel.is_set():
            raise Cancelled("stopped by the user")
        ex = cf.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._client.post, url, json=payload)
        t0 = time.time()
        try:
            while True:
                try:
                    return fut.result(timeout=1.0)
                except cf.TimeoutError:
                    if self.cancel.is_set():
                        raise Cancelled("stopped by the user")
                    waited = int(time.time() - t0)
                    if waited and waited % 5 == 0 and self.on_heartbeat:
                        try:
                            self.on_heartbeat(waited)
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _tell(self, msg: str) -> None:
        if self.on_wait:
            try:
                self.on_wait(msg)
            except Exception:  # noqa: BLE001
                pass

    def _parse(self, data: Dict[str, Any], latency: float) -> LLMResponse:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if isinstance(reasoning, (dict, list)):
            reasoning = json.dumps(reasoning)[:4000]
        if msg.get("reasoning_content") and not self.reasoning_field:
            # this API returns reasoning_content: DeepSeek-style, which wants all of it back
            self.reasoning_field, self.passback_all = "reasoning_content", True
        elif msg.get("reasoning") and not self.reasoning_field:
            self.reasoning_field = "reasoning"
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
            reasoning_details=msg.get("reasoning_details") or None,
        )

    def list_models(self) -> List[str]:
        try:
            r = self._client.get(f"{self.base_url}/models")
            if r.status_code == 200:
                return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            pass
        return []
