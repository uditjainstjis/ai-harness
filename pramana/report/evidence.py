"""Evidence bundle: every run leaves a self-contained, reviewable record.

runs/<id>/
  report.md        human summary: verdict, root cause, before/after table, diffstat, cost
  report.html      the same, styled, with the diff
  patch.diff       the change (git apply-able)
  evidence.json    machine-readable facts (checks, usage, timings, attempts)
  issue.md         the issue as the agent saw it
  transcript_attemptN.json   full model conversation per attempt
  trajectory.jsonl event log (every tool call, result, model call)
  scratch/         the agent's reproduction scripts
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List

from ..agent.verify import VERDICT_ICON, render_checks

STATUS_TEXT = {
    "verified": "VERIFIED FIX - a check fails on the original code and passes with the patch; no regressions.",
    "patched": "PATCH WITHOUT PROOF - a change was made, but no check demonstrates it (see the table).",
    "no_patch": "NO PATCH - the agent could not produce a change.",
    "error": "ERROR - the run did not complete.",
}


def _diffstat(patch: str):
    files = added = removed = 0
    names: List[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            files += 1
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                names.append(parts[1])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return files, added, removed, names


def evidence_dict(result, cfg) -> Dict[str, Any]:
    v = result.verification
    files, added, removed, names = _diffstat(result.patch or "")
    return {
        "status": result.status,
        "status_text": STATUS_TEXT.get(result.status, result.status),
        "error": result.error,
        "issue": {"title": result.issue.title if result.issue else "", "url": result.issue.url if result.issue else ""},
        "repo": str(result.repo.root) if result.repo else "",
        "model": result.model,
        "provider": result.provider,
        "best_attempt": result.best,
        "summary": result.summary,
        "patch": {"files": files, "added": added, "removed": removed, "paths": names},
        "verification": v.as_dict() if v else None,
        "attempts": [
            {
                "number": a.number,
                "steps": a.steps,
                "stop_reason": a.stop_reason,
                "strength": a.strength,
                "elapsed_s": a.elapsed_s,
                "usage": a.usage.as_dict(),
                "tool_counts": a.tool_counts,
                "review": a.review,
                "patch_chars": len(a.patch),
            }
            for a in result.attempts
        ],
        "usage": result.usage.as_dict(),
        "elapsed_s": result.elapsed_s,
        "localization_hints": result.hints,
        "config": cfg.describe(),
    }


def render_markdown(result, ev: Dict[str, Any]) -> str:
    v = result.verification
    u = ev["usage"]
    lines = [
        f"# Pramana run: {ev['issue']['title']}",
        "",
        f"**Result:** {ev['status_text']}",
        "",
        f"- Repository: `{ev['repo']}`",
        f"- Model: `{ev['model']}` via `{ev['provider']}`",
        f"- Attempts: {len(ev['attempts'])} (selected: {ev['best_attempt']})",
        f"- Cost: {u['total_tokens']:,} tokens ({u['input_tokens']:,} in, {u['cached_tokens']:,} cached, {u['output_tokens']:,} out) in {u['calls']} model calls"
        + (f", ${u['cost_usd']:.4f}" if u.get("cost_usd") else ""),
        f"- Wall time: {ev['elapsed_s']:.0f}s",
    ]
    if ev["error"]:
        lines += ["", f"**Error:** {ev['error']}"]
    if ev["summary"]:
        lines += ["", "## What changed and why", "", ev["summary"]]
    lines += ["", "## Evidence", ""]
    lines.append(render_checks(v.checks) if v else "(no verification ran)")
    if v and v.syntax_errors:
        lines += ["", "Syntax errors: " + "; ".join(v.syntax_errors)]
    p = ev["patch"]
    lines += ["", "## Patch", "", f"{p['files']} file(s), +{p['added']} -{p['removed']}: " + ", ".join(f"`{n}`" for n in p["paths"])]
    if result.patch:
        lines += ["", "```diff", result.patch.rstrip(), "```"]
    lines += ["", "## Attempts", "", "| # | steps | stop reason | evidence | tokens | time |", "|---|---|---|---|---|---|"]
    for a in ev["attempts"]:
        lines.append(f"| {a['number']} | {a['steps']} | {a['stop_reason']} | {a['strength']} | {a['usage']['total_tokens']:,} | {a['elapsed_s']:.0f}s |")
    lines += ["", "## Localization hints given to the agent", "", "```", result.hints or "(none)", "```"]
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#656d76;--card:#ffffff;--line:#e5e1d8;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--accent:#b4582c;}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15171a;--fg:#e6e6e6;--muted:#9aa4ae;--card:#1d2024;--line:#2f343a;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#e0875a;}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;margin:0;padding:24px 16px;}
main{max-width:980px;margin:0 auto;}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em}
.badge{display:inline-block;padding:6px 12px;border-radius:8px;font-weight:600;margin:10px 0}
.verified{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)} .patched{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}
.no_patch,.error{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0;overflow-x:auto}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kv div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kv b{display:block;font-size:20px} .kv span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
table{border-collapse:collapse;width:100%;font-size:13px} th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
code,pre{font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace} pre{margin:0;white-space:pre}
.add{color:var(--ok)} .del{color:var(--bad)} .hunk{color:var(--accent)}
.v-fixes{color:var(--ok);font-weight:600} .v-regression,.v-still_failing,.v-fails_after{color:var(--bad);font-weight:600}
"""


