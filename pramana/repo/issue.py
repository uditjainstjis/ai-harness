"""Issue intake: GitHub URL, owner/repo#N, local file (markdown / text / SWE-bench JSON) or raw
text. Also clones a repository given as a URL into the harness workspace."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import httpx

GH_ISSUE_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


@dataclass
class Issue:
    title: str
    body: str
    url: str = ""
    number: Optional[int] = None
    repo_slug: str = ""  # owner/name
    labels: List[str] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def render(self, max_chars: int = 16000) -> str:
        parts = []
        head = f"#{self.number} " if self.number else ""
        parts.append(f"Title: {head}{self.title}".rstrip())
        if self.url:
            parts.append(f"URL: {self.url}")
        if self.labels:
            parts.append("Labels: " + ", ".join(self.labels))
        parts.append("")
        parts.append(self.body.strip() or "(no description)")
        if self.comments:
            parts.append("\n--- Discussion (most relevant comments) ---")
            for c in self.comments:
                parts.append(c.strip())
                parts.append("---")
        text = condense("\n".join(parts))
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[... issue text truncated ...]"
        return text


FENCE_BLOCK_RE = re.compile(r"(```[^\n]*\n)(.*?)(```)", re.S)
PKG_LINE_RE = re.compile(r"^\s*[A-Za-z0-9_.\-\[\]]+\s+v?\d+(\.[\w\-+]+)*\s*$")


def _elide_lines(lines, keep_head: int, keep_tail: int, what: str):
    if len(lines) <= keep_head + keep_tail + 4:
        return lines
    return lines[:keep_head] + [f"[... {len(lines) - keep_head - keep_tail} lines of {what} elided by the harness ...]"] + lines[-keep_tail:]


def condense(text: str, block_limit: int = 1800) -> str:
    """Keep the issue's prose and short code intact; shorten long pasted outputs (package lists, logs,
    huge tracebacks) to head + tail. They are re-sent on every model turn."""
    def shrink_block(m):
        body = m.group(2)
        if len(body) <= block_limit:
            return m.group(0)
        lines = body.split("\n")
        pkg = sum(1 for l in lines if PKG_LINE_RE.match(l))
        what = "package list" if pkg > len(lines) * 0.5 else "output"
        return m.group(1) + "\n".join(_elide_lines(lines, 25, 15, what)) + "\n" + m.group(3)

    text = FENCE_BLOCK_RE.sub(shrink_block, text)
    # unfenced runs of `package version` lines (pip list / pip freeze pasted without a fence)
    out, run = [], []
    for line in text.split("\n") + [None]:
        if line is not None and PKG_LINE_RE.match(line):
            run.append(line)
            continue
        if run:
            out.extend(_elide_lines(run, 3, 2, "package list") if len(run) > 12 else run)
            run = []
        if line is not None:
            out.append(line)
    return "\n".join(out)


def _gh_token() -> str:
    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        return tok
    if shutil.which("gh"):  # the evaluator's machine may already be logged in to the gh CLI
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                return p.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "pramana-harness"}
    tok = _gh_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


EMBEDDED_RE = re.compile(r'<script type="application/json" data-target="react-app\.embeddedData">(.*?)</script>', re.S)


def _issue_from_html(owner: str, repo: str, number: int) -> Optional["Issue"]:
    """Last resort when the API is rate-limited: read the public issue page."""
    url = f"https://github.com/{owner}/{repo}/issues/{number}"
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 pramana"}) as c:
            r = c.get(url)
        if r.status_code != 200:
            return None
        for m in EMBEDDED_RE.finditer(r.text):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            payload = data.get("payload") or {}
            issue = payload.get("preloadedQuery", {}).get("response", {}).get("data", {}).get("repository", {}).get("issue") if isinstance(payload.get("preloadedQuery"), dict) else None
            issue = issue or payload.get("issue") or payload.get("preloadedIssue")
            if isinstance(issue, dict) and (issue.get("body") or issue.get("bodyText") or issue.get("title")):
                body = issue.get("body") or issue.get("bodyText") or ""
                return Issue(title=issue.get("title", ""), body=body, url=url, number=number, repo_slug=f"{owner}/{repo}")
        # plain fallback: <title> + the first comment's markdown body
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        body = re.search(r'<td class="d-block comment-body markdown-body[^"]*">(.*?)</td>', r.text, re.S)
        if title:
            text = re.sub(r"<[^>]+>", "", body.group(1)) if body else ""
            name = html.unescape(title.group(1)).split(" · ")[0].strip()
            if text.strip():
                return Issue(title=name, body=html.unescape(text).strip(), url=url, number=number, repo_slug=f"{owner}/{repo}")
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_github_issue(owner: str, repo: str, number: int, max_comments: int = 6) -> Issue:
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    with httpx.Client(timeout=30, headers=_gh_headers(), follow_redirects=True) as c:
        r = c.get(base)
        if r.status_code != 200:
            alt = _issue_from_html(owner, repo, number)
            if alt is not None:
                return alt
            hint = ""
            if r.status_code in (403, 429) and "rate limit" in r.text.lower():
                hint = ("\nGitHub rate-limits unauthenticated requests. Export GITHUB_TOKEN (any read-only token), "
                        "run `gh auth login`, or paste the issue text / a file path instead of the URL.")
            elif r.status_code == 404:
                hint = "\nCheck the URL (a private repository needs GITHUB_TOKEN)."
            raise RuntimeError(f"could not fetch {owner}/{repo}#{number}: GitHub returned {r.status_code}.{hint}")
        d = r.json()
        comments: List[str] = []
        if d.get("comments"):
            rc = c.get(base + "/comments", params={"per_page": 30})
            if rc.status_code == 200:
                for cm in rc.json()[:max_comments]:
                    body = (cm.get("body") or "").strip()
                    if body:
                        comments.append(f"@{(cm.get('user') or {}).get('login', '?')}: {body[:3000]}")
    return Issue(
        title=d.get("title", ""),
        body=d.get("body") or "",
        url=d.get("html_url", f"https://github.com/{owner}/{repo}/issues/{number}"),
        number=number,
        repo_slug=f"{owner}/{repo}",
        labels=[l.get("name", "") for l in d.get("labels", []) if isinstance(l, dict)],
        comments=comments,
    )


def parse_issue(spec: str, default_slug: str = "") -> Issue:
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty issue")
    m = GH_ISSUE_RE.search(spec)
    if m and len(spec) < 300 and "\n" not in spec.strip():
        return fetch_github_issue(m.group(1), m.group(2), int(m.group(3)))
    m = SHORT_RE.match(spec)
    if m:
        return fetch_github_issue(m.group(1), m.group(2), int(m.group(3)))
    if re.fullmatch(r"#?\d+", spec) and default_slug:
        owner, repo = default_slug.split("/", 1)
        return fetch_github_issue(owner, repo, int(spec.lstrip("#")))
    if "\n" not in spec and len(spec) < 1024:
        p = Path(spec).expanduser()
        if p.is_file():
            return issue_from_file(p)
    return issue_from_text(spec)


def issue_from_file(p: Path) -> Issue:
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".json":
        try:
            d = json.loads(text)
            if isinstance(d, dict) and ("problem_statement" in d or "body" in d):
                body = d.get("problem_statement") or d.get("body") or ""
                title = d.get("title") or body.strip().splitlines()[0][:120] if body.strip() else "issue"
                return Issue(title=title, body=body, repo_slug=d.get("repo", ""), extra=d)
        except ValueError:
            pass
    return issue_from_text(text)


def issue_from_text(text: str) -> Issue:
    text = text.strip()
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip()[:200] if lines else "issue"
    return Issue(title=title, body=text)


def repo_slug_from_remote(root: Path) -> str:
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(root), capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def ensure_repo(spec: str, workspace: Path, ref: str = "") -> Path:
    """Local path -> itself. URL or owner/name -> clone into the workspace (blobless, fast)."""
    spec = spec.strip()
    p = Path(spec).expanduser()
    if p.exists():
        return p.resolve()
    url = spec
    looks_remote = spec.startswith(("http://", "https://", "git@", "ssh://")) or re.fullmatch(r"[\w.-]+/[\w.-]+", spec)
    if not looks_remote:
        raise FileNotFoundError(f"repository path does not exist: {spec}")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", spec):
        url = f"https://github.com/{spec}.git"
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    name = f"{m.group(1)}__{m.group(2)}" if m else re.sub(r"\W+", "_", url)[-60:]
    workspace.mkdir(parents=True, exist_ok=True)
    dest = workspace / name
    if dest.exists():
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(dest), capture_output=True, text=True).stdout.strip()
        if not dirty:
            if ref:
                subprocess.run(["git", "checkout", "-q", ref], cwd=str(dest), check=False)
            return dest
        i = 2
        while (workspace / f"{name}_{i}").exists():
            i += 1
        dest = workspace / f"{name}_{i}"
    r = subprocess.run(["git", "clone", "-q", "--filter=blob:none", url, str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["git", "clone", "-q", url, str(dest)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed for {url}: {r.stderr[:300]}")
    if ref:
        subprocess.run(["git", "checkout", "-q", ref], cwd=str(dest), check=False)
    return dest


def list_github_issues(slug: str, state: str = "open", limit: int = 60) -> List[Dict[str, Any]]:
    """Open issues of a GitHub repository (pull requests excluded), newest first."""
    out: List[Dict[str, Any]] = []
    with httpx.Client(timeout=30, headers=_gh_headers(), follow_redirects=True) as c:
        r = c.get(f"https://api.github.com/repos/{slug}/issues", params={"state": state, "per_page": min(limit, 100)})
        if r.status_code != 200:
            raise RuntimeError(f"could not list the issues of {slug}: GitHub returned {r.status_code}")
        for d in r.json():
            if "pull_request" in d:
                continue
            out.append({"number": d.get("number"), "title": d.get("title", ""), "url": d.get("html_url", ""),
                        "labels": [l.get("name", "") for l in d.get("labels", []) if isinstance(l, dict)],
                        "body": (d.get("body") or "")[:600], "comments": d.get("comments", 0)})
    return out
