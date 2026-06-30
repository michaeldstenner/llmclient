# Ollama / llmclient Debugging Playbook

A field guide to the local LLM stack (llmclient + Ollama + its
consumers: bouncer, watchdog, squirrel, pithos). The goal: reach the
right conclusion *fast*, without the bold-but-wrong detours that this
kind of investigation tends to produce.

Read the **Gotchas** section first — most wasted time comes from those,
not from the data being hard to find. Then use the **Runbook** order.
The **Source Catalog** has exact paths and line formats. The **Case
Study** at the end is a worked example of the wrong turns to skip.

Box this was written on: Mac mini "mini4", Apple M4 Pro, 64 GB unified
memory, macOS. Ollama 0.24.0 via Homebrew. Adjust paths if the box
changes.

---

## Gotchas (read these first)

### 1. Timezone: the logs do not agree

This is the single biggest time-sink. Different sources use different
zones:

| Source | Timezone |
|---|---|
| `llmclient_log.jsonl` (`timestamp` field) | **UTC** |
| Ollama log (`time=...` and `[GIN]` lines) | **local** (EDT, −04:00) |
| `ps` START, file mtimes, launchd logs | **local** |
| git commit timestamps | **local** (−0400) |

So an `llmclient_log` failure at `2026-06-09T23:33Z` is **19:33 local**,
and lines up with the Ollama `[GIN]` line at `19:33`. **Establish the
offset and convert everything to one zone before cross-referencing.**
A failure that looks like it happened at "1–3 AM" (UTC) may actually be
early evening local — do not theorize about "overnight cron jobs"
before fixing the clock.

### 2. `llmclient_log.jsonl` is failure-only

In practice this file contains **only non-success outcomes**
(`timeout:*`, `circuit_*`, `error:*`). Successful calls do **not** appear
here. Consequences:

- You cannot count load or see concurrency from it.
- **Absence of a caller's entries ≠ that caller was idle.** It may have
  been succeeding (and thus invisible) the whole time.
- To see *actual* traffic and concurrency, use the **Ollama `[GIN]`
  log** (Source Catalog §4), not this file.

### 3. Effective config ≠ what's in the YAML

Consumers merge built-in defaults *under* the user YAML. bouncer does
this in `bouncer/config.py` via `_deep_merge(CONFIG_DEFAULTS, user)`.
So a value can be active that appears nowhere in
`~/.config/bouncer/config.yaml` — e.g. `first_token_timeout: 30` lives
in `CONFIG_DEFAULTS`, not the YAML.

Also beware **decoy literals**: `providers/__init__.py` had
`llm_cfg.get("first_token_timeout", 5)`, but that `5` only feeds a
human-readable error string; the operative value is passed separately.
**Trace the value that actually reaches `LLMConfig`/the provider**, not
the first grep hit.

### 4. Vendoring / version skew

- **bouncer vendors** llmclient at `bouncer/bouncer/llmclient/` (kept
  zero-dep). It runs *that* copy, not the main repo.
- **watchdog imports the editable** `llmclient` (main repo).
- The `llmc` CLI runs the **main repo** copy.

All of them share the same `queue.db` and `llmclient_log.jsonl` (shared
data dir is intentional — shared dir = shared queue). So the diagnostic
tool and the consumer under investigation may be running **different
llmclient versions** against the same files. Check the vendored copy's
version before assuming behavior matches main.

### 5. Stale logs that look live

watchdog has **two** launchd log locations:
- Current (real): `~/Library/Logs/watchdog/launchd.{out,err}.log`
  (written by the installed `~/Library/LaunchAgents/com.watchdog.plist`).
- Stale: `~/.local/share/watchdog/logs/launchd.{out,err}.log` (old
  plist; frozen). This one still showed a `Unknown profile: 'personal'`
  error long after the issue was fixed. **Check mtime before trusting a
  log.**

### 6. The bouncer hook blocks system-path reads

Reading `/opt/homebrew/...`, `~/.local/share/llmclient/...`, etc. from
Bash trips the PreToolUse hook `~/.claude/hooks/bouncer_hook.py`. The
required dance: run the command **without** any prefix first (it gets
denied), then **retry the exact command prefixed** with
`# ESCALATE: <reason>`. The `Read` tool is not subject to this hook —
prefer it for single files (configs, plists).

### 7. `OLLAMA_NUM_PARALLEL` is auto-selected and silently drifts

The Homebrew plist does **not** pin `OLLAMA_NUM_PARALLEL`. Ollama
auto-selects it at server start (memory/context driven). It has flipped
on its own across restarts: `1` → `4` (2026-04-26) → `1` again
(2026-06-11), same `0.24.0` binary. **A drift to 1 while llmclient still
admits 4 (`parallel_slots`, default 4) is the modern repeat of the §4
contention bug** — the queue releases 4 callers, Ollama serializes 1, the
other 3 starve and time out with `queue_wait_s≈0` + `call_s` pinned at
their cap (admitted-then-starved; see §2). Larger context pushes the auto
pick toward 1 (4×32k KV won't fit), so growing `num_ctx` (e.g. pithos's
66k-char prompts) can trigger the drop.

