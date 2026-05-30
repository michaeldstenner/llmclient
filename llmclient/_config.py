"""
Module-level configuration for llmclient.

Call configure() once at application startup to redirect llmclient
to an app-specific config directory and/or queue database.  Both
default to the llmclient-owned paths if not set.
"""
from pathlib import Path

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "llmclient"
_DEFAULT_QUEUE_DB   = Path.home() / ".local" / "share" / "llmclient" / "queue.db"

_config_dir: Path | None = None
_queue_db:   Path | None = None

_cache_clearers: list = []


def configure(
    config_dir: str | Path | None = None,
    queue_db: str | Path | None = None,
) -> None:
    """Configure llmclient paths (call once at application startup).

    config_dir
        Directory containing config.yaml for this application.  Its
        values overlay ~/.config/llmclient/config.yaml so you can keep
        shared defaults in the global file and only put app-specific
        overrides here.  Pass None to use the global file only.

    queue_db
        Path to the SQLite cooperative-queue database.  Different apps
        can each get an independent queue by pointing here at separate
        files.  Defaults to ~/.local/share/llmclient/queue.db.
    """
    global _config_dir, _queue_db
    _config_dir = Path(config_dir).expanduser() if config_dir is not None else None
    _queue_db   = Path(queue_db).expanduser()   if queue_db   is not None else None
    for fn in _cache_clearers:
        fn()


def get_db_path() -> Path:
    return _queue_db if _queue_db is not None else _DEFAULT_QUEUE_DB


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
