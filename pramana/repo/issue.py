"""Issue intake: GitHub URL, owner/repo#N, local file (markdown / text / SWE-bench JSON) or raw
text. Also clones a repository given as a URL into the harness workspace."""
from __future__ import annotations

import json
import os
import re
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

    def render(self, max_chars: int = 24000) -> str:
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
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[... issue text truncated ...]"
        return text


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "pramana-harness"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_github_issue(owner: str, repo: str, number: int, max_comments: int = 6) -> Issue:
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    with httpx.Client(timeout=30, headers=_gh_headers(), follow_redirects=True) as c:
        r = c.get(base)
        if r.status_code != 200:
            raise RuntimeError(f"GitHub API returned {r.status_code} for {owner}/{repo}#{number}: {r.text[:200]}")
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
