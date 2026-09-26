"""Pramana Studio: the app must accept whatever a user types, and serve its pages."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from pramana.web.server import Studio, interpret, make_handler


def test_interpret_is_forgiving():
    # plain words + a repo URL typed in the text box
    r = interpret("login breaks for emails with capitals https://github.com/acme/shop please fix", "")
    assert r["repo"] == "https://github.com/acme/shop" and "login breaks" in r["prompt"]
    # an issue link alone: repo inferred, the link stays the task (Pramana reads the issue from GitHub)
    r = interpret("https://github.com/psf/requests/issues/6710", "")
    assert r["repo"] == "psf/requests" and "issues/6710" in r["prompt"]
    # owner/repo#N shorthand
    assert interpret("pallets/flask#5000", "")["repo"] == "pallets/flask"
    # an issue link pasted into the repository box is treated as the task
    r = interpret("", "https://github.com/acme/shop/issues/12")
    assert r["repo"] == "acme/shop" and r["prompt"].endswith("/issues/12")
    # explicit repo wins; ~ is expanded
    r = interpret("fix the parser", "~/code/proj")
    assert r["repo"].endswith("/code/proj") and not r["repo"].startswith("~")


def test_studio_serves_pages_and_refuses_unclear_input(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "mock")
    studio = Studio()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(studio))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for path in ("/", "/static/app.js", "/static/app.css"):
            assert urllib.request.urlopen(base + path).status == 200
        assert json.load(urllib.request.urlopen(base + "/api/model"))["provider"] == "mock"
        req = urllib.request.Request(base + "/api/runs", data=json.dumps({"prompt": "fix it", "repo": ""}).encode(),
                                     headers={"content-type": "application/json"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("a task with no repository must be refused with a clear message")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "repository" in json.load(e)["error"].lower()
    finally:
        httpd.shutdown()
