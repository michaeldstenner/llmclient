# llmclient

Thin, stdlib-only Python library for calling local and cloud LLMs.
Supports Ollama, Anthropic, and any OpenAI-compatible endpoint.

Zero runtime dependencies. Python 3.11+.

---

## Quick start

```python
from llmclient import LLMClient

# Ollama (local)
client = LLMClient.ollama("qwen3:14b", log_caller="myapp")
result = client.call(
    user="What is 2 + 2?",
    system="Reply with only the number.",
    operation="arithmetic",
)
print(result.text)        # "4"
print(result.inference_s) # time spent on token generation

# Anthropic
client = LLMClient.anthropic(
    "claude-haiku-4-5-20251001",
    log_caller="myapp",
)

# OpenAI-compatible (LM Studio, Groq, etc.)
client = LLMClient.openai_compatible(
    "llama-3.1-8b",
    url="http://localhost:1234",
)
```

---

## Installation

```sh
# From the local repo (editable)
uv pip install -e /path/to/llmclient

# As a git dependency in another uv project
uv add git+https://github.com/michaeldstenner/llmclient
```

---

## API

### `LLMClient(cfg, *, abort_event=None)`

The main class. Convenience constructors are usually cleaner:

```python
LLMClient.ollama(model, *, abort_event=None, **cfg_kwargs)
LLMClient.anthropic(model, *, abort_event=None, **cfg_kwargs)
LLMClient.openai_compatible(model, *, abort_event=None, **cfg_kwargs)
LLMClient.claude_code(model="", *, abort_event=None, **cfg_kwargs)
LLMClient.from_profile(provider, model, *, abort_event=None, **cfg_kwargs)
LLMClient.from_dict(d, *, abort_event=None)
```

`from_profile` and `from_dict` set `queue_mode="cooperative"`
automatically for Ollama and `queue_mode="off"` for cloud providers.

`from_dict` accepts a flat dict (e.g. a YAML config stanza). `provider`
is optional and inferred from `url` if absent (port 11434 or "ollama" in
URL → `"ollama"`; `anthropic.com` → `"anthropic"`; anything else →
`"openai_compatible"`). All `LLMConfig` fields are accepted as keys.

```python
client = LLMClient.from_dict({
    'url':   'http://localhost:11434',
    'model': 'qwen3:32b',
    'extra_params': {'chat_template_kwargs': {'enable_thinking': False}},
})
```

### `client.call(user, system="", *, operation="call", context=None)`

Make a synchronous LLM call. Returns an `LLMResult`.

| Param | Type | Description |
|-------|------|-------------|
| `user` | str | User message |
| `system` | str | System prompt (empty = omitted) |
| `operation` | str | Label for the JSONL log (`"classify"`, etc.) |
| `context` | dict | Extra fields written to JSONL `context` object |

---

## `LLMConfig` fields

| Field | Default | Description |
|-------|---------|-------------|
| `provider` | — | `"ollama"` \| `"anthropic"` \| `"openai"` \| `"openai_compatible"` \| `"claude_code"` |
| `model` | — | Model identifier as configured |
| `url` | (resolved) | Base URL; resolved from `config.yaml` if empty |
| `timeout` | 60 | Request timeout in seconds |
| `api_key` | (resolved) | API key; resolved from env / `config.yaml` |
| `keep_alive` | `"60m"` | Ollama: keep model resident between calls |
| `num_ctx_auto` | `True` | Ollama: auto-size context window |
| `log_caller` | `""` | Caller name for JSONL log; empty = no logging |
| `queue_mode` | `"cooperative"` | `"cooperative"` \| `"off"` |
| `queue_timeout` | `None` | Seconds to wait in queue before giving up (`None` = unbounded) |
| `queue_stall_timeout` | `None` | Seconds without any inference completing before declaring the queue frozen; produces `timeout:queue_stall` (`None` = disabled) |
| `priority` | 50 | Queue priority (higher runs first) |
| `caller_max` | 4 | Max concurrent slots for this caller, per model |
| `first_token_timeout` | `None` | Ollama streaming: seconds from HTTP send to first token; enables streaming when set |
| `generation_timeout` | `None` | Ollama streaming: seconds from first token to completion; falls back to `timeout` |
| `retries` | 0 | Retry attempts on `timeout:generation` / unreachable (0 = no retries) |
| `retry_delay` | 15 | Seconds to wait between retries |
| `circuit_n` | 0 | Trip circuit after N consecutive triggering failures (0 = disabled) |
| `circuit_key` | `""` | Optional circuit scope override; defaults to `log_caller` |
| `circuit_cooldown_s` | 120.0 | Seconds before a tripped circuit allows a probe request |
| `circuit_triggers` | see below | Outcome strings that increment the failure counter |
| `extra_params` | `{}` | Pass-through to provider payload |

