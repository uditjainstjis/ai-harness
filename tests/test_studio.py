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


def test_every_run_is_kept_on_disk_and_reloaded(tmp_path, monkeypatch):
    import subprocess
    import time
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, check=True)
    paths = {"runs_dir": str(tmp_path / "runs"), "workspace_dir": str(tmp_path / "ws")}
    studio = Studio({"paths": paths, "agent": {"max_steps": 3, "max_attempts": 1}})
    assert str(studio.home).startswith(str(tmp_path))
    run = studio.start("f() should return 2", str(repo), "")
    for _ in range(300):
        if run.status in ("done", "error"):
            break
        time.sleep(0.1)
    d = studio.home / run.id
    assert (d / "input.json").exists() and (d / "result.json").exists()
    assert sum(1 for _ in open(d / "events.jsonl")) >= 2
    assert json.loads((d / "input.json").read_text())["prompt"] == "f() should return 2"
    assert run.id in (studio.home / "index.jsonl").read_text()
    again = Studio({"paths": paths})       # a restart
    assert run.id in again.runs and again.runs[run.id].result is not None
