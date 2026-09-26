"""One attempt = one agent trajectory from a clean tree to a gated submission."""
from __future__ import annotations

import copy
import json
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from ..llm import ChatModel, ContextOverflow, LLMError
from ..llm.base import ToolCall, Usage, short_error
from ..tools import ToolResult, Toolbox, canonicalize
from . import prompts
from .context import compact, compact_hard, estimate_tokens, strip_private
from .events import Events
from .verify import Gate, Verification


class FatalModelError(Exception):
    """Unrecoverable (e.g. bad credentials): abort the whole run with a clear message."""


@dataclass
class AttemptResult:
    number: int
    steps: int = 0
    stop_reason: str = ""
    verification: Optional[Verification] = None
    patch: str = ""
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0
    summary: str = ""
    review: Optional[Dict[str, Any]] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_counts: Dict[str, int] = field(default_factory=dict)
    created_files: List[str] = field(default_factory=list)
    touched_files: List[str] = field(default_factory=list)

    @property
    def strength(self) -> str:
        return self.verification.strength if self.verification else "none"


def _call_key(call: ToolCall) -> str:
    try:
        return call.name + ":" + json.dumps(call.arguments, sort_keys=True)[:2000]
    except (TypeError, ValueError):
        return call.name + ":" + str(call.arguments)[:2000]


def _brief(call: ToolCall) -> str:
    a = call.arguments or {}
    if call.name == "bash":
        return " ⏎ ".join(l.strip() for l in str(a.get("command", "")).splitlines() if l.strip())[:160]
    if call.name == "str_replace_editor":
        extra = ""
        if a.get("view_range"):
            extra = f" {a.get('view_range')}"
        return f"{a.get('command', '?')} {a.get('path', '')}{extra}"
    if call.name == "search":
        return f"{a.get('pattern', '')!r}" + (f" in {a.get('path')}" if a.get("path") else "")
    if call.name == "find_definition":
        return str(a.get("symbol", ""))
    if call.name == "find_files":
        return str(a.get("pattern", ""))
    if call.name == "compare":
        return " ⏎ ".join(l.strip() for l in str(a.get("command", "")).splitlines() if l.strip())[:150]
    if call.name == "submit":
        return (a.get("summary") or "")[:120]
    return json.dumps(a)[:120]


