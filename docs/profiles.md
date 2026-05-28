# llmclient Usage Profiles

Three profiles cover the common caller scenarios.  Each is a set of
`LLMConfig` field values (or equivalent YAML keys in bouncer-style
configs).  Think of them as starting points — tune per-caller as needed.

---

## The Three Profiles

### Fail Fast
*Interactive caller with a fallback path.*

The LLM result is useful but not required — bouncer can ask the user,
squirrel can fall back to text search, etc.  Getting a fast answer about
failure is more valuable than waiting for a slow success.

| Setting | Value | Reason |
|---|---|---|
| `priority` | `80` | Jump ahead of background work |
| `caller_max` | `1` | One interactive call at a time |
| `queue_timeout` | `30s` | Hard ceiling; fallback is cheap |
| `queue_stall_timeout` | `15s` | Bail fast if queue is frozen |
| `first_token_timeout` | `8s` | Model should respond quickly |
| `generation_timeout` | `30s` | Short outputs expected |
| `circuit_n` | `2` | Trip quickly → engage fallback |
| `circuit_cooldown_s` | `60s` | Recover quickly |
| `circuit_triggers` | `[queue_wait, queue_stall, first_token, unreachable]` | All timeouts count |

```yaml
# bouncer config (llm: section)
priority:             80
caller_max:           1
queue_timeout:        30
queue_stall_timeout:  15
first_token_timeout:  8
generation_timeout:   30
circuit_n:            2
circuit_cooldown_s:   60
```

---

### Hurry
*Interactive caller, no fallback — the LLM result is the product.*

Waiting is better than failing, but we still want the result as soon as
possible and should not hang forever if something goes wrong.

| Setting | Value | Reason |
|---|---|---|
| `priority` | `90` | Highest interactive priority |
| `caller_max` | `1` | One interactive call at a time |
| `queue_timeout` | `5m` | Catches misconfiguration/runaway callers |
| `queue_stall_timeout` | `2m` | Distinguish "busy" from "borked" |
| `first_token_timeout` | `30s` | Cold model load is OK |
| `generation_timeout` | `120s` | Allow longer outputs |
| `circuit_n` | `4` | Don't trip on transient blips |
| `circuit_cooldown_s` | `45s` | Recover quickly |
| `circuit_triggers` | `[queue_stall, first_token, unreachable]` | Not `queue_wait` — queuing is expected |

```yaml
priority:             90
caller_max:           1
queue_timeout:        300      # 5 min
queue_stall_timeout:  120      # 2 min
first_token_timeout:  30
generation_timeout:   120
circuit_n:            4
circuit_cooldown_s:   45
```

---

### No Rush
*Background batch processing — overnight syncs, email classification,
podcast analysis, etc.*

Individual calls have no urgency.  The job should yield to interactive
work, tolerate Ollama being busy for hours, and retry transient failures
automatically.  But it should still surface a clear signal if Ollama is
completely dead for an extended period.

| Setting | Value | Reason |
|---|---|---|
| `priority` | `20` | Yield to all interactive callers |
| `caller_max` | `1` | Don't hog slots |
| `queue_timeout` | `4h` | Catches pathological hangs |
| `queue_stall_timeout` | `10m` | Flag if nothing at all has moved |
| `first_token_timeout` | `60s` | Cold load is expected |
| `generation_timeout` | `600s` | Long contexts OK |
| `circuit_n` | `5` | Tolerate several transient failures |
| `circuit_cooldown_s` | `300s` | Don't rush back into errors |
| `circuit_triggers` | `[queue_stall, first_token, unreachable]` | Not `queue_wait` |
| `retries` | `1` | Auto-retry transient failures |
| `retry_delay` | `30s` | Brief pause before retry |

```yaml
priority:             20
caller_max:           1
queue_timeout:        14400    # 4 hours
queue_stall_timeout:  600      # 10 min
first_token_timeout:  60
generation_timeout:   600
circuit_n:            5
circuit_cooldown_s:   300
retries:              1
retry_delay:          30
```

---

## Key Concepts

### queue_timeout vs queue_stall_timeout

These work together to distinguish two different bad states:

**`queue_timeout`** is the absolute wall-clock ceiling on how long a
caller will wait in the queue before giving up.  It's a blunt instrument
— it fires regardless of whether the queue is healthy or frozen.  Use it
as a misconfiguration safety net, not as the primary failure signal.

**`queue_stall_timeout`** fires if no *other* caller has completed an
inference in the last N seconds.  It answers the question: "is Ollama
actually doing work, or has it stopped entirely?"

- Queue moving (completions happening, you're just next in line) →
  `queue_stall_timeout` never fires, `queue_timeout` is your ceiling.
- Queue frozen (nobody completing, slots stuck) →
  `queue_stall_timeout` fires first, with a queue snapshot attached.
- Fresh install / DB reset → `queue_stall_timeout` is skipped with a
  logged warning; `queue_timeout` remains the ceiling.

### circuit_triggers and queue_wait

`timeout:queue_wait` means "I timed out waiting in the queue."  For
**Fail Fast**, this should trip the circuit — if you're consistently
failing to get a queue slot, the fallback should take over.  For
**Hurry** and **No Rush**, waiting in the queue is expected and should
*not* be treated as a failure signal.

`timeout:queue_stall` means "the queue has frozen — nothing is
completing."  This is a real failure signal for all three profiles and
should always be in `circuit_triggers`.

### priority values

Higher is better.  Suggested bands:

| Band | Value | Who |
|---|---|---|
| Interactive, no fallback | 90 | Hurry callers |
| Interactive, with fallback | 80 | Fail Fast callers |
| Default | 50 | Anything not configured |
| Background | 20 | No Rush callers |

Keep the bands well-separated so priority ordering is unambiguous.
Don't put two different callers at the same non-default value unless
you genuinely want them to tie.

---

## Caller Reference

| Caller | Profile | Notes |
|---|---|---|
| bouncer | Fail Fast | Fallback: ask user |
| squirrel (search) | Fail Fast | Fallback: text search |
| squirrel (sync) | No Rush | Overnight background |
| watchdog | No Rush | Email classification |
| pithos | No Rush | Podcast analysis |

*(Update this table as callers are added or reconfigured.)*
