# llmclient Usage Profiles

This document illustrates three common caller scenarios with example
`LLMConfig` values.  **The specific numbers are illustrations, not
directives.**  Every knob exists because callers genuinely differ: a
hook that must complete in milliseconds has different needs than an
overnight batch job, and both differ from an interactive tool where
the user is staring at a spinner.

Read this to understand *which settings matter for your scenario and
why*.  Then choose values that fit your actual workload, error
tolerance, and fallback strategy.  The right `queue_stall_timeout` for
a one-shot CLI tool is not the same as the right value for a daemon
that runs all night — and both are probably different from what's shown
here.

When in doubt, err toward generous timeouts.  A timeout that's too
loose is annoying; a timeout that's too tight produces confusing
failures that look like bugs.

---

## Three Illustrative Scenarios

### Fail Fast
*Interactive caller with a fallback path — example values.*

The LLM result is useful but not required — bouncer can ask the user,
squirrel can fall back to text search, etc.  Getting a fast answer about
failure is more valuable than waiting for a slow success.

The values below assume a small-output classification task on a loaded
model.  A caller with larger outputs, a slower model, or a heavier
fallback cost should loosen the timeouts accordingly.

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
*Interactive caller, no fallback — example values.*

The LLM result is the product; waiting is better than failing.  The
values below are a reasonable starting point for a single-user tool.
A caller with longer expected outputs should increase `generation_timeout`;
one that needs tighter latency guarantees should decrease `queue_timeout`
and accept more frequent fallback-to-error.

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
*Background batch processing — example values.*

Individual calls have no urgency.  The job should yield to interactive
work, tolerate Ollama being busy for hours, and retry transient failures
automatically.  But it should still surface a clear signal if Ollama is
completely dead for an extended period.

The values below suit a job that runs overnight and doesn't need
babysitting.  A shorter-lived batch job could tighten `queue_stall_timeout`
considerably; a job processing very large contexts should increase
`generation_timeout` to match.

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

This table records what each caller is currently configured to do, as a
convenience for cross-referencing.  It is not a prescription — callers
should configure what actually makes sense for them, and update this
table when they diverge from the example values above.

| Caller | Scenario | Fallback |
|---|---|---|
| bouncer | Fail Fast | Ask user |
| squirrel (search) | Fail Fast | Text search |
| squirrel (sync) | No Rush | N/A — retries on next sync cycle |
| watchdog | No Rush | N/A — next scheduled run |
| pithos | No Rush | N/A — next scheduled run |

*(Keep this table current as callers are added or reconfigured.)*