def render_html(result, ev: Dict[str, Any]) -> str:
    v = result.verification
    u = ev["usage"]
    esc = html.escape
    rows = ""
    if v:
        for c in v.checks:
            rows += (
                f"<tr><td class='v-{esc(c.verdict)}'>{esc(VERDICT_ICON.get(c.verdict, c.verdict))}</td><td>{esc(c.origin)}</td>"
                f"<td><code>{esc(c.command)}</code></td><td>{esc(c.before.summary if c.before else '-')}</td>"
                f"<td>{esc(c.after.summary if c.after else '-')}</td></tr>"
            )
    diff_html = ""
    for line in (result.patch or "").splitlines():
        cls = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else "hunk" if line.startswith("@@") else ""
        diff_html += f"<span class='{cls}'>{esc(line)}</span>\n"
    attempts = "".join(
        f"<tr><td>{a['number']}</td><td>{a['steps']}</td><td>{esc(a['stop_reason'])}</td><td>{esc(a['strength'])}</td>"
        f"<td>{a['usage']['total_tokens']:,}</td><td>{a['elapsed_s']:.0f}s</td></tr>"
        for a in ev["attempts"]
    )
    p = ev["patch"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pramana run report</title><style>{CSS}</style></head><body><main>
<h1>{esc(ev['issue']['title'] or 'Pramana run')}</h1>
<div class="badge {esc(ev['status'])}">{esc(ev['status_text'])}</div>
<div class="kv">
<div><span>tokens</span><b>{u['total_tokens']:,}</b></div>
<div><span>model calls</span><b>{u['calls']}</b></div>
<div><span>wall time</span><b>{ev['elapsed_s']:.0f}s</b></div>
<div><span>patch</span><b>+{p['added']} / -{p['removed']}</b></div>
<div><span>model</span><b style="font-size:14px">{esc(ev['model'])}</b></div>
</div>
{f"<h2>What changed and why</h2><div class='card'>{esc(ev['summary'])}</div>" if ev['summary'] else ''}
{f"<h2>Error</h2><div class='card'>{esc(ev['error'])}</div>" if ev['error'] else ''}
<h2>Evidence (each check run on the original code and on the patched code)</h2>
<div class="card"><table><tr><th>verdict</th><th>origin</th><th>command</th><th>original code</th><th>with patch</th></tr>{rows or '<tr><td colspan=5>no checks ran</td></tr>'}</table></div>
<h2>Patch</h2><div class="card"><pre>{diff_html or '(empty)'}</pre></div>
<h2>Attempts</h2><div class="card"><table><tr><th>#</th><th>steps</th><th>stop</th><th>evidence</th><th>tokens</th><th>time</th></tr>{attempts}</table></div>
<h2>Localization hints</h2><div class="card"><pre>{esc(result.hints or '(none)')}</pre></div>
</main></body></html>"""


def write_bundle(result, cfg) -> None:
    d: Path = result.run_dir
    d.mkdir(parents=True, exist_ok=True)
    ev = evidence_dict(result, cfg)
    (d / "evidence.json").write_text(json.dumps(ev, indent=2, default=str))
    (d / "patch.diff").write_text(result.patch or "")
    if result.issue:
        (d / "issue.md").write_text(result.issue.render())
    (d / "report.md").write_text(render_markdown(result, ev))
    (d / "report.html").write_text(render_html(result, ev))
    for a in result.attempts:
        msgs = [{k: v for k, v in m.items() if not k.startswith("_")} for m in a.messages]
        (d / f"transcript_attempt{a.number}.json").write_text(json.dumps(msgs, indent=1, default=str))
