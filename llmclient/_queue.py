"""
Cooperative cross-process SQLite queue for Ollama slot management,
plus a per-caller circuit breaker.

Queue: each caller inserts a row, polls until promoted to 'running',
does its inference, then deletes the row.  Promotion logic is
model-scoped — requests for different models do not compete for the
same slots.  Within a model:

  1. Reap crashed processes (running rows whose PID is gone).
  2. Check model_running < global_max  (total slots for this model).
  3. Check caller_model_running < caller_max  (per-caller cap).
  4. Check no higher-priority eligible waiter for the same model.
  5. Atomically flip status → 'running'.

Circuit breaker: tracks consecutive triggering failures per circuit key in
the circuit_state table.  States derived from (tripped_at, probe_pid):

  tripped_at IS NULL                          → closed  (normal)
  tripped_at NOT NULL, cooldown live, no probe → open    (reject calls)
  tripped_at NOT NULL, cooldown expired        → half-open (allow probe)
  tripped_at NOT NULL, probe_pid NOT NULL      → probing

queue_meta: single-row bookkeeping.  Tracks last_release_at globally
and per-model (key 'last_release_at:<model>') so stall detection for
one model is not masked by completions from another.
"""
import logging
import os
import sqlite3
import time
import threading
from pathlib import Path

_POLL_S    = 0.25
_log       = logging.getLogger("llmclient.queue")

_CREATE_QUEUE = """
CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pid          INTEGER NOT NULL,
    caller       TEXT    NOT NULL,
    model        TEXT    NOT NULL DEFAULT '',
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
    circuit_key     TEXT    PRIMARY KEY,
    caller          TEXT    NOT NULL DEFAULT '',
    consecutive_n   INTEGER NOT NULL DEFAULT 0,
    last_failure_at REAL,
    tripped_at      REAL,
    probe_pid       INTEGER
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS queue_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _open() -> sqlite3.Connection:
    from ._config import get_db_path
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_QUEUE)
    conn.execute(_CREATE_CIRCUIT)
    conn.execute(_CREATE_META)
    _migrate_circuit_state(conn)
    # Migrate existing DBs that predate the model column
    try:
        conn.execute(
            "ALTER TABLE queue ADD COLUMN model TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        pass
    return conn


def _migrate_circuit_state(conn: sqlite3.Connection) -> None:
    """Migrate pre-circuit_key DBs while preserving caller-wide state."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(circuit_state)").fetchall()
    }
    if "circuit_key" in columns:
        return

    conn.execute("ALTER TABLE circuit_state RENAME TO circuit_state_old")
    conn.execute(_CREATE_CIRCUIT)
    conn.execute("""
        INSERT OR IGNORE INTO circuit_state
            (circuit_key, caller, consecutive_n, last_failure_at,
             tripped_at, probe_pid)
        SELECT
            caller, caller, consecutive_n, last_failure_at,
            tripped_at, probe_pid
        FROM circuit_state_old
    """)
    conn.execute("DROP TABLE circuit_state_old")


# ---------------------------------------------------------------------------
# Queue internals
# ---------------------------------------------------------------------------

def _try_promote(row_id: int, caller: str, model: str, priority: int,
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

        # Per-model global capacity check
        model_running = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status='running' AND model=?",
            [model],
        ).fetchone()[0]
        if model_running >= global_max:
            conn.execute("ROLLBACK")
            return False

        # Per-model per-caller capacity check
        caller_model_running = conn.execute(
            "SELECT COUNT(*) FROM queue "
            "WHERE status='running' AND caller=? AND model=?",
            [caller, model],
        ).fetchone()[0]
        if caller_model_running >= caller_max:
            conn.execute("ROLLBACK")
            return False

        # No higher-priority eligible waiter for the same model should jump
        # ahead.  "Eligible" means the waiter's own caller_model_running <
        # caller_max.
        blocking = conn.execute("""
            SELECT 1 FROM queue w
            WHERE  w.status   = 'waiting'
              AND  w.id      != ?
              AND  w.model   = ?
              AND  w.priority > ?
              AND  (
                SELECT COUNT(*) FROM queue r
                WHERE  r.status = 'running'
                  AND  r.caller = w.caller
                  AND  r.model  = w.model
              ) < w.caller_max
            LIMIT 1
        """, [row_id, model, priority]).fetchone()
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


