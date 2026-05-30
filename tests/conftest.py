"""Shared fixtures and HTTP mock helpers."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HTTP mock helpers
# ---------------------------------------------------------------------------

def make_mock_response(body: dict | bytes | str) -> MagicMock:
    if isinstance(body, dict):
        raw = json.dumps(body).encode()
    elif isinstance(body, str):
        raw = body.encode()
    else:
        raw = body
    mock = MagicMock()
    mock.read.return_value = raw
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


@contextmanager
def mock_urlopen(body: dict | bytes | str):
    """Patch urllib.request.urlopen to return a fixed body."""
    with patch("urllib.request.urlopen", return_value=make_mock_response(body)):
        yield


@contextmanager
def mock_urlopen_timeout():
    import socket
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        yield


@contextmanager
def mock_urlopen_http_error(code: int):
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            url="http://x", code=code, msg="err", hdrs=None, fp=None
        ),
    ):
        yield


@contextmanager
def mock_urlopen_url_error():
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        yield


# ---------------------------------------------------------------------------
# Queue DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    """Redirect the queue DB to a temp file and speed up polling."""
    db_path = tmp_path / "queue.db"
    import llmclient._queue as q_mod
    monkeypatch.setattr(q_mod, "_DB_PATH", db_path)
    monkeypatch.setattr(q_mod, "_POLL_S", 0.02)
    return db_path


def open_queue_db(db_path: Path) -> sqlite3.Connection:
    import llmclient._queue as q_mod
    old = q_mod._DB_PATH
    q_mod._DB_PATH = db_path
    conn = q_mod._open()
    q_mod._DB_PATH = old
    return conn


def insert_queue_row(
    db_path: Path,
    *,
    pid: int,
    caller: str = "test",
    model: str = "",
    priority: int = 50,
    caller_max: int = 4,
    global_max: int = 4,
    status: str = "waiting",
) -> int:
    import llmclient._queue as q_mod
    conn = q_mod._open.__wrapped__(db_path) if hasattr(q_mod._open, "__wrapped__") else _raw_open(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO queue "
            "(pid, caller, model, priority, caller_max, global_max, status, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [pid, caller, model, priority, caller_max, global_max, status, time.time()],
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _raw_open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level="", timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
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
    """)
    return conn


# ---------------------------------------------------------------------------
# Minimal LLMConfig stand-in (avoids touching keys.yaml)
# ---------------------------------------------------------------------------

def make_cfg(**kwargs):
    """Return an LLMConfig with sensible test defaults."""
    from llmclient import LLMConfig
    defaults = dict(
        provider="ollama",
        model="test-model",
        url="http://localhost:11434",
        timeout=5,
        api_key="test-key",
        log_caller="",
        queue_mode="off",
        priority=50,
        caller_max=4,
    )
    defaults.update(kwargs)
    return LLMConfig(**defaults)
