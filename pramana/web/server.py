"""Pramana Studio: the desktop-style app around the harness.

A small local web server (standard library only) that starts runs in background threads and streams
the agent's events to the browser over Server-Sent Events. `pramana ui` opens it as an app window.

Input is deliberately forgiving: one text box takes a plain-language request, a GitHub issue link or
owner/repo#N, and the repository can be a GitHub URL, owner/name, or a local folder. A GitHub link
inside the request is enough on its own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from ..agent.events import Events
from ..agent.orchestrator import Orchestrator, RunResult
from ..config import Config, load_config
from ..repo.issue import ensure_repo, parse_issue, repo_slug_from_remote

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]          # the Pramana checkout (bench/tasks lives here)
DEMOS = ROOT / "bench" / "tasks"

ISSUE_URL = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
REPO_URL = re.compile(r"(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/\s#?]|$)")
SHORT_ISSUE = re.compile(r"^\s*([\w.-]+)/([\w.-]+)#(\d+)\s*$")


def _plain(o: Any) -> Any:
    """JSON-safe view of dataclasses / paths."""
    if is_dataclass(o):
        return {k: _plain(v) for k, v in asdict(o).items()}
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, Path):
        return str(o)
    return o


ISSUE_NUMS = re.compile(r"(?:#|\bissues?\s*(?:no\.?|number)?\s*#?)(\d{1,6})\b", re.I)
GENERIC_ISSUES = re.compile(r"\b(fix|solve|resolve|close|handle|address|tackle|work on|clear|do)\b[^.\n]{0,40}\b(issues|bugs|tickets)\b", re.I)


def issues_intent(prompt: str) -> Any:
    """[numbers] when specific issues are named, "all" for a short generic "fix the issues", else None."""
    text = (prompt or "").strip()
    if ISSUE_URL.search(text) or SHORT_ISSUE.match(text):
        return None                              # a single issue link: an ordinary run
    nums = []
    for m in ISSUE_NUMS.finditer(text):
        n = int(m.group(1))
        if n not in nums:
            nums.append(n)
    more = re.findall(r"(?:,|\band\b|&)\s*#?(\d{1,6})\b", text) if nums else []
    for n in map(int, more):
        if n not in nums:
            nums.append(n)
    if nums:
        return nums
    if len(text) <= 90 and (GENERIC_ISSUES.search(text) or text.lower() in ("", "fix", "fix it", "fix everything", "fix all")):
        return "all"
    return None


def interpret(prompt: str, repo: str) -> Dict[str, str]:
    """Work out (repo, task) from whatever the user typed. Never asks the user to be precise."""
    prompt, repo = (prompt or "").strip(), (repo or "").strip()
    if repo and (ISSUE_URL.search(repo) or SHORT_ISSUE.match(repo)):   # an issue link typed as the repo
        prompt, repo = (repo + ("\n\n" + prompt if prompt else "")).strip(), ""
    if not repo:
        m = ISSUE_URL.search(prompt) or SHORT_ISSUE.match(prompt)
        if m:
            repo = f"{m.group(1)}/{m.group(2)}"
        else:
            m = REPO_URL.search(prompt)
            if m:
                repo = f"https://github.com/{m.group(1)}/{m.group(2)}"
                prompt = (prompt[:m.start()] + prompt[m.end():]).strip() or prompt
    if repo.startswith("~"):
        repo = str(Path(repo).expanduser())
    return {"prompt": prompt, "repo": repo}


class Run:
    """One task. Everything is written to disk as it happens (history survives restarts and crashes):
    input.json, events.jsonl (every live event), result.json, github.json, plus Pramana's own evidence
    bundle (trajectory, transcripts, report), which result.json points to."""

    def __init__(self, prompt: str, repo: str, test: str, home: Optional[Path] = None, want_pr: bool = False,
                 run_id: str = "") -> None:
        self.id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.prompt, self.repo_spec, self.test = prompt, repo, test
        self.status = "starting"
        self.events: List[Dict[str, Any]] = []
        self.cond = threading.Condition()
        self.result: Optional[Dict[str, Any]] = None
        self.created = time.time()
        self.title = (prompt.strip().splitlines() or ["task"])[0][:120]
        self.want_pr = want_pr
        self.github: List[Dict[str, Any]] = []
        self.model: Dict[str, Any] = {}
        self.dir = (home / self.id) if home else None
        self._loaded = True
        self.now, self.phase, self.calls, self.auto_pr = "", "", 0, False
        self.split = {"setup": 0.0, "model": 0.0, "tools": 0.0, "proof": 0.0, "other": 0.0}
        self._last_t = 0.0

    def _track(self, kind: str, d: Dict[str, Any]) -> None:
        """One human line for 'what is it doing right now' (shown live in the batch view), and where the
        time goes: each gap between events is charged to what ended it (a model reply, a tool, the proof)."""
        t = d.get("t")
        if kind == "stage" and d.get("name") == "ready":
            self.split["setup"] = round(time.time() - self.created, 1)
        if isinstance(t, (int, float)):
            gap = max(0.0, t - self._last_t)
            bucket = ("model" if kind in ("llm", "wait") else "tools" if kind == "tool_result"
                      else "proof" if kind in ("verify", "checkpoint", "independent_test", "review") else "other")
            self.split[bucket] = round(self.split[bucket] + gap, 1)
            self._last_t = t
        if kind == "phase":
            self.phase = d.get("name", self.phase)
        elif kind == "tool_call":
            self.now = f"{d.get('name', '')} {str(d.get('brief', ''))[:90]}".strip()
        elif kind == "wait":
            self.now = f"waiting for the model · {d.get('seconds')}s"
        elif kind == "llm":
            self.calls += 1
        elif kind == "verify":
            self.now = f"proof gate: {'accepted' if d.get('accepted') else 'not yet'} ({d.get('strength')})"
        elif kind == "triage":
            self.now = f"triage: {d.get('size')} → {d.get('plan')}"
        elif kind == "log" and ("fast path" in str(d.get("message")) or "busy" in str(d.get("message"))):
            self.now = str(d.get("message"))[:110]
        elif kind == "stage":
            self.now = str(d.get("message"))[:110]
        elif kind == "done":
            self.now = ""

    def push(self, kind: str, data: Dict[str, Any]) -> None:
        self._track(kind, data)
        with self.cond:
            ev = {"seq": len(self.events), "kind": kind, "at": round(time.time(), 2), **_plain(data)}
            self.events.append(ev)
            if self.dir:
                with open(self.dir / "events.jsonl", "a") as fh:
                    fh.write(json.dumps(ev) + "\n")
            self.cond.notify_all()

    def save(self, name: str, obj: Any) -> None:
        if self.dir:
            (self.dir / name).write_text(json.dumps(obj, indent=1, default=str))

    def load_events(self) -> None:
        if not self._loaded and self.dir and (self.dir / "events.jsonl").exists():
            with self.cond:
                self.events = [json.loads(l) for l in open(self.dir / "events.jsonl") if l.strip()]
                self._loaded = True

    @classmethod
    def from_disk(cls, d: Path) -> Optional["Run"]:
        try:
            inp = json.loads((d / "input.json").read_text())
        except (OSError, ValueError):
            return None
        run = cls(inp.get("prompt", ""), inp.get("repo", ""), inp.get("test", ""), d.parent, inp.get("want_pr", False), d.name)
        run.created, run.title, run.model = inp.get("created", run.created), inp.get("title") or run.title, inp.get("model") or {}
        run.batch_id, run.issue_number = inp.get("batch") or "", inp.get("issue")
        res = d / "result.json"
        run.result = json.loads(res.read_text()) if res.exists() else None
        run.status = "done" if run.result and run.result.get("status") != "error" else ("error" if run.result else "interrupted")
        gh = d / "github.json"
        run.github = json.loads(gh.read_text()) if gh.exists() else []
        run._loaded = False
        return run

    def summary(self) -> Dict[str, Any]:
        r = self.result or {}
        return {"id": self.id, "title": self.title, "repo": self.repo_spec, "status": self.status,
                "created": self.created, "verdict": r.get("status"), "elapsed_s": r.get("elapsed_s"),
                "tokens": (r.get("usage") or {}).get("total_tokens"), "want_pr": self.want_pr,
                "github": self.github, "model": self.model, "history_dir": str(self.dir or ""),
                "batch_id": getattr(self, "batch_id", ""), "issue": getattr(self, "issue_number", None),
                "now": self.now, "phase": self.phase, "calls": self.calls, "split": self.split}


class Batch:
    """Several GitHub issues, fixed one at a time. Each starts from the untouched base commit, is proven on
    its own, and a proven fix is committed to its own branch (pramana/issue-N): one pull request per issue."""

    def __init__(self, slug: str, numbers: List[int], want_pr: bool, home: Path, titles: Dict[int, str], auto_pr: bool = False) -> None:
        self.auto_pr = auto_pr
        self.id = "b" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.slug, self.numbers, self.want_pr, self.titles = slug, numbers, want_pr, titles
        self.items: List[Dict[str, Any]] = [{"number": n, "title": titles.get(n, ""), "run_id": "", "status": "queued"} for n in numbers]
        self.status, self.created, self.repo_path, self.base = "starting", time.time(), "", ""
        self.ended: Optional[float] = None
        self.home = home / "batches"
        self.home.mkdir(parents=True, exist_ok=True)

    def view(self, runs: Dict[str, "Run"]) -> Dict[str, Any]:
        items = []
        for it in self.items:
            run = runs.get(it["run_id"]) if it["run_id"] else None
            r = (run.result or {}) if run else {}
            items.append({**it, "status": (r.get("status") or run.status) if run else it["status"],
                          "tokens": (r.get("usage") or {}).get("total_tokens"), "elapsed_s": r.get("elapsed_s"),
                          "branch": r.get("branch", ""), "github": run.github if run else [],
                          "now": run.now if run else "", "phase": run.phase if run else "", "calls": run.calls if run else 0,
                          "started": run.created if run else None, "split": run.split if run else None})
        return {"id": self.id, "repo": self.slug, "status": self.status, "created": self.created, "want_pr": self.want_pr,
                "auto_pr": self.auto_pr, "running_now": sum(1 for x in items if x["status"] in ("running", "starting", "preparing")),
                "ended": self.ended, "elapsed_s": round((self.ended or time.time()) - self.created, 1),
                "repo_path": self.repo_path, "items": items}

    def save(self, runs: Dict[str, "Run"]) -> None:
        (self.home / f"{self.id}.json").write_text(json.dumps(self.view(runs), indent=1, default=str))


def _git(repo: Path, *args: str, check: bool = False) -> str:
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr[-300:]}")
    return p.stdout.strip()


def _reset(repo: Path, base: str) -> None:
    _git(repo, "checkout", "-q", "-f", base)
    _git(repo, "clean", "-fdq", "-e", ".venv", "-e", "venv", "-e", "node_modules", "-e", ".pramana-env")


APP_SETTINGS = Path.home() / "Library" / "Application Support" / "Pramana" / "settings.json"


def app_mode() -> bool:
    """Running as the Mac app (Pramana.app), where there is no shell to export AI_API_KEY from."""
    return os.environ.get("PRAMANA_APP") == "1"


def load_saved_model() -> Dict[str, str]:
    try:
        data = json.loads(APP_SETTINGS.read_text())
        return {k: str(v) for k, v in data.items() if k in ("AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL") and v}
    except (OSError, ValueError):
        return {}


def save_model_settings(sm: Dict[str, str]) -> None:
    """The app keeps the key the user pasted in their own Library folder, readable only by them (0600).
    Nothing is ever written into the repository or the app bundle."""
    APP_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_SETTINGS.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(sm, fh)
    os.replace(tmp, APP_SETTINGS)


class Studio:
    def __init__(self, cfg_overrides: Optional[Dict[str, Any]] = None) -> None:
        self.overrides = cfg_overrides or {}
        self.runs: Dict[str, Run] = {}
        self.lock = threading.Lock()
        self.session_model: Dict[str, str] = {}   # set from the settings panel
        self.app = app_mode()
        if self.app and not os.environ.get("AI_API_KEY"):
            self.session_model = load_saved_model()   # the key pasted into the Mac app last time
        self._models_cache: Dict[Any, Any] = {}
        self.batches: Dict[str, Batch] = {}
        try:
            self.home = Path(load_config(self.overrides).runs_dir) / "studio"
        except Exception:  # noqa: BLE001
            self.home = ROOT / "runs" / "studio"
        self.home.mkdir(parents=True, exist_ok=True)
        for d in sorted(self.home.iterdir()):
            if d.is_dir() and (run := Run.from_disk(d)):
                self.runs[run.id] = run

    # ------------------------------------------------------------------ model settings
    def config(self) -> Config:
        """The environment's config with this session's model choice applied on top. Never touches
        os.environ (parallel runs load their config at the same moment)."""
        from ..config import resolve_provider
        cfg = load_config(self.overrides)
        sm = dict(self.session_model)
        if not sm:
            return cfg
        if set(sm) == {"AI_MODEL"}:                       # same provider and key, another model
            cfg.model.name = sm["AI_MODEL"]
            return cfg
        cfg.api_key = sm.get("AI_API_KEY", cfg.api_key) or ""
        cfg.model.provider = sm.get("AI_PROVIDER") or "auto"
        cfg.model.base_url = sm.get("AI_BASE_URL") or ""
        cfg.model.name = sm.get("AI_MODEL") or ""
        resolve_provider(cfg)
        return cfg

    def model_info(self) -> Dict[str, Any]:
        try:
            cfg = self.config()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}
        prov = cfg.resolved_provider
        source = ("your logged-in Claude Code (no key needed)" if prov == "claude-cli"
                  else ("saved in Pramana's settings on this Mac" if self.app else "a key entered in this app (memory only)")
                  if self.session_model.get("AI_API_KEY")
                  else "AI_API_KEY" if cfg.api_key else "not configured")
        return {"ok": prov not in ("unconfigured", ""), "provider": prov, "model": cfg.model.name,
                "base_url": cfg.model.base_url if prov not in ("claude-cli", "mock") else "", "key_source": source,
                "app": self.app, "saved": bool(self.app and load_saved_model().get("AI_API_KEY"))}

    def models(self) -> Dict[str, Any]:
        """Chat models the current key can use, the recommended ones first."""
        from ..config import MODEL_PREFERENCE, _list_models
        cfg = load_config(self.overrides)   # the environment's key, whatever the session currently uses
        prov = cfg.resolved_provider
        if prov in ("claude-cli", "mock", "unconfigured") or not cfg.api_key:
            return {"provider": prov, "current": cfg.model.name, "models": []}
        key = (prov, cfg.model.base_url, cfg.api_key[-6:])
        cached = self._models_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            ids = cached[1]
        else:
            ids = _list_models(cfg.model.base_url, cfg.api_key, timeout=15) or []
            self._models_cache[key] = (time.time(), ids)
        skip = ("embed", "reward", "safety", "guard", "parse", "rerank", "vl-", "vision", "clip", "tts", "asr", "whisper", "omni")
        ids = [i for i in ids if not any(k in i.lower() for k in skip)]
        prefs = MODEL_PREFERENCE.get(prov, [])
        rank = lambda i: next((n for n, p in enumerate(prefs) if p in i), len(prefs))  # noqa: E731
        ids.sort(key=lambda i: (rank(i), i))
        return {"provider": prov, "current": cfg.model.name,
                "models": [{"id": i, "recommended": rank(i) < len(prefs)} for i in ids]}

    def set_model(self, body: Dict[str, Any]) -> Dict[str, Any]:
        mode = body.get("mode")
        if mode == "claude":
            self.session_model = {"AI_PROVIDER": "claude-cli", "AI_MODEL": body.get("model") or "sonnet",
                                  "AI_API_KEY": "", "AI_BASE_URL": ""}
        elif mode == "api":
            prev_key = self.session_model.get("AI_API_KEY", "")
            self.session_model = {"AI_PROVIDER": body.get("provider") or "auto", "AI_MODEL": body.get("model") or "",
                                  "AI_BASE_URL": body.get("base_url") or ""}
            key = (body.get("key") or "").strip() or prev_key     # changing only the model keeps the key
            if key:
                self.session_model["AI_API_KEY"] = key
            if self.app and key:
                save_model_settings({k: v for k, v in self.session_model.items() if v})
        elif mode == "forget":   # the Mac app's "Remove saved key"
            try:
                APP_SETTINGS.unlink()
            except OSError:
                pass
            self.session_model = {}
        else:  # the environment's key (what `make run` was started with), optionally another of its models
            self.session_model = {"AI_MODEL": body["model"]} if body.get("model") else {}
        return self.model_info()

    # ------------------------------------------------------------------ runs
    def issues_for(self, prompt: str, repo: str) -> Optional[Dict[str, Any]]:
        """If the request is about a GitHub repo's issues, the list to choose from (else None)."""
        from ..repo.issue import list_github_issues
        it = interpret(prompt, repo)
        intent = issues_intent(it["prompt"] if it["prompt"] else "")
        if intent is None and it["prompt"]:
            return None
        m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)", it["repo"]) or re.fullmatch(r"([\w.-]+)/([\w.-]+)", it["repo"])
        if not m:
            return None
        slug = f"{m.group(1)}/{m.group(2)}"
        issues = list_github_issues(slug)
        if isinstance(intent, list) and len(intent) == 1:
            return {"single": f"https://github.com/{slug}/issues/{intent[0]}", "repo": slug}
        if isinstance(intent, list):     # named issues older than the listed ones still belong in the picker
            from ..repo.issue import github_issue_brief
            have = {i["number"] for i in issues}
            named = [b for b in (github_issue_brief(slug, n) for n in intent if n not in have) if b]
            issues = named + issues
        pre = intent if isinstance(intent, list) else [i["number"] for i in issues]
        return {"select_issues": True, "repo": slug, "issues": issues, "preselect": pre}

    def start_batch(self, slug: str, numbers: List[int], want_pr: bool, auto_pr: bool = False) -> Batch:
        from ..repo.issue import list_github_issues
        if not numbers:
            raise ValueError("Pick at least one issue.")
        try:
            from ..repo.issue import github_issue_brief
            titles = {i["number"]: i["title"] for i in list_github_issues(slug)}
            for n in numbers:
                if n not in titles:
                    b = github_issue_brief(slug, n)
                    if b:
                        titles[n] = b["title"]
        except Exception:  # noqa: BLE001
            titles = {}
        batch = Batch(slug, numbers, want_pr, self.home, titles, auto_pr)
        self.batches[batch.id] = batch
        threading.Thread(target=self._batch_work, args=(batch,), daemon=True).start()
        return batch

    def _batch_work(self, batch: Batch) -> None:
        """Every issue at once, each in its own local clone (own environment, own branch), so fixes cannot
        interfere and each can become its own pull request. The fast path makes easy issues take seconds;
        only the ones it cannot prove escalate to the full agent."""
        from concurrent.futures import ThreadPoolExecutor
        from .github import gh_user
        try:
            cfg = self.config()
            workspace = Path(cfg.workspace_dir)
            main = ensure_repo(batch.slug, workspace)
            batch.repo_path, batch.status = str(main), "preparing"
            batch.base = _git(main, "rev-parse", "HEAD", check=True)
            origin = _git(main, "remote", "get-url", "origin") or f"https://github.com/{batch.slug}.git"
            u = gh_user()
            who = ["-c", f"user.name={u}", "-c", f"user.email={u}@users.noreply.github.com"] if u else \
                ["-c", "user.name=Pramana", "-c", "user.email=pramana@localhost"]
            batch.status = "running"
            lock = threading.Lock()

            def one(it: Dict[str, Any]) -> None:
                n = it["number"]
                if getattr(batch, "cancelled", False):
                    it["status"] = "cancelled"
                    return
                dest = workspace / f"{main.name}-issue-{n}-{batch.id[-4:]}"
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                subprocess.run(["git", "clone", "-q", str(main), str(dest)], capture_output=True)
                _git(dest, "remote", "set-url", "origin", origin)      # pull requests go to GitHub, not the local copy
                _git(dest, "checkout", "-q", batch.base)
                run = Run(f"https://github.com/{batch.slug}/issues/{n}", str(dest), "", self.home, batch.want_pr)
                run.batch_id, run.issue_number = batch.id, n
                run.title = f"#{n} {it['title']}".strip()
                self._register(run)
                with lock:
                    it["run_id"], it["status"] = run.id, "running"
                    batch.save(self.runs)
                self._work(run)
                r = run.result or {}
                files = [f for f in re.findall(r"^diff --git a/.* b/(.+)$", r.get("patch") or "", flags=re.M) if not f.startswith(".pramana/")]
                if r.get("status") in ("verified", "patched") and files:
                    branch = f"pramana/issue-{n}"
                    _git(dest, "checkout", "-q", "-B", branch)
                    for f in files:
                        _git(dest, "add", "--", f)
                    _git(dest, *who, "commit", "-q", "-m", f"Fix #{n}: {it['title']}".strip(), "-m", f"Closes #{n}")
                    r["branch"], r["commit"], r["closes"] = branch, _git(dest, "rev-parse", "HEAD"), n
                    run.save("result.json", r)
                    if batch.auto_pr and r.get("status") == "verified":   # automatic only when proven
                        self._auto_pr(run)
                if batch.auto_pr and r.get("status") != "verified":      # say why, instead of silently skipping
                    run.push("log", {"level": "info", "message": f"no automatic pull request: the result is "
                                     f"'{r.get('status') or run.status}' and only proven fixes are opened automatically"})
                with lock:
                    it["status"] = r.get("status") or run.status
                    batch.save(self.runs)

            with ThreadPoolExecutor(max_workers=max(1, min(10, len(batch.items)))) as ex:   # all at once; the shared limiter paces the API
                list(ex.map(one, batch.items))
            batch.status = "stopped" if getattr(batch, "cancelled", False) else "done"
            batch.ended = time.time()
        except Exception as e:  # noqa: BLE001
            batch.status = f"error: {type(e).__name__}: {e}"[:300]
        batch.save(self.runs)

    def start(self, prompt: str, repo: str, test: str, want_pr: bool = False, auto_pr: bool = False) -> Run:
        it = interpret(prompt, repo)
        if not it["prompt"]:
            raise ValueError("Tell Pramana what to do: describe the bug or feature, or paste a GitHub issue link.")
        if not it["repo"]:
            raise ValueError("Which repository? Paste a GitHub URL (or owner/name) or a local folder path.")
        run = Run(it["prompt"], it["repo"], test, self.home, want_pr)
        run.auto_pr = auto_pr
        self._register(run, raw_prompt=prompt, raw_repo=repo)

        def go() -> None:
            self._work(run)
            if run.auto_pr and (run.result or {}).get("status") == "verified" and (run.result or {}).get("patch"):
                self._auto_pr(run)
        threading.Thread(target=go, daemon=True).start()
        return run

    def _register(self, run: Run, raw_prompt: str = "", raw_repo: str = "") -> None:
        run.dir.mkdir(parents=True, exist_ok=True)
        info = self.model_info()
        run.model = {k: info.get(k) for k in ("provider", "model", "base_url", "key_source")}
        run.save("input.json", {"id": run.id, "created": run.created, "prompt": run.prompt, "repo": run.repo_spec,
                                "raw_prompt": raw_prompt or run.prompt, "raw_repo": raw_repo or run.repo_spec, "test": run.test,
                                "want_pr": run.want_pr, "title": run.title, "model": run.model,
                                "batch": getattr(run, "batch_id", ""), "issue": getattr(run, "issue_number", None)})
        with self.lock:
            self.runs[run.id] = run

    def _work(self, run: Run) -> None:
        try:
            cfg = self.config()
            run.status = "preparing"
            run.push("stage", {"name": "setup", "message": "getting the repository"})
            workspace = Path(cfg.workspace_dir)
            repo = ensure_repo(run.repo_spec, workspace)
            if str(repo.resolve()).startswith(str(workspace.resolve())) and not getattr(run, "skip_bootstrap", False):
                from ..repo.bootstrap import bootstrap
                run.push("stage", {"name": "setup", "message": "installing the project's dependencies"})
                for note in bootstrap(repo, log=lambda m: run.push("log", {"level": "info", "message": m})):
                    bad = "could not" in note or "FAILED" in note     # tests will not import the project: say so loudly
                    run.push("log", {"level": "warn" if bad else "info",
                                     "message": ("environment problem, tests may not run: " if bad else "env: ") + note})
            slug = repo_slug_from_remote(repo)
            issue = parse_issue(run.prompt, default_slug=slug)
            run.title = (f"#{run.issue_number} " if getattr(run, "issue_number", None) else "") + (issue.title[:120] or run.title)
            run.push("stage", {"name": "ready", "message": f"repository ready: {repo}", "repo": str(repo), "title": run.title})
            run.status = "running"
            events = Events()
            events.subscribe(run.push)
            from ..llm import build_model
            model = build_model(cfg)
            run.model_obj = model
            if getattr(run, "cancelled", False):
                raise SystemExit("stopped by the user")
            res: RunResult = Orchestrator(cfg, events, model=model).solve(repo, issue, acceptance_cmd=run.test or None)
            run.result = {
                "status": res.status, "summary": res.summary, "patch": res.patch, "error": res.error,
                "elapsed_s": round(res.elapsed_s, 1), "usage": _plain(res.usage.as_dict() if hasattr(res.usage, "as_dict") else res.usage),
                "run_dir": str(res.run_dir or ""), "repo": str(repo), "model": res.model, "provider": res.provider,
                "attempts": len(res.attempts),
                "checks": [{"command": c.command, "origin": c.origin, "verdict": c.verdict,
                            "before": _plain(c.before), "after": _plain(c.after)} for c in (res.verification.checks if res.verification else [])],
                "strength": res.verification.strength if res.verification else "none",
            }
            run.status = "done"
            self._finish(run)
            run.push("done", {"status": res.status})
        except SystemExit as e:
            run.status, run.result = "error", {"status": "error", "error": str(e)}
            self._finish(run)
            run.push("done", {"status": "error", "error": str(e)})
        except Exception as e:  # noqa: BLE001 - the app shows the problem instead of dying
            msg = f"{type(e).__name__}: {e}"
            run.status, run.result = "error", {"status": "error", "error": msg[:800], "trace": traceback.format_exc()[-4000:]}
            self._finish(run)
            run.push("done", {"status": "error", "error": msg[:800]})

    def _finish(self, run: Run) -> None:
        """result.json in the run folder + one line in the history index."""
        run.save("result.json", run.result)
        r = run.result or {}
        line = {"id": run.id, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "title": run.title, "repo": run.repo_spec,
                "prompt": run.prompt[:500], "model": run.model, "status": r.get("status"), "elapsed_s": r.get("elapsed_s"),
                "tokens": (r.get("usage") or {}).get("total_tokens"), "calls": (r.get("usage") or {}).get("calls"),
                "evidence": r.get("run_dir"), "error": (r.get("error") or "")[:300]}
        with open(self.home / "index.jsonl", "a") as fh:
            fh.write(json.dumps(line) + "\n")

    def _auto_pr(self, run: Run) -> None:
        p = self.github_preview(run, "pr")
        if p.get("ok"):
            self.github_create(run, {"kind": "pr", "title": p["title"], "body": p["body"],
                                     "branch": p.get("branch"), "files": p.get("files")})
        else:
            rec = {"kind": "pr", "ok": False, "error": p.get("error", "not available"), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            run.github.append(rec)
            run.save("github.json", run.github)
            run.push("github", rec)

    # ------------------------------------------------------------------ stop
    def stop_run(self, run: Run) -> None:
        run.cancelled = True
        backend = getattr(getattr(run, "model_obj", None), "backend", None)
        if backend is not None and hasattr(backend, "cancel"):
            backend.cancel.set()
        run.push("log", {"level": "warn", "message": "stop requested: ending after the current operation"})

    def stop_batch(self, batch: "Batch") -> None:
        batch.cancelled = True
        for it in batch.items:
            run = self.runs.get(it["run_id"]) if it["run_id"] else None
            if run is not None and run.status not in ("done", "error"):
                self.stop_run(run)
            elif not it["run_id"]:
                it["status"] = "cancelled"
        batch.save(self.runs)

    def stop_all(self) -> Dict[str, int]:
        """The big red button: cancel every batch and run, abort model calls, kill running commands."""
        from ..tools.shell import kill_all_commands
        batches = runs = 0
        for b in list(self.batches.values()):
            if b.status not in ("done", "stopped") and not str(b.status).startswith("error"):
                self.stop_batch(b)
                batches += 1
        for run in list(self.runs.values()):
            if run.status not in ("done", "error", "interrupted"):
                self.stop_run(run)
                runs += 1
        killed = kill_all_commands()
        return {"batches": batches, "runs": runs, "commands_killed": killed}

    # ------------------------------------------------------------------ GitHub
    def github_preview(self, run: Run, kind: str) -> Dict[str, Any]:
        from . import github
        r = run.result or {}
        return github.preview(kind, r.get("repo") or self._repo_path(run), r, run.prompt, run.title)

    def github_create(self, run: Run, body: Dict[str, Any]) -> Dict[str, Any]:
        from . import github
        kind = body.get("kind", "pr")
        r = run.result or {}
        out = github.create(kind, r.get("repo") or self._repo_path(run), body.get("title") or run.title,
                            body.get("body") or "", body.get("branch") or r.get("branch") or "", body.get("files") or [],
                            committed=bool(r.get("branch")))
        rec = {"kind": kind, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **out}
        run.github.append(rec)
        run.save("github.json", run.github)
        run.push("github", rec)
        return out

    @staticmethod
    def _repo_path(run: Run) -> str:
        for ev in reversed(run.events):
            if ev.get("kind") == "stage" and ev.get("repo"):
                return ev["repo"]
        return ""

    def demos(self) -> List[Dict[str, str]]:
        out = []
        for d in sorted(DEMOS.glob("*")) if DEMOS.exists() else []:
            t = d / "task.json"
            if (d / "repo").exists() and (d / "issue.md").exists():
                meta = json.loads(t.read_text()) if t.exists() else {}
                out.append({"id": d.name, "title": meta.get("title") or d.name, "language": meta.get("language", "")})
        return out

    def start_demo(self, demo_id: str) -> Run:
        src = DEMOS / demo_id
        if not re.fullmatch(r"[\w.-]+", demo_id) or not (src / "repo").exists():
            raise ValueError("unknown demo")
        dest = Path(self.config().workspace_dir) / "demos" / f"{demo_id}-{time.strftime('%H%M%S')}"
        shutil.copytree(src / "repo", dest)
        env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@localhost",
                   GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@localhost")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "initial"]):
            subprocess.run(cmd, cwd=dest, env=env, capture_output=True)
        return self.start((src / "issue.md").read_text(), str(dest), "", want_pr=False)


def make_handler(studio: Studio):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[Dict[str, str]] = None):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200):
            self._send(code, json.dumps(obj).encode())

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):  # noqa: N802
            u = urlsplit(self.path)
            p = u.path
            if p in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                f = (STATIC / p[len("/static/"):]).resolve()
                if f.is_relative_to(STATIC) and f.is_file():
                    ctype = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
                    return self._send(200, f.read_bytes(), ctype)
                return self._send(404, b"not found", "text/plain")
            if p == "/api/model":
                return self._json(studio.model_info())
            if p == "/api/models":
                return self._json(studio.models())
            if p == "/api/demos":
                return self._json(studio.demos())
            if p == "/api/runs":
                return self._json([r.summary() for r in sorted(studio.runs.values(), key=lambda r: -r.created)])
            if p == "/api/batches":
                return self._json([b.view(studio.runs) for b in sorted(studio.batches.values(), key=lambda b: -b.created)])
            mb = re.fullmatch(r"/api/batches/([\w-]+)", p)
            if mb:
                bt = studio.batches.get(mb.group(1))
                return self._json(bt.view(studio.runs) if bt else {"error": "no such batch"}, 200 if bt else 404)
            m = re.fullmatch(r"/api/runs/([\w-]+)(/events|/patch|/report|/github)?", p)
            if m:
                run = studio.runs.get(m.group(1))
                if not run:
                    return self._json({"error": "no such run"}, 404)
                sub = m.group(2)
                if sub == "/github":
                    return self._json(studio.github_preview(run, (parse_qs(u.query).get("kind") or ["pr"])[0]))
                if sub == "/events":
                    run.load_events()
                    return self._stream(run, int((parse_qs(u.query).get("since") or ["0"])[0]))
                if sub == "/patch":
                    return self._send(200, ((run.result or {}).get("patch") or "").encode(), "text/x-diff",
                                      {"content-disposition": f'attachment; filename="pramana-{run.id}.diff"'})
                if sub == "/report":
                    rd = (run.result or {}).get("run_dir")
                    f = Path(rd) / "report.html" if rd else None
                    if f and f.exists():
                        return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                    return self._send(404, b"report not ready", "text/plain")
                return self._json({**run.summary(), "prompt": run.prompt, "test": run.test, "result": run.result})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            p = urlsplit(self.path).path
            b = self._body()
            try:
                if p == "/api/runs":
                    if not b.get("as_text"):
                        pick = studio.issues_for(b.get("prompt", ""), b.get("repo", ""))
                        if pick and pick.get("select_issues"):
                            return self._json(pick)
                        if pick and pick.get("single"):
                            run = studio.start(pick["single"], pick["repo"], b.get("test", ""), bool(b.get("want_pr")), bool(b.get("auto_pr")))
                            return self._json({"id": run.id})
                    run = studio.start(b.get("prompt", ""), b.get("repo", ""), b.get("test", ""), bool(b.get("want_pr")), bool(b.get("auto_pr")))
                    return self._json({"id": run.id})
                if p == "/api/batch":
                    batch = studio.start_batch(b.get("repo", ""), [int(n) for n in b.get("numbers") or []], bool(b.get("want_pr")),
                                               bool(b.get("auto_pr")))
                    return self._json({"id": batch.id})
                if p == "/api/stop-all":
                    return self._json(studio.stop_all())
                m = re.fullmatch(r"/api/batches/([\w-]+)/stop", p)
                if m and m.group(1) in studio.batches:
                    studio.stop_batch(studio.batches[m.group(1)])
                    return self._json({"ok": True})
                m = re.fullmatch(r"/api/runs/([\w-]+)/(github|flags|stop)", p)
                if m and m.group(1) in studio.runs:
                    run = studio.runs[m.group(1)]
                    if m.group(2) == "stop":
                        studio.stop_run(run)
                        return self._json({"ok": True})
                    if m.group(2) == "flags":
                        run.want_pr = bool(b.get("want_pr"))
                        return self._json({"want_pr": run.want_pr})
                    return self._json(studio.github_create(run, b))
                if p == "/api/demo":
                    return self._json({"id": studio.start_demo(b.get("id", "")).id})
                if p == "/api/model":
                    return self._json(studio.set_model(b))
                if p == "/api/model/test":
                    return self._json(check_model(studio))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._send(404, b"not found", "text/plain")

        def _stream(self, run: Run, since: int):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "close")
            self.end_headers()
            i = since
            try:
                while True:
                    with run.cond:
                        while i >= len(run.events) and run.status not in ("done", "error", "interrupted"):
                            run.cond.wait(timeout=15)
                            if i >= len(run.events):
                                break
                        batch = run.events[i:]
                    if batch:
                        for ev in batch:
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        i += len(batch)
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    if run.status in ("done", "error", "interrupted") and i >= len(run.events):
                        return
            except (BrokenPipeError, ConnectionResetError):
                return

    return H


