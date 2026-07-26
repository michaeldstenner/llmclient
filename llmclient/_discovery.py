"""
Discovery: what can this machine actually call?

Two layers, both usable from the library and from `llmc`:

  - **Named models** — the `models:` section of config.yaml, mapping a
    short local name ("fast", "big-local") to a provider + model id and
    optional LLMConfig knobs.  See `configured_models()` /
    `LLMClient.from_name()`.
  - **Providers** — which backends are configured at all: resolved URL,
    whether a key resolves and from where (never the key itself), and
    the live model catalog each one offers.

Nothing here returns or logs an API key.
"""
import json
import urllib.error
import urllib.request

from ._keys import (
    _DEFAULT_URLS,
    _ENV_API_KEYS,
    _load_config,
    describe_api_key,
    resolve_api_key,
    resolve_url,
)

# Providers that need no key (local) or that cannot be enumerated at all.
_NO_KEY_PROVIDERS  = ("ollama",)
_NOT_LISTABLE      = ("claude_code", "claude_p")

# Config sections that are settings, not provider stanzas.
_NON_PROVIDER_SECTIONS = ("models",)

# How to enumerate a provider's catalog.  Unknown providers (e.g. a
# local "aip:" stanza) are treated as OpenAI-compatible.
_LISTING_STYLE = {
    "ollama":    "ollama",
    "anthropic": "anthropic",
}

_DEFAULT_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# named models (config.yaml `models:` section)
# ---------------------------------------------------------------------------

