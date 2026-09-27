"""Fast path: an easy issue is fixed in ONE model call and proven on the original and the patched code."""
from __future__ import annotations

import os
import subprocess
import sys

from pramana.agent.events import Events
from pramana.agent.fastpath import parse_reply
from pramana.agent.orchestrator import Orchestrator
from pramana.config import load_config
from pramana.llm import ChatModel
from pramana.llm.mock import MockLLM
from pramana.repo.issue import issue_from_text

PY = sys.executable

REPLY = f"""DIAGNOSIS: mean() divides by len(xs) with no empty-list case.

mathx/stats.py
<<<<<<< SEARCH
    return sum(xs) / len(xs)
=======
    if not xs:
        return 0.0
    return sum(xs) / len(xs)
>>>>>>> REPLACE

.pramana/test_issue.py
<<<<<<< SEARCH
=======
from mathx.stats import mean
assert mean([]) == 0.0
assert mean([1, 2, 3]) == 2.0
print("ok")
>>>>>>> REPLACE

TEST_COMMAND: {PY} .pramana/test_issue.py
"""


def test_parse_reply_reads_edits_and_command():
    diag, edits, cmd = parse_reply(REPLY)
    assert "empty-list" in diag and cmd.endswith(".pramana/test_issue.py")
    assert [e[0] for e in edits] == ["mathx/stats.py", ".pramana/test_issue.py"] and edits[1][1] == ""


def test_easy_issue_is_verified_in_one_call(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    (repo / "mathx").mkdir(parents=True)
    (repo / "mathx" / "__init__.py").write_text("")
    (repo / "mathx" / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    cfg = load_config()
    cfg.runs_dir = str(tmp_path / "runs")
    model = ChatModel(MockLLM([{"text": REPLY}]), tool_mode="native", name="mock", provider="mock")
    res = Orchestrator(cfg, Events(), model=model).solve(repo, issue_from_text("mean([]) raises ZeroDivisionError\n\nIt should return 0.0."))
    assert res.status == "verified", res.error
    assert res.usage.calls == 1                                   # one model call, nothing else
    assert "if not xs" in (repo / "mathx" / "stats.py").read_text()
    assert any(c.verdict == "fixes" for c in res.verification.checks)
