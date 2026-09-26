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
 issue ─┐
        ▼
 ┌────────────┐   ┌─────────────────┐   ┌───────────────────────────────┐   ┌──────────────┐
 │  Intake    │──▶│  Localize       │──▶│  Agent loop (one context)      │──▶│  Submit gate  │──┐
 │ repo scan, │   │ traceback/path/ │   │ bash · str_replace_editor ·    │   │ re-runs every │  │
 │ lang+tests,│   │ symbol/BM25     │   │ search · find_definition ·     │   │ check on the  │  │
 │ env probe  │   │ ranking (0 tok) │   │ find_files · submit            │   │ ORIGINAL and  │  │
 └────────────┘   └─────────────────┘   │ guards: loops, edit failures,  │   │ PATCHED code  │  │
                                        │ budget, context compaction     │◀──│ reject: regr./│  │
                                        └───────────────────────────────┘   │ still failing │  │
                                                     ▲                       └──────────────┘  │
                                                     │ attempt 2 (fresh context + lessons)      │
                                                     └──── only if attempt 1 ended without proof┘
                                                                                                ▼
                                        evidence bundle: report.md/html · patch.diff · evidence.json · trajectory
```

**1. Deterministic localization (zero model tokens).** Before the model sees anything, Pramana
ranks likely-relevant files from traceback frames, quoted paths, identifiers resolved through a
symbol index (Python `ast`, universal-ctags or regex fallback), and BM25 over the codebase. The
model starts at the right place instead of spending turns exploring.

**2. An agent-computer interface built for weak and strong models alike.**
- `str_replace_editor` accepts near-misses safely: pasted line numbers, trailing whitespace and
  different indentation schemes are tolerated, but only for a **unique** match. On a miss it
  shows the most similar region with line numbers, so the model recovers in one step.
- **Lint gate:** an edit that would make a file unparseable is rejected and rolled back with the
  exact syntax error (Python, JSON, JS, TOML).
- Commands run in their own process group with a timeout, closed stdin, head+tail truncation, the
  target repo's virtualenv activated, and a denylist (sudo, `git push`, `rm -rf /`, ...). The model's
  API key is scrubbed from every command's environment.

**3. The submit gate turns claims into evidence.** When the agent submits, the harness runs its
reproduction, its chosen tests and **automatically selected related tests** twice: once on the
patched tree and once on the original tree (the patch is reverse-applied through a private
temporary git index, so the user's index, HEAD and stash are never touched). Each check becomes:

| verdict | original → patched | effect |
|---|---|---|
| **fixes** | fail → pass | proof of the fix |
| no regression | pass → pass | fine, not proof |
| **REGRESSION** | pass → fail | submission rejected with the failure output |
| still failing | fail → fail | rejected if it is the agent's own reproduction or the acceptance test |

A submission with no fail→pass check is sent back once with "write a reproduction that fails on
the original code". An independent reviewer pass (fresh context: issue + diff + evidence only)
can return concrete defects once.

**4. Adaptive compute.** A second attempt (reset tree, fresh context, a digest of what failed)
runs **only** when the first ends without proof. Easy issues cost one trajectory; hard ones get
another try. The best attempt is chosen by evidence, then by patch size.

**5. Recovery.** Provider errors are retried with backoff; unsupported parameters are dropped
and remembered (`temperature` on reasoning models, `max_tokens` vs `max_completion_tokens`,
...); context overflow triggers compaction; malformed tool arguments are repaired or bounced with
the raw text; alias tool names (`view`, `execute_bash`, `read_file`, ...) are mapped onto the
declared schema; a model that writes tool calls as text is parsed anyway (XML, Hermes, Qwen, JSON
styles); an endpoint without native tool calling is switched to a text protocol automatically.
Loop detection, repeated edit failures, no-progress and budget warnings are injected as
harness notes.

**6. Efficiency.** The transcript is append-only so provider prompt caching works; old tool output
is elided in one pass only once the prompt crosses a threshold; the reviewer and the second
attempt run only when the evidence says they are needed. Every run reports tokens (input /
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