def _coerce(value: str, default):
    """Coerce a config string to the type implied by the LLMConfig default."""
    if isinstance(default, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if default is None:
        # int|None / float|None knobs (timeouts, deadlines)
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value
    return value


def _llmconfig_defaults() -> dict:
    from dataclasses import MISSING, fields
    from . import LLMConfig
    out = {}
    for f in fields(LLMConfig):
        if f.default is not MISSING:
            out[f.name] = f.default
        else:
            out[f.name] = ""      # provider / model — required, str
    return out


def configured_models() -> dict[str, dict]:
    """Named models from config.yaml, as {name: {provider, model, ...}}.

    Values are coerced to the types `LLMConfig` expects, so a stanza can
    be handed straight to `LLMClient.from_dict()`.  Malformed stanzas are
    skipped rather than raising — this is a discovery call.
    """
    section = _load_config().get("models", {})
    if not isinstance(section, dict):
        return {}
    defaults = _llmconfig_defaults()
    out: dict[str, dict] = {}
    for name, stanza in section.items():
        if not isinstance(stanza, dict) or not stanza.get("model"):
            continue
        entry: dict = {}
        for key, value in stanza.items():
            if key in defaults:
                try:
                    entry[key] = _coerce(value, defaults[key])
                except (TypeError, ValueError):
                    entry[key] = value
            else:
                entry[key] = value
        out[name] = entry
    return out


def resolve_named(name: str) -> dict:
    """Look up one named model; raises KeyError with the known names."""
    models = configured_models()
    if name not in models:
        known = ", ".join(sorted(models)) or "(none configured)"
        raise KeyError(
            f"no model named {name!r} in config.yaml `models:` "
            f"— known: {known}"
        )
    return dict(models[name])


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

def known_providers() -> list[str]:
    """Built-in providers plus any extra provider stanza in config.yaml."""
    names = list(_DEFAULT_URLS)
    cfg = _load_config()
    for section, values in cfg.items():
        if section in names or section in _NON_PROVIDER_SECTIONS:
            continue
        if not isinstance(values, dict):
            continue
        if "url" in values or "api_key" in values:
            names.append(section)
    return names


def provider_status(provider: str) -> dict:
    """Resolved URL and key *source* for one provider — never the key."""
    needs_key = provider not in _NO_KEY_PROVIDERS
    source    = describe_api_key(provider) if needs_key else ""
    url       = resolve_url(provider, "")
    return {
        "provider":   provider,
        "url":        url,
        "needs_key":  needs_key,
        "has_key":    bool(source),
        "key_source": source,
        "env_var":    _ENV_API_KEYS.get(provider, ""),
        "listable":   provider not in _NOT_LISTABLE,
        # Usable = we could actually make a call: a URL, and a key if one
        # is needed.  This is the "is openrouter on the table?" bit.
        "usable":     bool(url) and (bool(source) or not needs_key),
    }


def all_provider_status() -> list[dict]:
    return [provider_status(p) for p in known_providers()]


# ---------------------------------------------------------------------------
# live catalogs
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _v1_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


def _ollama_loaded(base_url: str, timeout: float) -> set[str]:
    try:
        body = _get_json(base_url + "/api/ps", {}, timeout)
    except Exception:
        return set()
    return {m.get("model", "") for m in body.get("models", [])}


def list_provider_models(
    provider: str, *, timeout: float = _DEFAULT_TIMEOUT_S
) -> tuple[list[dict], str]:
    """Live catalog for one provider.

    Returns (models, error).  Each model is {"id", "loaded", "detail"};
    `error` is "" on success and an outcome-ish string otherwise
    ("error:unreachable", "http_401", "error:no_key", ...).
    """
    status = provider_status(provider)
    if not status["listable"]:
        return [], "error:not_listable"
    base_url = status["url"]
    if not base_url:
        return [], "error:no_url"

    style = _LISTING_STYLE.get(provider, "openai")
    key   = ""
    if status["needs_key"]:
        key = resolve_api_key(provider, "")
        if not key:
            return [], "error:no_key"

    try:
        if style == "ollama":
            body   = _get_json(base_url + "/api/tags", {}, timeout)
            loaded = _ollama_loaded(base_url, min(timeout, 3))
            models = [
                {
                    "id":     m.get("model", ""),
                    "loaded": m.get("model", "") in loaded,
                    "detail": _ollama_detail(m),
                }
                for m in body.get("models", [])
            ]
        elif style == "anthropic":
            body = _get_json(
                base_url + "/v1/models?limit=1000",   # default page is 20
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout,
            )
            models = [
                {"id": m.get("id", ""), "loaded": False,
                 "detail": m.get("display_name", "")}
                for m in body.get("data", [])
            ]
        else:
            body = _get_json(
                _v1_root(base_url) + "/models",
                {"Authorization": f"Bearer {key}"} if key else {},
                timeout,
            )
            models = [
                {"id": _model_id(m), "loaded": False,
                 "detail": _openai_detail(m)}
                for m in body.get("data", [])
            ]
    except urllib.error.HTTPError as exc:
        return [], f"http_{exc.code}"
    except urllib.error.URLError:
        return [], "error:unreachable"
    except TimeoutError:
        return [], "timeout"
    except Exception as exc:                    # malformed body, etc.
        return [], f"error:{type(exc).__name__}"

    models = [m for m in models if m["id"]]
    models.sort(key=lambda m: m["id"])
    return models, ""


def _model_id(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("id", ""))
    return ""


def _openai_detail(item) -> str:
    if not isinstance(item, dict):
        return ""
    # OpenRouter carries context length and pricing; plain OpenAI does not.
    ctx = item.get("context_length") or (item.get("top_provider") or {}).get(
        "context_length"
    )
    return f"{int(ctx) // 1000}k ctx" if ctx else ""


def _ollama_detail(item: dict) -> str:
    size = item.get("size")
    if not size:
        return ""
    return f"{int(size) / 1e9:.1f} GB"


def catalog(*, timeout: float = _DEFAULT_TIMEOUT_S, live: bool = True) -> dict:
    """One machine-readable answer to "what can I call?".

    {"named": {...}, "providers": [{..., "models": [...], "error": ""}]}
    With live=False the provider catalogs are skipped (no network).
    """
    providers = []
    for status in all_provider_status():
        entry = dict(status)
        entry["models"] = []
        entry["error"]  = ""
        if not live:
            pass
        elif not status["usable"]:
            entry["error"] = "unconfigured"
        elif status["listable"]:
            models, error = list_provider_models(
                status["provider"], timeout=timeout
            )
            entry["models"] = models
            entry["error"]  = error
        providers.append(entry)
    return {"named": configured_models(), "providers": providers}
