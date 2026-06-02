"""
Tests for the futility circuit breaker — _sensor.py and the futility_*
functions in _queue.py.

All state tests use the queue_db fixture (temp DB + fast poll) so they
exercise the real SQLite-backed accumulator.
"""
import os
import time

import pytest

import llmclient._queue as q_mod
from llmclient._queue import futility_check, futility_update, acquire
from llmclient._sensor import (
    CallContext, ProbeResult, DefaultSensor, OllamaSensor, get_sensor,
)
from tests.conftest import make_cfg


# ---------------------------------------------------------------------------
# Test doubles / helpers
# ---------------------------------------------------------------------------

class FakeSensor:
    def __init__(self, weights, *, tau=20.0, boundary=4.0,
                 permanent=(), probe_result=None):
        self._w = weights
        self.tau_heal_s = tau
        self.trip_boundary = boundary
        self._perm = set(permanent)
        self._probe = probe_result

    def weight(self, outcome, ctx):
        return self._w.get(outcome, 1.0)

    def is_permanent(self, outcome):
        return outcome in self._perm

    def probe(self):
        return self._probe

    def expected_completion_s(self, ctx):
        return None


def _fut_cfg(**kw):
    base = dict(
        log_caller="tester",
        circuit_mode="futility",
        circuit_cooldown_s=120.0,
    )
    base.update(kw)
    return make_cfg(**base)


def _state(key="tester"):
    conn = q_mod._open()
    try:
        return conn.execute(
            "SELECT llr, tripped_at, probe_pid FROM circuit_state "
            "WHERE circuit_key=?",
            [key],
        ).fetchone()
    finally:
        conn.close()


