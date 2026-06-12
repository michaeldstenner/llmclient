# Storage & Configuration Model

**Status:** design, decided 2026-06-11. Implementation pending (breaking
change — see end). Supersedes the ad-hoc `data_dir`/`config_dir` handling
in `_config.py` / `_keys.py`.

This is the reference for *where llmclient puts things* and *how config
resolves*. It exists so we stop re-deriving it in chat every few weeks.

## The one decision that frames everything: library, not broker

llmclient stays a **library** loaded into each app — not a daemon/broker
that fronts Ollama. A broker would dissolve most of the problems below (a
server has one config, holds the keys, knows its own slot count, sees all
traffic), but at the cost of a process to run and supervise. We chose to
keep the library and emulate the few server-shaped responsibilities
(shared slot accounting, one authoritative config) through shared files.
If the file-juggling ever gets worse than running a daemon, revisit this.

## The category error we were making

`data_dir` used to control **three unrelated things at once**: config
location, log location, and queue identity. Setting it for one (e.g.
squirrel wanting its own logs) silently forked the others — which is how
squirrel ended up on a *separate* `queue.db`, defeating cross-app slot
coordination against the same GPU (the 2026-06 oversubscription
incidents; see `ollama-debugging-playbook.md`).

The fix is to classify each thing by **what it is** and give it the home
that matches its ownership:

| layer | what it is | home | keyed on |
|-------|-----------|------|----------|
| **data** | app-owned logs/history | `~/.local/share/<app>/` | `app` |
| **state** | shared slot coordination | `~/.local/state/llmclient/` | endpoint URL |
| **config** | machine-level settings | `~/.config/llmclient/` | machine (one file) |

`app` touches only the **data** row. State and config are shared by
design — state because it coordinates one physical resource, config
because it describes the machine. The asymmetry is correct: data has an
owner, the other two are shared facts.

A coordination rendezvous (the queue) *must* live somewhere strangers can
find it without knowing about each other. bouncer doesn't know squirrel
exists, so the queue can't be keyed on either app's identity — it's keyed
on the one thing both can independently compute: **the Ollama endpoint
URL** they both target.

## Identity fields

The old single `log_caller` is split. `caller` keeps its existing
role-level meaning (it is still the log `"caller"` field, the `caller_max`
scope, and the default `circuit_key`); `app` is the new coarse grouping
that picks the data home.

| field | default | sole job |
|-------|---------|----------|
| `app` | **required** (no argv[0] magic) | pick data home; group an app's roles |
| `caller` | `= app` | log identity + `caller_max` scope + breaker key |
| `log_file` | `~/.local/share/<app>/llm.jsonl` | where failures log (one file per app; `caller` field distinguishes roles) |
| `queue_file` | `~/.local/state/llmclient/queue.db` | shared rendezvous; override only to deliberately isolate |

Example: bouncer sets `app="bouncer"`, gets `caller="bouncer"` and logs to
`~/.local/share/bouncer/`. pithos sets `app="pithos"` once and
`caller="pithos-adcut"` per role — logs land together under
`~/.local/share/pithos/`, but `caller_max` is still per-role.

## Config resolution: last-wins + `locked`

One uniform mechanism. No per-key scopes, no auto-locking.

**Sources, applied first → last:**

```
1. llmclient built-in defaults
2. central  ~/.config/llmclient/config.yaml   (may set `locked`)
3. env vars
4. client LLMConfig args                       (most specific)
```

**Two rules:**

- **last writer wins** (later layer overrides earlier) — so a client's
  explicit arg normally beats a central default. Intuitive "my code
  wins."
- **`locked` stops looking** — when a layer sets a key *and* marks it
  locked, the key is frozen against all later layers. This is the *only*
  way central enforces a value, and it works because central is applied
  **early**: authority comes from being early-and-locked, not from being
  last.

```python
for layer in [builtin, central, env, client]:
    for key, val in layer:
        if key in frozen:        # locked → stop looking
            continue
        value[key] = val         # last-wins
        if layer.locks(key):
            frozen.add(key)
```

Worked traces:

| key | builtin | central | env | client | final | why |
|-----|---------|---------|-----|--------|-------|-----|
| `parallel_slots` | 4 | 8 🔒 | — | tries 16 | **8** | locked early; client skipped (warn) |
| `model` | — | qwen3:32b | — | llama3 | **llama3** | unlocked → last-wins |
| `api_key` | — | sk-… | $ENV | — | **env or central** | unlocked → env wins, else inherit |

Locking is **purely explicit** — nothing auto-locks. Central holds shared
values; the admin locks whichever must stay consistent. The client never
needs to carry the `api_key` for remote providers: leave it in central
(unlocked) and every client inherits it; env still overrides; lock it to
pin one key for everyone.

## `parallel_slots` is a budget, not a mirror

`parallel_slots` is the cap llmclient enforces (it feeds the queue's
`global_max`). It is **not** required to equal Ollama's `NUM_PARALLEL` —
setting it *lower* to leave physical slots open for direct/manual/other
use is a legitimate policy choice, and silent.

- `budget < NUM_PARALLEL` → intentional headroom. No warning.
- `budget > NUM_PARALLEL` → real oversubscription (the original bug). A
  heads-up is warranted *only* in this direction.

Because it is a **shared** number (every caller on an endpoint feeds the
same `global_max`), disagreement across clients makes the queue's
effective cap flutter. So the recommended practice — advisory, since
locking is explicit — is: **set `parallel_slots` in central and `lock`
it.** It's the one key where forgetting to lock has a shared-state cost.

`NUM_PARALLEL` cannot be read from Ollama's HTTP API (0.24.0: not in
`/api/ps`, `/api/show`, `/api/version`; `/api/config` etc. are 404). It is
only knowable locally (the serve process env / server-config log line), so
any oversubscription check is a **local-only, optional diagnostic** (e.g.
in `llmc status`), off the config-resolution path entirely.

## Discoverability: participant registry

Queue rows are deleted on release, so an idle app vanishes from the queue.
A small **persistent** table in `queue_file` records
`(app, caller, url, log_file, last_seen)`, upserted per call, so
`llmc status` can enumerate everyone using a box and point at each app's
log. This is what makes an accidental fork *visible* immediately.

## Implementation note (pending)

This is a **breaking change** to config resolution and storage paths, so
per `CLAUDE.local.md` it requires a coordinated rollout: bump the
`llmclient` version, then update bouncer, watchdog, pithos, and squirrel
together (squirrel's separate-`data_dir` call is the one that most needs
revisiting — it should keep its own `app`/logs but rejoin the shared
`queue_file`). Detection/oversubscription diagnostic and the participant
registry are additive and can land first.

Related: `profiles.md` (call-urgency presets — the natural way to bundle
the `preference` keys clients otherwise hand-roll),
`futility-circuit-breaker.md`, `ollama-debugging-playbook.md`.
