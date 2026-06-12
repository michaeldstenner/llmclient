"""
Module-level configuration for llmclient.

Call configure() once at application startup to identify the app and,
optionally, redirect storage.  See docs/storage-and-config-model.md.

Three storage layers, separated by ownership:

  - data   (per-app logs/history)   ~/.local/share/<app>/    keyed on `app`
  - state  (shared slot queue)      ~/.local/state/llmclient/  shared
  - config (machine settings)       ~/.config/llmclient/       shared

The slot queue lives in the shared `state` home and is deliberately
*decoupled* from the per-app data home: separate data dirs can no longer
fork the queue (which would defeat cross-app slot coordination against one
Ollama box).
"""
from pathlib import Path

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "llmclient"
_DEFAULT_DATA_ROOT  = Path.home() / ".local" / "share"
_DEFAULT_STATE_DIR  = Path.home() / ".local" / "state" / "llmclient"
_LEGACY_DATA_DIR    = _DEFAULT_DATA_ROOT / "llmclient"

_app:        str | None  = None
_config_dir: Path | None = None
_data_dir:   Path | None = None   # legacy explicit data home (logs/history)
_log_file:   Path | None = None
_queue_file: Path | None = None
_log_level:  str         = "errors"   # "off" | "errors" | "all"

_cache_clearers: list = []


def configure(
    app:        str | None        = None,
    *,
    config_dir: str | Path | None = None,
    data_dir:   str | Path | None = None,
    log_file:   str | Path | None = None,
    queue_file: str | Path | None = None,
    log_level:  str               = "errors",
) -> None:
    """Identify this app and (optionally) redirect storage.  Call once.

    app
        Application name (e.g. "bouncer", "pithos").  Sets the per-app
        data home ~/.local/share/<app>/ for logs/history.  Does NOT move
        the shared slot queue or the machine config — those are shared by
        design.  Recommended for every app.

    config_dir
        Extra directory whose config.yaml overlays the shared
        ~/.config/llmclient/config.yaml.  Rarely needed.

    data_dir
        Legacy: explicit data home for logs/history.  Superseded by
        `app`, kept for back-compat.  NOTE: unlike before, this no longer
        moves the slot queue — the queue is always shared (see
        `queue_file`) so apps cannot accidentally fork it.

    log_file
        Explicit path for this app's call log.  Overrides the default
        <data home>/llmclient_log.jsonl.

    queue_file
        Explicit slot-queue DB path.  Defaults to the SHARED
        ~/.local/state/llmclient/queue.db.  Override ONLY to deliberately
        isolate (e.g. tests) — doing so opts out of cross-app slot
        coordination.

    log_level
        "off" — none | "errors" — non-success only (default) | "all".
    """
    global _app, _config_dir, _data_dir, _log_file, _queue_file, _log_level
    _app        = app
    _config_dir = Path(config_dir).expanduser() if config_dir is not None else None
    _data_dir   = Path(data_dir).expanduser()   if data_dir   is not None else None
    _log_file   = Path(log_file).expanduser()   if log_file   is not None else None
    _queue_file = Path(queue_file).expanduser() if queue_file is not None else None
    _log_level  = log_level
    for fn in _cache_clearers:
        fn()


def get_data_dir() -> Path:
    """Per-app data home for logs/history (NOT the shared slot queue)."""
    if _data_dir is not None:
        return _data_dir
    if _app:
        return _DEFAULT_DATA_ROOT / _app
    return _LEGACY_DATA_DIR


def get_state_dir() -> Path:
    """Shared state home holding the slot queue."""
    if _queue_file is not None:
        return _queue_file.parent
    return _DEFAULT_STATE_DIR


def get_db_path() -> Path:
    """Slot-queue DB path.

    Shared across apps by design and decoupled from the data home, so
    separate data dirs cannot fork the queue.  Override via
    configure(queue_file=...) only to deliberately isolate.
    """
    if _queue_file is not None:
        return _queue_file
    return _DEFAULT_STATE_DIR / "queue.db"


def get_log_path() -> Path:
    if _log_file is not None:
        return _log_file
    return get_data_dir() / "llmclient_log.jsonl"


def get_log_level() -> str:
    return _log_level


def get_app() -> str | None:
    return _app


def get_config_files() -> list[Path]:
    """Return config file paths in descending priority order."""
    paths: list[Path] = []
    if _config_dir is not None:
        paths.append(_config_dir / "config.yaml")
    paths.append(_DEFAULT_CONFIG_DIR / "config.yaml")
    paths.append(_DEFAULT_CONFIG_DIR / "keys.yaml")  # legacy
    return paths


def _register_cache_clearer(fn) -> None:
    """Register a callback to be invoked when configure() is called."""
    _cache_clearers.append(fn)
