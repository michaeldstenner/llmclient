"""
FutilitySensor — the provider-specific evidence layer for the futility
circuit breaker.

The breaker core (in _queue.py) is provider-agnostic: it accumulates
evidence, respects grace/deadline, and decides bail vs wait.  Everything
that depends on *which* backend we're talking to lives behind the
FutilitySensor interface here.  See docs/futility-circuit-breaker.md.

Two implementations:
  - DefaultSensor — weight table over outcomes; no probe, no ETA.  Used for
    cloud providers (Anthropic, OpenAI-compatible).  The breaker degrades
    cleanly to "outcome-weighted LLR + hard deadline".
  - OllamaSensor — adds a cheap /api/ps liveness probe and a queue-aware
    tweak to the ambiguous first-token weight.

To add a richer sensor for a new provider, implement the FutilitySensor
methods and register it in get_sensor().
"""
import json
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ._breaker_params import get_params, weight_for


@dataclass
class CallContext:
    """Provider-agnostic state the breaker hands to the sensor.  Fields the
    provider can't observe are left at their neutral defaults."""
    elapsed_s:       float = 0.0
    time_remaining:  float | None = None   # to deadline; None = infinite
    last_outcome:    str | None = None
    running_jobs:    int = 0
    queue_depth:     int = 0
    queue_advancing: bool | None = None     # None = no progress signal


@dataclass
class ProbeResult:
    healthy: bool
    detail:  str = ""


@runtime_checkable
class FutilitySensor(Protocol):
    def weight(self, outcome: str, ctx: CallContext) -> float: ...
    def is_permanent(self, outcome: str) -> bool: ...
    def probe(self) -> "ProbeResult | None": ...
    def expected_completion_s(self, ctx: CallContext) -> float | None: ...


class DefaultSensor:
    """Outcome-weighted sensor with no active probe or load model.
    Correct default for high-capacity cloud backends."""

    def __init__(self, provider: str, cfg=None):
        p = get_params(provider)
        self._provider  = provider
        self._permanent = p["permanent"]
        self.tau_heal_s = p["tau_heal_s"]
        self.trip_boundary = p["trip_boundary"]
        self._cfg = cfg

    def weight(self, outcome: str, ctx: CallContext) -> float:
        return weight_for(self._provider, outcome)

    def is_permanent(self, outcome: str) -> bool:
        return outcome in self._permanent

    def probe(self) -> "ProbeResult | None":
        return None

    def expected_completion_s(self, ctx: CallContext) -> float | None:
        return None


class OllamaSensor(DefaultSensor):
    """Ollama-specific sensor: /api/ps liveness probe + queue-aware
    first-token weighting."""

    def weight(self, outcome: str, ctx: CallContext) -> float:
        w = super().weight(outcome, ctx)
        # An ambiguous first-token timeout is far more damning when the
        # queue is empty — there is no busy-backend excuse for the delay.
        if outcome == "timeout:first_token" and ctx is not None \
                and ctx.running_jobs == 0:
            w += 1.0
        return w

    def probe(self) -> "ProbeResult | None":
        url = self._probe_url()
        if not url:
            return None
        models = _ollama_ps(url)
        if models is None:
            # /api/ps did not respond → server hung or down.
            return ProbeResult(False, "unreachable")
        # Server responded (even with no models loaded) → it is alive and
        # will reload on demand.  Safe to close and retry.
        n = len(models)
        return ProbeResult(True, f"{n} model(s) resident" if n else "alive, idle")

    def expected_completion_s(self, ctx: CallContext) -> float | None:
        # v1: no load model yet.  Reserved for the deadline-relative
        # predictive bail (needs HEALTHY_FT_P99 from fitted data).
        return None

    def _probe_url(self) -> str:
        cfg = self._cfg
        explicit = (getattr(cfg, "ps_url", "") or getattr(cfg, "url", "")) if cfg else ""
        try:
            from ._keys import resolve_url
            return resolve_url("ollama", explicit)
        except Exception:
            return explicit


def _ollama_ps(url: str, timeout: float = 3.0) -> list[dict] | None:
    """GET {url}/api/ps. Returns the models list (possibly empty) when the
    server responds, or None on timeout / connection error.  Never raises."""
    try:
        with urllib.request.urlopen(url + "/api/ps", timeout=timeout) as r:
            return json.loads(r.read()).get("models", [])
    except Exception:
        return None


def get_sensor(provider: str, cfg=None) -> FutilitySensor:
    """Factory: provider → sensor instance.  The breaker only ever calls
    the FutilitySensor methods; it never learns which concrete type it has."""
    if provider == "ollama":
        return OllamaSensor(provider, cfg)
    return DefaultSensor(provider, cfg)