def _set_state(key="tester", *, llr=0.0, updated=None, tripped=None,
               probe_pid=None):
    conn = q_mod._open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO circuit_state (circuit_key, caller, consecutive_n, "
            "llr, llr_updated_at, tripped_at, probe_pid) VALUES (?,?,0,?,?,?,?) "
            "ON CONFLICT(circuit_key) DO UPDATE SET llr=excluded.llr, "
            "llr_updated_at=excluded.llr_updated_at, "
            "tripped_at=excluded.tripped_at, probe_pid=excluded.probe_pid",
            [key, key, llr, updated, tripped, probe_pid],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


UNREACH = {"error:unreachable": 3.0, "noise": 0.0, "success": -10.0}


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

def test_default_sensor_weights():
    s = DefaultSensor("anthropic")
    assert s.weight("http_529", CallContext()) == 2.5
    assert s.weight("http_429", CallContext()) == 0.0      # rate limit ≈ neutral
    assert s.weight("success", CallContext()) < 0          # health evidence
    assert s.is_permanent("http_401") is True
    assert s.is_permanent("http_529") is False
    assert s.probe() is None                                # no probe for cloud
    assert s.expected_completion_s(CallContext()) is None


def test_ollama_sensor_first_token_queue_bump():
    s = OllamaSensor("ollama")
    empty = CallContext(running_jobs=0)
    busy  = CallContext(running_jobs=2)
    # Ambiguous first-token is more damning when the queue is empty.
    assert s.weight("timeout:first_token", empty) > s.weight("timeout:first_token", busy)


def test_get_sensor_dispatch():
    assert isinstance(get_sensor("ollama"), OllamaSensor)
    assert isinstance(get_sensor("anthropic"), DefaultSensor)
    assert isinstance(get_sensor("openai"), DefaultSensor)


def test_ollama_probe_healthy_and_down(monkeypatch):
    cfg = make_cfg(provider="ollama", url="http://localhost:11434")
    s = OllamaSensor("ollama", cfg)
    monkeypatch.setattr("llmclient._sensor._ollama_ps", lambda url, timeout=3.0: [])
    pr = s.probe()
    assert pr is not None and pr.healthy is True
    monkeypatch.setattr("llmclient._sensor._ollama_ps", lambda url, timeout=3.0: None)
    pr = s.probe()
    assert pr is not None and pr.healthy is False


# ---------------------------------------------------------------------------
# futility_update — accumulator math
# ---------------------------------------------------------------------------

def test_accumulates_and_trips(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor(UNREACH, boundary=4.0)
    ctx = CallContext()

    futility_update(cfg, sensor, "error:unreachable", ctx, is_probe=False)
    llr, tripped, _ = _state()
    assert llr == pytest.approx(3.0, abs=0.05)
    assert tripped is None                      # 3 < boundary 4

    futility_update(cfg, sensor, "error:unreachable", ctx, is_probe=False)
    llr, tripped, _ = _state()
    # ~6.0 minus a hair of leak between the two updates.
    assert llr == pytest.approx(6.0, abs=0.05)
    assert tripped is not None                  # 6 ≥ 4 → tripped


def test_success_resets(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor(UNREACH, boundary=4.0)
    ctx = CallContext()
    _set_state(llr=6.0, updated=time.time(), tripped=time.time())

    futility_update(cfg, sensor, "success", ctx, is_probe=False)
    llr, tripped, _ = _state()
    assert llr == pytest.approx(0.0)
    assert tripped is None


def test_permanent_trips_immediately(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor({"http_401": 0.0}, boundary=4.0, permanent={"http_401"})
    futility_update(cfg, sensor, "http_401", CallContext(), is_probe=False)
    llr, tripped, _ = _state()
    assert llr == pytest.approx(4.0)            # forced to boundary
    assert tripped is not None


def test_leak_decays_evidence(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor(UNREACH, tau=20.0, boundary=100.0)  # high boundary: no trip
    # Seed llr=3 one tau ago, then add a zero-weight observation.
    _set_state(llr=3.0, updated=time.time() - 20.0)
    futility_update(cfg, sensor, "noise", CallContext(), is_probe=False)
    llr, _, _ = _state()
    # 3 * e^-1 ≈ 1.10
    assert 0.8 < llr < 1.5


def test_probe_failure_rearms_cooldown(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor(UNREACH, boundary=4.0)
    old = time.time() - 1000
    # updated=now so the leak doesn't decay the seeded evidence; tripped=old
    # so we can detect the cooldown clock being re-armed forward.
    _set_state(llr=6.0, updated=time.time(), tripped=old, probe_pid=os.getpid())
    futility_update(cfg, sensor, "error:unreachable", CallContext(), is_probe=True)
    llr, tripped, probe_pid = _state()
    assert tripped is not None and tripped > old + 500    # cooldown re-armed
    assert probe_pid is None


def test_probe_recovery_closes(queue_db):
    cfg = _fut_cfg()
    sensor = FakeSensor(UNREACH, boundary=4.0)
    _set_state(llr=6.0, updated=time.time(), tripped=time.time(),
               probe_pid=os.getpid())
    futility_update(cfg, sensor, "success", CallContext(), is_probe=True)
    llr, tripped, probe_pid = _state()
    assert tripped is None and probe_pid is None and llr == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# futility_check — state machine
# ---------------------------------------------------------------------------

def test_check_closed_proceeds(queue_db):
    cfg = _fut_cfg()
    assert futility_check(cfg, FakeSensor(UNREACH)) == "proceed"


def test_check_open_during_cooldown(queue_db):
    cfg = _fut_cfg(circuit_cooldown_s=120.0)
    _set_state(llr=6.0, updated=time.time(), tripped=time.time())
    assert futility_check(cfg, FakeSensor(UNREACH)) == "open"


def test_check_ps_probe_healthy_closes(queue_db):
    cfg = _fut_cfg(circuit_cooldown_s=0.0, ps_probe=True)
    _set_state(llr=6.0, updated=time.time(), tripped=time.time() - 1)
    sensor = FakeSensor(UNREACH, probe_result=ProbeResult(True, "alive"))
    assert futility_check(cfg, sensor) == "proceed"
    _llr, tripped, _ = _state()
    assert tripped is None                       # cheap probe re-closed it


def test_check_ps_probe_unhealthy_stays_open(queue_db):
    cfg = _fut_cfg(circuit_cooldown_s=0.0, ps_probe=True)
    _set_state(llr=6.0, updated=time.time(), tripped=time.time() - 1)
    sensor = FakeSensor(UNREACH, probe_result=ProbeResult(False, "down"))
    assert futility_check(cfg, sensor) == "open"
    _llr, tripped, _ = _state()
    assert tripped is not None                   # re-armed


def test_check_no_ps_probe_returns_probe(queue_db):
    cfg = _fut_cfg(circuit_cooldown_s=0.0, ps_probe=False)
    _set_state(llr=6.0, updated=time.time(), tripped=time.time() - 1)
    assert futility_check(cfg, FakeSensor(UNREACH)) == "probe"
    _llr, _tripped, probe_pid = _state()
    assert probe_pid == os.getpid()


# ---------------------------------------------------------------------------
# Backward compatibility — count mode is the default and untouched
# ---------------------------------------------------------------------------

def test_count_mode_is_default():
    from llmclient import LLMConfig
    assert LLMConfig(provider="ollama", model="m").circuit_mode == "count"


def test_futility_functions_noop_in_count_mode(queue_db):
    cfg = make_cfg(log_caller="c", circuit_mode="count", circuit_n=2)
    assert futility_check(cfg, FakeSensor(UNREACH)) == "proceed"
    futility_update(cfg, FakeSensor(UNREACH), "error:unreachable",
                    CallContext(), is_probe=False)
    assert _state("c") is None                   # no futility row written


# ---------------------------------------------------------------------------
# acquire grace gating
# ---------------------------------------------------------------------------

def test_acquire_grace_suppresses_stall_bail(queue_db):
    """With grace_s large, an already-stale queue must hit the hard
    queue_timeout (not the stall bail) — proving grace gates the stall path."""
    from unittest.mock import patch

    # Occupy the only slot with a running row, and record an ancient
    # completion so the stall condition is immediately true.
    conn = q_mod._open()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO queue (pid, caller, model, priority, caller_max, "
        "global_max, status, submitted_at, started_at) "
        "VALUES (?, 'busy', 'test-model', 50, 4, 1, 'running', ?, ?)",
        [os.getpid(), time.time(), time.time()],
    )
    old = str(time.time() - 1000)
    conn.execute("INSERT OR REPLACE INTO queue_meta (key, value) VALUES "
                 "('last_release_at:test-model', ?)", [old])
    conn.execute("INSERT OR REPLACE INTO queue_meta (key, value) VALUES "
                 "('last_release_at', ?)", [old])
    conn.execute("COMMIT")
    conn.close()

    with patch("llmclient._keys.get_parallel_slots", return_value=1):
        cfg = make_cfg(
            log_caller="tester", model="test-model",
            queue_mode="cooperative", caller_max=4,
            queue_stall_timeout=0.01, queue_timeout=0.3,
        )
        rid, wait_s, reason, _snap = acquire(cfg, None, grace_s=10.0)
        assert rid is None
        # Grace (10s) suppresses the stall bail, so we fall to the hard
        # ceiling at 0.3s instead of bailing on stall at 0.01s.
        assert reason == "queue_timeout"
        assert wait_s < 5.0


# ---------------------------------------------------------------------------
# circuit_key scoping + migration
# ---------------------------------------------------------------------------

def test_circuit_key_falls_back_to_caller(queue_db):
    """With no circuit_key, breaker state keys on log_caller (back-compat)."""
    cfg = _fut_cfg(log_caller="svc")
    futility_update(cfg, FakeSensor({"x": 5.0}, boundary=4.0), "x",
                    CallContext(), is_probe=False)
    row = _state("svc")                          # keyed by the caller name
    assert row is not None
    assert row[0] == pytest.approx(5.0, abs=0.1)


def test_circuit_key_scopes_independently(queue_db):
    """Same caller, different circuit_key → independent breaker state."""
    sensor = FakeSensor({"x": 5.0}, boundary=4.0)
    a = _fut_cfg(log_caller="svc", circuit_key="svc|m1")
    b = _fut_cfg(log_caller="svc", circuit_key="svc|m2")
    futility_update(a, sensor, "x", CallContext(), is_probe=False)
    assert _state("svc|m1") is not None          # m1 tripped
    assert _state("svc|m2") is None              # m2 untouched
    assert futility_check(b, sensor) == "proceed"


def test_count_mode_keys_on_circuit_key(queue_db):
    from llmclient._queue import circuit_check, circuit_record
    cfg = make_cfg(log_caller="svc", circuit_key="svc|m1",
                   circuit_n=1, circuit_triggers=("error:unreachable",))
    assert circuit_check(cfg) == "proceed"
    circuit_record(cfg, "error:unreachable", is_probe=False)
    conn = q_mod._open()
    try:
        row = conn.execute(
            "SELECT tripped_at, caller FROM circuit_state WHERE circuit_key=?",
            ["svc|m1"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None
    assert row[1] == "svc"                        # caller kept as metadata
    assert circuit_check(cfg) == "open"


def test_migration_rekeys_caller_to_circuit_key(queue_db):
    """An old caller-PK circuit_state migrates to circuit_key on open."""
    import sqlite3
    queue_db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(queue_db))
    raw.execute(
        "CREATE TABLE circuit_state ("
        " caller TEXT PRIMARY KEY, consecutive_n INTEGER NOT NULL DEFAULT 0,"
        " last_failure_at REAL, tripped_at REAL, probe_pid INTEGER)"
    )
    raw.execute(
        "INSERT INTO circuit_state (caller, consecutive_n, tripped_at) "
        "VALUES ('legacy', 3, 123.0)"
    )
    raw.commit()
    raw.close()
    conn = q_mod._open()                          # triggers the migration
    try:
        cols = {r[1] for r in
                conn.execute("PRAGMA table_info(circuit_state)").fetchall()}
        assert "circuit_key" in cols and "llr" in cols
        row = conn.execute(
            "SELECT circuit_key, caller, consecutive_n, tripped_at, llr "
            "FROM circuit_state WHERE circuit_key='legacy'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("legacy", "legacy", 3, 123.0, 0.0)
