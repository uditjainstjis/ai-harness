"""Sandboxed-ish command execution inside the target repository.

* Runs `bash -c` with stdin closed (no interactive hangs) in its own process group, so a
  timeout kills the whole tree.
* Output goes to a temp file, so a runaway printer cannot exhaust memory; we read head+tail.
* The harness's own virtualenv is scrubbed from the environment; the target repo's venv
  (./.venv, ./venv, ./env) is activated if present; the repo root (and src/) is prepended
  to PYTHONPATH so scratch scripts import the repo's code.
* A small denylist blocks destructive or outward-facing commands.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

DENYLIST = [
    (re.compile(r"(^|[;&|\s])sudo\s"), "sudo is not available in this sandbox"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME)(\s|$|/\*)"), "refusing to delete a root/home directory"),
    (re.compile(r"\bgit\s+push\b"), "pushing to remotes is not allowed"),
    (re.compile(r"\b(shutdown|reboot|halt|mkfs(\.\w+)?)\b"), "system-level command blocked"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb blocked"),
    (re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]"), "git clean would delete the harness scratch area; remove specific files instead"),
]


@dataclass
class CommandResult:
    command: str
    exit_code: Optional[int]
    output: str
    duration_s: float
    timed_out: bool = False
    truncated_lines: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def check_denylist(command: str) -> Optional[str]:
    for rx, why in DENYLIST:
        if rx.search(command):
            return why
    return None


def find_repo_venv(repo: Path) -> Optional[Path]:
    for name in (".venv", "venv", "env", ".env"):
        cand = repo / name
        if (cand / "bin" / "python").exists() or (cand / "Scripts" / "python.exe").exists():
            return cand
    return None


def build_env(repo: Path, extra: Optional[Dict[str, str]] = None, venv: Optional[Path] = None) -> Dict[str, str]:
    env = dict(os.environ)
    in_own_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    own_bin = os.path.join(sys.prefix, "Scripts" if os.name == "nt" else "bin")
    for var in ("PYTHONHOME", "__PYVENV_LAUNCHER__"):
        env.pop(var, None)
    if in_own_venv and env.get("VIRTUAL_ENV", "").rstrip("/") == sys.prefix.rstrip("/"):
        env.pop("VIRTUAL_ENV", None)
    path_parts = [
        p for p in env.get("PATH", "").split(os.pathsep)
        if p and not (in_own_venv and os.path.normpath(p) == os.path.normpath(own_bin))
    ]
    venv = venv or find_repo_venv(repo)
    if venv is not None:
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        path_parts.insert(0, str(bindir))
        env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(path_parts)
    py_paths = [str(repo)]
    if (repo / "src").is_dir() and not (repo / "src" / "__init__.py").exists():
        py_paths.insert(0, str(repo / "src"))
    if env.get("PYTHONPATH"):
        py_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    env.update(
        {
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "MANPAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "EDITOR": "true",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONUNBUFFERED": "1",
            # never leave .pyc files in the user's repository, and never read a stale one
            "PYTHONDONTWRITEBYTECODE": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }
    )
    env.pop("AI_API_KEY", None)  # the model's own credential never reaches repo code it runs
    if extra:
        env.update(extra)
    return env


def ensure_python_shim(env: Dict[str, str], shim_dir: Path) -> Dict[str, str]:
    """Many systems only ship `python3`; models (and READMEs) say `python`. Bridge the gap."""
    import shutil as _sh

    if _sh.which("python", path=env.get("PATH")):
        return env
    py3 = _sh.which("python3", path=env.get("PATH"))
    if not py3:
        return env
    shim_dir.mkdir(parents=True, exist_ok=True)
    link = shim_dir / "python"
    if not link.exists():
        try:
            link.symlink_to(py3)
        except OSError:
            return env
    env = dict(env)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    return env


def clean_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    # collapse carriage-return progress bars to their final state
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            segs = [s for s in line.split("\r") if s.strip()]
            line = segs[-1] if segs else ""
        lines.append(line)
    return "\n".join(lines)


def truncate(text: str, max_chars: int = 12000, head_frac: float = 0.45) -> (str, int):
    if len(text) <= max_chars:
        return text, 0
    head_n = int(max_chars * head_frac)
    tail_n = max_chars - head_n
    head, tail = text[:head_n], text[-tail_n:]
    omitted = text[head_n : len(text) - tail_n]
    n_lines = omitted.count("\n")
    marker = (
        f"\n\n[... {n_lines} lines / {len(omitted)} chars omitted to save context. "
        f"Narrow the command (grep, head, tail, -k, -x) to see a specific part ...]\n\n"
    )
    return head + marker + tail, n_lines


def run_command(
    command: str,
    cwd: Path,
    timeout: float = 180,
    env: Optional[Dict[str, str]] = None,
    max_chars: int = 12000,
) -> CommandResult:
    t0 = time.time()
    with tempfile.TemporaryFile(mode="w+b") as out:
        try:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            return CommandResult(command, None, f"failed to start command: {e}", 0.0)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
        out.seek(0, os.SEEK_END)
        size = out.tell()
        cap = 4 * 1024 * 1024
        if size > cap:
            out.seek(0)
            head = out.read(cap // 2)
            out.seek(size - cap // 2)
            tail = out.read()
            raw = head + b"\n[... output too large, middle discarded ...]\n" + tail
        else:
            out.seek(0)
            raw = out.read()
    text = clean_output(raw.decode("utf-8", errors="replace"))
    text, dropped = truncate(text, max_chars=max_chars)
    return CommandResult(
        command=command,
        exit_code=None if timed_out else proc.returncode,
        output=text,
        duration_s=time.time() - t0,
        timed_out=timed_out,
        truncated_lines=dropped,
    )


def format_result(res: CommandResult) -> str:
    if res.timed_out:
        head = f"[command timed out after {res.duration_s:.0f}s and was killed; consider a narrower command or a longer timeout]"
    else:
        head = f"[exit code {res.exit_code}; {res.duration_s:.1f}s]"
    body = res.output.rstrip()
    return f"{head}\n{body}" if body else f"{head}\n(no output)"
