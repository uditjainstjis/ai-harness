# Pramana

**An autonomous coding-agent harness whose patches carry their own proof.**

*Pramāṇa* (Sanskrit): a means of valid knowledge, i.e. evidence. Every change Pramana makes is
re-executed against the **original** code and the **patched** code, so a fix comes with a
fail→pass demonstration instead of a claim. Same model as everyone else; the harness does the
engineering.

```bash
export AI_API_KEY="<key>"     # provider auto-detected from the key (OpenAI, Anthropic, Gemini, Groq, OpenRouter, ...)
make setup                    # installs into ./.venv (uv if present, else venv + pip)
make run                      # interactive: give it a repo + a GitHub issue (URL, owner/repo#N, file, or pasted text)
make test                     # offline unit tests + (with a key) the end-to-end benchmark with hidden tests
```

Non-interactive: `make run REPO=/path/or/git-url ISSUE=https://github.com/o/r/issues/123 TEST="pytest tests/test_x.py"`
(the issue can also be piped on stdin).

---

## How it works

```
 issue ──▶ Intake ──▶ Zero-token repro ──▶ Localize ──▶ Acceptance ──▶ Agent loop ──▶ Submit gate ──▶ Independent ──▶ Reviewer ──▶ Evidence
          repo scan,  run the issue's      traceback,   criteria       (one context,  every check    test writer    predicts     bundle +
          lang/tests, code snippets on     paths,       (maintainer's  7 tools, loop  run on the     (blind agent,  maintainer's  run index
          env probe   the ORIGINAL code    symbols,     checklist,     & budget       ORIGINAL and   never sees     test, checks
                      (traceback feeds     BM25, test   1 call)        guards)        PATCHED code   the patch)     the diff
                      localization)        imports                                    │                             │
                                                                                      └── rejected → back to agent ◀┘
                                           attempt 2 (fresh context + lessons) only if attempt 1 ends without proof
```

### Five layers of evidence, every one of them executed

| # | layer | what runs | what it proves |
|---|---|---|---|
| 1 | **issue snippet** | code blocks from the issue, run on the original code before the model's first turn | the bug is real here; the traceback seeds localization (0 model tokens) |
| 2 | **agent reproduction** | the agent's script, run by the harness on original **and** patched code | fail → pass |
| 3 | **related existing tests** | test files for every changed module, selected automatically (the agent cannot skip them) | pass → pass (no regression) |
| 4 | **independent regression test** | written by a *second* agent that sees only the issue, never the patch | the fix satisfies the issue as a maintainer would test it, not just the agent's own reading |
| 5 | **reviewer** | fresh-context review that first predicts the maintainers' regression test, then checks the diff against it | root cause, sibling cases, unchanged behaviour |

A check that passed on the original code but fails with the patch is a **regression** and the
submission is returned with the failure output; a reproduction or independent test that still fails
is returned the same way. Pre-existing failures (fail → fail in unrelated tests) are recognised and
ignored, and the agent can ask the same question at any time with the `compare` tool.

### The agent-computer interface

- **`str_replace_editor`** accepts near-misses safely: pasted line numbers, trailing whitespace and
  a different indentation scheme are tolerated (re-indented level by level), but only for a
  **unique** match. On a miss it shows the most similar region with line numbers. **Lint gate:** an
  edit that would make a Python/JSON/JS/TOML file unparseable is rejected with the exact error.
  Editing back to an earlier version of a file is detected and called out (oscillation).
- **`compare`** runs any command on the original and on the current code and says *fixed /
  regression / pre-existing*: the antidote to agents that "fix" unrelated failing tests.
- **`search`**, **`find_definition`**, **`find_files`**: ripgrep → `git grep` → Python fallbacks;
  symbols via Python `ast`, universal-ctags or regex; results grouped and capped.