**Keep `parallel_slots` == the live `NUM_PARALLEL`.** To pin Ollama:
- `brew services restart ollama` **regenerates the plist** and Homebrew 4
  loads the formula from its JSON API cache — so editing the plist *or*
  the formula `.rb` does **not** survive a `brew services` cycle. That is
  exactly how a live-set value gets lost.
- Durable pin: set it in the plist and reload via **launchctl**, not brew:
  ```
  /usr/libexec/PlistBuddy -c \
    "Add :EnvironmentVariables:OLLAMA_NUM_PARALLEL string 4" \
    ~/Library/LaunchAgents/homebrew.mxcl.ollama.plist
  launchctl bootout  gui/$(id -u)/homebrew.mxcl.ollama
  launchctl bootstrap gui/$(id -u) \
    ~/Library/LaunchAgents/homebrew.mxcl.ollama.plist
  ```
  Confirm with the `server config` line (Gotcha shows `OLLAMA_NUM_PARALLEL:4`).
  **Anyone who later runs `brew services restart ollama` reverts it to the
  auto pick — reapply via launchctl.**

### 8. Editable installs are "ghosts": running but can't cold-start

Consumers (pithos, etc.) run as launchd daemons via
`uv run --project … python -m <app>.main serve`. Their venv editable
installs of the app **and** llmclient rely on `.pth` files that get
**skipped** — `_editable_impl_<app>.pth` carries a `com.apple.provenance`
xattr, and uv leaves duplicate scars (`… 2.pth`, `… 3.pth`). When skipped,
`lib/` never lands on `sys.path` and `import <app>` fails in a *fresh*
process. **The live daemon keeps working only because it imported when the
`.pth` was still good** — so it survives in memory but **any restart,
launchd KeepAlive after a crash, or reboot fails to come back up.**

Detect: run the exact launchd command by hand —
`uv run --project <path> python -c "import <app>.serve, llmclient"`. If it
`ModuleNotFoundError`s, the daemon is a ghost. **Never restart a consumer
daemon without first confirming a clean cold import**, or you brick it.
Fix: `uv sync --reinstall` in the project, strip the xattr
(`xattr -d com.apple.provenance <…>.pth`), re-test the cold import, *then*
restart (`launchctl kickstart -k gui/$(id -u)/<label>`).

### 9. The slot queue moved; killed consumers leave orphan slots

As of v0.9.0 the shared slot queue is **decoupled from the data home**:
it lives at **`~/.local/state/llmclient/queue.db`** (note `state`, not
`share`). Per-app `~/.local/share/<app>/` now holds only logs. So the old
`~/.local/share/llmclient/queue.db` is stale (frozen mtime) — editing it
does nothing; `llmc status` reads the `state` one. A consumer **killed**
mid-call cannot release its slot, and the queue does **not** auto-reap a
dead holder, so `llmc status` shows a phantom `running` slot on a dead pid
indefinitely. Clear it: confirm the pid is dead (`ps -p <pid>`), then
`DELETE FROM queue WHERE pid=<pid>` in the `state` db.

---

## Runbook (do it in this order)

1. **Fix the clock.** Note the UTC↔local offset. Convert the reported
   failure window to local time.
2. **Live state:** `llmc status` and `ollama ps`. Is the model loaded?
   What's holding slots right now? Is the queue backed up?
3. **Ollama `[GIN]` log for the window** (§4). This is the fastest
   discriminator: it shows every HTTP request with **status code and
   duration**, and overlapping durations reveal concurrency.
   - `500 | ~Ns` where N ≈ a caller's timeout → that caller **aborted**
     (client-side timeout), not an Ollama error.
   - `200 | long` → slow but completed (a caller with a longer timeout
     survived the same slowness).
   - Many overlapping long durations → concurrency saturation.
4. **Ollama lifecycle lines for the window** (§4): `server config`
   (restart), `starting runner` / `llama runner started in Xs` (load
   time), `load request="{... Parallel:N KvSize:M ...}"` (concurrency +
   KV sizing), `system memory` / `gpu memory` (free VRAM/RAM).
5. **Effective merged config** of the failing caller (§5), not the YAML.
6. **Direct timed probe** replicating the caller's *exact* request shape
   (§6) — separates "model/GPU is slow" from "this caller's request is
   special".
7. **Box resources** only if the above points at limits: `memory_pressure`,
   `sysctl vm.swapusage`, `sysctl hw.memsize` (§7). On 64 GB with a ~24 GB
   model, memory is rarely the cause — check swap=0 to confirm fast.

