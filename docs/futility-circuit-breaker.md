# Futility-Based Circuit Breaking for llmclient

**Status:** design, approved 2026-06-02. Supersedes the consecutive-failure
counter currently in `llmclient/_queue.py`.

## TL;DR

The circuit breaker is not a classifier that fires once. It is a
**sequential, irreversible stopping decision**: at every moment we choose
*keep waiting* or *bail*, and we can never un-bail. The right object is
therefore optimal stopping, not threshold detection.

The whole system reduces to **two caller-set times** and one definition:

- `grace` — minimum wait before we will even *consider* bailing.
- `deadline` — pencils down, no matter what (may be infinite).
- **Futility ≡ "P(success before the deadline) is low."** We bail the
  instant futility is established, never before `grace`, always by
  `deadline`.

Everything statistical (outcome weights, self-heal rate, latency
distributions) is **fit from logged data and lives in llmclient**. The
caller sets only the two times. The "what do I do after bailing" logic
(retry, fall back to another model, ask a human) stays in caller code and
is explicitly out of scope here.

## The mental-model correction

The original breaker treated each call as a discrete event to be
binary-classified ("did Ollama fail?") and tripped after N consecutive
positives. That framing is wrong in three ways:

1. **It is not one decision, it is a sequence.** We get a stream of
   measurements and may act at any time.
2. **The action is irreversible.** Bailing is absorbing — you cannot
   un-bail and recover the in-flight work. This asymmetry is the whole
   game.
3. **The hidden state changes underneath us.** Ollama heals on its own
   (a long call returns, a model finishes loading), so evidence must
   decay toward the prior over time.

Irreversibility gives waiting an **option value**: continuing preserves
the right to bail later with better information, so the correct bail
threshold is biased toward patience by exactly that option value. A good
sensor (see `/api/ps` below) doesn't just speed detection — it *raises*
how long it's worth waiting, because each extra moment buys real
information instead of just burning clock.

## Busy ≠ broken: the breaker is a queue manager

llmclient is, effectively, a queue manager in front of one Ollama box.
The dominant healthy state under load is **busy** — we slammed Ollama with
work and it is chewing through it fine. In that state, *waiting is almost
always the right answer.* So:

- **The default disposition is to wait.** Bailing is the exception that
  must be positively justified.
- The entire job of the diagnostics is one discrimination: **is the queue
  advancing, or is nothing moving?** Advancing → keep waiting toward the
  deadline. Frozen → bail (after `grace`).

## Futility = P(success before deadline) is low

This is the keystone definition. It unifies two situations that look
different but call for the same action:

- **Frozen** — Ollama will never succeed. P(success) ≈ 0.
- **Too busy to make it** — Ollama is healthy but, given queue depth and
  observed throughput, won't finish *before the deadline*. P(success
  before deadline) ≈ 0 even though P(success eventually) is high.

> The ice-cream-line test: you have 10 minutes before a meeting and the
> line is barely moving. You don't wait the full 10 minutes to discover
> you missed it — you bail the instant you realize "this isn't happening."

Defining futility relative to the deadline (not relative to Ollama's
health) is what lets a single mechanism serve every workflow: a tight
deadline automatically makes "busy" mean "bail to plan B," while an
infinite deadline makes it mean "wait it out." The caller never sets an
operating point — the deadline specializes the futility judgment for them.

## The deadline is not redundant with the detector

A perfect detector still does not make the deadline optional, because the
two cover **disjoint failure spaces**:

- **Detector** answers "is *Ollama* broken?" — the modeled failure.
- **Deadline** answers "is something I *can't see* broken?" — priority
  inversion starving our row, a hung socket that never trips first-token
  logic, a stall in a dependency Ollama knows nothing about. The
  busy-vs-broken classifier is structurally blind to all of it.

(Request-*volume* pathologies — e.g. a caller in a loop hammering the
queue — are a third, separate mechanism's job: the per-caller `caller_max`
concurrency cap. Three mechanisms, three disjoint jobs.)

