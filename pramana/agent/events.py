"""Tiny synchronous event bus: the agent emits, the TUI / trajectory recorder listen."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

Listener = Callable[[str, Dict[str, Any]], None]


class Events:
    def __init__(self) -> None:
        self._subs: List[Listener] = []
        self.t0 = time.time()

    def subscribe(self, fn: Listener) -> None:
        self._subs.append(fn)

    def emit(self, kind: str, **data: Any) -> None:
        data.setdefault("t", round(time.time() - self.t0, 2))
        for fn in list(self._subs):
            try:
                fn(kind, data)
            except Exception:  # noqa: BLE001 - a UI bug must never kill the agent
                pass
