"""Model layer: one `ChatModel.chat(messages, tools)` interface over every backend.

`ChatModel` owns the tool-calling strategy:
  native  - send JSON-schema tools, parse native tool calls;
  text    - describe tools in the system prompt, parse <tool> blocks from text;
  auto    - native first; permanently switch to text if the endpoint rejects tools.
In native mode, tool calls a model writes as *text* are still recovered.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from .base import (
    ContextOverflow,
    LLMError,
    LLMResponse,
    ToolCall,
    ToolSpec,
    ToolsUnsupported,
    Usage,
)
from .textproto import parse_text_tool_calls, to_text_messages

__all__ = [
    "ChatModel",
    "build_model",
    "ContextOverflow",
    "LLMError",
    "LLMResponse",
    "ToolCall",
    "ToolSpec",
    "Usage",
]


class ChatModel:
    def __init__(self, backend: Any, tool_mode: str = "auto", name: str = "", provider: str = "", native_capable: bool = True) -> None:
        self.backend = backend
        self.name = name
        self.provider = provider
        self.tool_mode = tool_mode if native_capable else "text"
        if self.tool_mode == "auto" and not native_capable:
            self.tool_mode = "text"
        self.usage = Usage()
        self._lock = threading.Lock()
        self.on_call: Optional[Callable[[LLMResponse], None]] = None

    @property
    def effective_mode(self) -> str:
        return "text" if self.tool_mode == "text" else "native"

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[ToolSpec]] = None, temperature: Optional[float] = None) -> LLMResponse:
        tools = tools or []
        if tools and self.tool_mode != "text":
            try:
                resp = self.backend.chat(messages, tools=tools, temperature=temperature)
            except ToolsUnsupported:
                if self.tool_mode == "native":
                    raise
                self.tool_mode = "text"
                return self.chat(messages, tools, temperature)
            if not resp.tool_calls and resp.text:
                # model wrote its tool call as text despite native tools: recover it
                prose, calls = parse_text_tool_calls(resp.text, tools)
                if calls:
                    resp.tool_calls = calls
            self._account(resp)
            return resp
        wire = to_text_messages(messages, tools) if tools else messages
        resp = self.backend.chat(wire, tools=None, temperature=temperature)
        if tools:
            prose, calls = parse_text_tool_calls(resp.text, tools)
            resp.tool_calls = calls
            # keep the raw text (including tool blocks) as the assistant content so the
            # transcript the model sees next turn is exactly what it wrote
        self._account(resp)
        return resp

    def _account(self, resp: LLMResponse) -> None:
        with self._lock:
            self.usage.add(resp.usage)
        if self.on_call:
            try:
                self.on_call(resp)
            except Exception:  # noqa: BLE001
                pass


def build_model(cfg: Any) -> ChatModel:
    """Build a ChatModel from a resolved pramana.config.Config."""
    m = cfg.model
    kind = cfg.resolved_kind
    if cfg.resolved_provider == "unconfigured":
        raise LLMError(
            "No model credential found. Export AI_API_KEY (and, for OpenAI-compatible endpoints "
            "that are not auto-detected, AI_BASE_URL / AI_MODEL), or set [model] in pramana.toml."
        )
    if kind == "anthropic":
        from .anthropic import AnthropicLLM

        if not cfg.api_key:
            raise LLMError("AI_API_KEY is not set (Anthropic provider).")
        backend = AnthropicLLM(cfg.api_key, m.name, base_url=m.base_url, temperature=m.temperature,
                               max_output_tokens=m.max_output_tokens, timeout_s=m.request_timeout_s)
        return ChatModel(backend, m.tool_mode, m.name, cfg.resolved_provider)
    if kind == "claude-cli":
        from .claude_cli import ClaudeCLILLM

        return ChatModel(ClaudeCLILLM(m.name), "text", m.name, "claude-cli", native_capable=False)
    if kind == "mock":
        from .mock import MockLLM

        return ChatModel(MockLLM(), m.tool_mode, "mock", "mock")
    from .openai_compat import OpenAICompatLLM

    if not cfg.api_key and cfg.resolved_provider not in ("ollama",) and "localhost" not in m.base_url and "127.0.0.1" not in m.base_url:
        raise LLMError(f"AI_API_KEY is not set (provider {cfg.resolved_provider}).")
    backend = OpenAICompatLLM(
        m.base_url, cfg.api_key, m.name, temperature=m.temperature, max_output_tokens=m.max_output_tokens,
        reasoning_effort=m.reasoning_effort, timeout_s=m.request_timeout_s, provider=cfg.resolved_provider,
        seed=getattr(cfg.agent, "seed", None),
    )
    return ChatModel(backend, m.tool_mode, m.name, cfg.resolved_provider)
