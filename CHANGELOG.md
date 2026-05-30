# Changelog

## [0.5.2] — 2026-05-30

### Added

- **`llmclient.configure(config_dir, queue_db)`** — new module-level
  function for app-specific configuration.  Call once at startup.

- **`config.yaml`** — new preferred config file name.  Replaces
  `keys.yaml` (which is still read as a legacy fallback).  The file
  format is identical; the new name reflects that it holds more than
  just keys (URLs, `parallel_slots`, etc.).

- **Layered config resolution** — when `configure(config_dir=...)` is
  called, llmclient reads `{config_dir}/config.yaml` as a top-priority
  overlay on `~/.config/llmclient/config.yaml`.  Per-key precedence:
  app config beats global config beats legacy `keys.yaml`.  This lets
  apps ship their own config without requiring users to maintain a
  separate llmclient config folder.

- **Per-app queue DB** — `configure(queue_db=...)` redirects the
  cooperative SQLite queue to a different file.  Apps that want an
  independent Ollama slot budget can each point at their own DB;
  apps that want shared slot management can point at the same file.
  Defaults to `~/.local/share/llmclient/queue.db` (unchanged).

## [0.5.0] — 2026-05-30

### Changed

- **Queue slot accounting is now per-model.**  Previously `global_max`
  counted all concurrent Ollama requests together — a nomic-embed-text
  call consumed one of qwen3's slots.  Now `global_max` and
  `caller_max` are each scoped to a model; requests for different
  models do not compete.  This corrects over-throttling in mixed
  embed+generation workloads.

- **`queue.db` schema** — new `model TEXT NOT NULL DEFAULT ''` column
  on the `queue` table.  Existing DBs auto-migrate via `ALTER TABLE`
  on first open.

- **`queue_snapshot` entries** — include `model` field.

- **`release(queue_id, model="")` signature** — now accepts an
  optional model name.  Writes `last_release_at:<model>` to
  `queue_meta` alongside the global key so stall detection for a slow
  model is not masked by completions from a different model.  Internal
  API; callers using `LLMClient` are unaffected.

### Changed (CLI)

- **`llmc status` output restructured** — CONNECTIONS section is now
  first (grouped by model with per-model slot counts), followed by
  OLLAMA (model list + expiry), then QUEUE.  Queue-managed connections
  are grouped under their model; unmanaged connections (PIDs not in the
  queue) appear in a separate `unmanaged` block with an idle-keep-alive
  caveat.  Running rows show time-since-started; waiting rows show
  time-since-submitted.

- **`llmc queue` table** — includes `model` column.

---

## [0.4.1] — 2026-05-28

### Added

- **`LLMConfig.queue_stall_timeout`** (`float | None`, default `None`)
  — fires `timeout:queue_stall` if no inference has completed within
  this window while a caller is waiting.  Distinguishes "busy but
  moving" from "Ollama has stopped doing work."  Complements
  `queue_timeout` (absolute wall-clock ceiling).

- **`timeout:queue_stall` outcome** — new outcome value.  Carries a
  queue snapshot like `timeout:queue_wait`.  Not in the default
  `circuit_triggers` set; callers decide whether to treat it as a
  circuit-breaker trigger.

- **`LLMResult.queue_snapshot`** (`list[dict] | None`, default `None`)
  — queue state captured at the moment a `timeout:queue_wait` or
  `timeout:queue_stall` fires.  Each entry: `{ id, pid, caller,
  priority, status, age_s, running_s }`.  Included in the llmclient
  call log when present.

- **`queue_meta` table** in `queue.db` — tracks `last_release_at`,
  updated on every `release()` call.  Powers the stall-detection check.

- **`llmc log` subcommand** — shows recent entries from
  `~/.local/share/*/llm_calls.jsonl`.  Flags: `--warn` (default),
  `--error`, `--all`; `--last N` (default 5); `--caller NAME`;
  `--json`.  Shows queue snapshot inline when present.

- **`docs/profiles.md`** — recommended `LLMConfig` settings for three
  caller scenarios: *Fail Fast* (interactive, fallback available),
  *Hurry* (interactive, result required), and *No Rush* (background
  batch).

### Changed

- `_queue.acquire()` returns a 4-tuple
  `(queue_id, wait_s, reason, snapshot)` instead of 3-tuple.  New
  reason value: `"queue_stalled"`.  **Internal API** — not public, but
  any code calling `acquire()` directly needs updating.

