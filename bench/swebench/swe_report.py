"""Summarize SWE-bench runs per tag: resolve rate, tokens, time; per-instance comparison."""
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

import os
RES = Path(os.environ.get("PRAMANA_SWE_WORK", str(Path.home() / "pramana_work"))) / "results"


def load(tag):
    rows = {}
    for f in glob.glob(str(RES / f"run-{tag}*.jsonl")):
        for l in open(f):
            if l.strip():
                r = json.loads(l)
                rows[r["instance_id"]] = r
    return rows


def main(tags):
    data = {t: load(t) for t in tags}
    ids = sorted(set().union(*[set(d) for d in data.values()]))
    print(f"{'instance':32s} " + " ".join(f"{t:>22s}" for t in tags))
    for iid in ids:
        cells = []
        for t in tags:
            r = data[t].get(iid)
            cells.append("-" if not r else f"{'PASS' if r['resolved'] else 'fail'} {r['tokens'] / 1000:6.0f}k {r['wall_seconds']:5.0f}s")
        print(f"{iid:32s} " + " ".join(f"{c:>22s}" for c in cells))
    for t in tags:
        rows = list(data[t].values())
        if not rows:
            continue
        n = len(rows)
        solved = sum(r["resolved"] for r in rows)
        tok = sum(r["tokens"] for r in rows) / n
        print(f"{t}: {solved}/{n} resolved ({100 * solved / n:.0f}%), mean tokens {tok / 1000:.0f}k, "
              f"mean wall {sum(r['wall_seconds'] for r in rows) / n:.0f}s, statuses "
              + str(dict(sorted(defaultdict(int, {s: sum(1 for r in rows if r['status'] == s) for s in {r['status'] for r in rows}}).items()))))
    common = set.intersection(*[set(d) for d in data.values()]) if len(tags) > 1 else set()
    if common:
        print(f"on {len(common)} common instances: " + ", ".join(
            f"{t} {sum(data[t][i]['resolved'] for i in common)}/{len(common)} ({sum(data[t][i]['tokens'] for i in common) / len(common) / 1000:.0f}k tok)"
            for t in tags))


if __name__ == "__main__":
    main(sys.argv[1:] or ["v3", "v4"])
