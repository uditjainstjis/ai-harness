"""Development backend: drives a locally logged-in Claude Code CLI (`claude -p`) as a plain
text-completion model. Lets the team iterate with a Claude subscription without an API key.
It is never selected automatically; set provider = "claude-cli" explicitly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from .base import ContextOverflow, LLMError, LLMResponse, ToolSpec, Usage


class ClaudeCLILLM:
    def __init__(self, model: str = "haiku", timeout_s: float = 600.0) -> None:
        self.model = model or "haiku"
        self.timeout_s = timeout_s
        if not shutil.which("claude"):
            raise LLMError("provider claude-cli selected but the `claude` executable is not on PATH")

    @staticmethod
    def _render(messages: List[Dict[str, Any]]) -> (str, str):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        parts = []
        for m in messages:
            if m["role"] == "system":
                continue
            tag = "USER" if m["role"] == "user" else "ASSISTANT"
            parts.append(f"=== {tag} ===\n{m.get('content') or ''}")
        convo = "\n\n".join(parts)
        prompt = (
            "Below is the conversation so far. Write ONLY the next ASSISTANT message "
            "(no '=== ASSISTANT ===' header, do not write the USER's side).\n\n" + convo + "\n\n=== ASSISTANT ===\n"
        )
        return system, prompt

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[ToolSpec]] = None, temperature: Optional[float] = None) -> LLMResponse:
        system, prompt = self._render(messages)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(system or "You are a helpful assistant.")
            sys_path = fh.name
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--system-prompt-file", sys_path,
        ]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        last_err = ""
        try:
            for attempt in range(4):
                t0 = time.time()
                try:
                    proc = subprocess.run(
                        cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout_s,
                        cwd=tempfile.gettempdir(), env=env,
                    )
                except subprocess.TimeoutExpired:
                    last_err = "claude CLI timed out"
                    continue
                try:
                    data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
                except (ValueError, IndexError):
                    data = {}
                if data.get("is_error") or not data:
                    last_err = (data.get("result") or proc.stderr or proc.stdout or "unknown error")[:500]
                    if "too long" in last_err.lower() or "context" in last_err.lower():
                        raise ContextOverflow(last_err)
                    time.sleep(3 * (attempt + 1))
                    continue
                u = data.get("usage") or {}
                usage = Usage(
                    input_tokens=int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0),
                    output_tokens=int(u.get("output_tokens") or 0),
                    cached_tokens=int(u.get("cache_read_input_tokens") or 0),
                    cost_usd=float(data.get("total_cost_usd") or 0.0),
                    calls=1,
                )
                return LLMResponse(text=(data.get("result") or "").strip(), tool_calls=[], usage=usage,
                                   stop_reason=data.get("stop_reason") or "", latency_s=time.time() - t0)
        finally:
            try:
                os.unlink(sys_path)
            except OSError:
                pass
        raise LLMError(f"claude CLI failed: {last_err}")
