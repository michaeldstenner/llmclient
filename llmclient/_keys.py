"""
Config and endpoint resolution.

Resolution order for API keys (first non-empty wins):
  1. Explicit value in LLMConfig
  2. Standard env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)
  3. macOS Keychain, service "llmclient:<provider>" (see _keychain_get)
  4. {config_dir}/config.yaml  (if llmclient.configure(config_dir=...) was called)
  5. ~/.config/llmclient/config.yaml
  6. ~/.config/llmclient/keys.yaml  (legacy name, still supported)
"""
import os
import subprocess

_DEFAULT_URLS = {
    "anthropic":         "https://api.anthropic.com",
    "openai":            "https://api.openai.com",
    "openai_compatible": "",
    "ollama":            "http://localhost:11434",
    # NOTE: call_openai() appends "/v1/chat/completions" to this base (same
    # convention as "openai": "https://api.openai.com" with no /v1 here) —
    # so the OpenRouter *host* root goes here, not OpenRouter's own
    # published "base URL" (https://openrouter.ai/api/v1), or requests
    # would double up on /v1.
    "openrouter":        "https://openrouter.ai/api",
}

_ENV_API_KEYS = {
    "anthropic":         "ANTHROPIC_API_KEY",
    "openai":            "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
    "openrouter":        "OPENROUTER_API_KEY",
}

_KEYCHAIN_TIMEOUT_S = 5

_cached_config: dict | None = None


def _clear_cache() -> None:
    global _cached_config
    _cached_config = None


from ._config import _register_cache_clearer  # noqa: E402
_register_cache_clearer(_clear_cache)


def _parse_simple_yaml(text: str) -> dict:
    """Parse the two-level key: value YAML subset used by config.yaml."""
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


def _load_config() -> dict:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    from ._config import get_config_files
    merged: dict = {}
    # Lowest-priority first so higher-priority files win on update.
    for path in reversed(get_config_files()):
        if path.exists():
            try:
                data = _parse_simple_yaml(path.read_text(encoding="utf-8"))
                for section, values in data.items():
                    if section not in merged:
                        merged[section] = {}
                    merged[section].update(values)
            except Exception:
                pass
    _cached_config = merged
    return merged


def resolve_url(provider: str, explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    cfg = _load_config()
    from_file = cfg.get(provider, {}).get("url", "")
    if from_file:
        return from_file.rstrip("/")
    return _DEFAULT_URLS.get(provider, "").rstrip("/")


def _keychain_get(service: str, account: str) -> str:
    """Fetch a password from the macOS Keychain via the `security` CLI.

    Returns "" on any failure (not found, `security` missing, non-macOS,
    timeout, ACL prompt refused, ...) — never raises, never logs the
    account/service on failure and never logs the secret on success.
    """
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", service, "-a", account, "-w",
            ],
            capture_output=True, text=True, timeout=_KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_key_name(key_name: str) -> str:
    """Named-account resolution order: explicit arg -> configure(app=) -> 'default'."""
    if key_name:
        return key_name
    from ._config import get_app
    app = get_app()
    if app:
        return app
    return "default"


def resolve_api_key(provider: str, explicit: str, key_name: str = "") -> str:
    if explicit:
        return explicit
    env_var = _ENV_API_KEYS.get(provider, "")
    if env_var:
        from_env = os.environ.get(env_var, "")
        if from_env:
            return from_env

    service = f"llmclient:{provider}"
    account = _resolve_key_name(key_name)
    from_keychain = _keychain_get(service, account)
    if from_keychain:
        return from_keychain
    if account != "default":
        from_keychain_default = _keychain_get(service, "default")
        if from_keychain_default:
            return from_keychain_default

    return _load_config().get(provider, {}).get("api_key", "")


def get_parallel_slots() -> int:
    try:
        return int(_load_config().get("ollama", {}).get("parallel_slots", 4))
    except (ValueError, TypeError):
        return 4
