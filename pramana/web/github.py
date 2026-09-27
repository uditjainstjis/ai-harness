"""Real GitHub actions for a finished run: open a pull request with the fix, or file an issue.

Uses the GitHub CLI (`gh`) that the user is already logged in with. Nothing is created without an
explicit request from the app; `preview` only reads. When the user cannot push to the repository,
the fix goes to their fork (created if needed) and the pull request targets the original.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..repo.issue import repo_slug_from_remote


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 180) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd[:3])} timed out"


def gh_user() -> Optional[str]:
    if not shutil.which("gh"):
        return None
    code, out = _run(["gh", "api", "user", "--jq", ".login"], timeout=30)
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else None


def changed_files(patch: str) -> List[str]:
    files = re.findall(r"^diff --git a/.* b/(.+)$", patch or "", flags=re.M)
    return [f for f in files if not f.startswith(".pramana/")]


def _slug(repo: Path) -> str:
    slug = repo_slug_from_remote(repo) if repo and (repo / ".git").exists() else ""
    return slug or ""


def pr_body(result: Dict[str, Any], prompt: str) -> str:
    lines = ["## What this fixes", "", (result.get("summary") or prompt or "").strip(), ""]
    checks = result.get("checks") or []
    if checks:
        lines += ["## Evidence", "", "Every check was run on the original code and on the patched code:", "",
                  "| check | original → patched |", "|---|---|"]
        label = {"fixes": "fails → **passes**", "passes_both": "passes → passes", "regression": "passes → FAILS"}
        for c in checks:
            cmd = (c.get("command") or "").replace("|", "\\|")
            lines.append(f"| `{cmd[:110]}` | {label.get(c.get('verdict'), c.get('verdict'))} |")
        lines.append("")
    model = f"{result.get('model')} via {result.get('provider')}" if result.get("model") else ""
    lines += ["---", f"Prepared with Pramana{' (' + model + ')' if model else ''}: the fix was reproduced, changed and "
              "verified on the original and the patched code before this pull request was opened."]
    return "\n".join(lines)


def issue_body(result: Dict[str, Any], prompt: str) -> str:
    lines = ["## Report", "", prompt.strip(), ""]
    if result.get("summary"):
        lines += ["## What Pramana found", "", result["summary"].strip(), ""]
    if result.get("patch"):
        lines += ["## Proposed change", "", "```diff", result["patch"][:12000], "```", ""]
    lines += ["---", "Filed from Pramana."]
    return "\n".join(lines)


def preview(kind: str, repo_path: str, result: Dict[str, Any], prompt: str, title_hint: str) -> Dict[str, Any]:
    """What would be created, where. Read-only."""
    out = _preview(kind, repo_path, result, prompt, title_hint)
    if out.get("ok") and kind == "pr" and result.get("closes"):
        n = result["closes"]
        out["branch"] = result.get("branch") or out.get("branch")
        out["title"] = out["title"] if out["title"].lower().startswith(("fix #", f"#{n}")) else f"Fix #{n}: {out['title'].lstrip('#0123456789 ')}"
        out["body"] = f"Closes #{n}\n\n" + out.get("body", "")
    return out


def _preview(kind: str, repo_path: str, result: Dict[str, Any], prompt: str, title_hint: str) -> Dict[str, Any]:
    repo = Path(repo_path) if repo_path else None
    out: Dict[str, Any] = {"kind": kind, "ok": False}
    user = gh_user()
    if not user:
        out["error"] = "GitHub CLI not logged in. Run `gh auth login` once in a terminal, then try again."
        return out
    slug = _slug(repo) if repo else ""
    if not slug:
        out["error"] = "This repository is not on GitHub (no github.com remote), so there is nothing to open a pull request or issue on. Download the patch instead."
        return out
    code, info = _run(["gh", "api", f"repos/{slug}", "--jq", "{b: .default_branch, push: .permissions.push, fork: .fork}"], timeout=30)
    meta = json.loads(info) if code == 0 and info.startswith("{") else {}
    title = (title_hint or "").strip() or (result.get("summary") or prompt or "Fix").strip().split("\n")[0]
    title = re.sub(r"\s+", " ", title)[:90]
    out.update({"ok": True, "user": user, "repo": slug, "base": meta.get("b") or "main", "can_push": bool(meta.get("push")), "title": title})
    if kind == "pr":
        files = changed_files(result.get("patch") or "")
        if not files:
            out.update({"ok": False, "error": "There is no change to put in a pull request yet."})
            return out
        branch = "pramana/" + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "fix")
        out.update({"branch": branch, "files": files, "body": pr_body(result, prompt),
                    "push_to": slug if meta.get("push") else f"{user}/{slug.split('/')[1]} (your fork — created if needed)"})
    else:
        out["body"] = issue_body(result, prompt)
    return out


def create(kind: str, repo_path: str, title: str, body: str, branch: str = "", files: Optional[List[str]] = None,
           committed: bool = False) -> Dict[str, Any]:
    """Actually create the pull request / issue. Returns {ok, url} or {ok: False, error}."""
    repo = Path(repo_path)
    slug = _slug(repo)
    user = gh_user()
    if not (slug and user):
        return {"ok": False, "error": "GitHub is not available for this repository."}
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
        body_file = fh.name
    try:
        if kind == "issue":
            code, out = _run(["gh", "issue", "create", "--repo", slug, "--title", title, "--body-file", body_file], timeout=90)
            url = next((l for l in out.splitlines() if l.startswith("https://github.com/")), "")
            return {"ok": code == 0 and bool(url), "url": url, "error": "" if code == 0 else out[-600:]}
        # --- pull request: branch, commit only the fix, push (origin or fork), open the PR
        _, info = _run(["gh", "api", f"repos/{slug}", "--jq", "{b: .default_branch, push: .permissions.push}"], timeout=30)
        meta = json.loads(info) if info.startswith("{") else {}
        base, can_push = meta.get("b") or "main", bool(meta.get("push"))
        branch = branch or "pramana/fix"
        git = ["git", "-c", f"user.name={user}", "-c", f"user.email={user}@users.noreply.github.com"]
        if not committed:   # (a batch has already committed this issue's fix on its own branch)
            code, out = _run(git + ["checkout", "-B", branch], cwd=repo)
            if code != 0:
                return {"ok": False, "error": "could not create the branch: " + out[-400:]}
            for f in files or []:
                _run(["git", "add", "--", f], cwd=repo)
            code, out = _run(git + ["commit", "-m", title, "-m", "Prepared with Pramana; verified on the original and the patched code."], cwd=repo)
            if code != 0 and "nothing to commit" not in out:
                return {"ok": False, "error": "could not commit: " + out[-400:]}
        owner = slug.split("/")[0]
        if can_push:
            remote = f"https://github.com/{slug}.git"
        else:
            _run(["gh", "repo", "fork", slug, "--clone=false", "--remote=false"], timeout=120)
            remote = f"https://github.com/{user}/{slug.split('/')[1]}.git"
            owner = user
        src = f"refs/heads/{branch}" if committed else "HEAD"
        push = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", "-f", remote, f"{src}:refs/heads/{branch}"]
        code, out = _run(push, cwd=repo, timeout=300)
        if code != 0 and "shallow" in out.lower():
            _run(["git", "fetch", "--unshallow", "--quiet"], cwd=repo, timeout=900)
            code, out = _run(push, cwd=repo, timeout=300)
        if code != 0:
            return {"ok": False, "error": "push failed: " + out[-600:]}
        code, out = _run(["gh", "pr", "create", "--repo", slug, "--base", base, "--head", f"{owner}:{branch}",
                          "--title", title, "--body-file", body_file], timeout=120)
        url = next((l for l in out.splitlines() if l.startswith("https://github.com/")), "")
        return {"ok": code == 0 and bool(url), "url": url, "error": "" if code == 0 else out[-600:]}
    finally:
        Path(body_file).unlink(missing_ok=True)
