"""Independent test writer: a second agent that never sees the patch.

After the submit gate accepts a patch, this agent gets only the issue, the predicted acceptance
criteria and read access to the repository. It writes the regression test a maintainer would add
(into .pramana/, it cannot touch source files) and returns a command. The harness runs that test on
the original and the patched code. A test that still fails on the patched code sends the submission
back to the main agent with the output - executable evidence instead of a reviewer's opinion.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..llm import ChatModel, LLMError
from ..llm.base import ToolSpec
from ..tools import TOOL_SPECS, Toolbox, canonicalize

WRITER_SYSTEM = """You are an independent QA engineer. Another engineer has changed this repository to resolve the issue below; you do NOT see their change. Your job: write the regression test the project's maintainers would add for this issue, so the harness can check the change against it.

Rules
- Derive every expected value from the ISSUE TEXT (and the acceptance criteria), never from what the current code happens to return. Only assert what the issue states or clearly implies; if an expected value is not determinable from the issue, do not assert it.
- Cover each example in the issue plus the obvious sibling cases the criteria list.
- Put the test in .pramana/ (you cannot edit source files). Use the project's test style (look at an existing test file for imports/fixtures). A plain script with asserts that exits non-zero on failure is fine too.
- Run it once to make sure it executes (import errors etc. are your bugs - fix them). Whether it passes or fails against the current code is NOT your concern: do not change expectations to make it pass.
- Finish with `done`, giving the exact command that runs your test from the repository root.
- Be quick: you have a small step budget."""

DONE_SPEC = ToolSpec(
    name="done",
    description="Finish: provide the shell command (run from the repo root) that runs your independent test.",
    parameters={"type": "object", "properties": {
        "command": {"type": "string", "description": "e.g. python -m pytest -q .pramana/test_independent.py"},
        "notes": {"type": "string", "description": "One line: what the test checks."}},
        "required": ["command"]},
)

READ_TOOLS = {"bash", "str_replace_editor", "search", "find_definition", "find_files"}


class TestWriter:
    def __init__(self, model: ChatModel, toolbox: Toolbox, events, max_steps: int = 14) -> None:
        self.model = model
        self.toolbox = toolbox
        self.events = events
        self.max_steps = max_steps

    def specs(self) -> List[ToolSpec]:
        return [t for t in TOOL_SPECS if t.name in READ_TOOLS] + [DONE_SPEC]

    def run(self, issue_text: str, criteria: str, repo_summary: str, related_tests: str) -> Optional[Dict[str, Any]]:
        user = (f"<issue>\n{issue_text[:12000]}\n</issue>\n\n"
                + (f"<acceptance_criteria>\n{criteria}\n</acceptance_criteria>\n\n" if criteria else "")
                + f"<repository>\n{repo_summary}\n{('Existing related tests: ' + related_tests) if related_tests else ''}\n</repository>\n\n"
                "Write the independent regression test now.")
        messages: List[Dict[str, Any]] = [{"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": user}]
        t0 = time.time()
        for step in range(1, self.max_steps + 1):
            try:
                resp = self.model.chat(messages, tools=self.specs(), temperature=0.0)
            except LLMError as e:
                self.events.emit("log", level="warn", message=f"independent test writer stopped: {str(e)[:150]}")
                return None
            calls = []
            for tc in resp.tool_calls:
                if tc.name == "done":
                    calls.append(tc)
                    continue
                c = canonicalize(tc)
                if c is not None and c.name in READ_TOOLS:
                    calls.append(c)
            resp.tool_calls = calls
            messages.append(resp.as_message())
            if not calls:
                messages.append({"role": "user", "content": "Use a tool, or call `done` with your test command."})
                continue
            for call in calls:
                if call.name == "done":
                    cmd = str((call.arguments or {}).get("command") or "").strip()
                    if cmd:
                        self.events.emit("independent_test", status="written", command=cmd, steps=step,
                                         seconds=round(time.time() - t0, 1))
                        return {"command": cmd, "notes": (call.arguments or {}).get("notes", ""), "steps": step}
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": "done", "content": "command is required"})
                    continue
                args = call.arguments or {}
                if call.name == "str_replace_editor" and args.get("command") in ("str_replace", "insert", "create", "undo_edit"):
                    if ".pramana/" not in str(args.get("path", "")) and not str(args.get("path", "")).startswith(".pramana"):
                        messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                         "content": "Refused: you may only create/edit files under .pramana/."})
                        continue
                res = self.toolbox.execute(call)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": res.output})
        self.events.emit("independent_test", status="gave_up", steps=self.max_steps)
        return None
