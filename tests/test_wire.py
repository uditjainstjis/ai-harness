"""Provider wire formats and HTTP behaviour, tested offline against a local fake server."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pramana.llm.anthropic import AnthropicLLM
from pramana.llm.base import ToolSpec
from pramana.llm.openai_compat import OpenAICompatLLM

TOOLS = [ToolSpec("bash", "run", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})]
HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "fix it"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                                                        {"id": "call_2", "name": "bash", "arguments": {"command": "pwd"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "a.py"},
    {"role": "tool", "tool_call_id": "call_2", "name": "bash", "content": "/repo"},
    {"role": "user", "content": "harness note"},
]


class FakeServer:
    """Serves scripted (status, body) responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(n))})
                status, body = outer.responses.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_openai_wire_retries_negotiates_and_parses(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c9", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"pytest -q\"}"}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                                   "prompt_tokens_details": {"cached_tokens": 64}}}
    srv = FakeServer([
        (500, {"error": {"message": "internal"}}),
        (400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}}),
        (200, ok),
    ])
    try:
        llm = OpenAICompatLLM(srv.url, "sk-test", "some-model", temperature=0.0, provider="openai")
        resp = llm.chat(HISTORY, tools=TOOLS)
    finally:
        srv.close()
    assert resp.tool_calls[0].name == "bash" and resp.tool_calls[0].arguments == {"command": "pytest -q"}
    assert resp.usage.cached_tokens == 64
    assert len(srv.requests) == 3
    assert srv.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert "temperature" in srv.requests[1]["body"] and "temperature" not in srv.requests[2]["body"]
    msgs = srv.requests[2]["body"]["messages"]
    # every assistant tool call is answered by a tool message, in order, before any other message
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in msgs[3:5]] == ["call_1", "call_2"]
    assert "name" not in msgs[3]  # plain OpenAI: no name on tool messages


def test_anthropic_wire_alternation_and_tool_results(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = {"content": [{"type": "text", "text": "running tests"},
                      {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "pytest"}}],
          "stop_reason": "tool_use", "usage": {"input_tokens": 50, "cache_read_input_tokens": 400, "output_tokens": 12}}
    srv = FakeServer([(529, {"error": "overloaded"}), (200, ok)])
    try:
        llm = AnthropicLLM("sk-ant-test", "claude-x", base_url=srv.url)
        resp = llm.chat(HISTORY, tools=TOOLS)
    finally:
        srv.close()
    assert resp.text == "running tests" and resp.tool_calls[0].arguments == {"command": "pytest"}
    assert resp.usage.cached_tokens == 400 and resp.usage.input_tokens == 450
    req = srv.requests[-1]
    assert req["path"] == "/v1/messages" and req["headers"]["x-api-key"] == "sk-ant-test"
    body = req["body"]
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "user" and all(a != b for a, b in zip(roles, roles[1:]))  # strict alternation
    last_user = body["messages"][-1]["content"]
    assert [b["type"] for b in last_user] == ["tool_result", "tool_result", "text"]  # results first, then the note
    assert {b["tool_use_id"] for b in last_user if b["type"] == "tool_result"} == {"call_1", "call_2"}
    assert last_user[-1]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["text"] == "sys" and body["tools"][0]["input_schema"]["required"] == ["command"]


def test_openai_context_overflow_is_typed(monkeypatch):
    from pramana.llm.base import ContextOverflow

    srv = FakeServer([(400, {"error": {"message": "This model's maximum context length is 8192 tokens."}})])
    try:
        with pytest.raises(ContextOverflow):
            OpenAICompatLLM(srv.url, "k", "m", provider="groq").chat(HISTORY, tools=TOOLS)
    finally:
        srv.close()


def test_azure_style_endpoint(monkeypatch):
    ok = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": {}}
    srv = FakeServer([(200, ok)])
    try:
        llm = OpenAICompatLLM(srv.url, "azkey", "my-deployment", provider="azure")
        llm.base_url = srv.url  # keep the fake server's path root
        resp = llm.chat([{"role": "user", "content": "x"}], tools=None)
    finally:
        srv.close()
    req = srv.requests[-1]
    assert resp.text == "hi"
    assert req["headers"].get("api-key") == "azkey" and "Authorization" not in req["headers"]
    assert "/openai/deployments/my-deployment/chat/completions" in req["path"] and "api-version=" in req["path"]


def test_deepseek_thinking_mode_gets_all_reasoning_back(monkeypatch):
    """DeepSeek V4 thinks by default and rejects (HTTP 400) any tool-carrying request whose history has an
    assistant turn without reasoning_content. Pramana must learn that from the first reply and send it all."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    reply = lambda i: {"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": f"thought {i}",  # noqa: E731
                                                "tool_calls": [{"id": f"c{i}", "type": "function",
                                                                "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"}}]},
                                    "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    srv = FakeServer([(200, reply(1)), (200, reply(2))])
    try:
        llm = OpenAICompatLLM(srv.url, "sk-test", "deepseek-flash", provider="deepseek")
        hist = [{"role": "system", "content": "sys"}, {"role": "user", "content": "fix it"},
                {"role": "assistant", "content": "earlier turn with no reasoning"}, {"role": "user", "content": "go on"}]
        r1 = llm.chat(hist, tools=TOOLS)
        hist += [r1.as_message(), {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "a.py"}]
        llm.chat(hist, tools=TOOLS)
    finally:
        srv.close()
    sent = [m for m in srv.requests[1]["body"]["messages"] if m["role"] == "assistant"]
    assert all("reasoning_content" in m for m in sent)  # every assistant turn, even the one without reasoning
    assert sent[-1]["reasoning_content"] == "thought 1"


def test_missing_reasoning_content_400_turns_passback_on(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok = {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}], "usage": {}}
    srv = FakeServer([(400, {"error": {"message": "Missing `reasoning_content` field in the assistant message at message index 2."}}),
                      (200, ok)])
    try:
        llm = OpenAICompatLLM(srv.url, "sk-test", "some-model", provider="openai")
        resp = llm.chat(HISTORY, tools=TOOLS)
    finally:
        srv.close()
    assert resp.text == "done"
    assert all("reasoning_content" in m for m in srv.requests[1]["body"]["messages"] if m["role"] == "assistant")
