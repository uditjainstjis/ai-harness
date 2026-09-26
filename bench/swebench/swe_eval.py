"""SWE-bench Verified subset runner for Pramana (no Docker; uv venvs per repo/version).

usage:
  swe_eval.py pick  --n 24 --seed 0 > ids.txt
  swe_eval.py gold  ids.txt            # validate env: gold patch must resolve (no LLM)
  swe_eval.py run   ids.txt --tag v1   # run the harness + grade with hidden tests
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import swe_specs  # noqa: E402
from swe_specs import MAP_REPO_TO_PARSER as PARSERS  # noqa: E402
from swe_specs import MAP_REPO_VERSION_TO_SPECS as SPECS  # noqa: E402
from swebench.harness.constants import MAP_REPO_TO_REQS_PATHS  # noqa: E402

WORK = Path(os.environ.get("PRAMANA_SWE_WORK", str(Path.home() / "pramana_work")))
ENVS = WORK / "envs"
CHECKOUTS = WORK / "checkouts"
RESULTS = WORK / "results"
ROWS = {r["instance_id"]: r for r in json.load(open(WORK / "swe_verified.json"))}
LIGHT_REPOS = ["psf/requests", "pytest-dev/pytest", "sympy/sympy", "django/django", "pallets/flask", "pylint-dev/pylint"]
PY_OVERRIDE = {"3.6": "3.8", "3.5": "3.8", "3.7": "3.8"}


def sh(cmd, cwd=None, check=True, timeout=1800, env=None):
    p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed: {p.stderr[-800:]}")
    return p


def spec_of(inst):
    return SPECS[inst["repo"]][inst["version"]]


def env_for(inst):
    spec = spec_of(inst)
    py = PY_OVERRIDE.get(spec["python"], spec["python"])
    key = f"{inst['repo'].replace('/', '__')}-{inst['version']}"
    env = ENVS / key
    if (env / "bin" / "python").exists() and (env / ".ready").exists():
        return env
    if env.exists():
        shutil.rmtree(env)
    ENVS.mkdir(parents=True, exist_ok=True)
    sh(["uv", "venv", "-q", "--python", py, str(env)])
    pyexe = str(env / "bin" / "python")
    pkgs = []
    if spec.get("packages") and spec["packages"] not in ("requirements.txt", "environment.yml"):
        pkgs += spec["packages"].split()
    pkgs += spec.get("pip_packages", [])
    if spec.get("packages") == "requirements.txt":
        for rp in MAP_REPO_TO_REQS_PATHS.get(inst["repo"], []):
            url = f"https://raw.githubusercontent.com/{inst['repo']}/{inst['environment_setup_commit']}/{rp}"
            p = sh(["curl", "-sfL", url], check=False)
            if p.returncode == 0:
                for line in p.stdout.splitlines():
                    line = line.split("#")[0].strip()
                    if line and not line.startswith(("-e", "-r", "--")) and "://" not in line:
                        pkgs.append(line)
    if "pytest" not in " ".join(pkgs) and inst["repo"] != "pytest-dev/pytest":
        pkgs.append("pytest")
    if pkgs:
        p = sh(["uv", "pip", "install", "-q", "--python", pyexe, *pkgs], check=False, timeout=1800)
        if p.returncode != 0:
            for pk in pkgs:
                q = sh(["uv", "pip", "install", "-q", "--python", pyexe, pk], check=False, timeout=900)
                if q.returncode != 0:
                    print(f"  [env] skip {pk}: {q.stderr.strip().splitlines()[-1][:120] if q.stderr.strip() else '?'}")
    (env / ".ready").write_text("ok")
    return env


def private_env(inst, tag=""):
    """Instance-private copy of the shared env (uv installs from its cache: fast, APFS clones)."""
    shared = env_for(inst)
    env = ENVS / f"inst-{inst['instance_id']}{'-' + tag if tag else ''}"
    if env.exists():
        shutil.rmtree(env)
    spec = spec_of(inst)
    py = PY_OVERRIDE.get(spec["python"], spec["python"])
    sh(["uv", "venv", "-q", "--python", py, str(env)])
    frozen = sh([str(shared / "bin" / "python"), "-m", "pip", "freeze"], check=False).stdout if (shared / "bin" / "pip").exists() else ""
    if not frozen.strip():
        frozen = sh(["uv", "pip", "freeze", "--python", str(shared / "bin" / "python")], check=False).stdout
    reqs = [l for l in frozen.splitlines() if l.strip() and not l.startswith(("-e", "#")) and " @ file:" not in l]
    if reqs:
        req_file = env / "reqs.txt"
        req_file.write_text("\n".join(reqs) + "\n")
        p = sh(["uv", "pip", "install", "-q", "--python", str(env / "bin" / "python"), "-r", str(req_file)], check=False, timeout=1800)
        if p.returncode != 0:
            for r in reqs:
                sh(["uv", "pip", "install", "-q", "--python", str(env / "bin" / "python"), r], check=False, timeout=600)
    return env


def checkout(inst, tag=""):
    """Tarball download (one HTTP stream, retried) -> local git repo with a single baseline commit."""
    d = CHECKOUTS / (inst["instance_id"] + (f"-{tag}" if tag else ""))
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    url = f"https://codeload.github.com/{inst['repo']}/tar.gz/{inst['base_commit']}"
    tgz = CHECKOUTS / f"{d.name}.tgz"
    ok = False
    for attempt in range(4):
        p = sh(["curl", "-sfL", "--retry", "3", "--retry-delay", "3", "--connect-timeout", "30", "--max-time", "900", "-o", str(tgz), url], check=False, timeout=1000)
        if p.returncode == 0 and tgz.exists() and tgz.stat().st_size > 1000:
            ok = True
            break
        time.sleep(5)
    if ok:
        sh(["tar", "-xzf", str(tgz), "-C", str(d), "--strip-components", "1"])
        tgz.unlink()
    else:  # fall back to a shallow git fetch
        sh(["git", "init", "-q"], cwd=d)
        sh(["git", "remote", "add", "origin", f"https://github.com/{inst['repo']}.git"], cwd=d)
        sh(["git", "fetch", "-q", "--depth", "1", "origin", inst["base_commit"]], cwd=d, timeout=1200)
        sh(["git", "checkout", "-q", "FETCH_HEAD"], cwd=d)
    if not (d / ".git").exists():
        genv = dict(os.environ, GIT_AUTHOR_NAME="swe", GIT_AUTHOR_EMAIL="swe@localhost", GIT_COMMITTER_NAME="swe", GIT_COMMITTER_EMAIL="swe@localhost")
        sh(["git", "init", "-q"], cwd=d)
        sh(["git", "add", "-A", "-f", "."], cwd=d, env=genv)
        sh(["git", "commit", "-q", "-m", f"base {inst['base_commit'][:12]}"], cwd=d, env=genv)
    with open(d / ".git" / "info" / "exclude", "a") as fh:
        fh.write("\n.venv\n")
    base = sh(["git", "rev-parse", "HEAD"], cwd=d).stdout.strip()
    (d / ".git" / "swe_base").write_text(base)
    return d


def install_repo(inst, d, env):
    pyexe = str(env / "bin" / "python")
    spec = spec_of(inst)
    inst_cmd = spec.get("install", "")
    editable = "-e" in inst_cmd
    args = ["uv", "pip", "install", "-q", "--python", pyexe] + (["-e", str(d)] if editable else [str(d)])
    ver = inst["version"] if inst["version"].count(".") >= 2 else inst["version"] + ".0"
    ienv = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION=ver)  # shallow clones have no tags
    p = sh(args, check=False, timeout=900, env=ienv)
    if p.returncode != 0:
        print(f"  [install] warning: {p.stderr.strip()[-300:]}")
    link = d / ".venv"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(env, target_is_directory=True)


def test_directives(inst):
    directives = re.findall(r"diff --git a/.* b/(.*)", inst["test_patch"])
    bad = (".json", ".png", "csv", ".txt", ".md", ".jpg", ".jpeg", ".pkl", ".yml", ".yaml", ".toml")
    directives = [x for x in directives if not x.endswith(bad)]
    if inst["repo"] == "django/django":
        out = []
        for x in directives:
            x = x[:-3] if x.endswith(".py") else x
            x = x[len("tests/"):] if x.startswith("tests/") else x
            out.append(x.replace("/", "."))
        return out
    return directives


def test_env(d, env):
    e = dict(os.environ)
    e.pop("VIRTUAL_ENV", None)
    e["PATH"] = f"{env}/bin:" + e.get("PATH", "")
    e["VIRTUAL_ENV"] = str(env)
    e["PYTHONPATH"] = str(d)
    e.update({"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONWARNINGS": "ignore"})
    return e


def grade(inst, d, env, apply_gold=False, env_broken=()):
    """Apply (gold) patch if asked, reset + apply the hidden test patch, run, parse."""
    if apply_gold:
        p = subprocess.run(["git", "apply", "-"], cwd=d, input=inst["patch"], text=True, capture_output=True)
        if p.returncode != 0:
            return {"resolved": False, "error": "gold patch failed to apply: " + p.stderr[-300:]}
    test_files = re.findall(r"diff --git a/.* b/(.*)", inst["test_patch"])
    base = (d / ".git" / "swe_base").read_text().strip() if (d / ".git" / "swe_base").exists() else inst["base_commit"]
    for f in test_files:
        r = subprocess.run(["git", "checkout", "-q", base, "--", f], cwd=d, capture_output=True)
        if r.returncode != 0 and (d / f).exists():  # file is new in the test patch: remove agent's copy
            (d / f).unlink()
    p = subprocess.run(["git", "apply", "-"], cwd=d, input=inst["test_patch"], text=True, capture_output=True)
    if p.returncode != 0:
        return {"resolved": False, "error": "test patch failed to apply: " + p.stderr[-300:]}
    spec = spec_of(inst)
    cmd = spec["test_cmd"] + " " + " ".join(test_directives(inst))
    t0 = time.time()
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=d, capture_output=True, text=True, timeout=1800, env=test_env(d, env))
        log = r.stdout + "\n" + r.stderr
    except subprocess.TimeoutExpired:
        return {"resolved": False, "error": "tests timed out"}
    status = PARSERS[inst["repo"]](log, None)
    f2p = json.loads(inst["FAIL_TO_PASS"]) if isinstance(inst["FAIL_TO_PASS"], str) else inst["FAIL_TO_PASS"]
    p2p = json.loads(inst["PASS_TO_PASS"]) if isinstance(inst["PASS_TO_PASS"], str) else inst["PASS_TO_PASS"]
    ok = lambda t: status.get(t) in ("PASSED", "XFAIL")  # noqa: E731
    f_ok = sum(ok(t) for t in f2p)
    p_ok = sum(ok(t) for t in p2p)
    p2p_counted = [t for t in p2p if t not in set(env_broken)]
    res = {
        "resolved": f_ok == len(f2p) and all(ok(t) for t in p2p_counted),
        "resolved_strict": f_ok == len(f2p) and p_ok == len(p2p),
        "all_failed_p2p": [t for t in p2p if not ok(t)],
        "f2p": f"{f_ok}/{len(f2p)}", "p2p": f"{p_ok}/{len(p2p)}", "test_seconds": round(time.time() - t0, 1),
        "failed_f2p": [t for t in f2p if not ok(t)][:5], "failed_p2p": [t for t in p2p if not ok(t)][:5],
    }
    if not res["resolved"]:
        res["log_tail"] = log[-1500:]
    return res


def reset(d):
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=d, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=d, capture_output=True)


def cmd_pick(a):
    rng = random.Random(a.seed)
    per_repo = {"psf/requests": 3, "pytest-dev/pytest": 4, "sympy/sympy": 6, "django/django": 9, "pallets/flask": 1, "pylint-dev/pylint": 1}
    ids = []
    for repo, n in per_repo.items():
        pool = sorted(i for i, r in ROWS.items() if r["repo"] == repo)
        rng.shuffle(pool)
        ids += pool[:n]
    print("\n".join(ids))


def load_ids(path):
    return [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]


def cmd_gold(a):
    RESULTS.mkdir(parents=True, exist_ok=True)
    gold_path = RESULTS / "gold.json"
    gold = json.load(open(gold_path)) if gold_path.exists() else {}
    for iid in load_ids(a.ids):
        if iid in gold and not a.force:
            print(f"{iid}: cached {gold[iid]['resolved']}")
            continue
        inst = ROWS[iid]
        t0 = time.time()
        try:
            env = env_for(inst)
            d = checkout(inst)
            install_repo(inst, d, env)
            g = grade(inst, d, env, apply_gold=True)
        except Exception as e:  # noqa: BLE001
            g = {"resolved": False, "error": f"{type(e).__name__}: {e}"[:400]}
        g["seconds"] = round(time.time() - t0, 1)
        gold[iid] = g
        json.dump(gold, open(gold_path, "w"), indent=1)
        print(f"{iid}: gold resolved={g['resolved']} {g.get('f2p', '')} {g.get('p2p', '')} {g.get('error', '')[:200]} ({g['seconds']}s)", flush=True)
        shutil.rmtree(CHECKOUTS / iid, ignore_errors=True)


def cmd_run(a):
    from pramana.agent.events import Events
    from pramana.agent.orchestrator import Orchestrator
    from pramana.config import load_config
    from pramana.repo.issue import issue_from_text
    from pramana.ui.live import LiveView
    from rich.console import Console

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"run-{a.tag}.jsonl"
    done = set()
    for f in RESULTS.glob(f"run-{a.tag}*.jsonl"):
        done |= {json.loads(l)["instance_id"] for l in open(f) if l.strip()}
    if a.shard:
        out_path = RESULTS / f"run-{a.tag}-shard{a.shard.replace('/', 'of')}.jsonl"
    gold = json.load(open(RESULTS / "gold.json")) if (RESULTS / "gold.json").exists() else {}
    console = Console(width=140)
    ids = load_ids(a.ids)
    if a.shard:
        k, n = map(int, a.shard.split("/"))
        ids = [x for i, x in enumerate(ids) if i % n == k]
    for iid in ids:
        if iid in done:
            continue
        gi = gold.get(iid, {})
        gold_f2p_ok = gi.get("f2p", "0/1").split("/")[0] == gi.get("f2p", "0/1").split("/")[1]
        if gold and not gold_f2p_ok:
            print(f"{iid}: skipped (gold patch does not pass FAIL_TO_PASS in this env)")
            continue
        env_broken = gi.get("all_failed_p2p") or gi.get("failed_p2p") or []
        inst = ROWS[iid]
        cfg = load_config({"agent": {k: v for k, v in (("max_steps", a.max_steps), ("max_attempts", a.attempts)) if v}})
        if a.no_review:
            cfg.agent.review = False
        t0 = time.time()
        env = private_env(inst, a.tag)
        d = checkout(inst, a.tag)
        install_repo(inst, d, env)
        issue = issue_from_text(inst["problem_statement"])
        events = Events()
        view = LiveView(console, plain=True)
        events.subscribe(view)
        res = Orchestrator(cfg, events).solve(d, issue)
        model_patch = res.patch
        g = grade(inst, d, env, env_broken=env_broken)
        rec = {
            "instance_id": iid, "repo": inst["repo"], "difficulty": inst.get("difficulty"), "status": res.status,
            "resolved": g.get("resolved"), "grade": g, "tokens": res.usage.total_tokens, "input_tokens": res.usage.input_tokens,
            "cached_tokens": res.usage.cached_tokens, "output_tokens": res.usage.output_tokens, "calls": res.usage.calls,
            "harness_seconds": res.elapsed_s, "attempts": len(res.attempts),
            "strengths": [x.strength for x in res.attempts], "stops": [x.stop_reason for x in res.attempts],
            "steps": [x.steps for x in res.attempts], "run_dir": str(res.run_dir), "patch_chars": len(model_patch),
            "model": cfg.model.name, "provider": cfg.resolved_provider, "wall_seconds": round(time.time() - t0, 1),
            "error": res.error,
        }
        with open(out_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"\n=== {iid}: resolved={g.get('resolved')} status={res.status} f2p={g.get('f2p')} p2p={g.get('p2p')} tokens={res.usage.total_tokens} {rec['wall_seconds']}s ===\n", flush=True)
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(env, ignore_errors=True)
    rows = [json.loads(l) for f in RESULTS.glob(f"run-{a.tag}*.jsonl") for l in open(f) if l.strip()]
    solved = sum(1 for r in rows if r["resolved"])
    print(f"TOTAL {solved}/{len(rows)} resolved; tokens {sum(r['tokens'] for r in rows):,}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pick")
    p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("gold")
    p.add_argument("ids")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("ids")
    p.add_argument("--tag", default="v1")
    p.add_argument("--max-steps", type=int, dest="max_steps")
    p.add_argument("--attempts", type=int)
    p.add_argument("--no-review", action="store_true", dest="no_review")
    p.add_argument("--shard", default="", help="k/n: run every n-th instance starting at k")
    a = ap.parse_args()
    {"pick": cmd_pick, "gold": cmd_gold, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
