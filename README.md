# llmclient

Thin, stdlib-only Python library for calling local and cloud LLMs
from personal daemon-style tools. Extracted from
[bouncer](https://github.com/michaeldstenner/bouncer)'s providers
architecture; generalises across the whole stack (bouncer, watchdog,
squirrel, and future projects).

Zero runtime dependencies. Python 3.11+.

---

## Quick start

```python
from llmclient import LLMClient

# Ollama (local)
client = LLMClient.ollama("qwen3:14b", log_caller="mytool")
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
    log_caller="mytool",
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

# As a path dependency in another uv project
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
| `provider` | — | `"ollama"` \| `"anthropic"` \| `"openai"` \| `"openai_compatible"` |
| `model` | — | Model identifier as configured |
| `url` | (resolved) | Base URL; resolved from `keys.yaml` if empty |
| `timeout` | 60 | Request timeout in seconds |
| `api_key` | (resolved) | API key; resolved from env / `keys.yaml` |
| `keep_alive` | `"60m"` | Ollama: keep model resident between calls |
| `num_ctx_auto` | `True` | Ollama: auto-size context window |
| `log_caller` | `""` | Caller name for JSONL log; empty = no logging |
| `queue_mode` | `"cooperative"` | `"cooperative"` \| `"off"` |
| `priority` | 50 | Queue priority (higher runs first) |
| `caller_max` | 4 | Max concurrent Ollama slots for this caller |
| `retries` | 0 | Retry attempts on timeout / unreachable (0 = no retries) |
| `retry_delay` | 15 | Seconds to wait between retries |
| `extra_params` | `{}` | Pass-through to provider payload |

`extra_params` keys recognised by providers: `temperature`, `max_tokens`,
`num_predict`, `num_ctx`, `keep_alive`, `timeout`.

---

## `LLMResult` fields

| Field | Type | Description |
|-------|------|-------------|
| `text` | str \| None | Response text; None on failure |
| `outcome` | str | `"success"` \| `"timeout"` \| `"timeout:model_loaded_but_slow"` \| `"timeout:model_not_loaded"` \| `"http_NNN"` \| `"error:unreachable"` \| `"error:…"` \| `"aborted"` |
| `total_s` | float | Wall-clock: `queue_wait_s + call_s` |
| `queue_wait_s` | float | Time blocked waiting for an Ollama slot |
| `call_s` | float | Time from HTTP send to response |
| `inference_s` | float | Pure token generation (Ollama body field; cloud: == `call_s`) |
| `load_s` | float | Model cold-start time (Ollama body field; cloud: 0.0) |
| `prompt_chars` | int | `len(system) + len(user)` |
| `response_chars` | int | `len(text)` or 0 on failure |
| `prompt_tokens` | int \| None | From provider response body |
| `response_tokens` | int \| None | From provider response body |
| `is_success` | bool | `True` when `outcome == "success"` (property) |

---

## Key resolution

`url` and `api_key` are resolved in this order (first non-empty wins):

1. Explicit value in `LLMConfig`
2. Standard env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
3. `~/.config/llmclient/keys.yaml`

**`~/.config/llmclient/keys.yaml`** format:

```yaml
anthropic:
  api_key: sk-ant-...

openai:
  api_key: sk-...

ollama:
  url: http://localhost:11434
  parallel_slots: 4   # must match OLLAMA_NUM_PARALLEL
```

`parallel_slots` sets the global cap for the cooperative queue.
Default is 4 when the file is absent or the key is missing.

---

## JSONL call log

When `log_caller` is set, one line is appended to
`~/.local/share/<caller>/llm_calls.jsonl` after every call:

```json
{
  "timestamp":        "2026-05-02T19:46:46.121",
  "caller":           "watchdog",
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

`elapsed_s` is `queue_wait_s + call_s` (total wall-clock), matching the
base schema defined in `agent_tools/docs/llm-call-logging.md`.

Quick diagnostics:

```sh
# All calls with timing, newest-last
tail -50 ~/.local/share/bouncer/llm_calls.jsonl \
  | jq -r '[.timestamp,.outcome,.elapsed_s,.inference_s,.load_s] | @tsv'

# Only failures
jq 'select(.outcome != "success")' \
  ~/.local/share/watchdog/llm_calls.jsonl
```

---

## Cooperative queue

When `queue_mode="cooperative"` (default for Ollama), calls enter a
shared SQLite queue at `~/.local/share/llmclient/queue.db` and wait
until a slot is free. This prevents silent pileup inside Ollama when
multiple daemons (bouncer, watchdog, squirrel) call the same instance.

**Recommended per-caller config:**

| Caller | `priority` | `caller_max` |
|--------|-----------|-------------|
| bouncer | 100 | 4 |
| watchdog | 10 | 1 |
| squirrel | 10 | 1 |

Promotion rules (all atomic, inside `BEGIN IMMEDIATE`):

1. Reap rows whose PID is gone (crash-safe).
2. `global_running < global_max` (from `keys.yaml`).
3. `caller_running < caller_max`.
4. No higher-priority *eligible* waiter exists (a blocked caller
   does not prevent lower-priority ones from proceeding).

Pass an `abort_event: threading.Event` to `LLMClient` to cancel
waiting or mid-inference — the result will have `outcome="aborted"`.

---

## Migrating from bouncer providers

The old signature in bouncer's `providers/__init__.py`:

```python
call_llm(tool_name, tool_input, cwd, config)
  -> (decision, reason, prompt_chars)
```

New signature:

```python
client.call(user, system)
  -> LLMResult   # .text, .outcome, .total_s, ...
```

Bouncer keeps its own `_build_prompt` / `_parse_llm_text` logic.
The migration wrapper looks like:

```python
from llmclient import LLMClient, LLMConfig

def call_llm(tool_name, tool_input, cwd, config):
    cfg = LLMConfig(
        provider=config["llm"]["provider"],
        model=config["llm"]["model"],
        timeout=config["llm"].get("timeout", 25),
        log_caller="bouncer",
        priority=100,
        caller_max=4,
    )
    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    result = LLMClient(cfg, abort_event=ABORT_EVENT).call(
        user=user_text,
        system=system_text,
        operation="classify",
        context={"cwd": str(cwd), "tool": tool_name},
    )
    if result.text is None:
        return None, result.outcome, len(system_text) + len(user_text)
    return _parse_llm_text(result.text) + (result.prompt_chars,)
```

---

## `llmc` CLI

A diagnostic and probe CLI installed as `llmc`:

```sh
llmc status                             # Ollama state, connections, queue
llmc queue                              # queue state only
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

This prevents bouncer-style short-prompt callers from requesting a
smaller context than what is already loaded, which would force Ollama
to reload the model and block behind any in-progress inference at
the larger size. The ratchet resets naturally when all models unload
during idle periods.

---

## Running tests

```sh
just install   # uv pip install -e ".[dev]"
just test      # pytest -v
```