Default `circuit_triggers`:
`("timeout:queue_wait", "timeout:first_token", "error:unreachable")`.
`timeout:generation` is intentionally excluded — inference started, the
model is not unavailable.

`extra_params` keys recognised by providers: `temperature`, `max_tokens`,
`num_predict`, `num_ctx`, `keep_alive`, `timeout`, `first_token_timeout`,
`generation_timeout`, `tools` (claude_code only — passed as `--tools`
to the CLI).

The `claude_code` provider shells out to `claude --print` rather than
hitting the API directly, so it uses your Claude subscription rather
than per-token API billing. The `claude` binary is resolved via the
`LLMCLIENT_CLAUDE_BIN` env var, `PATH`, or `~/.local/bin/claude`. The
`model` field maps to `--model`; leave it empty to use Claude Code's
configured default. `url` and `api_key` are ignored.

---

## `LLMResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `text` | str \| None | Response text; None on failure |
| `outcome` | str | `"success"` \| `"aborted"` \| `"circuit_open"` \| `"timeout:queue_wait"` \| `"timeout:queue_stall"` \| `"timeout:first_token"` \| `"timeout:generation"` \| `"error:unreachable"` \| `"http_NNN"` \| `"error:…"` |
| `total_s` | float | Wall-clock: `queue_wait_s + call_s` |
| `queue_wait_s` | float | Time blocked waiting for an Ollama slot |
| `call_s` | float | Time from HTTP send to response |
| `inference_s` | float | Pure token generation (Ollama body field; cloud: == `call_s`) |
| `load_s` | float | Model cold-start time (Ollama body field; cloud: 0.0) |
| `prompt_chars` | int | `len(system) + len(user)` |
| `response_chars` | int | `len(text)` or 0 on failure |
| `prompt_tokens` | int \| None | From provider response body |
| `response_tokens` | int \| None | From provider response body |
| `queue_snapshot` | list[dict] \| None | Queue state at timeout; present on `timeout:queue_wait` and `timeout:queue_stall`; each entry: `{id, pid, caller, model, priority, status, age_s, running_s}` |
| `is_success` | bool | `True` when `outcome == "success"` (property) |

---

## App-specific configuration

Call `llmclient.configure()` once at application startup:

```python
import llmclient
llmclient.configure(
    config_dir="~/.config/myapp",      # app-specific config overlay
    data_dir="~/.local/share/myapp",   # dedicated queue + log dir
    log_level="all",                   # "off" | "errors" | "all"
)
```

All parameters are optional.

`config_dir` is layered on top of `~/.config/llmclient/config.yaml` —
shared keys stay in the global file; only app-specific values go in
the app file.

`data_dir` controls where `queue.db` and `llmclient_log.jsonl` live.
Apps sharing a `data_dir` share both a queue and a log.  Apps that
want independent Ollama slot budgets should each point at a separate
directory.  Defaults to `~/.local/share/llmclient/`.

`log_level`:
- `"off"` — nothing logged
- `"errors"` — non-success outcomes only (default)
- `"all"` — every call; queue snapshot always included

### Viewing logs

```
llmc log                # non-success entries from default data dir
llmc log --all          # all entries
llmc log --caller bouncer --last 20
llmc --dir ~/.local/share/bouncer/ log --all
```

---

## Key / URL resolution

`url` and `api_key` are resolved in this order (first non-empty wins):

1. Explicit value in `LLMConfig`
2. Standard env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
3. `{config_dir}/config.yaml` (if `configure(config_dir=...)` was called)
4. `~/.config/llmclient/config.yaml`
5. `~/.config/llmclient/keys.yaml` (legacy name, still supported)

**`config.yaml`** format (same for app-specific and global):

```yaml
anthropic:
  api_key: sk-ant-...

openai:
  api_key: sk-...

ollama:
  url: http://localhost:11434
  parallel_slots: 4   # must match OLLAMA_NUM_PARALLEL
```

`parallel_slots` sets the per-model global cap for the cooperative
queue.  Requests for different models do not compete for the same
slots.
Default is 4 when the file is absent or the key is missing.

---

## JSONL call log

When `log_caller` is set, one line is appended to
`~/.local/share/<caller>/llm_calls.jsonl` after every call:

```json
{
  "timestamp":        "2026-05-02T19:46:46.121",
  "caller":           "myapp",
  "operation":        "classify",
  "provider":         "ollama",
  "model":            "qwen3:14b",
  "prompt_chars":     1834,
  "prompt_tokens_est": 524,
  "prompt_tokens":    36,
  "response_tokens":  8,
  "queue_wait_s":     12.3,
  "call_s":           7.1,
  "inference_s":      6.8,
  "load_s":           0.0,
  "elapsed_s":        19.4,
  "outcome":          "success",
  "response_chars":   87,
  "context":          {}
}
```

