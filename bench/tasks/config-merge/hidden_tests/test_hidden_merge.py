import copy

from layercfg import load_layers


DEFAULTS = {"db": {"host": "localhost", "port": 5432}, "debug": False, "tags": ["a"]}


def test_inputs_not_mutated():
    snapshot = copy.deepcopy(DEFAULTS)
    load_layers([DEFAULTS, {"db": {"host": "staging-db"}, "debug": True}])
    assert DEFAULTS == snapshot


def test_isolated_results():
    staging = load_layers([DEFAULTS, {"db": {"host": "staging-db"}, "debug": True}])
    prod = load_layers([DEFAULTS, {"db": {"port": 6432}}])
    assert staging == {"db": {"host": "staging-db", "port": 5432}, "debug": True, "tags": ["a"]}
    assert prod == {"db": {"host": "localhost", "port": 6432}, "debug": False, "tags": ["a"]}


def test_result_does_not_alias_inputs():
    override = {"db": {"port": 1}}
    result = load_layers([DEFAULTS, override])
    result["db"]["host"] = "changed"
    result["tags"].append("z")
    assert DEFAULTS["db"]["host"] == "localhost"
    assert DEFAULTS["tags"] == ["a"]
    assert override == {"db": {"port": 1}}


def test_single_and_empty():
    assert load_layers([]) == {}
    one = {"x": {"y": 1}}
    r = load_layers([one])
    r["x"]["y"] = 2
    assert one == {"x": {"y": 1}}
