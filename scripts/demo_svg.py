"""Record a real run of one bundled task and export terminal screenshots (SVG) for docs/.

usage: .venv/bin/python scripts/demo_svg.py [task-name]
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pramana import cli  # noqa: E402
from pramana.agent.events import Events  # noqa: E402
from pramana.agent.orchestrator import Orchestrator  # noqa: E402
from pramana.bench import ROOT, grade, prepare  # noqa: E402
from pramana.config import load_config  # noqa: E402
from pramana.repo.issue import issue_from_file  # noqa: E402
from pramana.ui.live import LiveView  # noqa: E402

import json  # noqa: E402


def main() -> None:
    task = sys.argv[1] if len(sys.argv) > 1 else "config-merge"
    cfg = load_config()
    task_dir = ROOT / "bench" / "tasks" / task
    repo = prepare(task_dir, Path(cfg.runs_dir) / ".bench-cache")
    issue = issue_from_file(task_dir / "issue.md")
    rec = Console(record=True, width=118, force_terminal=True, color_system="truecolor")
    events = Events()
    view = LiveView(rec, plain=True)
    events.subscribe(view)
    res = Orchestrator(cfg, events).solve(repo, issue)
    cli.console = rec
    cli.show_result(res)
    g = grade(task_dir, repo, json.loads((task_dir / "task.json").read_text()))
    rec.print(f"[bold]hidden tests after the run:[/] {'[green]PASS[/]' if g['resolved'] else '[red]FAIL[/]'}")
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    rec.save_svg(str(out / f"demo-{task}.svg"), title=f"pramana · {task}")
    panel_console = Console(record=True, width=118, force_terminal=True, color_system="truecolor")
    panel_console.print(view.render())
    panel_console.save_svg(str(out / f"panel-{task}.svg"), title="pramana live view")
    print(out / f"demo-{task}.svg", res.status, g["resolved"])


if __name__ == "__main__":
    main()
