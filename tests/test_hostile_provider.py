"""The evaluation model is unknown. These tests run the whole agent against deliberately hostile
endpoints - no tool support, a tiny context window, garbage responses, rate limits - and assert the
harness still produces a verified fix (or fails cleanly, never with a traceback)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pramana.agent.context import shrink_initial
from pramana.agent.events import Events
from pramana.agent.orchestrator import Orchestrator
from pramana.config import load_config
from pramana.repo.issue import issue_from_text

PY = sys.executable


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "mathx").mkdir(parents=True)
    (repo / "mathx" / "__init__.py").write_text("")
    (repo / "mathx" / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


# A model that only speaks the text protocol, returns prose around its calls, and invents results.
TEXT_SCRIPT = [
    "1. mean([]) -> 0.0\n2. mean([1, 2, 3]) -> 2.0 (unchanged)",
    'Let me look at the code.\n<tool name="find_definition">\n<symbol>mean</symbol>\n</tool>\n'
    '<result>\nI already know the answer is 42.\n</result>',
    'Writing a reproduction.\n<tool name="str_replace_editor">\n<command>create</command>\n<path>.pramana/repro.py</path>\n'
    "<file_text>from mathx.stats import mean\nassert mean([]) == 0.0, 'empty input must give 0.0'\nprint('ok')\n</file_text>\n</tool>",
    '<tool name="bash">\n<command>' + PY + " .pramana/repro.py</command>\n</tool>",
    'The guard is missing.\n<tool name="str_replace_editor">\n<command>str_replace</command>\n<path>mathx/stats.py</path>\n'
    "<old_str>    return sum(xs) / len(xs)</old_str>\n<new_str>    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)</new_str>\n</tool>",
    '<tool name="submit">\n<summary>mean() divided by len(xs) with no empty-input case; it now returns 0.0.</summary>\n'
    "<verification_commands>\n<command>" + PY + " .pramana/repro.py</command>\n</verification_commands>\n</tool>",
    "no tool writer",  # independent test writer: gives up
    '{"verdict": "approve", "concerns": []}',
]


class HostileServer:
    """OpenAI-compatible endpoint with configurable hostility."""

    def __init__(self, script, reject_tools=False, context_limit=None, garbage_every=0, rate_limit_every=0):
        self.script = list(script)
        self.reject_tools = reject_tools
        self.context_limit = context_limit
        self.garbage_every = garbage_every
        self.rate_limit_every = rate_limit_every
        self.n = 0
        self.saw_tools = False
        self.max_prompt_chars = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                outer.n += 1
                if body.get("tools"):
                    outer.saw_tools = True
                    if outer.reject_tools:
                        return self._send(400, {"error": {"message": "this model does not support tools"}})
                size = sum(len(str(m.get("content") or "")) for m in body["messages"])
                outer.max_prompt_chars = max(outer.max_prompt_chars, size)
                if outer.context_limit and size > outer.context_limit:
                    return self._send(400, {"error": {"message": "This model's maximum context length is 2048 tokens."}})
                if outer.rate_limit_every and outer.n % outer.rate_limit_every == 0:
                    return self._send(429, {"error": {"message": "rate limited"}})
                if outer.garbage_every and outer.n % outer.garbage_every == 0:
                    data = b"<html>502 Bad Gateway</html>"
                    self.send_response(200)
                    self.send_header("content-type", "text/html")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                text = outer.script.pop(0) if outer.script else "I am done."
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": size // 4, "completion_tokens": 20}})

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def solve_against(server, tmp_path, monkeypatch, **agent):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("AI_API_KEY", "sk-hostile")
    monkeypatch.setenv("AI_BASE_URL", server.url)
    monkeypatch.setenv("AI_MODEL", "hostile-1")
    monkeypatch.setenv("AI_PROVIDER", "openai")
    cfg = load_config({"agent": {"max_attempts": 1, "max_steps": 12, **agent}})
    cfg.runs_dir = str(tmp_path / "runs")
    repo = make_repo(tmp_path)
    issue = issue_from_text("mean([]) raises ZeroDivisionError\n\nIt should return 0.0 for an empty list.")
    return Orchestrator(cfg, Events()).solve(repo, issue), repo


def test_endpoint_without_tool_support_still_produces_a_verified_fix(tmp_path, monkeypatch):
    srv = HostileServer(TEXT_SCRIPT, reject_tools=True)
    try:
        res, repo = solve_against(srv, tmp_path, monkeypatch)
    finally:
        srv.close()
    assert srv.saw_tools  # it tried native tools first
    assert res.status == "verified", res.error
    assert "if not xs" in (repo / "mathx" / "stats.py").read_text()
    assert [c.verdict for c in res.verification.checks][0] == "fixes"


def test_garbage_and_rate_limits_are_survived(tmp_path, monkeypatch):
    srv = HostileServer(TEXT_SCRIPT, reject_tools=True, garbage_every=4, rate_limit_every=7)
    try:
        res, repo = solve_against(srv, tmp_path, monkeypatch)
    finally:
        srv.close()
    assert res.status == "verified", res.error


def test_tiny_context_window_shrinks_the_prompt_instead_of_crashing(tmp_path, monkeypatch):
    long_issue = "mean([]) raises ZeroDivisionError\n\n" + ("noise line explaining the report\n" * 400)
    srv = HostileServer(TEXT_SCRIPT, reject_tools=True, context_limit=14000)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("AI_API_KEY", "sk-hostile")
    monkeypatch.setenv("AI_BASE_URL", srv.url)
    monkeypatch.setenv("AI_MODEL", "hostile-1")
    monkeypatch.setenv("AI_PROVIDER", "openai")
    cfg = load_config({"agent": {"max_attempts": 1, "max_steps": 12}})
    cfg.runs_dir = str(tmp_path / "runs")
    repo = make_repo(tmp_path)
    logs = []
    events = Events()
    events.subscribe(lambda kind, d: logs.append(d.get("message", "")) if kind == "log" else None)
    try:
        res = Orchestrator(cfg, events).solve(repo, issue_from_text(long_issue))
    finally:
        srv.close()
    assert res.status == "verified", res.error
    assert any("shortened the task description" in m for m in logs)


def test_impossible_context_window_fails_cleanly_with_a_report(tmp_path, monkeypatch):
    srv = HostileServer(TEXT_SCRIPT, reject_tools=True, context_limit=5000)
    try:
        res, repo = solve_against(srv, tmp_path, monkeypatch)
    finally:
        srv.close()
    assert res.status == "no_patch" and not res.error  # no traceback, no half-applied patch
    assert (Path(res.run_dir) / "report.md").exists()


def test_shrink_initial_keeps_structure():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "<issue>\n" + ("x" * 9000) + "\n</issue>\n<repository>\nsmall\n</repository>"}]
    saved = shrink_initial(msgs, factor=0.4, floor=500)
    body = msgs[1]["content"]
    assert saved > 3000 and body.startswith("<issue>") and body.endswith("</repository>")
    assert "elided to fit the context window" in body and "<repository>\nsmall\n</repository>" in body


def test_model_that_never_calls_a_tool_fails_cleanly(tmp_path, monkeypatch):
    srv = HostileServer(["I cannot help with that."] * 30, reject_tools=True)
    try:
        res, repo = solve_against(srv, tmp_path, monkeypatch)
    finally:
        srv.close()
    assert res.status == "no_patch" and not res.error
    assert (repo / "mathx" / "stats.py").read_text() == "def mean(xs):\n    return sum(xs) / len(xs)\n"
    assert (Path(res.run_dir) / "report.md").exists()