- **`bash`**: own process group, timeout, closed stdin, head+tail truncation, the target repo's
  virtualenv activated, a `python`→`python3` shim where needed, a denylist (sudo, `git push`,
  `rm -rf /`, ...), and the model's API key scrubbed from the environment.
- Argument tolerance: every common spelling of a line range (`view_range`, `line_start/line_end`,
  `start/end`, `offset/limit`, `"120-250"`), alias tool names (`view`, `create`, `execute_bash`,
  `functions.bash`, ...), and tools inferred from argument shape. Unknown arguments are never
  ignored silently: the result says which ones were ignored.

### Model robustness

Native tool calling where available; automatic switch to a text protocol when an endpoint rejects
tools; tool calls written as text (XML, `<invoke>`, Hermes, Qwen, JSON) are recovered, and
anything a model "imagines" after its calls (self-written results) is discarded. Unsupported
parameters are dropped and remembered (`temperature` on reasoning models, `max_tokens` vs
`max_completion_tokens`, ...). 5xx errors get retries that *perturb* the request (some endpoints
fail deterministically on one transcript). Reasoning models (gpt-oss) get their own recent reasoning
passed back. Context overflow triggers compaction.

### Efficiency

Append-only transcript (provider prompt caching works) until the prompt crosses a threshold, then
one batch compaction; file views made stale by an edit are elided immediately (their line numbers
are wrong anyway); full-suite test runs are flagged. Second attempts, the independent test writer
and the reviewer only run when the evidence calls for them. Every run reports tokens (input /
cached / output), model calls and wall time.

## Evidence bundle

Every run writes `runs/<timestamp>-<issue>/`:

| file | contents |
|---|---|
| `report.md`, `report.html` | verdict, root-cause summary, before/after table, diff, tokens, attempts |
| `patch.diff` | the change, `git apply`-able |
| `evidence.json` | machine-readable: checks, verdicts, usage, timings, config (never the key) |
| `trajectory.jsonl` | every model call, tool call and tool result |
| `transcript_attemptN.json` | the full conversation per attempt |
| `scratch/` | the agent's reproduction scripts |

The target repository is left with **only the fix** applied (scratch files are moved into the
bundle).

## Model configuration

`pramana.toml` holds the model configuration; the credential only ever comes from `AI_API_KEY`.

| key prefix | provider | default model (override with `[model] name` or `AI_MODEL`) |
|---|---|---|
| `sk-ant-` | Anthropic (native tool use + prompt caching) | `claude-sonnet-5` |
| `AIza` | Gemini (OpenAI-compatible endpoint) | `gemini-2.5-flash` |
| `gsk_` | Groq | `openai/gpt-oss-120b` |
| `sk-or-` | OpenRouter | `openai/gpt-oss-120b` |
| `xai-`, `nvapi-`, `csk-`, `hf_`, `fw_`, `tgp_` | xAI, NVIDIA, Cerebras, Hugging Face, Fireworks, Together | see `pramana/config.py` |
| `sk-` | OpenAI | `gpt-5-mini` |
| anything else | any OpenAI-compatible endpoint: set `AI_BASE_URL` (+ `AI_MODEL`) | |

If the Organising Committee prescribes a model, set `name` in `pramana.toml` (or `AI_MODEL`);
nothing else changes. `make doctor` checks connectivity and whether native tool calling works.
Runs are deterministic where the provider allows it (temperature 0, fixed seed).

## Layout

```
pramana/
  cli.py                 make run / solve / doctor / bench
  config.py              pramana.toml + env, provider detection
  llm/                   OpenAI-compatible, Anthropic, text protocol, (dev) Claude CLI, mock
  tools/                 shell, editor (tolerant matching + lint gate), search, tool schemas
  repo/                  intake, git tracking, symbols, localization, issue fetching, env bootstrap
  agent/                 prompts, loop, submit gate, context manager, orchestrator
  report/                evidence bundle writer
  ui/                    live terminal view
bench/tasks/             end-to-end tasks with hidden tests (make test)
tests/                   offline unit tests
```