---

## Source Catalog

### 1. `llmc` CLI (llmclient's own diagnostics)

Runs the main-repo llmclient against the default data dir. Subcommands:

- `llmc status` — Ollama connections, loaded models + ctx + expiry, and
  the llmclient queue (who's running/waiting).
- `llmc queue` — queue state only.
- `llmc log` — recent call-log entries.
- `llmc reset` — clear tripped circuit breakers.
- `llmc call` / `llmc parallel` — make test call(s).
- `--dir PATH` — point at a non-default data dir.

### 2. llmclient data dir: `~/.local/share/llmclient/`

(`_DEFAULT_DATA_DIR`. Consumers that don't call `configure(data_dir=…)`
use this — bouncer and watchdog both do, hence shared.)

- `llmclient_log.jsonl` — **failure-only** call log (see Gotcha 2).
  Useful fields per row: `timestamp` (UTC!), `caller`, `outcome`,
  `model`, `call_s`, `queue_wait_s`, `inference_s`, `load_s`,
  `prompt_chars`, `prompt_tokens_est`, `response_tokens`. A row with
  `call_s` pinned at exactly the caller's timeout and `queue_wait_s=0`
  means: admitted immediately, then never got a first token.
- `queue.db` — SQLite. Tables:
  - `queue(id, pid, caller, priority, caller_max, global_max, status,
    submitted_at, started_at, model)` — live + historical slots.
  - `queue_meta` — `last_release_at` and `last_release_at:<model>`
    (drives stall detection).
  - `circuit_state(circuit_key, caller, consecutive_n, last_failure_at,
    tripped_at, probe_pid, llr, llr_updated_at)`. `tripped_at IS NULL`
    and `consecutive_n=0` → breaker healthy. `circuit_key` is e.g.
    `bouncer|ollama|qwen3:32b|http://localhost:11434`.

  Read it with the consumer's venv python + `sqlite3` (the system
  python3 may lack the package path). `global_max` in a row = the
  concurrency cap that applied (= `get_parallel_slots()` = Ollama
  `NUM_PARALLEL`).

### 3. Ollama process & runtime state

- `ps aux | grep "ollama runner"` — one line per loaded model: the
  model **blob path**, `--port`, and RSS. A ~24 GB RSS runner is the
  32B model; a ~0.5 GB one is the embed model.
- `pgrep -f "ollama serve"` — the server (its log fds point at the log,
  see §4).
- `ollama ps` / `GET /api/ps` — loaded models, sizes, expiry.

### 4. Ollama server log: `/opt/homebrew/var/log/ollama.log`

Homebrew install; the `ollama serve` process holds it on fd 1/2 (find
it with `lsof -p <serve-pid> | grep log` if the path differs). ~300k+
lines — always grep a **time window** (local time!). Line types:

- `msg="server config" env="map[... OLLAMA_NUM_PARALLEL:4
  OLLAMA_CONTEXT_LENGTH:8192 OLLAMA_KEEP_ALIVE:5m0s
  OLLAMA_KV_CACHE_TYPE:q8_0 OLLAMA_FLASH_ATTENTION:true
  OLLAMA_MAX_QUEUE:512 OLLAMA_LOAD_TIMEOUT:5m0s ...]"` — printed on
  **server (re)start**. A `server config` line inside your window means
  Ollama restarted then. Also the authoritative source for the live env
  knobs (NUM_PARALLEL etc.).
- `msg="starting runner" cmd="... --model <blob> --port <N>"`,
  then `msg="waiting for llama runner to start responding"`, then
  `msg="llama runner started in X seconds"` — model load lifecycle and
  **load time**. (A 32B loads in ~14 s here; an embed model ~0.4 s.)
- `msg=load request="{Operation:fit ... Parallel:4 BatchSize:512
  FlashAttention:Enabled KvSize:32768 KvCacheType:q8_0 GPULayers:65 ...}"`
  — concurrency (`Parallel`) and **KV cache size** (`KvSize` = ctx ×
  parallel). With NUM_PARALLEL=4 and ctx 8192, KvSize=32768.
- `msg="system memory" total free free_swap` and
  `msg="gpu memory" available free` — resource snapshot at load.
- **`[GIN] 2026/06/09 - 20:03:12 | 500 | 30.033713s | 127.0.0.1 |
  POST "/api/generate"`** — the gold. One per HTTP request:
  `STATUS | DURATION | IP | METHOD path`. Interpreting:
  - `500 | ≈<caller timeout>` → **client aborted the stream** at its
    timeout (e.g. bouncer's 30 s `first_token_timeout` shows as
    `500 | 30.03s`). Not an Ollama-side error.
  - `200 | <long>` → completed but slow; a caller with a longer timeout
    (watchdog, 300 s) survives the same condition.
  - **Overlapping durations** across rows → concurrent generations =
    NUM_PARALLEL contention. On one GPU, K concurrent 32B generations
    run ~K× slower; that's how a normally-fast first token blows past a
    short timeout.