class Attempt:
    def __init__(self, number: int, model: ChatModel, toolbox: Toolbox, gate: Gate, events: Events,
                 system_prompt: str, initial_message: str, max_steps: int, budget_left_fn,
                 compact_at_tokens: int = 60000, keep_recent: int = 8, temperature: Optional[float] = None,
                 reviewer=None, independent=None) -> None:
        self.n = number
        self.model = model
        self.toolbox = toolbox
        self.gate = gate
        self.events = events
        self.system_prompt = system_prompt
        self.initial = initial_message
        self.max_steps = max_steps
        self.budget_left = budget_left_fn
        self.compact_at = compact_at_tokens
        self.keep_recent = keep_recent
        self.temperature = temperature
        self.reviewer = reviewer
        self.independent = independent
        self._independent_done = False
        self.result = AttemptResult(number=number)
        self._history: Deque[str] = deque(maxlen=12)
        self._nudged: set = set()
        self._edit_fail: Counter = Counter()
        self._edits = 0
        self._accepted = False
        self._reviewed = False
        self._last_prompt_tokens = 0
        self._step = 0
        toolbox.on_submit = self._on_submit

    # ------------------------------------------------------------------ submit gate
    def _on_submit(self, payload: Dict[str, Any]) -> ToolResult:
        final = self._step >= self.max_steps - 1
        self.events.emit("phase", name="verify", attempt=self.n)
        v = self.gate.verify(payload.get("summary", ""), payload.get("verification_commands", []), final=final)
        self.result.verification = v
        self.result.summary = payload.get("summary", "")
        self.events.emit("verify", attempt=self.n, accepted=v.accepted, strength=v.strength, round=v.round,
                         checks=[{"command": c.command, "verdict": c.verdict, "origin": c.origin} for c in v.checks])
        if v.accepted and self.independent is not None and not self._independent_done and not final and v.patch.strip():
            self._independent_done = True
            self.events.emit("phase", name="review", attempt=self.n)
            ind = self.independent()
            if ind and ind.get("check") is not None:
                chk = ind["check"]
                v.checks.append(chk)
                self.events.emit("verify", attempt=self.n, accepted=chk.verdict in ("fixes", "passes_both"), strength=v.strength,
                                 round=v.round, checks=[{"command": chk.command, "verdict": chk.verdict, "origin": chk.origin}])
                if chk.verdict in ("still_failing", "fails_after", "regression"):
                    v.accepted = False
                    body = (ind.get("file_content") or "")[:3000]
                    return ToolResult(
                        "HOLD ON - an independent regression test, written from the issue text by an agent that did not see "
                        f"your patch, FAILS on your patched code.\n\nCommand: {chk.command}\n\nTest:\n{body}\n\n"
                        f"Output with your patch (tail):\n{chk.after.tail if chk.after else ''}\n\n"
                        "Decide which side is wrong. If the test's expectation matches the issue, your fix is incomplete: fix it "
                        "and re-verify (you may run this test yourself). If the test contradicts the issue, say why in your "
                        "summary. Then call submit again.",
                        is_error=True,
                    )
        if v.accepted and self.reviewer is not None and not self._reviewed and not final and v.patch.strip():
            self._reviewed = True
            self.events.emit("phase", name="review", attempt=self.n)
            review = self.reviewer(v)
            self.result.review = review
            self.events.emit("review", attempt=self.n, **(review or {}))
            if review and review.get("verdict") == "revise" and review.get("concerns"):
                concerns = "\n".join(f"- {c}" for c in review["concerns"][:5])
                v.accepted = False
                return ToolResult(
                    v.feedback + "\n\nHOWEVER, an independent reviewer who read the issue and your patch raised these concerns:\n"
                    + concerns + "\n\nIf a concern is valid, fix it and re-verify. If it is not, say why in your summary. "
                    "Then call submit again.",
                    is_error=True,
                )
        if v.accepted:
            self._accepted = True
        return ToolResult(v.feedback, is_error=not v.accepted, meta={"accepted": v.accepted})

    # ------------------------------------------------------------------ model call
    def _call_model(self, messages: List[Dict[str, Any]]):
        temperature = self.temperature
        for attempt in range(5):
            try:
                return self.model.chat(strip_private(messages), tools=self.toolbox.specs(), temperature=temperature)
            except ContextOverflow:
                self.events.emit("log", level="warn", message="context overflow: compacting transcript")
                if compact_hard(messages, keep_recent=max(2, self.keep_recent // 2 - attempt)) == 0 and attempt >= 1:
                    return None
            except LLMError as e:
                msg = str(e)
                if "authentication" in msg.lower() or "HTTP 401" in msg or "HTTP 403" in msg:
                    raise FatalModelError(msg)
                if "HTTP 404" in msg and ("model" in msg.lower()):
                    raise FatalModelError(msg)
                self.events.emit("log", level="warn", message=f"provider error, retrying ({attempt + 1}/5): {short_error(e)}")
                # Some endpoints fail deterministically on a specific transcript. Identical retries
                # cannot help there, so each retry perturbs the request a little more.
                if attempt == 1:
                    # the newest tool output is the most common trigger: shorten it
                    last_tool = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
                    if last_tool and len(last_tool.get("content") or "") > 600:
                        c = last_tool["content"]
                        last_tool["content"] = c[:300] + "\n[... output shortened after a provider error ...]\n" + c[-200:]
                    else:
                        messages.append({"role": "user", "content": "Continue with the task.", "_nudge": True})
                elif attempt == 2:
                    messages.append({"role": "user", "content": "Continue with the task.", "_nudge": True})
                    compact(messages, keep_recent=2, head=300, tail=200)
                elif attempt == 3:
                    temperature = 0.7
                time.sleep(3 * (attempt + 1))
        return None

    # ------------------------------------------------------------------ guards
    def _track(self, call: ToolCall, res: ToolResult) -> None:
        self.result.tool_counts[call.name] = self.result.tool_counts.get(call.name, 0) + 1
        if call.name != "submit":
            self._history.append(_call_key(call) + ("#ERR" if res.is_error else ""))
        if call.name == "str_replace_editor" and (call.arguments or {}).get("command") in ("str_replace", "insert", "create"):
            path = str((call.arguments or {}).get("path", ""))
            if res.is_error:
                self._edit_fail[path] += 1
            else:
                self._edit_fail[path] = 0
                self._edits += 1

    def _guards(self) -> List[str]:
        notes: List[str] = []
        counts = Counter(self._history)
        for key, n in counts.items():
            if n >= 3 and key not in self._nudged:
                self._nudged.add(key)
                notes.append(prompts.REPEAT_NUDGE.format(name=key.split(":", 1)[0], n=n))
        for path, n in self._edit_fail.items():
            tag = f"editfail:{path}:{n // 3}"
            if n >= 3 and tag not in self._nudged:
                self._nudged.add(tag)
                notes.append(prompts.EDIT_FAIL_NUDGE.format(n=n, path=path or "(no path given - always pass path)"))
        if self._edits == 0 and self._step >= max(8, int(self.max_steps * 0.5)) and "noedit" not in self._nudged:
            self._nudged.add("noedit")
            notes.append(prompts.NO_EDIT_NUDGE.format(used=self._step, total=self.max_steps))
        left = self.max_steps - self._step
        if left == 10 and "b10" not in self._nudged:
            self._nudged.add("b10")
            notes.append(prompts.BUDGET_NUDGE.format(left=left, what="Converge: finish the fix, verify it, and submit."))
        if left == 3 and "b3" not in self._nudged:
            self._nudged.add("b3")
            notes.append(prompts.BUDGET_NUDGE.format(left=left, what="Submit now with your best verified change."))
        return notes

    def _rel_path(self, path: Any) -> str:
        if not path:
            return ""
        try:
            ed = self.toolbox.editor
            return ed.rel(ed.resolve(str(path)))
        except Exception:  # noqa: BLE001
            return str(path)

    @staticmethod
    def _elide_stale_views(messages: List[Dict[str, Any]], rel: str) -> None:
        """Earlier views of a file that was just edited show stale content and line numbers."""
        for m in messages:
            if m.get("role") == "tool" and m.get("_view_path") == rel and not m.get("_elided") and len(m.get("content") or "") > 400:
                m["content"] = (f"[Earlier view of {rel} elided by the harness: the file was edited afterwards, so that "
                                "content and its line numbers are stale. View the file again if you need it.]")
                m["_elided"] = True

    def _maybe_compact(self, messages: List[Dict[str, Any]]) -> None:
        size = self._last_prompt_tokens or estimate_tokens(messages)
        if size > self.compact_at:
            n = compact(messages, keep_recent=self.keep_recent)
            if n:
                self.events.emit("log", level="info", message=f"context manager elided {n} old observations (prompt ~{size} tokens)")
                self._last_prompt_tokens = 0

    def _guess_verification_cmds(self) -> List[str]:
        cmds = []
        for c in reversed(self.toolbox.commands):
            cmd = c["command"]
            if (".pramana/" in cmd or "pytest" in cmd or "runtests" in cmd or " test" in cmd) and cmd not in cmds:
                if len(cmd) < 300 and "pip install" not in cmd:
                    cmds.append(cmd)
            if len(cmds) >= 2:
                break
        return list(reversed(cmds))

    # ------------------------------------------------------------------ main loop
    def run(self) -> AttemptResult:
        t0 = time.time()
        usage0 = copy.copy(self.model.usage)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.initial},
        ]
        self.result.messages = messages
        no_tool_streak = 0
        stop = "max_steps"
        self.events.emit("attempt", attempt=self.n, status="start")
        self.events.emit("phase", name="localize", attempt=self.n)
        for step in range(1, self.max_steps + 1):
            self._step = step
            if self.budget_left() <= 0:
                stop = "token_budget"
                break
            self._maybe_compact(messages)
            self.events.emit("step", attempt=self.n, step=step, max_steps=self.max_steps)
            resp = self._call_model(messages)
            if resp is None:
                stop = "model_error"
                break
            self._last_prompt_tokens = resp.usage.input_tokens
            unknown: List[str] = []
            if resp.tool_calls:
                canon = []
                for tc in resp.tool_calls:
                    c = canonicalize(tc)
                    if c is None:
                        unknown.append(tc.name)
                    else:
                        canon.append(c)
                resp.tool_calls = canon
            if resp.text or resp.tool_calls:
                messages.append(resp.as_message())
            if unknown and not resp.tool_calls:
                # nothing usable in this turn: one clear note instead of an empty assistant turn + two nudges
                messages.append({"role": "user", "_nudge": True, "content": (
                    f"You tried to call tool(s) that do not exist: {', '.join(sorted(set(unknown)))}. Available tools: "
                    "bash, str_replace_editor, search, find_definition, find_files, compare, submit. Call one of them.")})
                no_tool_streak += 1
                if no_tool_streak >= 3:
                    stop = "no_tool_calls"
                    break
                continue
            ignored_note = f"(Ignored call(s) to unknown tool(s): {', '.join(sorted(set(unknown)))}.)" if unknown else ""
            self.events.emit("llm", attempt=self.n, step=step, text=resp.text[:2000], n_calls=len(resp.tool_calls),
                             input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                             cached_tokens=resp.usage.cached_tokens, latency_s=round(resp.latency_s, 2),
                             total_tokens=self.model.usage.total_tokens)
            if not resp.tool_calls:
                no_tool_streak += 1
                if no_tool_streak >= 3:
                    stop = "no_tool_calls"
                    break
                messages.append({"role": "user", "content": prompts.NO_TOOL_NUDGE, "_nudge": True})
                continue
            no_tool_streak = 0
            for call in resp.tool_calls:
                self.events.emit("tool_call", attempt=self.n, step=step, name=call.name, brief=_brief(call), args=call.arguments)
                args = call.arguments or {}
                if call.name == "str_replace_editor" and args.get("command") in ("str_replace", "insert", "create"):
                    scratch_file = ".pramana/" in str(args.get("path", ""))
                    self.events.emit("phase", name="reproduce" if scratch_file else "fix", attempt=self.n)
                elif call.name == "bash" and ".pramana/" in str(args.get("command", "")):
                    self.events.emit("phase", name="reproduce", attempt=self.n)
                res = self.toolbox.execute(call)
                tool_msg = {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": res.output}
                if call.name == "str_replace_editor" and not res.is_error:
                    rel = self._rel_path(args.get("path"))
                    if args.get("command") == "view" and rel:
                        tool_msg["_view_path"] = rel
                    elif args.get("command") in ("str_replace", "insert", "create", "undo_edit") and rel:
                        self._elide_stale_views(messages, rel)
                messages.append(tool_msg)
                self.events.emit("tool_result", attempt=self.n, step=step, name=call.name, is_error=res.is_error,
                                 output=res.output[:4000], meta={k: v for k, v in res.meta.items() if k != "patch"})
                self._track(call, res)
                if self._accepted:
                    break
            if self._accepted:
                stop = "submitted"
                break
            if ignored_note:  # after the tool results: tool messages must directly follow their call
                messages.append({"role": "user", "content": ignored_note, "_nudge": True})
            for note in self._guards():
                messages.append({"role": "user", "content": note, "_nudge": True})
                self.events.emit("nudge", attempt=self.n, message=note)
        self.result.steps = self._step
        self.result.stop_reason = stop
        if not self._accepted:
            patch = self.toolbox_git_patch()
            if patch.strip():
                self.events.emit("phase", name="verify", attempt=self.n)
                v = self.gate.verify("(auto-verified: the agent did not submit before stopping)", self._guess_verification_cmds(), final=True)
                self.result.verification = v
                self.events.emit("verify", attempt=self.n, accepted=v.accepted, strength=v.strength, round=v.round,
                                 checks=[{"command": c.command, "verdict": c.verdict, "origin": c.origin} for c in v.checks])
        self.result.patch = self.toolbox_git_patch()
        self.result.created_files = list(self.toolbox.editor.created)
        self.result.touched_files = list(self.toolbox.editor.touched)
        u = Usage()
        u.add(self.model.usage)
        u.input_tokens -= usage0.input_tokens
        u.output_tokens -= usage0.output_tokens
        u.cached_tokens -= usage0.cached_tokens
        u.reasoning_tokens -= usage0.reasoning_tokens
        u.cost_usd -= usage0.cost_usd
        u.calls -= usage0.calls
        self.result.usage = u
        self.result.elapsed_s = round(time.time() - t0, 1)
        self.events.emit("attempt", attempt=self.n, status="end", stop_reason=stop, strength=self.result.strength,
                         steps=self.result.steps, tokens=u.total_tokens)
        return self.result

    def toolbox_git_patch(self) -> str:
        return self.gate.git.patch()
