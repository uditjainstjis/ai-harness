"""Full pipeline with a scripted model: intake -> loop -> gate (before/after) -> reviewer -> evidence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pramana.agent.events import Events
from pramana.agent.orchestrator import Orchestrator
from pramana.config import load_config
from pramana.llm import ChatModel
from pramana.llm.mock import MockLLM
from pramana.repo.issue import issue_from_text


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "mathx").mkdir(parents=True)
    (repo / "mathx" / "__init__.py").write_text("")
    (repo / "mathx" / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


def test_full_pipeline_with_scripted_model(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    py = sys.executable
    script = [
        {"text": "1. mean([]) -> 0.0\n2. mean([1, 2, 3]) -> 2.0 (unchanged)"},  # acceptance-criteria prediction
        {"tool_calls": [{"name": "find_definition", "arguments": {"symbol": "mean"}}]},
        {"tool_calls": [{"name": "str_replace_editor", "arguments": {
            "command": "create", "path": ".pramana/repro.py",
            "file_text": "from mathx.stats import mean\nassert mean([]) == 0.0, 'empty input must give 0.0'\nprint('ok')\n"}}]},
        {"tool_calls": [{"name": "bash", "arguments": {"command": f"{py} .pramana/repro.py"}}]},
        {"text": "The division by zero comes from len([]) == 0.", "tool_calls": [{"name": "str_replace_editor", "arguments": {
            "command": "str_replace", "path": "mathx/stats.py",
            "old_str": "    return sum(xs) / len(xs)", "new_str": "    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)"}}]},
        {"tool_calls": [{"name": "submit", "arguments": {
            "summary": "mean() divided by len(xs) without handling empty input; it now returns 0.0 for [].",
            "verification_commands": [f"{py} .pramana/repro.py"]}}]},
        {"text": '{"verdict": "approve", "concerns": []}'},  # reviewer
    ]
    monkeypatch.setenv("AI_PROVIDER", "mock")
    cfg = load_config()
    cfg.runs_dir = str(tmp_path / "runs")
    cfg.agent.max_attempts = 1
    model = ChatModel(MockLLM(script), "native", "mock", "mock")
    events = Events()
    seen = []
    events.subscribe(lambda kind, data: seen.append(kind))
    res = Orchestrator(cfg, events, model=model).solve(repo, issue_from_text("mean([]) raises ZeroDivisionError\n\nIt should return 0.0."))

    assert res.status == "verified", res.error
    assert "if not xs" in (repo / "mathx" / "stats.py").read_text()
    verdicts = [c.verdict for c in res.verification.checks]
    assert verdicts[0] == "fixes"
    assert not (repo / ".pramana").exists()  # scratch moved into the evidence bundle
    bundle = res.run_dir
    for name in ("report.md", "report.html", "patch.diff", "evidence.json", "trajectory.jsonl", "transcript_attempt1.json"):
        assert (bundle / name).exists(), name
    assert (bundle / "scratch" / "repro.py").exists()
    ev = json.loads((bundle / "evidence.json").read_text())
    assert ev["status"] == "verified" and ev["patch"]["files"] == 1
    assert "verify" in seen and "review" in seen and "criteria" in seen
    first_user = json.loads((bundle / "transcript_attempt1.json").read_text())[1]["content"]
    assert "<acceptance_criteria>" in first_user and "mean([]) -> 0.0" in first_user
