"""Command line entry point.

  pramana run      interactive session (what `make run` launches): asks for a repository and an
                   issue (GitHub URL / owner/repo#N / file / pasted text), solves it, shows the
                   evidence, then waits for the next issue. Non-interactive when stdin is piped or
                   --issue is given.
  pramana solve    one-shot, scriptable (exit code 0 = verified fix, 1 = unverified patch, 2 = none)
  pramana doctor   configuration + model connectivity check
  pramana bench    run the bundled benchmark (see bench/)
"""
from __future__ import annotations

import argparse
import re
import json
import os
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import __version__
from .agent.events import Events
from .agent.orchestrator import Orchestrator, RunResult
from .config import Config, load_config
from .repo.issue import Issue, ensure_repo, parse_issue, repo_slug_from_remote
from .ui.live import ACCENT, LiveView

console = Console()


def banner(cfg: Config) -> None:
    key = "[green]set[/]" if cfg.api_key else ("[yellow]not needed[/]" if cfg.resolved_provider in ("ollama", "claude-cli", "mock") else "[red]MISSING (export AI_API_KEY)[/]")
    t = Text.assemble(
        ("Pramana", f"bold {ACCENT}"), (f" v{__version__} ", "dim"),
        ("· an autonomous coding agent whose patches carry their own proof\n", ""),
        ("model ", "dim"), (f"{cfg.model.name}", "bold"), (f"  via {cfg.resolved_provider}", "dim"),
        ("  · key ", "dim"),
    )
    t.append_text(Text.from_markup(key))
    console.print(Panel(t, border_style=ACCENT))


def looks_like_repo(spec: str) -> bool:
    spec = spec.strip()
    if not spec or "\n" in spec:
        return False
    if spec.startswith(("http://", "https://", "git@", "ssh://", "~", "/", "./", "../")):
        return True
    if re.fullmatch(r"[\w.-]+/[\w.-]+", spec):
        return True
    return Path(spec).expanduser().exists()


def read_multiline(prompt: str) -> str:
    console.print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() in ("END", "EOF"):
            break
        if not lines:
            s = line.strip()
            # single-line forms are accepted immediately
            if s.startswith(("http://", "https://")) or (s and Path(s).expanduser().is_file()) or (
                "/" in s and "#" in s and " " not in s
            ) or s.lstrip("#").isdigit():
                return s
        lines.append(line)
    return "\n".join(lines).strip()


def show_result(res: RunResult) -> None:
    style = {"verified": "bold green", "patched": "bold yellow", "no_patch": "bold red", "error": "bold red"}[res.status]
    label = {
        "verified": "VERIFIED FIX",
        "patched": "PATCH (unproven)",
        "no_patch": "NO PATCH",
        "error": "ERROR",
    }[res.status]
    console.print()
    console.rule(Text(f" {label} ", style=style), style=style)
    if res.error:
        console.print(Text(res.error, style="red"))
    if res.summary:
        console.print(Panel(res.summary, title="what changed and why", border_style="dim"))
    v = res.verification
    if v and v.checks:
        t = Table(title="evidence: each check run on the original and on the patched code", title_style="dim", expand=True)
        for col in ("verdict", "origin", "command", "original", "patched"):
            t.add_column(col, overflow="fold")
        for c in v.checks:
            color = "green" if c.verdict == "fixes" else "red" if c.verdict in ("regression", "still_failing", "fails_after") else "white"
            t.add_row(Text(c.verdict, style=color), c.origin, c.command[:80],
                      c.before.summary[:40] if c.before else "-", c.after.summary[:40] if c.after else "-")
        console.print(t)
    if res.patch.strip():
        patch = res.patch if len(res.patch) < 12000 else res.patch[:12000] + "\n... (truncated; see patch.diff)"
        console.print(Syntax(patch, "diff", theme="ansi_light", word_wrap=True))
    u = res.usage
    console.print(Text(
        f"tokens {u.total_tokens:,} (in {u.input_tokens:,} · cached {u.cached_tokens:,} · out {u.output_tokens:,}) · "
        f"{u.calls} model calls · {res.elapsed_s:.0f}s · attempts {len(res.attempts)}", style="dim"))
    if res.repo and res.patch.strip():
        console.print(Text.assemble(("the fix is applied in ", "dim"), (str(res.repo.root), "bold"), ("  (git diff to inspect)", "dim")))
    if res.run_dir:
        report = Path(res.run_dir) / "report.html"
        console.print(Text.assemble(("evidence bundle: ", "dim"), (str(res.run_dir), "bold"), ("  (report.md / report.html / patch.diff)", "dim")))
        if report.exists():
            console.print(Text.assemble(("open the report: ", "dim"), (report.resolve().as_uri(), f"link {report.resolve().as_uri()}")))


