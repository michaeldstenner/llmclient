"""
Tests for _queue.py — cooperative SQLite slot management.

All tests use a temp DB (queue_db fixture) and a fast poll interval
(0.02 s) so they run quickly without real process contention.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import llmclient._queue as q_mod
from llmclient._queue import acquire, release, _try_promote, _delete_row
from tests.conftest import make_cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level="", timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pid          INTEGER NOT NULL,
            caller       TEXT    NOT NULL,
            priority     INTEGER NOT NULL DEFAULT 50,
            caller_max   INTEGER NOT NULL DEFAULT 4,
            global_max   INTEGER NOT NULL DEFAULT 4,
            status       TEXT    NOT NULL DEFAULT 'waiting',
            submitted_at REAL    NOT NULL,
            started_at   REAL
        )
    """)
    return conn


def _insert(db_path, *, pid, caller="test", priority=50,
            caller_max=4, global_max=4, status="waiting") -> int:
    conn = _open(db_path)
    cur = conn.execute(
        "INSERT INTO queue "
        "(pid,caller,priority,caller_max,global_max,status,submitted_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [pid, caller, priority, caller_max, global_max, status, time.time()],
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _status(db_path, row_id) -> str | None:
    conn = _open(db_path)
    row = conn.execute("SELECT status FROM queue WHERE id=?", [row_id]).fetchone()
    conn.close()
    return row[0] if row else None


def _count(db_path, status) -> int:
    conn = _open(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM queue WHERE status=?", [status]
    ).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# _try_promote
# ---------------------------------------------------------------------------

def test_promote_empty_queue(queue_db):
    """A single waiting row promotes immediately."""
    row_id = _insert(queue_db, pid=os.getpid())
    assert _try_promote(row_id, "test", 50, 4, 4) is True
    assert _status(queue_db, row_id) == "running"


def test_promote_blocked_by_global_max(queue_db):
    """A slot-full queue blocks promotion."""
    _insert(queue_db, pid=os.getpid(), status="running")
    waiting = _insert(queue_db, pid=os.getpid())
    assert _try_promote(waiting, "test", 50, 4, 1) is False
    assert _status(queue_db, waiting) == "waiting"


def test_promote_blocked_by_caller_max(queue_db):
    """Per-caller cap blocks a second call from the same caller."""
    _insert(queue_db, pid=os.getpid(), caller="bouncer", status="running")
    waiting = _insert(queue_db, pid=os.getpid(), caller="bouncer")
    assert _try_promote(waiting, "bouncer", 50, 1, 4) is False


def test_promote_blocked_by_higher_priority_waiter(queue_db):
    """A lower-priority waiter yields to a higher-priority one."""
    high = _insert(queue_db, pid=os.getpid(), priority=100, caller="bouncer",
                   caller_max=4)
    low  = _insert(queue_db, pid=os.getpid(), priority=10, caller="watchdog",
                   caller_max=4)
    assert _try_promote(low, "watchdog", 10, 4, 4) is False
    # High-priority can still promote
    assert _try_promote(high, "bouncer", 100, 4, 4) is True


def test_promote_higher_priority_blocked_by_own_caller_max_does_not_block_lower(queue_db):
    """
    If the high-priority waiter is itself blocked (caller_max reached),
    the lower-priority waiter should be able to proceed.
    """
    # bouncer already has a running call; caller_max=1 means it can't run
    _insert(queue_db, pid=os.getpid(), caller="bouncer", status="running")
    high = _insert(queue_db, pid=os.getpid(), priority=100, caller="bouncer",
                   caller_max=1)
    low  = _insert(queue_db, pid=os.getpid(), priority=10, caller="watchdog",
                   caller_max=4)
    # high is blocked by its own caller_max, so low should proceed
    assert _try_promote(low, "watchdog", 10, 4, 4) is True


def test_crash_reaping(queue_db):
    """A running row with a dead PID is reaped on the next promotion attempt."""
    dead_pid = 999999  # guaranteed not to exist
    _insert(queue_db, pid=dead_pid, status="running")
    waiting = _insert(queue_db, pid=os.getpid())
    # Promote should reap dead_pid row and then succeed
    assert _try_promote(waiting, "test", 50, 4, 4) is True
    assert _count(queue_db, "running") == 1


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------

def test_acquire_release_round_trip(queue_db, monkeypatch):
    monkeypatch.setattr(q_mod, "_DB_PATH", queue_db)
    monkeypatch.setattr(q_mod, "_POLL_S", 0.02)
    from unittest.mock import patch
    with patch("llmclient._keys.get_parallel_slots", return_value=4):
        cfg = make_cfg(log_caller="test", priority=50, caller_max=4, queue_mode="cooperative")
        row_id, wait_s, reason = acquire(cfg, None)
        assert row_id is not None
        assert wait_s >= 0.0
        assert reason == "ok"
        assert _status(queue_db, row_id) == "running"
        release(row_id)
        assert _status(queue_db, row_id) is None


def test_acquire_aborted_immediately(queue_db, monkeypatch):
    monkeypatch.setattr(q_mod, "_DB_PATH", queue_db)
    monkeypatch.setattr(q_mod, "_POLL_S", 0.02)
    from unittest.mock import patch
    with patch("llmclient._keys.get_parallel_slots", return_value=4):
        abort = threading.Event()
        abort.set()  # already fired
        cfg = make_cfg(log_caller="test", priority=50, caller_max=4)
        row_id, wait_s, reason = acquire(cfg, abort)
        assert row_id is None
        assert reason == "aborted"
        assert _count(queue_db, "waiting") == 0


def test_acquire_waits_then_proceeds(queue_db, monkeypatch):
    """A waiter blocked by a full slot eventually runs after release."""
    monkeypatch.setattr(q_mod, "_DB_PATH", queue_db)
    monkeypatch.setattr(q_mod, "_POLL_S", 0.02)
    from unittest.mock import patch

    with patch("llmclient._keys.get_parallel_slots", return_value=1):
        cfg = make_cfg(log_caller="test", priority=50, caller_max=4)

        # Occupy the one slot
        row1, _, _r = acquire(cfg, None)
        assert row1 is not None

        # Start second acquire in a thread — it should wait
        results: dict = {}
        def _second():
            row2, w, _ = acquire(cfg, None)
            results["row_id"] = row2
            results["wait_s"] = w
            if row2 is not None:
                release(row2)

        t = threading.Thread(target=_second, daemon=True)
        t.start()
        time.sleep(0.05)  # let it enter the wait loop
        assert results == {}  # still blocked

        release(row1)         # free the slot
        t.join(timeout=2.0)
        assert results.get("row_id") is not None
        assert results["wait_s"] > 0.0
