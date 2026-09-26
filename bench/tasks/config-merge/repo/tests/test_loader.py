from layercfg import load_layers


def test_later_layer_wins():
    assert load_layers([{"a": 1}, {"a": 2}]) == {"a": 2}


def test_nested():
    assert load_layers([{"db": {"host": "x", "port": 1}}, {"db": {"port": 2}}]) == {"db": {"host": "x", "port": 2}}
