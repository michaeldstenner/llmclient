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


def _strip_quotes(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def _parse_simple_yaml(text: str) -> dict:
    """Parse the indented `key: value` YAML subset used by config.yaml.

    Nesting is arbitrary-depth (the `models:` section needs three
    levels); scalar values are always returned as strings.  A key with
    an empty value opens a nested mapping.
    """
    root: dict = {}
    # (indent of the key that opened the container, container)
    stack: list[tuple[int, dict]] = [(-1, root)]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        indent = len(line) - len(line.lstrip())
        while indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if val:
            container[key] = _strip_quotes(val)
        else:
            child: dict = {}
            container[key] = child
            stack.append((indent, child))
    return root


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
                    if not isinstance(values, dict):
                        merged[section] = values
                        continue
                    if not isinstance(merged.get(section), dict):
                        merged[section] = {}
                    merged[section].update(values)
            except Exception:
                pass
    _cached_config = merged
    return merged


def resolve_url(provider: str, explicit: str) -> str:
    if explicit:
        return explicit.rstrip("/")
    section = _load_config().get(provider, {})
    from_file = section.get("url", "") if isinstance(section, dict) else ""
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


def resolve_api_key_with_source(
    provider: str, explicit: str = "", key_name: str = ""
) -> tuple[str, str]:
    """Resolve a key and report *where* it came from.

    The source string is safe to display (it never contains the key
    itself); ("", "") means no key was found anywhere.
    """
    if explicit:
        return explicit, "explicit"
    env_var = _ENV_API_KEYS.get(provider, "")
    if env_var:
        from_env = os.environ.get(env_var, "")
        if from_env:
            return from_env, f"env:{env_var}"

    service = f"llmclient:{provider}"
    account = _resolve_key_name(key_name)
    from_keychain = _keychain_get(service, account)
    if from_keychain:
        return from_keychain, f"keychain:{account}"
    if account != "default":
        from_keychain_default = _keychain_get(service, "default")
        if from_keychain_default:
            return from_keychain_default, "keychain:default"

    section = _load_config().get(provider, {})
    from_file = section.get("api_key", "") if isinstance(section, dict) else ""
    if from_file:
        return from_file, f"config:{provider}.api_key"
    return "", ""


def resolve_api_key(provider: str, explicit: str, key_name: str = "") -> str:
    return resolve_api_key_with_source(provider, explicit, key_name)[0]


def describe_api_key(provider: str, key_name: str = "") -> str:
    """Source string for `provider`'s key, or "" if none resolves.

    Never returns the key — intended for status output.
    """
    return resolve_api_key_with_source(provider, "", key_name)[1]


def get_parallel_slots() -> int:
    try:
        return int(_load_config().get("ollama", {}).get("parallel_slots", 4))
    except (ValueError, TypeError):
        return 4
