# Changelog

## [0.9.0] — 2026-06-11

Storage & configuration model — see `docs/storage-and-config-model.md`.
This release lands the **storage decoupling** slice; config-resolution
(last-wins + `locked`) and the endpoint-keyed queue / participant
registry follow.

### Changed

- **The slot queue is now decoupled from the data home.**  `get_db_path()`
  no longer derives from `data_dir`; it lives in the shared state home
  `~/.local/state/llmclient/queue.db` (override with
  `configure(queue_file=...)`).  This fixes the class of bug where an app
  passing `configure(data_dir=...)` for its own logs *silently forked the
  queue*, defeating cross-app slot coordination against one Ollama box.
  Apps that set `data_dir` keep their own logs there but now
  automatically rejoin the shared queue — no code change required for the
  fix.

### Added

- **`configure(app=...)`** — name the application; sets the per-app data
  home `~/.local/share/<app>/` for logs/history.  Plus `log_file=` and
  `queue_file=` for explicit overrides.  `data_dir`/`config_dir` retained
  for back-compat (now keyword-only).
- `get_state_dir()` helper.

### Migration

- Keyword callers of `configure()` are unaffected.  Positional callers
  must switch to keywords (`app` is now the first positional).
- `llmc --dir PATH` inspects that dir's own `queue.db` (legacy / isolated
  queues); without `--dir`, `llmc` reads the shared state queue.

## [0.8.0] — 2026-06-02

### Added

- **`circuit_key` — configurable breaker scope.**  New optional
  `LLMConfig` field (default `""`).  When empty it falls back to
  `log_caller`, so existing callers keep their per-caller breaker
  unchanged.  Set it to scope the breaker more narrowly — e.g.
  `"bouncer|<provider>|<model>|<url>"` so switching model or endpoint
  doesn't share one trip state.  Applies to **both** `circuit_mode`
  values (count and futility).

### Changed

- `circuit_state` is now keyed on `circuit_key` (was `caller`); `caller`
  is retained as a metadata column for diagnostics / `llmc reset` output.
  Existing DBs are auto-migrated on open (the old `caller` PRIMARY KEY
  becomes the seed `circuit_key`); the migration composes with the
  v0.7.0 `llr`/`llr_updated_at` add-columns from any starting point.
- The breaker no-op gate now keys on `circuit_key or log_caller`.
- `llmc reset` reads/clears by `circuit_key` and shows `caller` when it
  differs.

No API removals; `circuit_key` is additive and defaults to prior
behavior.  No `outcome` strings changed.

## [0.7.0] — 2026-06-02

### Added

- **Futility circuit breaker (`circuit_mode: "futility"`).**  Opt-in
  alternative to the consecutive-failure counter.  Models the breaker as a
  sequential, irreversible stopping decision rather than a binary
  classifier: a leaky log-likelihood-ratio accumulator (Wald SPRT with a
  forgetting factor) decides when continued waiting is futile.  See
  `docs/futility-circuit-breaker.md`.
  - New `LLMConfig` fields (all optional, defaults preserve old behavior):
    `circuit_mode` (`"count"` default | `"futility"`), `grace_s`,
    `deadline_s`, `ps_probe`, `ps_url`.
  - New outcome string `circuit_futile` (emitted when the futility breaker
    is open), alongside the existing `circuit_open` for count mode.
  - Provider-agnostic core via a `FutilitySensor` interface (`_sensor.py`):
    `OllamaSensor` (cheap `/api/ps` liveness probe + queue-aware weighting)
    and `DefaultSensor` (HTTP-outcome weights for cloud providers, no probe
    — the breaker degrades cleanly to "outcome-weighted LLR + deadline").
  - Per-provider evidence weights and self-heal constant in
    `_breaker_params.py` (hand-set defaults; refit from logs via
    `scripts/fit_breaker_params.py` once `log_level="all"` data exists).
  - `circuit_state` gains `llr` / `llr_updated_at` columns (auto-migrated).
  - `llmc reset` now also clears the LLR accumulator.

Count mode (`circuit_n`, `circuit_cooldown_s`, `circuit_triggers`,
`circuit_open`) is unchanged and remains the default; no consumer behavior
changes until `circuit_mode: "futility"` is set.

## [0.6.2] — 2026-06-02

### Fixed

- **`timeout:queue_wait` no longer trips the circuit breaker.**  It was
  in the default `circuit_triggers`, but queue-wait timeouts are
  caller-side congestion (the caller filled its own `caller_max` slots),
  not an Ollama-health signal.  Counting them caused a self-reinforcing
  false-positive loop: load → self-congestion → queue-wait timeout →
  trip, even with a perfectly healthy backend.  Triggers are now
  `timeout:first_token` and `error:unreachable` only.

## [0.6.1] — 2026-06-02

### Added

- **`llmc reset` command.**  Clears all tripped circuit breakers in
  `queue.db`, printing each caller name and the consecutive-failure
  count that was reset.  Respects the `--dir` flag so it works
  against any app's data directory.

## [0.6.0] — 2026-05-31

### Changed

- **Central log replaces per-caller logs.**  All calls now write to
  `llmclient_log.jsonl` in the data directory (default
  `~/.local/share/llmclient/`).  Multiple processes sharing a data
  dir share a single log; `fcntl.flock` serialises concurrent writes.
  The old per-caller `~/.local/share/<caller>/llm_calls.jsonl` files
  are no longer written.

- **`configure()` gains `data_dir` and `log_level`; removes `queue_db`.**
  `data_dir` is the new home for both `queue.db` and
  `llmclient_log.jsonl`.  `queue_db` (added in 0.5.2) is removed —
  use `data_dir` instead.  `log_level` controls what gets written:
  `"off"`, `"errors"` (default, non-success only), or `"all"` (every
  call, with queue snapshot always included).

- **Logging is always-on.**  `write_log` is called for every call
  regardless of whether `log_caller` is set.  `log_caller` remains a
  metadata field in log entries to identify the caller; it no longer
  gates logging.

- **Queue snapshot at `all` level.**  When `log_level="all"`, the
  queue state is captured on every call (not just timeouts) so you
  can see what was running concurrently at the time of any call.

### Changed (CLI)

- **`llmc --dir PATH`** — global flag that sets the data directory
  before any subcommand runs.  Use this to inspect a specific app's
  queue and log: `llmc --dir ~/.local/share/bouncer/ log`.

- **`llmc log` flags simplified** — `--errors` (default) shows
  non-success outcomes; `--all` shows everything.  The old `--warn` /
  `--error` distinction is removed.

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