def solve_once(cfg: Config, repo_spec: str, issue_spec: str, acceptance: Optional[str], plain: bool = False) -> RunResult:
    workspace = Path(cfg.workspace_dir)
    slug_hint = ""
    issue: Optional[Issue] = None
    # an issue URL can also tell us which repository to use
    if not repo_spec:
        issue = parse_issue(issue_spec)
        if issue.repo_slug:
            repo_spec = issue.repo_slug
        else:
            raise SystemExit("No repository given. Pass --repo (path or git URL) or use a GitHub issue URL.")
    repo = ensure_repo(repo_spec, workspace)
    if str(repo.resolve()).startswith(str(workspace.resolve())):
        # we cloned it ourselves: make its tests runnable (never touch a user's own environment)
        from .repo.bootstrap import bootstrap

        with console.status("[dim]preparing the repository environment (dependencies, test runner)...[/]"):
            for note in bootstrap(repo, log=lambda m: console.print(f"[dim]  {escape(m)}[/]")):
                console.print(f"[dim]  env: {escape(note)}[/]")
    if issue is None:
        slug_hint = repo_slug_from_remote(repo)
        issue = parse_issue(issue_spec, default_slug=slug_hint)
    events = Events()
    view = LiveView(console, plain=plain)
    events.subscribe(view)
    with view:
        res = Orchestrator(cfg, events).solve(repo, issue, acceptance_cmd=acceptance or None)
    show_result(res)
    return res


def preflight(cfg: Config) -> bool:
    """One tiny model call so a bad key / model name fails in seconds, not after repo intake."""
    from .llm import build_model

    with console.status("[dim]checking the model connection...[/]"):
        try:
            model = build_model(cfg)
            resp = model.chat([{"role": "user", "content": "Reply with the single word: ready"}], tools=None)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]model check failed:[/] {escape(str(e)[:400])}")
            console.print("[dim]Check AI_API_KEY (and AI_MODEL / AI_BASE_URL or pramana.toml if the provider is not auto-detected).[/]")
            return False
    console.print(f"[green]✓[/] model reachable in {resp.latency_s:.1f}s")
    return True


def cmd_run(args) -> int:
    cfg = load_config(_overrides(args))
    banner(cfg)
    if not getattr(args, "skip_preflight", False) and not preflight(cfg):
        return 2
    interactive = sys.stdin.isatty() and not args.issue
    if not interactive:
        issue_spec = args.issue or sys.stdin.read()
        if not issue_spec.strip():
            console.print("[red]No issue given (pass --issue or pipe the issue text on stdin).[/]")
            return 2
        try:
            res = solve_once(cfg, args.repo or os.environ.get("REPO", ""), issue_spec, args.test, plain=args.plain)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:  # noqa: BLE001 - never show a raw traceback to an operator
            console.print(f"[red]error:[/] {type(e).__name__}: {escape(str(e)[:500])}")
            return 2
        return _exit_code(res)
    last_repo = args.repo or os.environ.get("REPO", "")
    while True:
        try:
            default = f" [{last_repo}]" if last_repo else ""
            repo = console.input(f"[bold]Repository[/] (local path or git URL; blank = infer from a GitHub issue URL){default}: ").strip() or last_repo
            issue_spec = ""
            if re.search(r"github\.com/[\w.-]+/[\w.-]+/(issues|pull)/\d+", repo) or re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", repo):
                # an issue link typed at the repository prompt: use it as the issue, infer the repo
                issue_spec, repo = repo, ""
                console.print("[dim]  treating that as the issue; the repository will be taken from it.[/]")
            elif repo and not looks_like_repo(repo):
                # issue text pasted at the repository prompt: keep it and ask for the repository after
                issue_spec, repo = repo, ""
                console.print("[dim]  that looks like issue text, not a repository.[/]")
                repo = console.input("[bold]Repository[/] (local path or git URL): ").strip()
            if not issue_spec:
                issue_spec = read_multiline(
                    "[bold]Issue[/]: paste a GitHub issue URL, owner/repo#N, a file path, or the issue text. "
                    "Finish multi-line text with a line containing only END (or Ctrl-D)."
                )
            if not issue_spec:
                console.print("[yellow]No issue entered.[/]")
                continue
            acceptance = console.input("[bold]Acceptance test command[/] (optional, Enter to skip): ").strip() or args.test
        except (KeyboardInterrupt, EOFError):
            console.print("\nbye.")
            return 0
        try:
            res = solve_once(cfg, repo, issue_spec, acceptance, plain=args.plain)
            last_repo = str(res.repo.root) if res.repo else repo
        except KeyboardInterrupt:
            console.print("\n[yellow]interrupted.[/]")
        except SystemExit as e:
            console.print(f"[red]{escape(str(e))}[/]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]error: {type(e).__name__}: {escape(str(e))}[/]")
        try:
            again = console.input("\nSolve another issue? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return 0
        if again not in ("y", "yes"):
            return 0


def cmd_solve(args) -> int:
    cfg = load_config(_overrides(args))
    if not args.issue:
        console.print("[red]--issue is required[/]")
        return 2
    try:
        res = solve_once(cfg, args.repo or "", args.issue, args.test, plain=args.plain)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]error:[/] {type(e).__name__}: {escape(str(e)[:500])}")
        return 2
    if args.json:
        print(json.dumps({"status": res.status, "run_dir": str(res.run_dir), "tokens": res.usage.total_tokens,
                          "elapsed_s": res.elapsed_s}))
    return _exit_code(res)


