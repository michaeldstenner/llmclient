"""
Cooperative cross-process SQLite queue for Ollama slot management.

Each caller inserts a row, polls until promoted to 'running', does its
inference, then deletes the row.  Promotion logic:

  1. Reap crashed processes (running rows whose PID is gone).
  2. Check global_running < global_max  (total Ollama slots).
  3. Check caller_running < caller_max  (per-caller cap).
  4. Check no higher-priority eligible waiter exists.
  5. Atomically flip status → 'running'.

All steps happen inside BEGIN IMMEDIATE so only one process wins
the slot even when many poll simultaneously.
"""
import os
import sqlite3
import time
import threading
from pathlib import Path

_DB_PATH   = Path.home() / ".local" / "share" / "llmclient" / "queue.db"
_POLL_S    = 0.25

_CREATE = """
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


def _open() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE)
    return conn


def _try_promote(row_id: int, caller: str, priority: int,
                 caller_max: int, global_max: int) -> bool:
    """Open a fresh connection and attempt atomic promotion. Returns True on success."""
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Reap processes whose PID is gone
        for rid, pid in conn.execute(
            "SELECT id, pid FROM queue WHERE status='running'"
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
) -> tuple[int | None, float]:
    """Insert a queue row and wait until promoted.

    Returns (queue_id, queue_wait_s).
    queue_id is None if abort_event fired while waiting.
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

    t0 = time.monotonic()
    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                _delete_row(row_id)
                return None, time.monotonic() - t0

            if _try_promote(row_id, caller, cfg.priority, cfg.caller_max, global_max):
                return row_id, time.monotonic() - t0

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
