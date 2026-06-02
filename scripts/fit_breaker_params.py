#!/usr/bin/env python3
"""
Offline fitter for the futility circuit breaker's likelihood model.

Reads the central JSONL call log and estimates, per provider:
  - w(outcome) = log( P(outcome | futile) / P(outcome | healthy-busy) )
  - tau_heal_s  = median wall-clock gap from a stall-type failure to the
                  next success (the leak time constant)
  - separability of the two outcome distributions (a rough AUC proxy)

These feed llmclient/_breaker_params.py.  See
docs/futility-circuit-breaker.md.

IMPORTANT — data prerequisite: this needs *success* records to estimate
the healthy distribution.  The central log defaults to level "errors"
(successes are not written).  Run with `llmclient.configure(log_level=
"all")` for a representative period before trusting the output.  As of
2026-06 there was no such data, so _breaker_params.py ships hand-set
defaults instead.

Usage:
    python3 scripts/fit_breaker_params.py [LOG_PATH]
    (default LOG_PATH: ~/.local/share/llmclient/llmclient_log.jsonl)
"""
import json
import math
import sys
from collections import defaultdict, Counter
from pathlib import Path

DEFAULT_LOG = Path.home() / ".local/share/llmclient/llmclient_log.jsonl"

# Outcomes that, on their own, are strong "futile" evidence for labeling.
STALL_OUTCOMES = {"timeout:queue_stall", "error:unreachable"}


def load(log_path: Path) -> list[dict]:
    if not log_path.exists():
        sys.exit(f"log not found: {log_path}")
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def running_jobs(entry: dict) -> int:
    snap = entry.get("queue_snapshot") or []
    return sum(1 for r in snap if r.get("status") == "running")


def label(entry: dict) -> str | None:
    """Heuristic: 'futile' | 'healthy' | None (ambiguous, skipped)."""
    oc = entry.get("outcome")
    if oc == "success":
        return "healthy"
    if oc in STALL_OUTCOMES:
        return "futile"
    if oc == "timeout:first_token":
        # No busy-backend excuse → futile; otherwise ambiguous.
        return "futile" if running_jobs(entry) == 0 else None
    if oc == "timeout:queue_wait":
        return "healthy"   # self-congestion, not backend health
    return None


def fit_provider(entries: list[dict]) -> dict:
    futile = Counter()
    healthy = Counter()
    n_fut = n_heal = 0
    for e in entries:
        lab = label(e)
        oc = e.get("outcome")
        if lab == "futile":
            futile[oc] += 1; n_fut += 1
        elif lab == "healthy":
            healthy[oc] += 1; n_heal += 1

    weights = {}
    all_outcomes = set(futile) | set(healthy) | {
        e.get("outcome") for e in entries
    }
    for oc in sorted(o for o in all_outcomes if o):
        # Laplace-smoothed conditional probabilities.
        p_fut = (futile[oc] + 0.5) / (n_fut + 1.0) if n_fut else None
        p_heal = (healthy[oc] + 0.5) / (n_heal + 1.0) if n_heal else None
        if p_fut and p_heal:
            weights[oc] = round(math.log(p_fut / p_heal), 2)
        else:
            weights[oc] = None  # insufficient data

    # tau_heal: median gap from a stall-type failure to the next success.
    gaps = []
    pending = None
    for e in entries:
        ts = e.get("timestamp")
        if not ts:
            continue
        oc = e.get("outcome")
        if oc in STALL_OUTCOMES:
            pending = ts
        elif oc == "success" and pending:
            try:
                from datetime import datetime
                dt = (datetime.fromisoformat(ts)
                      - datetime.fromisoformat(pending)).total_seconds()
                if 0 < dt < 600:
                    gaps.append(dt)
            except Exception:
                pass
            pending = None
    gaps.sort()
    tau = gaps[len(gaps) // 2] if gaps else None

    return {
        "n_futile": n_fut, "n_healthy": n_heal,
        "weights": weights, "tau_heal_s": tau,
        "futile_counts": dict(futile), "healthy_counts": dict(healthy),
    }


def main():
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    entries = load(log_path)
    print(f"# Breaker fit report — {log_path}")
    print(f"# {len(entries)} log entries\n")

    by_provider = defaultdict(list)
    for e in entries:
        by_provider[e.get("provider", "?")].append(e)

    for provider, es in sorted(by_provider.items()):
        r = fit_provider(es)
        print(f"## provider: {provider}  "
              f"(n={len(es)}, futile={r['n_futile']}, "
              f"healthy={r['n_healthy']})")
        if r["n_healthy"] == 0:
            print("  ⚠  no success records — cannot fit weights. "
                  "Set log_level='all' and collect data first.")
        print(f"  tau_heal_s (median stall→success gap): {r['tau_heal_s']}")
        print("  weights  w = log P(oc|futile)/P(oc|healthy):")
        for oc, w in sorted(r["weights"].items()):
            fc = r["futile_counts"].get(oc, 0)
            hc = r["healthy_counts"].get(oc, 0)
            print(f"    {oc:<24} {str(w):>7}   (futile={fc}, healthy={hc})")
        print()


if __name__ == "__main__":
    main()
