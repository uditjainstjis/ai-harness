"""Live terminal dashboard (Rich). Falls back to plain log lines when stdout is not a TTY."""
from __future__ import annotations

import sys
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PHASES = ["intake", "localize", "reproduce", "fix", "verify", "review"]
ICONS = {"bash": "$", "str_replace_editor": "✎", "search": "⌕", "find_definition": "ƒ", "find_files": "▤", "submit": "✔"}
ACCENT = "#c2622d"


class LiveView:
    def __init__(self, console: Optional[Console] = None, plain: Optional[bool] = None) -> None:
        self.console = console or Console()
        self.plain = (not self.console.is_terminal) if plain is None else plain
        self.phase = ""
        self.done_phases: List[str] = []
        self.attempt = 1
        self.step = 0
        self.max_steps = 0
        self.tokens = 0
        self.cached = 0
        self.calls = 0
        self.t0 = time.time()
        self.feed: Deque[Text] = deque(maxlen=14)
        self.title = ""
        self.model = ""
        self.repo = ""
        self.hints: List[str] = []
        self.verdicts: List[Dict[str, Any]] = []
        self.status = "running"
        self._live: Optional[Live] = None
        self._pending: Dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> "LiveView":
        if not self.plain:
            self._live = Live(self.render(), console=self.console, refresh_per_second=8, transient=False)
            self._live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._live is not None:
            self._live.update(self.render())
            self._live.__exit__(*exc)
            self._live = None

    # ------------------------------------------------------------------ events
    def __call__(self, kind: str, d: Dict[str, Any]) -> None:
        line: Optional[Text] = None
        if kind == "run" and d.get("status") == "start":
            self.title, self.model, self.repo = d.get("issue", ""), f"{d.get('model')} ({d.get('provider')})", d.get("repo", "")
            line = Text(f"run → {d.get('run_dir')}", style="dim")
        elif kind == "phase":
            name = d.get("name", "")
            if name != self.phase:
                if self.phase and self.phase not in self.done_phases:
                    self.done_phases.append(self.phase)
                self.phase = name
        elif kind == "intake":
            line = Text(f"intake: {d.get('language')} · {d.get('files')} files · tests: {d.get('test_command') or '?'}", style="cyan")
            for n in d.get("notes") or []:
                self._push(Text(f"  note: {n}", style="yellow"))
        elif kind == "localized":
            self.hints = d.get("top") or []
            line = Text(f"localized in {d.get('seconds')}s → " + ", ".join(self.hints[:3]), style="cyan")
        elif kind == "attempt":
            self.attempt = d.get("attempt", 1)
            if d.get("status") == "start" and self.attempt > 1:
                self.done_phases = ["intake", "localize"]
                line = Text(f"── attempt {self.attempt} (fresh context, lessons from attempt {self.attempt - 1}) ──", style=f"bold {ACCENT}")
            elif d.get("status") == "end":
                line = Text(f"attempt {self.attempt} ended: {d.get('stop_reason')} · evidence {d.get('strength')} · {d.get('steps')} steps", style="bold")
        elif kind == "step":
            self.step, self.max_steps = d.get("step", 0), d.get("max_steps", 0)
        elif kind == "llm":
            self.tokens = d.get("total_tokens", self.tokens)
            self.cached += d.get("cached_tokens", 0)
            self.calls += 1
            txt = (d.get("text") or "").strip().replace("\n", " ")
            if txt:
                line = Text("  💭 " + txt[:150] + ("…" if len(txt) > 150 else ""), style="italic dim")
        elif kind == "tool_call":
            icon = ICONS.get(d.get("name", ""), "•")
            line = Text.assemble((f"  {icon} ", ACCENT), (f"{d.get('name')} ", "bold"), (str(d.get("brief", ""))[:110], ""))
        elif kind == "tool_result":
            if d.get("is_error"):
                first = (d.get("output") or "").strip().splitlines()[:1]
                line = Text("    ↳ " + (first[0][:140] if first else "error"), style="red")
            elif d.get("name") == "str_replace_editor" and (d.get("meta") or {}).get("edited"):
                first = (d.get("output") or "").splitlines()[:1]
                line = Text("    ↳ " + (first[0][:140] if first else "edited"), style="green")
        elif kind == "verify":
            self.verdicts = d.get("checks") or []
            ok = d.get("accepted")
            line = Text(f"  ⚖ gate round {d.get('round')}: {'ACCEPTED' if ok else 'REJECTED'} (evidence {d.get('strength')})",
                        style="bold green" if ok and d.get("strength") == "strong" else ("bold yellow" if ok else "bold red"))
            for c in self.verdicts:
                v = c.get("verdict")
                style = "green" if v == "fixes" else "red" if v in ("regression", "still_failing", "fails_after") else "dim"
                self._push(Text(f"      {v:<14} {c.get('command')[:90]}", style=style))
        elif kind == "review":
            v = d.get("verdict")
            line = Text(f"  🔍 reviewer: {v}" + (" - " + "; ".join(d.get("concerns") or [])[:160] if d.get("concerns") else ""),
                        style="yellow" if v == "revise" else "green")
        elif kind == "nudge":
            line = Text("  ⚑ harness: " + (d.get("message") or "")[:150], style="magenta")
        elif kind == "log":
            lvl = d.get("level", "info")
            line = Text(f"  {lvl}: {d.get('message', '')[:200]}", style="red" if lvl == "error" else "yellow" if lvl == "warn" else "dim")
        if line is not None:
            self._push(line)
        if self._live is not None:
            self._live.update(self.render())

    def _push(self, line: Text) -> None:
        self.feed.append(line)
        if self.plain:
            self.console.print(line)

    # ------------------------------------------------------------------ render
    def render(self):
        el = time.time() - self.t0
        head = Table.grid(expand=True)
        head.add_column(ratio=3)
        head.add_column(ratio=2, justify="right")
        head.add_row(Text(self.title[:90] or "Pramana", style="bold"), Text(self.model, style="dim"))
        phases = Text()
        for p in PHASES:
            if p == self.phase:
                phases.append(f" ● {p} ", style=f"bold {ACCENT}")
            elif p in self.done_phases:
                phases.append(f" ✓ {p} ", style="green")
            else:
                phases.append(f" ○ {p} ", style="dim")
        stats = Text(
            f"attempt {self.attempt} · step {self.step}/{self.max_steps} · {self.tokens:,} tokens · {self.calls} calls · {el:0.0f}s",
            style="dim",
        )
        body = Group(head, phases, stats, Text(""), *list(self.feed))
        return Panel(body, title=Text(" PRAMANA · proof-carrying coding agent ", style=f"bold {ACCENT}"), border_style=ACCENT)