`elapsed_s` is `queue_wait_s + call_s` (total wall-clock).

Quick diagnostics:

```sh
# All calls with timing, newest-last
tail -50 ~/.local/share/myapp/llm_calls.jsonl \
  | jq -r '[.timestamp,.outcome,.elapsed_s,.inference_s,.load_s] | @tsv'

# Only failures
jq 'select(.outcome != "success")' \
  ~/.local/share/myapp/llm_calls.jsonl
```

---

## Cooperative queue

When `queue_mode="cooperative"` (default for Ollama), calls enter a
shared SQLite queue at `~/.local/share/llmclient/queue.db` and wait
until a slot is free. This prevents silent pileup when multiple
processes share the same Ollama instance.

Example priority configuration for two callers:

| Caller | `priority` | `caller_max` | `queue_timeout` |
|--------|-----------|-------------|----------------|
| interactive | 100 | 4 | 8s |
| background  | 10  | 1 | None |

Promotion rules (all atomic, inside `BEGIN IMMEDIATE`):

1. Reap rows whose PID is gone (crash-safe).
2. `model_running < global_max` — counts only rows for the same
   model; requests for different models do not compete.
3. `caller_model_running < caller_max` — per-caller cap, also
   scoped to the same model.
4. No higher-priority *eligible* waiter for the same model exists
   (a blocked caller does not prevent lower-priority ones from
   proceeding).

Set `queue_timeout` to fail fast when Ollama is saturated rather
than waiting indefinitely. Outcome will be `"timeout:queue_wait"`.

Pass an `abort_event: threading.Event` to `LLMClient` to cancel
waiting or mid-inference — the result will have `outcome="aborted"`.

### Streaming and two-phase timeouts (Ollama)

Set `first_token_timeout` to enable streaming mode. The call is split
into two phases with independent deadlines:

- **Phase 1** (`first_token_timeout`): time from HTTP send to first
  token. Times out as `"timeout:first_token"` — ollama has not yet
  started on the request.
- **Phase 2** (`generation_timeout`, default: `timeout`): time from
  first token to completion. Times out as `"timeout:generation"` —
  inference started but is running slow.

`timeout:first_token` triggers the circuit breaker (ollama unavailable).
`timeout:generation` does not (inference started; the model is working).

### Circuit breaker

Set `circuit_n > 0` to enable. After `circuit_n` consecutive triggering
failures for a given circuit key, the circuit trips for
`circuit_cooldown_s` seconds. During that window, calls return
`"circuit_open"` immediately without touching Ollama. After the cooldown,
one probe request is allowed through; success resets the circuit, failure
restarts the cooldown.

By default the circuit key is `log_caller`, preserving caller-wide circuit
behavior. Set `circuit_key` to a more specific value when one caller needs
independent circuit state for different backends, models, or test fixtures.

Circuit state is stored in the same SQLite DB as the queue
(`circuit_state` table), so it is shared across processes.

---

## `llmc` CLI

A diagnostic and probe CLI installed as `llmc`:

```sh
llmc status                             # Ollama state, connections, queue
llmc queue                              # queue state only
llmc log                                # recent warn/error entries (last 5)
llmc log --all --last 20                # all outcomes, last 20
llmc log --caller bouncer               # filter to one caller
llmc log --json                         # raw JSONL output
llmc call -m MODEL PROMPT               # single call, full timing
llmc call -m MODEL -s "system prompt" PROMPT
llmc call -m MODEL --json PROMPT        # full LLMResult as JSON
llmc call -p anthropic -m MODEL PROMPT
llmc parallel -m MODEL -n 4 PROMPT     # N concurrent calls
```

`llmc status` shows:
- Loaded Ollama models, context size, active slot count (N/MAX)
- All direct connections to `:11434` grouped by process, with the
  script path for Python/bash processes and saturation warnings
- llmclient queue state

`llmc parallel` bypasses the queue and sends N requests
simultaneously, reporting per-request timing and overall speedup
vs. serial — useful for verifying `OLLAMA_NUM_PARALLEL` is working.

---

## Context window ratchet

When `num_ctx_auto=True`, the Ollama provider queries `/api/ps`
before sending a request and applies an upward-only ratchet:

```
effective_num_ctx = max(computed_num_ctx, loaded_ctx)
```

This prevents short-prompt callers from requesting a smaller context
than what is already loaded, which would force Ollama to reload the
model and block behind any in-progress inference at the larger size.
The ratchet resets naturally when all models unload during idle
periods.

---

## Running tests

```sh
just install   # uv pip install -e ".[dev]"
just test      # pytest -v
```
