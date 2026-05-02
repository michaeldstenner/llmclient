"""
Keys and endpoint resolution.

Resolution order (first non-empty wins):
  1. Explicit value in LLMConfig
  2. Standard env var (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  3. ~/.config/llmclient/keys.yaml
"""
import os
from functools import lru_cache
from pathlib import Path

_KEYS_PATH = Path.home() / ".config" / "llmclient" / "keys.yaml"

_DEFAULT_URLS = {
    "anthropic":        "https://api.anthropic.com",
    "openai":           "https://api.openai.com",
    "openai_compatible": "",
    "ollama":           "http://localhost:11434",
}

_ENV_API_KEYS = {
    "anthropic":        "ANTHROPIC_API_KEY",
    "openai":           "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
}


def _parse_simple_yaml(text: str) -> dict:
    """Parse the two-level key: value YAML subset used by keys.yaml."""
    result: dict = {}
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            if stripped.endswith(":"):
                section = stripped[:-1]
                result[section] = {}
        elif section and ":" in stripped:
            key, _, val = stripped.partition(":")
            result[section][key.strip()] = val.strip()
    return result


@lru_cache(maxsize=1)
def _load_keys() -> dict:
    if not _KEYS_PATH.exists():
        return {}
    try:
        return _parse_simple_yaml(_KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_url(provider: str, explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    keys = _load_keys()
    from_file = keys.get(provider, {}).get("url", "")
    if from_file:
        return from_file.rstrip("/")
    return _DEFAULT_URLS.get(provider, "").rstrip("/")


def resolve_api_key(provider: str, explicit: str) -> str:
    if explicit:
        return explicit
    env_var = _ENV_API_KEYS.get(provider, "")
    if env_var:
        from_env = os.environ.get(env_var, "")
        if from_env:
            return from_env
    return _load_keys().get(provider, {}).get("api_key", "")


def get_parallel_slots() -> int:
    keys = _load_keys()
    try:
        return int(keys.get("ollama", {}).get("parallel_slots", 4))
    except (ValueError, TypeError):
        return 4
