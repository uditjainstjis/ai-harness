# SWE-bench Verified, locally (no Docker)

How the numbers in the main README were produced. Real GitHub issues, graded by their hidden
tests, through the same code path as `make run`.

```bash
export PRAMANA_SWE_WORK=~/pramana_work            # scratch space (checkouts, envs, results)
uv venv --python 3.12 $PRAMANA_SWE_WORK/toolsenv
uv pip install --python $PRAMANA_SWE_WORK/toolsenv/bin/python --no-deps "swebench==3.0.17"
uv pip install --python $PRAMANA_SWE_WORK/toolsenv/bin/python docker requests unidiff tqdm -e ../..
# dataset rows (500 instances) -> $PRAMANA_SWE_WORK/swe_verified.json (see swe_eval.py header)
python swe_eval.py pick --seed 0 > ids.txt       # stratified sample of easy-to-install repos
python swe_eval.py gold ids.txt                  # validate each environment with the official patch
python swe_eval.py run ids.txt --tag v1 --shard 0/2 &  python swe_eval.py run ids.txt --tag v1 --shard 1/2
python swe_report.py v1
```

* Checkout = source tarball of `base_commit` turned into a one-commit git repo; per-task uv virtualenv
  built from SWE-bench's own version specs (`SETUPTOOLS_SCM_PRETEND_VERSION` for tag-less trees).
* **Gold validation first:** a task counts only if the official patch passes its FAIL_TO_PASS
  tests here; PASS_TO_PASS tests that fail even with the official patch in this environment
  (platform-specific) are excluded from grading.
* A task is **resolved** when all FAIL_TO_PASS and all remaining PASS_TO_PASS tests pass after the
  hidden test patch is applied to the harness's working tree.
