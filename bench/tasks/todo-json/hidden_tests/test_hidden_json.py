import io
import json

from todo.cli import main
from todo.store import Store


def _store(tmp_path):
    store = Store(str(tmp_path / "t.json"))
    main(["add", "a"], store=store, out=io.StringIO())
    main(["add", "b", "--priority", "1"], store=store, out=io.StringIO())
    main(["add", "c"], store=store, out=io.StringIO())
    main(["done", "2"], store=store, out=io.StringIO())
    return store


def run(store, *argv):
    out = io.StringIO()
    main(list(argv), store=store, out=out)
    return out.getvalue()


def test_json(tmp_path):
    data = json.loads(run(_store(tmp_path), "list", "--json"))
    assert data == [
        {"index": 1, "text": "a", "priority": 2, "done": False},
        {"index": 2, "text": "b", "priority": 1, "done": True},
        {"index": 3, "text": "c", "priority": 2, "done": False},
    ]


def test_pending_json_keeps_indexes(tmp_path):
    data = json.loads(run(_store(tmp_path), "list", "--json", "--pending"))
    assert [d["index"] for d in data] == [1, 3]


def test_pending_text(tmp_path):
    assert run(_store(tmp_path), "list", "--pending") == "1. [ ] a (p2)\n3. [ ] c (p2)\n"


def test_text_unchanged(tmp_path):
    assert run(_store(tmp_path), "list") == "1. [ ] a (p2)\n2. [x] b (p1)\n3. [ ] c (p2)\n"


def test_empty_json(tmp_path):
    store = Store(str(tmp_path / "empty.json"))
    assert json.loads(run(store, "list", "--json")) == []