def _read_queue_state() -> list[dict]:
    """Return a snapshot of all queue rows for diagnostics."""
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT id, pid, caller, model, priority, status, "
            "submitted_at, started_at "
            "FROM queue ORDER BY submitted_at"
        ).fetchall()
        now = time.time()
        return [
            {
                "id":        r[0],
                "pid":       r[1],
                "caller":    r[2],
                "model":     r[3],
                "priority":  r[4],
                "status":    r[5],
                "age_s":     round(now - r[6], 1),
                "running_s": round(now - r[7], 1) if r[7] else None,
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()


def acquire(
    cfg,
    abort_event: threading.Event | None,
) -> tuple[int | None, float, str, list[dict] | None]:
    """Insert a queue row and wait until promoted.

    Returns (queue_id, queue_wait_s, reason, queue_snapshot).
    reason is "ok", "aborted", "queue_timeout", or "queue_stalled".
    queue_id is None when reason != "ok".
    queue_snapshot is populated on "queue_timeout" and "queue_stalled".
    """
    from ._keys import get_parallel_slots

    global_max = get_parallel_slots()
    caller     = cfg.log_caller or "unknown"
    model      = cfg.model or ""

    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO queue "
            "(pid, caller, model, priority, caller_max, global_max, "
            " status, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?)",
            [os.getpid(), caller, model, cfg.priority,
             cfg.caller_max, global_max, time.time()],
        )
        row_id = cur.lastrowid
        conn.execute("COMMIT")
    finally:
        conn.close()

    t0            = time.monotonic()
    qt            = cfg.queue_timeout
    stall_t       = cfg.queue_stall_timeout
    deadline      = t0 + qt if qt is not None else None
    no_history_logged = False

    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                _delete_row(row_id)
                return None, time.monotonic() - t0, "aborted", None

            if deadline is not None and time.monotonic() >= deadline:
                snapshot = _read_queue_state()
                _delete_row(row_id)
                return None, time.monotonic() - t0, "queue_timeout", snapshot

            if stall_t is not None:
                last_rel = _read_last_release(model)
                if last_rel is None:
                    if not no_history_logged:
                        _log.warning(
                            "queue stall check skipped for caller=%r model=%r: "
                            "no completion history in queue_meta "
                            "(fresh install or DB reset) — "
                            "queue_timeout hard ceiling still applies",
                            caller, model,
                        )
                        no_history_logged = True
                elif (time.time() - last_rel) > stall_t:
                    snapshot = _read_queue_state()
                    _delete_row(row_id)
                    return None, time.monotonic() - t0, "queue_stalled", snapshot

            if _try_promote(row_id, caller, model,
                            cfg.priority, cfg.caller_max, global_max):
                return row_id, time.monotonic() - t0, "ok", None

            time.sleep(_POLL_S)
    except Exception:
        try:
            _delete_row(row_id)
        except Exception:
            pass
        raise


def _read_last_release(model: str = "") -> float | None:
    """Return the timestamp of the most recent inference completion.

    Checks the model-specific key first so that completions for one
    model don't mask a stall in another.  Falls back to the global key
    when no model-specific record exists yet.
    """
    conn = _open()
    try:
        if model:
            row = conn.execute(
                "SELECT value FROM queue_meta WHERE key=?",
                [f"last_release_at:{model}"],
            ).fetchone()
            if row:
                return float(row[0])
        row = conn.execute(
            "SELECT value FROM queue_meta WHERE key='last_release_at'"
        ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def release(queue_id: int, model: str = "") -> None:
    """Remove the running row and record completion time.

    Writes last_release_at globally and per-model so stall detection
    for each model stays independent.
    """
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM queue WHERE id=?", [queue_id])
        now = str(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO queue_meta (key, value) "
            "VALUES ('last_release_at', ?)",
            [now],
        )
        if model:
            conn.execute(
                "INSERT OR REPLACE INTO queue_meta (key, value) "
                "VALUES (?, ?)",
                [f"last_release_at:{model}", now],
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        conn.close()


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
    No-ops (returns "proceed") when circuit_n <= 0 or no circuit key exists.
    """
    caller = cfg.log_caller
    circuit_key = _circuit_key(cfg)
    if not circuit_key or cfg.circuit_n <= 0:
        return "proceed"

    now      = time.time()
    cooldown = cfg.circuit_cooldown_s

    conn = None
    try:
        conn = _open()
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT consecutive_n, tripped_at, probe_pid "
            "FROM circuit_state WHERE circuit_key=?",
            [circuit_key],
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO circuit_state "
                "(circuit_key, caller, consecutive_n) VALUES (?, ?, 0)",
                [circuit_key, caller or ""],
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
                "UPDATE circuit_state SET probe_pid=NULL WHERE circuit_key=?",
                [circuit_key],
            )
            probe_pid = None

        # Half-open or still cooling down?
        if (now - tripped_at) >= cooldown:
            conn.execute(
                "UPDATE circuit_state SET probe_pid=? WHERE circuit_key=?",
                [os.getpid(), circuit_key],
            )
            conn.execute("COMMIT")
            return "probe"

        conn.execute("COMMIT")
        return "open"

    except Exception:
        try:
            if conn is not None:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        return "proceed"
    finally:
        if conn is not None:
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
    circuit_key = _circuit_key(cfg)
    if not circuit_key or cfg.circuit_n <= 0:
        return

    triggers = set(cfg.circuit_triggers)
    now      = time.time()

    conn = None
    try:
        conn = _open()
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT consecutive_n, tripped_at, probe_pid "
            "FROM circuit_state WHERE circuit_key=?",
            [circuit_key],
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO circuit_state "
                "(circuit_key, caller, consecutive_n) VALUES (?, ?, 0)",
                [circuit_key, caller or ""],
            )
            row = (0, None, None)

        consecutive_n, tripped_at, _probe_pid = row

        if outcome == "success":
            conn.execute(
                "UPDATE circuit_state "
                "SET consecutive_n=0, last_failure_at=NULL, "
                "    tripped_at=NULL, probe_pid=NULL "
                "WHERE circuit_key=?",
                [circuit_key],
            )
        elif outcome in triggers:
            new_n = consecutive_n + 1
            if is_probe:
                # Probe failed — re-trip and reset the cooldown clock.
                conn.execute(
                    "UPDATE circuit_state "
                    "SET consecutive_n=?, last_failure_at=?, "
                    "    tripped_at=?, probe_pid=NULL "
                    "WHERE circuit_key=?",
                    [new_n, now, now, circuit_key],
                )
            else:
                should_trip = (new_n >= cfg.circuit_n)
                if should_trip:
                    conn.execute(
                        "UPDATE circuit_state "
                        "SET consecutive_n=?, last_failure_at=?, tripped_at=? "
                        "WHERE circuit_key=?",
                        [new_n, now, now, circuit_key],
                    )
                else:
                    conn.execute(
                        "UPDATE circuit_state "
                        "SET consecutive_n=?, last_failure_at=? "
                        "WHERE circuit_key=?",
                        [new_n, now, circuit_key],
                    )
        else:
            # Non-triggering outcome — still clear probe_pid if we were probing.
            if is_probe:
                conn.execute(
                    "UPDATE circuit_state SET probe_pid=NULL WHERE circuit_key=?",
                    [circuit_key],
                )

        conn.execute("COMMIT")
    except Exception:
        try:
            if conn is not None:
                conn.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        if conn is not None:
            conn.close()


def _circuit_key(cfg) -> str:
    """Return caller-compatible circuit scope unless explicitly overridden."""
    return (getattr(cfg, "circuit_key", "") or cfg.log_caller or "").strip()
