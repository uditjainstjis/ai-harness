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


def _proj(tmp_path):
    repo = tmp_path / "proj"
    (repo / "mathx").mkdir(parents=True)
    (repo / "mathx" / "__init__.py").write_text("")
    (repo / "mathx" / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    cfg = load_config()
    cfg.runs_dir = str(tmp_path / "runs")
    return repo, cfg


def _solve(tmp_path, reply, text="mean([]) raises ZeroDivisionError\n\nIt should return 0.0."):
    repo, cfg = _proj(tmp_path)
    model = ChatModel(MockLLM([{"text": reply}] * 2), tool_mode="native", name="mock", provider="mock")
    return repo, Orchestrator(cfg, Events(), model=model).solve(repo, issue_from_text(text))


def test_a_new_file_that_is_the_fix_stays_in_the_patch(tmp_path):
    """Measured on PR-Agent #3050: a file the fix created was dropped as a 'side effect', leaving an empty patch."""
    reply = REPLY.replace("mathx/stats.py\n<<<<<<< SEARCH\n    return sum(xs) / len(xs)\n=======\n    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)\n",
                          "mathx/safe.py\n<<<<<<< SEARCH\n=======\ndef safe_div(a, b):\n    return a / b if b else 0.0\n>>>>>>> REPLACE\n\n"
                          "mathx/stats.py\n<<<<<<< SEARCH\n    return sum(xs) / len(xs)\n=======\n    from mathx.safe import safe_div\n    return safe_div(sum(xs), len(xs))\n")
    repo, res = _solve(tmp_path, reply)
    assert res.status == "verified", res.error
    assert "mathx/safe.py" in res.patch and "safe_div" in res.patch


def test_a_change_to_config_files_only_is_not_called_verified(tmp_path):
    """A test that reads back a file the model just wrote proves nothing about behaviour."""
    reply = f"""DIAGNOSIS: the workflow is missing.

.github/workflows/review.yml
<<<<<<< SEARCH
=======
name: review
on: pull_request
>>>>>>> REPLACE

.pramana/test_issue.py
<<<<<<< SEARCH
=======
import os
assert os.path.exists(".github/workflows/review.yml")
>>>>>>> REPLACE

TEST_COMMAND: {PY} .pramana/test_issue.py
"""
    repo, res = _solve(tmp_path, reply, "Add a review workflow\n\nWe should review every pull request.")
    assert res.status == "patched"
    assert ".github/workflows/review.yml" in res.patch


def test_an_edit_with_a_wrong_path_lands_in_the_file_that_holds_its_text(tmp_path):
    repo, res = _solve(tmp_path, REPLY.replace("mathx/stats.py\n<<<<<<<", "mathx/statistics.py\n<<<<<<<", 1))
    assert res.status == "verified", res.error
    assert "if not xs" in (repo / "mathx" / "stats.py").read_text()


def test_a_reply_cut_off_while_reasoning_does_not_use_up_the_second_round(tmp_path):
    """Measured on sqlparse/arrow with a 120B model: 6 of 13 first replies were ~30k chars of reasoning cut off at the
    output limit, which spent round 1; the answer that followed then had no round left to correct a bad edit."""
    repo, cfg = _proj(tmp_path)
    thinking = {"text": "We are given an issue: mean([]) divides by zero. " * 40, "stop_reason": "length"}
    bad_edit = {"text": REPLY.replace("    return sum(xs) / len(xs)\n=======", "    return sum(values) / len(values)\n=======")}
    model = ChatModel(MockLLM([thinking, bad_edit, {"text": REPLY}]), tool_mode="native", name="mock", provider="mock")
    res = Orchestrator(cfg, Events(), model=model).solve(repo, issue_from_text("mean([]) raises ZeroDivisionError\n\nIt should return 0.0."))
    assert res.status == "verified", res.error
    assert res.usage.calls == 3                      # reasoning, finish-now answer, corrected answer


def test_a_reply_cut_off_inside_its_first_edit_is_rescued(tmp_path):
    """Measured on recipes #4 (120B): both rounds were ~32k chars of reasoning cut off while writing the test file.
    The half-written block has a SEARCH marker, which used to hide the cut-off from the rescue."""
    repo, cfg = _proj(tmp_path)
    half = {"text": "We are given an issue. " * 40 + "\n.pramana/test_issue.py\n<<<<<<< SEARCH\n=======\nfrom mathx", "stop_reason": "length"}
    bad_edit = {"text": REPLY.replace("    return sum(xs) / len(xs)\n=======", "    return sum(values) / len(values)\n=======")}
    model = ChatModel(MockLLM([half, bad_edit, {"text": REPLY}]), tool_mode="native", name="mock", provider="mock")
    res = Orchestrator(cfg, Events(), model=model).solve(repo, issue_from_text("mean([]) raises ZeroDivisionError\n\nIt should return 0.0."))
    assert res.status == "verified", res.error
    assert res.usage.calls == 3                      # cut-off reply, finish-now answer, corrected answer


FUNC = ('def mean(xs):\n    """Average of xs."""\n    # sum then divide\n    total = 0\n    for x in xs:\n'
        '        total += x\n    count = len(xs)\n    return total / count\n')


def test_near_miss_edit_keeps_the_real_context_lines():
    from pramana.agent.fastpath import near_miss_apply
    search = ('    """Average of xs."""\n    # add them up then divide\n    total = 0\n    for x in xs:\n'
              '        total += x\n    count = len(xs)\n    return total / count')
    replace = search.replace("    return total / count", "    if not count:\n        return 0.0\n    return total / count")
    out = near_miss_apply(FUNC, search, replace)
    assert out is not None
    text, line = out
    assert "# sum then divide" in text and "add them up" not in text      # the real comment survives
    assert "    if not count:\n        return 0.0\n    return total / count\n" in text and line == 2


def test_near_miss_refuses_when_the_changed_line_itself_is_misremembered():
    from pramana.agent.fastpath import near_miss_apply
    search = ('    """Average of xs."""\n    # sum then divide\n    total = 0\n    for x in xs:\n'
              '        total += x\n    count = len(xs)\n    return total / len(xs)')
    replace = search.replace("    return total / len(xs)", "    return total / count if count else 0.0")
    assert near_miss_apply(FUNC, search, replace) is None


def test_near_miss_refuses_an_ambiguous_region():
    from pramana.agent.fastpath import near_miss_apply
    search = FUNC.replace("# sum then divide", "# add up").rstrip("\n")
    assert near_miss_apply(FUNC + "\n\n" + FUNC, search, search.replace("return total / count", "return 0")) is None


def test_an_edit_repeated_verbatim_in_the_reply_is_applied_once(tmp_path):
    """Measured on real repos: the model repeated an edit; the copy could not find its (already replaced) text."""
    block = ("mathx/stats.py\n<<<<<<< SEARCH\n    return sum(xs) / len(xs)\n=======\n"
             "    return (sum(xs) / len(xs)) if xs else 0.0\n>>>>>>> REPLACE\n\n")
    first = REPLY.index("mathx/stats.py")
    reply = REPLY[:first] + block + block + REPLY[REPLY.index(".pramana/test_issue.py"):]
    repo, res = _solve(tmp_path, reply)
    assert res.status == "verified", res.error
    assert res.usage.calls == 1
