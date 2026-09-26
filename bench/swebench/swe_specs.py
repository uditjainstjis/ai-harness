"""Load SWE-bench specs + log parsers without importing swebench/__init__ (which needs docker/datasets)."""
import sys, types
from pathlib import Path
import glob as _glob
import os as _os
_WORK = Path(_os.environ.get("PRAMANA_SWE_WORK", str(Path.home() / "pramana_work")))
SP = Path(next(iter(_glob.glob(str(_WORK / "toolsenv/lib/python3.*/site-packages/swebench"))), ""))
for name, path in (("swebench", SP), ("swebench.harness", SP / "harness")):
    m = types.ModuleType(name); m.__path__ = [str(path)]; sys.modules[name] = m
_ds = types.ModuleType("datasets")
for attr in ("Dataset", "load_dataset", "load_from_disk"):
    setattr(_ds, attr, None)
sys.modules.setdefault("datasets", _ds)
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS  # noqa
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER  # noqa
