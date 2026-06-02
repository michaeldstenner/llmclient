"""
Per-provider parameters for the futility circuit breaker.

These are the *likelihood model* — a property of the backend, shared by all
callers (see docs/futility-circuit-breaker.md). The weights are
log-likelihood-ratio evidence values: w(obs) ≈ log( P(obs | futile) /
P(obs | healthy-busy) ).  Positive = evidence the backend won't succeed;
negative = evidence it is healthy.  A success is a large negative weight,
so "one success clears the circuit" falls out of the math rather than being
a special case.

IMPORTANT: these are **hand-set defaults**, not fitted.  As of 2026-06 the
central log runs at level "errors" and contains no success records, so
there is no data to fit against.  To replace these with empirical values:
run with `configure(log_level="all")` for a while, then
`scripts/fit_breaker_params.py`.  Until then, treat the numbers as
reasonable priors, not measurements.
"""

# Default trip boundary on the accumulated leaky-LLR.  Roughly: one
# definitive failure (unreachable, w≈3) plus an ambiguous one, or several
# ambiguous ones in a row before the leak bleeds them off.
DEFAULT_TRIP_BOUNDARY = 4.0

# Weight used for any non-success outcome not in a provider's table.
_UNKNOWN_WEIGHT = 1.0

# Success weight: large negative so a single success drives the accumulator
# to its floor (0).  Shared by all providers.
_SUCCESS_WEIGHT = -10.0


_OLLAMA = {
    "trip_boundary": DEFAULT_TRIP_BOUNDARY,
    # Median stall→success recovery gap.  Hand-set; refit from logs.
    "tau_heal_s": 20.0,
    "permanent": set(),
    "weights": {
        "success":              _SUCCESS_WEIGHT,
        "error:unreachable":    3.0,   # definitive: can't reach Ollama
        "timeout:queue_stall":  2.0,   # nothing completing → frozen
        "timeout:first_token":  1.0,   # ambiguous; sensor bumps if queue empty
        "timeout:generation":   0.5,   # produced tokens then stalled — milder
        "timeout:queue_wait":   0.0,   # SELF-congestion (own caller_max), not health
        "timeout":              1.0,   # generic timeout, ambiguous
        "error:empty_response": 0.5,
        "aborted":              0.0,   # our choice, not a backend signal
        "circuit_open":         0.0,   # don't double-count
        "circuit_futile":       0.0,
    },
}

# Cloud providers (Anthropic, OpenAI-compatible).  Hand-set from HTTP
# semantics — no saturation data exists yet (capacity never approached).
_DEFAULT = {
    "trip_boundary": DEFAULT_TRIP_BOUNDARY,
    "tau_heal_s": 30.0,
    # Permanent errors: retrying never helps → bail immediately.
    "permanent": {"http_400", "http_401", "http_403", "http_404", "http_422"},
    "weights": {
        "success":             _SUCCESS_WEIGHT,
        "error:unreachable":   3.0,    # cannot reach the API at all
        "http_529":            2.5,    # Anthropic overloaded
        "http_503":            2.5,    # service unavailable
        "http_502":            2.0,    # bad gateway
        "http_504":            2.0,    # gateway timeout
        "http_500":            1.5,    # server error — could be transient
        "timeout":             1.0,
        "timeout:first_token": 1.0,
        "timeout:generation":  0.5,
        "http_429":            0.0,    # rate limit: server alive, just pacing
        "aborted":             0.0,
        "error:empty_response":0.5,
    },
}

_BY_PROVIDER = {
    "ollama": _OLLAMA,
}


def get_params(provider: str) -> dict:
    """Return the breaker parameter dict for a provider (falls back to the
    cloud/default profile for anything without a dedicated table)."""
    return _BY_PROVIDER.get(provider, _DEFAULT)


def weight_for(provider: str, outcome: str) -> float:
    """Base evidence weight for an outcome under a provider."""
    table = get_params(provider)["weights"]
    if outcome == "success":
        return table.get("success", _SUCCESS_WEIGHT)
    return table.get(outcome, _UNKNOWN_WEIGHT)