- `_queue.release()` now writes `last_release_at` to `queue_meta` in
  the same transaction as the row deletion.

- `LLMConfig.queue_stall_timeout` added between `queue_timeout` and
  `priority` in field order (frozen dataclass — positional construction
  will break; keyword construction is unaffected).

---

## [0.4.0] — 2026-05-27

### Added

- **`LLMConfig.queue_timeout`** (`float | None`) — how long to wait
  for a queue slot before returning `timeout:queue_wait`.  `None` waits
  indefinitely (previous behavior).

- **`LLMConfig.first_token_timeout`** (`int | None`) — enables
  streaming mode on Ollama; fires `timeout:first_token` if the first
  token isn't received within this window.

- **`LLMConfig.generation_timeout`** (`int | None`) — maximum time
  allowed for token generation after the first token arrives.  Falls
  back to `timeout` when unset.  Fires `timeout:generation`.

- **Circuit breaker** — `LLMConfig.circuit_n`, `circuit_cooldown_s`,
  `circuit_triggers`.  Tracks consecutive triggering failures per caller
  in a new `circuit_state` table in `queue.db`.  Half-open probe support.
  Outcome `circuit_open` when tripped.

- **`acquire()` reason field** — now returns a 3-tuple
  `(queue_id, wait_s, reason)`.  Reason: `"ok"`, `"aborted"`, or
  `"queue_timeout"`.

### Changed

- Ollama provider now uses streaming when `first_token_timeout` is set.
  Two independent deadlines replace the previous single timeout.

- Retired outcome strings `timeout:model_loaded_but_slow` and
  `timeout:model_not_loaded`; non-streaming Ollama timeouts now produce
  `timeout:generation`.

---

## [0.3.1] — 2026-05-28 (unreleased fix, included in 0.4.0 tag)

### Fixed

- **Queue reaper swept only `running` rows** — dead PIDs in `waiting`
  status were never reaped.  A ghost waiter with high priority would
  block all live lower-priority callers indefinitely.  Reaper now sweeps
  all rows regardless of status.

---

## [0.3.0] — 2026-05-11

### Added

- **`claude_code` provider** — shells out to the Claude Code CLI
  (`claude -p`) instead of hitting the Anthropic API directly.  Enables
  subscription-billed usage without an API key.  Resolves binary via
  `LLMCLIENT_CLAUDE_BIN`, `PATH`, or `~/.local/bin/claude`.  Supports
  `abort_event`, `timeout`, `--model`, `--system-prompt`, and a `tools`
  extra param.

- **`LLMClient.claude_code()`** convenience constructor.

---

## [0.2.1] — 2026-05-12

### Added

- **OpenAI-compatible embeddings** — `dispatch_embed` for
  `openai_compatible` provider; `EmbedResult` extended accordingly.

---

## [0.2.0] — 2026-05-03

### Added

- **`LLMResult.is_success` / `EmbedResult.is_success`** properties.

- **`LLMClient.from_dict()`** — builds a client from a flat config dict
  with provider inferred from URL if absent.

- **Retry support** — `LLMConfig.retries` and `retry_delay`.  Each
  attempt gets its own queue acquire/release cycle.  Retryable outcomes:
  `timeout:generation`, `error:unreachable` (and legacy equivalents).

- **`llmc call --json`** — emits the full `LLMResult` as JSON.

- **`llmc` CLI** — `status`, `queue`, `call`, `parallel` subcommands
  for diagnosing Ollama contention and testing parallelism.

- **`LLMClient.cfg` property** — public accessor for the config.

- **`extra_params` routing** — recognized Ollama option keys route into
  `options{}`; everything else goes to the top-level payload (e.g.
  `format`, `system`, `chat_template_kwargs`).  `think` is now
  configurable via `extra_params` instead of hardcoded `False`.

### Fixed

- **`num_ctx_auto` ratchet** — queries `/api/ps` before sending and
  uses `max(computed, loaded_ctx)` to avoid forcing model reloads when
  a short-prompt caller requests a smaller context than currently loaded.

- **`llmc status` process names** — detects macOS framework Python
  binary (`Python`, capital P); shows `Path(script).name` instead of
  full path; handles `python -c` snippets; chases parent PID up to 2
  levels when Python has no script arg.
