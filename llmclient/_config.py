"""
Module-level configuration for llmclient.

Call configure() once at application startup to redirect llmclient
to an app-specific config directory, data directory, and log level.
"""
from pathlib import Path

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "llmclient"
_DEFAULT_DATA_DIR   = Path.home() / ".local" / "share" / "llmclient"

_config_dir: Path | None = None
_data_dir:   Path | None = None
_log_level:  str         = "errors"   # "off" | "errors" | "all"

_cache_clearers: list = []


def configure(
    config_dir: str | Path | None = None,
    data_dir:   str | Path | None = None,
    log_level:  str               = "errors",
) -> None:
    """Configure llmclient paths and logging (call once at app startup).

    config_dir
        Directory containing config.yaml for this app.  Its values
        overlay ~/.config/llmclient/config.yaml so shared defaults
        stay in the global file and only app-specific overrides go
        here.  Pass None to use the global file only.

    data_dir
        Directory for runtime data: queue.db and llmclient_log.jsonl.
        Apps sharing a data_dir share a queue and a log.  Apps that
        want independent slot budgets should point at separate dirs.
        Defaults to ~/.local/share/llmclient/.

    log_level
        "off"    — no logging
        "errors" — log non-success outcomes only (default)
        "all"    — log every call, including queue snapshot on success
    """
    global _config_dir, _data_dir, _log_level
    _config_dir = Path(config_dir).expanduser() if config_dir is not None else None
    _data_dir   = Path(data_dir).expanduser()   if data_dir   is not None else None
    _log_level  = log_level
    for fn in _cache_clearers:
        fn()


def get_data_dir() -> Path:
    return _data_dir if _data_dir is not None else _DEFAULT_DATA_DIR


def get_db_path() -> Path:
    return get_data_dir() / "queue.db"


def get_log_path() -> Path:
    return get_data_dir() / "llmclient_log.jsonl"


def get_log_level() -> str:
    return _log_level


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
