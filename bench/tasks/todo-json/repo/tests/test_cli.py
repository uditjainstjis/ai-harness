import io

from todo.cli import main
from todo.store import Store


def test_add_and_list(tmp_path):
    store = Store(str(tmp_path / "t.json"))
    main(["add", "buy milk"], store=store, out=io.StringIO())
    out = io.StringIO()
    main(["list"], store=store, out=out)
    assert out.getvalue() == "1. [ ] buy milk (p2)\n"