### 5. Consumer configs

- **bouncer:** user `~/.config/bouncer/config.yaml`, merged over
  `CONFIG_DEFAULTS` in `bouncer/config.py` (+ project `.bouncer/` and
  local overrides via `_deep_merge`). The LLM config is assembled in
  `bouncer/providers/__init__.py` (`call_llm`). Effective-config
  reminders in Gotcha 3.
- **watchdog:** `~/.config/watchdog/config.yaml` with **profiles**
  (`main`, `dev`; `default_profile`). LLMConfig built in
  `lib/watchdog/llm.py` (imports editable llmclient; sets `timeout=300`,
  no `caller_max` → inherits default 4). Daemon = launchd
  `~/Library/LaunchAgents/com.watchdog.plist`, logs in
  `~/Library/Logs/watchdog/` (Gotcha 5). watchdog's own per-call log is
  `~/.local/share/watchdog/llm_calls.jsonl` (may be stale).

### 6. Direct timed time-to-first-token probe

The decisive "is it the model/GPU or this caller's request?" test. Send
a streaming `/api/generate` and time the first non-empty `response`
chunk. **Replicate the caller's exact request shape** — `think`,
`chat_template_kwargs` (e.g. `enable_thinking: false`), `num_ctx`,
`num_predict`, `keep_alive` — because those change behavior (a reasoning
model with thinking on can delay the first *response* token a long
time). Run it both as a trivial `num_predict:1` baseline and as the
caller's real shape, and compare. (qwen3:32b here: ~0.5 s trivial,
~0.9 s for bouncer's shape when the GPU is idle.)

### 7. Box specs / memory

- `sysctl hw.memsize` / `system_profiler SPHardwareDataType` — RAM, chip.
- `memory_pressure` — free %, pageouts.
- `sysctl vm.swapusage` — **swap used = 0** means no paging → memory is
  not the bottleneck. Unified memory: a ~24 GB model on 64 GB leaves
  plenty; rule memory out quickly here.

---

## Case Study: "bouncer keeps timing out" (2026-06-09)

**Symptom:** bouncer logged a burst of `timeout:first_token` (then
`circuit_open` storms), UTC `23:29–00:09` = **local 19:29–20:09**.

**Wrong turns (skip these next time):**

1. *"first_token_timeout defaults to 5 s."* No — that `5` was an
   error-string fallback. Effective value is **30 s** from
   `CONFIG_DEFAULTS`. → *Trace the merged config (Gotcha 3).*
2. *"Nightly cold-load problem."* Timezone illusion — UTC made evening
   look like 1–3 AM. → *Fix the clock first (Gotcha 1).*
3. *"watchdog is hogging slots."* Plausible but unprovable from the
   **failure-only** log, which showed no watchdog rows (Gotcha 2). Also
   watchdog calls the model **serially** (1 slot), so it wasn't the
   whole story.
4. *"Reasoning/thinking block delays the first token."* Disproved by a
   direct probe of bouncer's exact request: **0.9 s** (Source §6).
5. *"The llama runner was stuck starting."* Disproved by the log:
   `llama runner started in 14.25 seconds`.

**What actually settled it:** the Ollama `[GIN]` log for the window.
`/api/generate` requests **overlapped**, each taking **17–74 s**.
bouncer's were the `500 | 30.03s` lines (aborted at its 30 s cap);
watchdog's were `200 | 20–74s` (survived via its 300 s timeout). With
`OLLAMA_NUM_PARALLEL=4`, a burst of 3–4 concurrent 32B generations on
one GPU inflated per-request latency past 30 s, so only the
short-timeout caller (bouncer) tripped. Self-clearing once the burst
drained (a later idle probe was 0.5 s).

**Root cause:** request **concurrency** under NUM_PARALLEL=4, not
hardware, memory, cold-load, thinking, or a stuck runner.

**On the fix:** the blunt move (`OLLAMA_NUM_PARALLEL=1`) is wrong for
this stack. The design intends high parallelism mediated by a
**priority-ordered shared queue** — see [profiles.md](./profiles.md).
bouncer is the "Fail Fast" profile (priority 80, `caller_max:1`, should
jump ahead of background work); watchdog is "No Rush" (priority 20,
yields). The real lever is configuring each caller to its profile (and
the queue's `global_max`/priority semantics), not globally throttling
Ollama. Reconcile the framework with bouncer's actual config — it's
currently leaning on `CONFIG_DEFAULTS` rather than its profile.

**Meta-lesson:** go to the `[GIN]` log early (Runbook step 3). It would
have shown "concurrent slow requests, bouncer aborts at 30 s" in one
look, skipping all five wrong turns.
