"""Tests for _discovery.py — named models and provider status."""
import pytest

import llmclient._config as config_mod
import llmclient._keys as keys_mod
from llmclient import LLMClient
from llmclient._discovery import (
    catalog,
    configured_models,
    known_providers,
    provider_status,
    resolve_named,
)

CONFIG = """
ollama:
  url: http://box:11434
openrouter:
  api_key: sk-or-test
aip:
  url: https://aip.example.com/v1
  api_key: sk-aip-test

models:
  fast:
    provider: openrouter
    model: anthropic/claude-haiku-4.5
    timeout: 30
  local:
    provider: ollama
    model: qwen3:32b
    num_ctx_auto: false
    priority: 90
  broken:
    provider: ollama
"""


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONFIG)
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    monkeypatch.setattr(config_mod, "_app", None)
    # Never touch the real Keychain or environment in tests.
    monkeypatch.setattr(keys_mod, "_keychain_get", lambda service, account: "")
    for var in keys_mod._ENV_API_KEYS.values():
        monkeypatch.delenv(var, raising=False)
    keys_mod._clear_cache()
    yield
    keys_mod._clear_cache()


# ---------------------------------------------------------------------------
# named models
# ---------------------------------------------------------------------------

def test_configured_models_parses_nested_stanzas():
    models = configured_models()
    assert models["fast"]["provider"] == "openrouter"
    assert models["fast"]["model"] == "anthropic/claude-haiku-4.5"
    assert models["local"]["model"] == "qwen3:32b"     # colon in value survives


def test_configured_models_coerces_types():
    models = configured_models()
    assert models["fast"]["timeout"] == 30             # int, not "30"
    assert models["local"]["num_ctx_auto"] is False    # bool, not "false"
    assert models["local"]["priority"] == 90


def test_configured_models_skips_stanza_without_model():
    assert "broken" not in configured_models()


def test_resolve_named_unknown_lists_known_names():
    with pytest.raises(KeyError) as exc:
        resolve_named("nope")
    assert "fast" in str(exc.value) and "local" in str(exc.value)


def test_from_name_builds_client():
    client = LLMClient.from_name("fast")
    assert client.cfg.provider == "openrouter"
    assert client.cfg.model == "anthropic/claude-haiku-4.5"
    assert client.cfg.timeout == 30
    assert client.cfg.queue_mode == "off"          # non-ollama


def test_from_name_ollama_gets_cooperative_queue():
    assert LLMClient.from_name("local").cfg.queue_mode == "cooperative"


def test_from_name_overrides_win():
    client = LLMClient.from_name("fast", timeout=5, log_caller="probe")
    assert client.cfg.timeout == 5
    assert client.cfg.log_caller == "probe"


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

def test_known_providers_includes_config_only_stanza():
    names = known_providers()
    assert "openrouter" in names and "ollama" in names
    assert "aip" in names          # not built in; discovered from config.yaml
    assert "models" not in names   # the named-model section is not a provider


def test_provider_status_reports_source_not_key():
    status = provider_status("openrouter")
    assert status["has_key"] is True
    assert status["key_source"] == "config:openrouter.api_key"
    assert status["usable"] is True
    assert "sk-or-test" not in repr(status)


def test_provider_status_ollama_needs_no_key():
    status = provider_status("ollama")
    assert status["needs_key"] is False
    assert status["usable"] is True
    assert status["url"] == "http://box:11434"


def test_provider_status_missing_key_is_not_usable():
    status = provider_status("openai")
    assert status["has_key"] is False
    assert status["usable"] is False


def test_provider_status_env_var_beats_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    source = provider_status("openrouter")["key_source"]
    assert source == "env:OPENROUTER_API_KEY"


def test_catalog_offline_never_probes(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("network call in live=False catalog")
    monkeypatch.setattr("llmclient._discovery._get_json", _boom)
    data = catalog(live=False)
    assert set(data["named"]) == {"fast", "local"}
    assert all(p["models"] == [] for p in data["providers"])
    assert "sk-or-test" not in repr(data)
