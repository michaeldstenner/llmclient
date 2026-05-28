"""
Cooperative cross-process SQLite queue for Ollama slot management,
plus a per-caller circuit breaker.

Queue: each caller inserts a row, polls until promoted to 'running',
does its inference, then deletes the row.  Promotion logic:

  1. Reap crashed processes (running rows whose PID is gone).
  2. Check global_running < global_max  (total Ollama slots).
  3. Check caller_running < caller_max  (per-caller cap).
  4. Check no higher-priority eligible waiter exists.
  5. Atomically flip status → 'running'.

Circuit breaker: tracks consecutive triggering failures per caller in
the circuit_state table.  States derived from (tripped_at, probe_pid):

  tripped_at IS NULL                          → closed  (normal)
  tripped_at NOT NULL, cooldown live, no probe → open    (reject calls)
  tripped_at NOT NULL, cooldown expired        → half-open (allow probe)
  tripped_at NOT NULL, probe_pid NOT NULL      → probing

All multi-step reads/writes happen inside BEGIN IMMEDIATE so only one
process wins races when many poll simultaneously.
"""
import os
import sqlite3
import time
import threading
from pathlib import Path

_DB_PATH   = Path.home() / ".local" / "share" / "llmclient" / "queue.db"
_POLL_S    = 0.25

_CREATE_QUEUE = """
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
"""

_CREATE_CIRCUIT = """
CREATE TABLE IF NOT EXISTS circuit_state (
    caller          TEXT    PRIMARY KEY,
    consecutive_n   INTEGER NOT NULL DEFAULT 0,
    last_failure_at REAL,
    tripped_at      REAL,
    probe_pid       INTEGER
)
"""


def _open() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_QUEUE)
    conn.execute(_CREATE_CIRCUIT)
    return conn


# ---------------------------------------------------------------------------
# Queue internals
# ---------------------------------------------------------------------------

def _try_promote(row_id: int, caller: str, priority: int,
                 caller_max: int, global_max: int) -> bool:
    """Open a fresh connection and attempt atomic promotion. Returns True on success."""
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Reap processes whose PID is gone (both waiting and running)
        for rid, pid in conn.execute(
            "SELECT id, pid FROM queue"
        ).fetchall():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                conn.execute("DELETE FROM queue WHERE id=?", [rid])

        # Global capacity check
        global_running = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status='running'"
        ).fetchone()[0]
        if global_running >= global_max:
            conn.execute("ROLLBACK")
            return False

        # Per-caller capacity check
        caller_running = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status='running' AND caller=?",
            [caller],
        ).fetchone()[0]
        if caller_running >= caller_max:
            conn.execute("ROLLBACK")
            return False

        # No higher-priority eligible waiter should jump ahead of me.
        # "Eligible" means the waiter's own caller_running < caller_max.
        blocking = conn.execute("""
            SELECT 1 FROM queue w
            WHERE  w.status   = 'waiting'
              AND  w.id      != ?
              AND  w.priority > ?
              AND  (
                SELECT COUNT(*) FROM queue r
                WHERE  r.status = 'running' AND r.caller = w.caller
              ) < w.caller_max
            LIMIT 1
        """, [row_id, priority]).fetchone()
        if blocking:
            conn.execute("ROLLBACK")
            return False

        conn.execute(
            "UPDATE queue SET status='running', started_at=? WHERE id=?",
            [time.time(), row_id],
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _delete_row(row_id: int) -> None:
    conn = _open()
    try:
        conn.execute("DELETE FROM queue WHERE id=?", [row_id])
    finally:
        conn.close()


def acquire(
    cfg,
    abort_event: threading.Event | None,
) -> tuple[int | None, float, str]:
    """Insert a queue row and wait until promoted.

    Returns (queue_id, queue_wait_s, reason).
    reason is "ok", "aborted", or "queue_timeout".
    queue_id is None when reason != "ok".
    """
    from ._keys import get_parallel_slots

    global_max = get_parallel_slots()
    caller     = cfg.log_caller or "unknown"

    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO queue "
            "(pid, caller, priority, caller_max, global_max, status, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, 'waiting', ?)",
            [os.getpid(), caller, cfg.priority, cfg.caller_max, global_max, time.time()],
        )
        row_id = cur.lastrowid
        conn.execute("COMMIT")
    finally:
        conn.close()

    t0       = time.monotonic()
    qt       = cfg.queue_timeout
    deadline = t0 + qt if qt is not None else None
    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                _delete_row(row_id)
                return None, time.monotonic() - t0, "aborted"

            if deadline is not None and time.monotonic() >= deadline:
                _delete_row(row_id)
                return None, time.monotonic() - t0, "queue_timeout"

            if _try_promote(row_id, caller, cfg.priority, cfg.caller_max, global_max):
                return row_id, time.monotonic() - t0, "ok"

            time.sleep(_POLL_S)
    except Exception:
        try:
            _delete_row(row_id)
        except Exception:
            pass
        raise