This is why an infinite deadline is a real and valid setting, but it is a
*statement*: "the only thing that can break here is Ollama, and the
detector owns that." True for background batch; false for an unattended
interactive gate like bouncer, which most needs the backstop against its
own unmodeled pathologies.

## The config basis: two times, fully orthogonal

We rejected "patience: high|medium|low" as the basis — patience is a
*diagonal* across two independent axes (how long you'll wait, and how
eager you are to quit early), not a coordinate. It survives only as an
optional preset (a labeled line through the space), never as the basis.

The orthogonal basis is two times:

| Knob | Meaning | Units |
|------|---------|-------|
| `grace` | "give it at least this long" — no judging, no bailing before this. Suppresses twitchy bails on momentary blips. | seconds |
| `deadline` | "pencils down" — bail by here regardless of evidence. May be ∞. | seconds or ∞ |

Both are seconds; a human can state both without any statistical
intuition. The operating point (how much evidence constitutes futility)
stays **fit/default inside llmclient** and only acts in the window between
`grace` and `deadline`.

**`grace` length trades against detector quality.** A short grace is only
safe if the busy-vs-broken signal is reliable, because below a good
detector the grace period is the *only* twitch-protection. With a strong
sensor you can afford a short grace; without one, lengthen it.

### Why this reproduces every workflow without an operating-point dial

| Workflow | `grace` | `deadline` | What carries it |
|----------|---------|-----------|-----------------|
| bouncer (unattended gate) | 5–10s | finite, generous (backstop) | default: busy→wait, frozen→bail |
| watchdog / batch | minutes | ∞ or very long | trusts detector fully |
| real-time / voice | ~0 | tiny (2–3s) | "won't finish in time" *is* futility → bails early |
| model cascade (good plan B) | ~0 | short | short deadline ⇒ predicts miss ⇒ bail to plan B |

The last two have the *opposite* instinct from bouncer — for them a busy
queue means "bail," not "wait." A health-relative detector gets them
backwards; a **deadline-relative** futility definition flips them
automatically. That is the test the two-knob basis had to pass, and it
passes.

## Worked example: bouncer — why slow success beats fast failure

bouncer is an unattended approval gate: it lets a long agentic workflow
run *without a human babysitting it*. Its fallback is therefore not "ask
the user" in the cheap sense — it is **summon the human**, which is
expensive. That fixes its preference order:

```
1. bouncer runs quickly
2. bouncer runs slowly      ← a slow SUCCESS
3. bouncer fails quickly    ← summons the human
4. bouncer fails slowly     ← summons the human AND wasted the wait
```

The load-bearing inversion is **2 over 3: a slow success beats a fast
failure.** Nobody cares if the workflow takes 25 minutes instead of 20
because some gate calls were slow. What's unacceptable is being dragged
back to approve something every couple of minutes — and the *worst* case
(4) is being dragged back *and* having watched bouncer burn a long
timeout first.

This dictates bouncer's config exactly, and it is the canonical
"expensive Plan B → generous deadline" row of the table above:

- **No `first_token_timeout` / `generation_timeout`.** A hard first-token
  bail produces outcome #3 (fast failure → babysitting) precisely when #2
  (wait for the slow success) is wanted. These are vestigial knobs from
  the pre-futility ("count") system; in futility mode they must be absent
  (the breaker ignores them — see "Old config" cleanup below / the
  storage-and-config notes).
- **Generous `deadline_s`** (minutes, not seconds). Bail reluctantly. The
  deadline is a backstop against *unmodeled* hangs (priority inversion, a
  hung socket), not a latency target.
- **`ps_probe=True` is what makes #3 beat #4.** The only time a *fast*
  failure is correct is when Ollama is genuinely frozen — and then you'd
  be babysitting anyway, so failing fast (3) beats failing slow (4). The
  `/api/ps` probe separates frozen from busy cheaply, so bouncer waits
  through *busy* (→ slow success) and bails fast only on *frozen*.