def cmd_doctor(args) -> int:
    cfg = load_config(_overrides(args))
    banner(cfg)
    t = Table(show_header=False)
    for k, v in cfg.describe().items():
        t.add_row(k, str(v))
    t.add_row("config file", str(cfg.config_path))
    import shutil as _sh

    for tool in ("git", "rg", "ctags", "node"):
        t.add_row(tool, "found" if _sh.which(tool) else "missing (optional)" if tool != "git" else "MISSING (required)")
    console.print(t)
    from .llm import build_model
    from .llm.base import ToolSpec

    try:
        model = build_model(cfg)
        resp = model.chat([{"role": "user", "content": "Reply with the single word: ready"}], tools=None)
        console.print(f"[green]model reachable[/] ({resp.latency_s:.1f}s): {resp.text[:60]!r}")
        spec = ToolSpec("echo", "Echo a message back.", {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]})
        resp = model.chat([{"role": "user", "content": "Call the echo tool with message 'hi'."}], tools=[spec])
        mode = model.effective_mode
        ok = bool(resp.tool_calls and resp.tool_calls[0].name == "echo")
        console.print(f"tool calling: [{'green' if ok else 'yellow'}]{mode} mode {'works' if ok else 'did not produce a call'}[/]")
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]model check failed: {escape(str(e))}[/]")
        return 1


def cmd_bench(args) -> int:
    from .bench import run_bench

    cfg = load_config(_overrides(args))
    return run_bench(cfg, args.suite, args.only, plain=args.plain)


def _exit_code(res: RunResult) -> int:
    return {"verified": 0, "patched": 1}.get(res.status, 2)


def _overrides(args) -> dict:
    o = {"model": {}, "agent": {}}
    if getattr(args, "model", None):
        o["model"]["name"] = args.model
    if getattr(args, "provider", None):
        o["model"]["provider"] = args.provider
    if getattr(args, "max_steps", None):
        o["agent"]["max_steps"] = args.max_steps
    if getattr(args, "attempts", None):
        o["agent"]["max_attempts"] = args.attempts
    if getattr(args, "no_review", False):
        o["agent"]["review"] = False
    return o


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pramana", description="Pramana: autonomous coding-agent harness with proof-carrying patches")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--repo", help="target repository: local path, git URL or owner/name")
        p.add_argument("--issue", help="GitHub issue URL, owner/repo#N, file path, or issue text")
        p.add_argument("--test", help="acceptance test command that must pass after the fix")
        p.add_argument("--model", help="override the model name")
        p.add_argument("--provider", help="override the provider")
        p.add_argument("--max-steps", type=int, dest="max_steps")
        p.add_argument("--attempts", type=int)
        p.add_argument("--no-review", action="store_true", dest="no_review")
        p.add_argument("--plain", action="store_true", help="plain log output instead of the live view")
        p.add_argument("--skip-preflight", action="store_true", dest="skip_preflight", help="skip the startup model check")

    p_run = sub.add_parser("run", help="interactive session (make run)")
    common(p_run)
    p_solve = sub.add_parser("solve", help="solve one issue non-interactively")
    common(p_solve)
    p_solve.add_argument("--json", action="store_true")
    p_doc = sub.add_parser("doctor", help="check configuration and model connectivity")
    p_doc.add_argument("--model")
    p_doc.add_argument("--provider")
    p_bench = sub.add_parser("bench", help="run the bundled benchmark")
    common(p_bench)
    p_bench.add_argument("--suite", default="mini")
    p_bench.add_argument("--only", nargs="*")
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["run"] + (argv or sys.argv[1:]))
    fn = {"run": cmd_run, "solve": cmd_solve, "doctor": cmd_doctor, "bench": cmd_bench}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