def release(queue_id: int) -> None:
    """Remove the running row, freeing the slot."""
    _delete_row(queue_id)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def circuit_check(cfg) -> str:
    """
    Returns "proceed", "open", or "probe".
    "proceed" — circuit closed, make the call normally.
    "open"    — circuit open, skip the call.
    "probe"   — half-open, this process is the designated probe.

    Fails open (returns "proceed") on any DB error.
    No-ops (returns "proceed") when circuit_n <= 0 or log_caller is empty.
    """
    caller = cfg.log_caller
    if not caller or cfg.circuit_n <= 0:
        return "proceed"

    now      = time.time()
    cooldown = cfg.circuit_cooldown_s

    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT consecutive_n, tripped_at, probe_pid "
            "FROM circuit_state WHERE caller=?",
            [caller],
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO circuit_state (caller, consecutive_n) VALUES (?, 0)",
                [caller],
            )
            conn.execute("COMMIT")
            return "proceed"

        _n, tripped_at, probe_pid = row

        if tripped_at is None:
            conn.execute("COMMIT")
            return "proceed"

        # Circuit open or probing — check for stale probe PID first.
        if probe_pid is not None:
            try:
                os.kill(probe_pid, 0)
                probe_alive = True
            except ProcessLookupError:
                probe_alive = False

            if probe_alive:
                conn.execute("COMMIT")
                return "open"
            # Crashed probe — clear it and fall through.
            conn.execute(
                "UPDATE circuit_state SET probe_pid=NULL WHERE caller=?",
                [caller],
            )
            probe_pid = None

        # Half-open or still cooling down?
        if (now - tripped_at) >= cooldown:
            conn.execute(
                "UPDATE circuit_state SET probe_pid=? WHERE caller=?",
                [os.getpid(), caller],
            )
            conn.execute("COMMIT")
            return "probe"

        conn.execute("COMMIT")
        return "open"

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return "proceed"
    finally:
        conn.close()


def circuit_record(cfg, outcome: str, is_probe: bool) -> None:
    """
    Update circuit breaker state after a call completes.

    success         → reset consecutive_n, clear trip.
    triggering      → increment consecutive_n; trip if threshold reached;
                      if is_probe, re-trip and reset cooldown.
    non-triggering  → no state change (but clear probe_pid if is_probe).
    """
    caller = cfg.log_caller
    if not caller or cfg.circuit_n <= 0:
        return

    triggers = set(cfg.circuit_triggers)
    now      = time.time()

    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT consecutive_n, tripped_at, probe_pid "
            "FROM circuit_state WHERE caller=?",
            [caller],
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO circuit_state (caller, consecutive_n) VALUES (?, 0)",
                [caller],
            )
            row = (0, None, None)

        consecutive_n, tripped_at, _probe_pid = row

        if outcome == "success":
            conn.execute(
                "UPDATE circuit_state "
                "SET consecutive_n=0, last_failure_at=NULL, "
                "    tripped_at=NULL, probe_pid=NULL "
                "WHERE caller=?",
                [caller],
            )
        elif outcome in triggers:
            new_n = consecutive_n + 1
            if is_probe:
                # Probe failed — re-trip and reset the cooldown clock.
                conn.execute(
                    "UPDATE circuit_state "
                    "SET consecutive_n=?, last_failure_at=?, "
                    "    tripped_at=?, probe_pid=NULL "
                    "WHERE caller=?",
                    [new_n, now, now, caller],
                )
            else:
                should_trip = (new_n >= cfg.circuit_n)
                if should_trip:
                    conn.execute(
                        "UPDATE circuit_state "
                        "SET consecutive_n=?, last_failure_at=?, tripped_at=? "
                        "WHERE caller=?",
                        [new_n, now, now, caller],
                    )
                else:
                    conn.execute(
                        "UPDATE circuit_state "
                        "SET consecutive_n=?, last_failure_at=? "
                        "WHERE caller=?",
                        [new_n, now, caller],
                    )
        else:
            # Non-triggering outcome — still clear probe_pid if we were probing.
            if is_probe:
                conn.execute(
                    "UPDATE circuit_state SET probe_pid=NULL WHERE caller=?",
                    [caller],
                )

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        conn.close()