- **`caller_max=4`, not 1.** The workflow fires several gate calls; they
  should run concurrently so bouncer "runs quickly" (#1) when the GPU is
  free. bouncer is the *priority* work — it should be able to use all
  slots; background callers yield to it, not the reverse. (Whether
  background must always leave bouncer headroom is the separate
  interactive-headroom question.)

So despite the old "Fail Fast" label in `profiles.md`, bouncer is a
**patient gate**: wait through busy, fail fast only when truly frozen.

## Architecture: shared likelihood, per-caller times

Signal-detection theory factors this cleanly, and the factorization is the
ownership boundary:

- **Likelihood model — `P(observation | state)`** is a property of the
  *backend*. Shared across all callers, estimable from the global logs
  (`queue.db`, `llmclient_log.jsonl`). **Lives in llmclient.** It computes
  and exposes the sufficient statistic (accumulated evidence / belief).
- **Decision boundaries** are cost ratios — how bad is a wasted wait vs a
  needless bail — and are wildly caller-specific. **Live in caller
  config**, expressed as `grace` + `deadline`.

### The statistic underneath (for the implementers, not the users)

The accumulator is a **leaky log-likelihood ratio** — Wald's SPRT with a
forgetting factor:

```
S  ←  λ·S + w(obs),     λ = exp(−Δt / τ_heal)
```

- `w(obs) = log( P(obs | futile) / P(obs | healthy-busy) )` — the
  per-observation evidence weight. These are **fit from logs**, not tuned
  by hand. A success is just a large negative weight (a frozen backend
  essentially cannot produce one), so "one success resets it" falls out
  naturally rather than being a special case.
- `τ_heal` is Ollama's mean self-recovery time, **measured** as the median
  gap between a stall and the next success. The leak is what makes old
  evidence decay toward the prior — principled, not an arbitrary
  half-life.
- Bail when `S` crosses the futility boundary — but the boundary is shaped
  by *time remaining to deadline*: as the deadline nears, the bar for
  "won't make it" drops, because even healthy-but-slow now predicts a
  miss. This is how "P(success before deadline)" enters the math.

Because of boundary overshoot and the leak, Wald's closed-form error
bounds are approximate; **we calibrate the weights and boundaries by
simulation against logged data**, not by trusting the formula. The
target false-bail / missed-futility rates are the right *intent* even
though the achieved rates are verified empirically.

### `/api/ps` — the high-separation sensor

Passive first-token latency cannot separate "frozen" from "busy" — both
produce long waits. Ollama's `/api/ps` endpoint can: it reports which
models are resident and whether a runner is actively processing. This is
a near-noiseless observation of frozen-vs-busy, and it has two further
payoffs:

1. **It sees load that bypasses the queue.** Squirrel talks to Ollama
   directly; its calls never appear in `queue.db`. `/api/ps` sees them.
   This *removes the need* to migrate squirrel onto the queue purely for
   detection purposes.
2. **It de-confounds model reload.** `load_duration` is only visible on
   success, so a cold-start (qwen3:32b reloading, 10–20s) looks like a
   freeze. `/api/ps` shows residency *before* the call, turning an
   unobserved confound into an observed covariate: model not resident →
   widen the expected first-token budget rather than suspecting a freeze.

Used as the half-open *probe*, `/api/ps` is also far cheaper than a real
generation and steals no real slot — directly resolving the old "the probe
steals a slot from a legitimate caller" problem.

## Provider abstraction: universal decision, provider-specific evidence

The diagnostics that distinguish frozen from busy are necessarily
provider-specific (`/api/ps` is an Ollama endpoint; a cloud provider speaks
HTTP status codes instead). But the *decision* — accumulate evidence,
respect `grace`, bail by `deadline`, treat futility as P(success before
deadline) — is entirely universal. We keep them apart. The breaker core
must never `import` anything provider-specific or branch on
`cfg.provider`.

Everything provider-specific hides behind one small interface:

```python
class FutilitySensor(Protocol):
    def weight(self, outcome: str, ctx: CallContext) -> float:
        """Evidence for one observation. Positive = futile, negative =
        healthy. From per-provider fitted (or hand-set) params. Handles
        that provider's own outcome strings."""
    def is_permanent(self, outcome: str) -> bool:
        """True for errors retrying will never fix (auth, bad request).
        Bails immediately, bypassing the grace window."""
    def probe(self) -> "ProbeResult | None":
        """Cheap active health check while the circuit is open.
        None if the provider has no cheap probe."""
    def expected_completion_s(self, ctx: "CallContext") -> float | None:
        """ETA to success given current load, for the deadline-relative
        judgment. None if unknowable."""
```

`CallContext` carries only provider-agnostic state: `elapsed_s`,
`time_remaining`, the last outcome, and whatever load signals the sensor
chose to populate. The breaker consumes the abstract evidence and applies
the universal stopping rule.

**Graceful degradation is the point.** A provider implements as much as it
has signal for; the breaker degrades cleanly when signals are absent:

- **Ollama** → full sensor: `/api/ps` probe, queue-progress, load-based
  ETA. Rich discrimination.
- **Cloud (Anthropic / OpenAI-compatible)** → a `DefaultSensor`: a
  reasonable weight table over HTTP outcomes, `probe()` and
  `expected_completion_s()` return `None`. The breaker then automatically
  collapses to "outcome-weighted LLR + the hard deadline" — exactly right
  for a backend you've never saturated. The deadline backstop carries it;
  the weights are a cheap bonus if it ever *does* get overloaded.

A coupling to note: the llmclient queue (hence the queue-depth /
queue-progress signal) runs only for Ollama today. That is simply part of
the *Ollama* sensor — providers without a queue supply no progress signal
and return `None` for ETA. The abstraction absorbs the asymmetry rather
than fighting it.

### Adding a provider later (the future-day guide)

When a cloud provider eventually gets saturated enough to need real
discrimination, the work is small and additive — no core changes:

1. **Start with `DefaultSensor`.** It already gives that provider
   grace + deadline + LLR over its HTTP outcomes. Often this is enough.
2. **Tune the weight table** for that provider's outcome strings. Fit from
   its logs if you have saturation data; otherwise hand-set from the
   semantics (see the starter table in the impl plan). Rule of thumb:
   - permanent (`http_401/403/400`) → `is_permanent` true (bail now).
   - overload/unavailable (`http_529/503/500`) → strongly futile.
   - rate-limit (`http_429`) → ~0; it means the server is alive and
     talking to you, just pacing you — analogous to `timeout:queue_wait`,
     a self-congestion signal, **not** a backend-health signal.
   - timeout / unreachable → moderately futile (ambiguous, like Ollama's
     first-token).
3. **Add a `probe()`** only if the provider has a cheap liveness endpoint
   worth the call (often not — cloud capacity makes probing pointless).
4. **Add `expected_completion_s()`** only if you have a load signal to base
   it on (cloud usually doesn't; leave it `None`).
5. **Register** the sensor in the provider→sensor factory map. Fit
   per-provider `τ_heal` and weights into `_breaker_params.py` keyed by
   provider when real data exists.

The likelihood model is per-provider; the stopping logic is shared. That
boundary is the whole design.

## Explicitly out of scope

- **"Then what" after a bail** — retry, fall back to a cheaper model, hit
  a cache, ask a human. This is per-caller policy, trivially done with
  `if`/`try`/`except` in the caller. We may someday let llmclient take a
  fallback chain or callback, but not now.
- **The succeed/fail vs Plan-A/Plan-B distinction is preserved**, though,
  as the *reason a caller picks its times*: when Plan B is good, a bail is
  cheap, so you choose a short deadline (bail readily); when Plan B is
  "summon the human," a bail is expensive, so you choose a generous
  deadline (bail reluctantly). The distinction lives in the caller's head
  and documentation, not in llmclient code.

## Recommended build order

1. **Measure first (offline).** Fit `w(obs)` and `τ_heal` from existing
   `queue.db` / `llmclient_log.jsonl`. We cannot design the boundaries
   until we see how separable the outcome distributions actually are.
2. **`/api/ps` probe + pre-call covariate.** Highest leverage, lowest
   risk; also closes the squirrel blind spot and the reload confound.
3. **The leaky-LLR / deadline-relative breaker**, replacing the
   consecutive counter, behind a new config mode so the old behavior stays
   available.

See the implementation plan (scratch) for the detailed, hand-off-ready
breakdown.
