"""Offline tests: no network, no API key. Run with `make test` (or `python -m pytest -q tests`)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pramana.agent.verify import Gate
from pramana.config import detect_provider, load_config
from pramana.llm.base import ToolCall, parse_json_args
from pramana.llm.textproto import parse_text_tool_calls, to_text_messages
from pramana.repo.git import GitTracker
from pramana.repo.localize import Localizer
from pramana.repo.symbols import SymbolIndex
from pramana.tools import TOOL_SPECS, Toolbox, canonicalize
from pramana.tools.editor import EditError, Editor
from pramana.tools.shell import build_env, check_denylist, run_command


def make_repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    return repo


# ---------------------------------------------------------------- config
def test_provider_detection():
    assert detect_provider("sk-ant-abc") == "anthropic"
    assert detect_provider("AIzaSyXX") == "gemini"
    assert detect_provider("gsk_123") == "groq"
    assert detect_provider("sk-or-v1-x") == "openrouter"
    assert detect_provider("sk-proj-123") == "openai"
    assert detect_provider("weird") is None


def test_key_only_from_env(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "gsk_testkey")
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    cfg = load_config()
    assert cfg.resolved_provider == "groq"
    assert cfg.api_key == "gsk_testkey"
    assert "gsk_testkey" not in json.dumps(cfg.describe())


# ---------------------------------------------------------------- parsing
def test_tolerant_json():
    assert parse_json_args('{"a": 1,}') == {"a": 1}
    assert parse_json_args('{"code": "line1\nline2"}') == {"code": "line1\nline2"}
    assert parse_json_args('Sure! {"x": "y"}') == {"x": "y"}


def test_text_protocol_roundtrip():
    text = 'Let me look.\n<tool name="str_replace_editor">\n<command>view</command>\n<path>a.py</path>\n<view_range>[1, 20]</view_range>\n</tool>'
    prose, calls = parse_text_tool_calls(text, TOOL_SPECS)
    assert prose == "Let me look."
    assert calls[0].name == "str_replace_editor"
    assert calls[0].arguments == {"command": "view", "path": "a.py", "view_range": [1, 20]}
    hermes = '<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>'
    assert parse_text_tool_calls(hermes, TOOL_SPECS)[1][0].arguments == {"command": "ls"}
    qwen = "<function=bash>\n<parameter=command>\nls -la\n</parameter>\n</function>"
    assert parse_text_tool_calls(qwen, TOOL_SPECS)[1][0].arguments == {"command": "ls -la"}
    msgs = to_text_messages([{"role": "system", "content": "s"}, {"role": "tool", "name": "bash", "tool_call_id": "1", "content": "out"}], TOOL_SPECS)
    assert "How to call tools" in msgs[0]["content"] and "<tool_result" in msgs[1]["content"]


def test_canonicalize_aliases():
    c = canonicalize(ToolCall("1", "view", {"file_path": "x.py"}))
    assert c.name == "str_replace_editor" and c.arguments == {"path": "x.py", "command": "view"}
    c = canonicalize(ToolCall("2", "execute_bash", {"cmd": "ls"}))
    assert c.name == "bash" and c.arguments == {"command": "ls"}
    assert canonicalize(ToolCall("3", "browser.open", {})) is None


# ---------------------------------------------------------------- editor
def test_editor_exact_and_tolerant(tmp_path):
    repo = make_repo(tmp_path, {"m.py": "def f(x):\n    return x+1\n\n\ndef g(y):\n    return y\n"})
    ed = Editor(repo, repo / ".pramana")
    out = ed.str_replace("m.py", "    return x+1", "    return x + 1")
    assert "Edited m.py" in out
    # pasted line numbers + wrong indentation are tolerated
    out = ed.str_replace("m.py", "     5\tdef g(y):\n     6\t    return y", "def g(y):\n    return y * 2")
    assert "line numbers" in out
    assert "y * 2" in (repo / "m.py").read_text()
    out = ed.str_replace("m.py", "def f(x):\n  return x + 1", "def f(x):\n  return x + 2")  # 2-space vs 4-space
    assert "indentation" in out
    assert "    return x + 2" in (repo / "m.py").read_text()


def test_editor_rejects_syntax_errors_and_ambiguity(tmp_path):
    repo = make_repo(tmp_path, {"m.py": "a = 1\nb = 1\n"})
    ed = Editor(repo, repo / ".pramana")
    with pytest.raises(EditError, match="unparseable"):
        ed.str_replace("m.py", "a = 1", "a = (1")
    assert (repo / "m.py").read_text() == "a = 1\nb = 1\n"
    with pytest.raises(EditError, match="occurs 2 times"):
        ed.str_replace("m.py", "= 1", "= 2")
    with pytest.raises(EditError, match="most similar"):
        ed.str_replace("m.py", "a = 10\nb = 1", "x")
    ed.str_replace("m.py", "= 1", "= 2", replace_all=True)
    assert ed.undo_edit("m.py").startswith("Reverted")
    assert (repo / "m.py").read_text() == "a = 1\nb = 1\n"


def test_editor_blocks_writes_outside_repo(tmp_path):
    repo = make_repo(tmp_path, {"m.py": "x = 1\n"})
    ed = Editor(repo, repo / ".pramana")
    with pytest.raises(EditError, match="outside"):
        ed.create(str(tmp_path / "evil.py"), "x")


# ---------------------------------------------------------------- shell
def test_shell_timeout_and_denylist(tmp_path):
    repo = make_repo(tmp_path, {"a.txt": "hi\n"})
    res = run_command("sleep 5", repo, timeout=1, env=build_env(repo))
    assert res.timed_out
    assert check_denylist("sudo rm -rf /") and check_denylist("git push origin main")
    assert check_denylist("python -m pytest -q") is None
    assert "AI_API_KEY" not in build_env(repo)


def test_shell_truncates_huge_output(tmp_path):
    repo = make_repo(tmp_path, {"a.txt": "hi\n"})
    res = run_command("seq 1 200000", repo, timeout=30, env=build_env(repo), max_chars=5000)
    assert len(res.output) < 6000 and "omitted" in res.output and res.output.rstrip().endswith("200000")


# ---------------------------------------------------------------- git + gate
def test_git_patch_and_baseline(tmp_path):
    repo = make_repo(tmp_path, {"pkg/m.py": "def f():\n    return 1\n"})
    (repo / "junk.txt").write_text("pre-existing untracked")
    git = GitTracker(repo)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 2\n")
    (repo / "pkg" / "new.py").write_text("X = 1\n")
    (repo / ".pramana").mkdir(exist_ok=True)
    (repo / ".pramana" / "repro.py").write_text("print('scratch')\n")
    patch = git.patch()
    assert "return 2" in patch and "pkg/new.py" in patch
    assert "junk.txt" not in patch and "repro.py" not in patch
    with git.baseline():
        assert "return 1" in (repo / "pkg" / "m.py").read_text()
        assert not (repo / "pkg" / "new.py").exists()
        assert (repo / ".pramana" / "repro.py").exists()
    assert "return 2" in (repo / "pkg" / "m.py").read_text()
    assert subprocess.run(["git", "stash", "list"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_gate_classifies_fix_and_regression(tmp_path):
    repo = make_repo(tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n\ndef one():\n    return 1\n"})
    git = GitTracker(repo)
    env = build_env(repo)
    gate = Gate(repo, git, ["calc.py"], "", "unknown", env, timeout_s=60)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 2\n")
    v = gate.verify("fix add", ["python3 -c 'import calc; assert calc.add(2, 2) == 4'",
                                "python3 -c 'import calc; assert calc.one() == 1'"])
    verdicts = [c.verdict for c in v.checks]
    assert verdicts == ["fixes", "regression"]
    assert not v.accepted and "REGRESSION" in v.feedback
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef one():\n    return 1\n")
    v = gate.verify("fix add", ["python3 -c 'import calc; assert calc.add(2, 2) == 4'",
                                "python3 -c 'import calc; assert calc.one() == 1'"])
    assert v.accepted and v.strength == "strong"


def test_localizer_finds_symbol_from_issue(tmp_path):
    repo = make_repo(tmp_path, {
        "lib/net/urls.py": "def parse_query_string(s):\n    return {}\n",
        "lib/other.py": "def unrelated():\n    pass\n",
        "tests/test_urls.py": "from lib.net.urls import parse_query_string\n",
    })
    files = ["lib/net/urls.py", "lib/other.py", "tests/test_urls.py"]
    src, tests = Localizer(repo, files, SymbolIndex(repo, files)).localize(
        "`parse_query_string` drops repeated keys, e.g. parse_query_string('a=1&a=2')")
    assert src[0].path == "lib/net/urls.py"
    assert tests and tests[0].path == "tests/test_urls.py"


def test_toolbox_end_to_end_calls(tmp_path):
    repo = make_repo(tmp_path, {"app.py": "def greet(n):\n    return 'hi ' + n\n"})
    tb = Toolbox(repo, repo / ".pramana", SymbolIndex(repo, ["app.py"]))
    r = tb.execute(ToolCall("1", "find_definition", {"symbol": "greet"}))
    assert "app.py:1" in r.output
    r = tb.execute(ToolCall("2", "search", {"pattern": "return 'hi"}))
    assert "app.py" in r.output
    r = tb.execute(ToolCall("3", "bash", {"command": "echo ok && exit 3"}))
    assert r.is_error and "exit code 3" in r.output
    r = tb.execute(ToolCall("4", "str_replace_editor", {"path": "app.py", "old_str": "'hi '", "new_str": "'hello '"}))
    assert not r.is_error and "hello" in (repo / "app.py").read_text()


def test_compare_tool_and_oscillation(tmp_path):
    repo = make_repo(tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n", "legacy.py": "X = 1\n"})
    git = GitTracker(repo)
    tb = Toolbox(repo, repo / ".pramana", SymbolIndex(repo, ["calc.py"]), git=git)
    r = tb.execute(ToolCall("0", "compare", {"command": "python3 -c 'import calc; assert calc.add(2, 2) == 4'"}))
    assert "not changed anything" in r.output
    tb.execute(ToolCall("1", "str_replace_editor", {"command": "str_replace", "path": "calc.py", "old_str": "a - b", "new_str": "a + b"}))
    r = tb.execute(ToolCall("2", "compare", {"command": "python3 -c 'import calc; assert calc.add(2, 2) == 4'"}))
    assert "FIXED by your change" in r.output
    r = tb.execute(ToolCall("3", "compare", {"command": "python3 -c 'import legacy; assert legacy.X == 2'"}))
    assert "Fails on BOTH" in r.output
    assert "a + b" in (repo / "calc.py").read_text()  # restored after the baseline run
    tb.execute(ToolCall("4", "str_replace_editor", {"command": "str_replace", "path": "calc.py", "old_str": "a + b", "new_str": "a * b"}))
    r = tb.execute(ToolCall("5", "str_replace_editor", {"command": "str_replace", "path": "calc.py", "old_str": "a * b", "new_str": "a + b"}))
    assert "going back and forth" in r.output


def test_issue_snippets_extracted_and_run(tmp_path):
    from pramana.repo import snippets

    text = ("Bug\n```python\nfrom mathy import half\nprint(half(3))\n```\nand in the REPL:\n"
            "```\n>>> from mathy import half\n>>> half(5)\n2\n```\n```\nTraceback (most recent call last):\n  x\n```")
    found = snippets.extract(text)
    assert len(found) == 2 and "print(repr(half(5)))" in found[1].code
    repo = make_repo(tmp_path, {"mathy.py": "def half(x):\n    return x // 2\n"})
    scratch = repo / ".pramana"
    scratch.mkdir()
    from pramana.tools.shell import ensure_python_shim

    runs = snippets.run_snippets(repo, scratch, ensure_python_shim(build_env(repo), scratch / ".bin"), text)
    assert runs[0]["exit_code"] == "0" and "1" in runs[0]["output"]
    assert "2" in runs[1]["output"]


def test_text_protocol_nested_array_items():
    text = ('<tool name="submit">\n<summary>fixed</summary>\n<verification_commands>\n'
            '<command>python .pramana/repro.py</command>\n<command>python -m pytest -q tests</command>\n'
            '</verification_commands>\n</tool>')
    _, calls = parse_text_tool_calls(text, TOOL_SPECS)
    assert calls[0].arguments["verification_commands"] == ["python .pramana/repro.py", "python -m pytest -q tests"]


def test_text_protocol_invoke_style_and_hallucination_cut():
    from pramana.llm.textproto import truncate_hallucination

    text = ('I will look.\n<function_calls>\n<invoke name="bash">\n<parameter name="command">ls -la</parameter>\n</invoke>\n'
            '</function_calls>\n<result>\nfile_a\n</result>\n<tool name="bash">\n<parameter name="command">rm x</parameter>\n</tool>')
    cut = truncate_hallucination(text)
    assert "<result>" not in cut and "rm x" not in cut
    _, calls = parse_text_tool_calls(cut, TOOL_SPECS)
    assert len(calls) == 1 and calls[0].name == "bash" and calls[0].arguments == {"command": "ls -la"}
    _, calls = parse_text_tool_calls('<tool name="bash">\n<parameter name="command">pwd</parameter>\n</tool>', TOOL_SPECS)
    assert calls[0].arguments == {"command": "pwd"}


def test_canonicalize_editor_subcommands_and_arg_inference():
    c = canonicalize(ToolCall("1", "create", {"path": ".pramana/r.py", "content": "print(1)"}))
    assert c.name == "str_replace_editor" and c.arguments["command"] == "create" and c.arguments["file_text"] == "print(1)"
    c = canonicalize(ToolCall("2", "functions.bash", {"command": "ls"}))
    assert c.name == "bash"
    c = canonicalize(ToolCall("3", "apply_patch_tool", {"path": "a.py", "old_str": "x", "new_str": "y"}))
    assert c.name == "str_replace_editor" and c.arguments["command"] == "str_replace"
    c = canonicalize(ToolCall("4", "open_file", {"path": "a.py", "start_line": 5, "end_line": 9}))
    assert c.arguments == {"path": "a.py", "command": "view", "view_range": [5, 9]}
    assert canonicalize(ToolCall("5", "weather", {"city": "Delhi"})) is None


def test_view_range_spellings_and_unknown_arg_note(tmp_path):
    repo = make_repo(tmp_path, {"big.py": "".join(f"x{i} = {i}\n" for i in range(1, 901))})
    tb = Toolbox(repo, repo / ".pramana", SymbolIndex(repo, ["big.py"]))
    for args in ({"line_start": 500, "line_end": 505}, {"start": 500, "end": 505}, {"offset": 500, "limit": 6},
                 {"lines": "500-505"}, {"view_range": "500, 505"}):
        r = tb.execute(ToolCall("v", "str_replace_editor", {"command": "view", "path": "big.py", **args}))
        assert "lines 500-505" in r.output and "x500 = 500" in r.output, args
    r = tb.execute(ToolCall("u", "search", {"pattern": "x77 =", "max_results": 3}))
    assert "Ignored unknown argument(s) for `search`: max_results" in r.output
