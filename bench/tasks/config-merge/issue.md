load_layers corrupts the base configuration it is given

We keep a module-level `DEFAULTS` dict and build per-environment configs with `load_layers([DEFAULTS, env_overrides])`. After building the *staging* config, the *production* config suddenly contains staging values:

```python
DEFAULTS = {"db": {"host": "localhost", "port": 5432}, "debug": False}
staging = load_layers([DEFAULTS, {"db": {"host": "staging-db"}, "debug": True}])
prod = load_layers([DEFAULTS, {"db": {"port": 6432}}])
print(prod)   # {'db': {'host': 'staging-db', 'port': 6432}, 'debug': True}   <- staging leaked in!
print(DEFAULTS)  # also modified
```

`load_layers` must never modify any of the layers passed to it (including nested dicts), and the returned config must not share mutable nested dicts with the inputs (mutating the result must not affect the layers either).