def check_model(studio: Studio) -> Dict[str, Any]:
    from ..llm import build_model
    t0 = time.time()
    try:
        model = build_model(studio.config())
        resp = model.chat([{"role": "user", "content": "Reply with the single word: ready"}], tools=None)
        return {"ok": True, "seconds": round(time.time() - t0, 1), "reply": (resp.text or "")[:40]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


def open_window(url: str) -> None:
    """Open the app as its own window (Chrome/Edge/Chromium app mode), else the default browser."""
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser", "Arc"):
            if Path(f"/Applications/{app}.app").exists() or Path.home().joinpath(f"Applications/{app}.app").exists():
                if app != "Arc":
                    subprocess.Popen(["open", "-na", app, "--args", f"--app={url}", "--window-size=1320,900"])
                    return
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            if shutil.which(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    webbrowser.open(url)


def has_display() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def start(port: int = 8765, overrides: Optional[Dict[str, Any]] = None):
    """Serve the Studio on a background thread; returns (url, studio, httpd). Used by the Mac app."""
    studio = Studio(overrides)
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(studio))
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"no free port between {port} and {port + 19}")
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", studio, httpd


def serve(port: int = 8765, open_app: bool = True, overrides: Optional[Dict[str, Any]] = None) -> None:
    studio = Studio(overrides)
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(studio))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port between {port} and {port + 19}")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    info = studio.model_info()
    print(f"\n  Pramana Studio is running at {url}")
    print(f"  model: {info.get('model')} via {info.get('provider')}  ·  key: {info.get('key_source')}")
    print("  (Ctrl-C to stop)\n", flush=True)
    if open_app:
        threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
