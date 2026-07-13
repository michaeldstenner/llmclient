"""Tests for _keys.py — YAML parsing and resolution chain."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import llmclient._config as config_mod
import llmclient._keys as keys_mod
from llmclient._keys import _parse_simple_yaml, resolve_url, resolve_api_key, get_parallel_slots


def _clear_cache():
    keys_mod._cached_config = None


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    """Isolate each test: default config state and fresh cache."""
    monkeypatch.setattr(config_mod, "_config_dir", None)
    monkeypatch.setattr(config_mod, "_data_dir", None)
    monkeypatch.setattr(config_mod, "_app", None)
    monkeypatch.setattr(config_mod, "_log_level", "errors")
    # Default: no real Keychain access in tests; individual tests override.
    monkeypatch.setattr(keys_mod, "_keychain_get", lambda service, account: "")
    _clear_cache()
    yield
    _clear_cache()


# ---------------------------------------------------------------------------
# _parse_simple_yaml
# ---------------------------------------------------------------------------

def test_parse_simple_yaml_basic():
    text = """
anthropic:
  api_key: sk-ant-123
  url: https://api.anthropic.com

ollama:
  url: http://localhost:11434
  parallel_slots: 4
"""
    result = _parse_simple_yaml(text)
    assert result["anthropic"]["api_key"] == "sk-ant-123"
    assert result["anthropic"]["url"] == "https://api.anthropic.com"
    assert result["ollama"]["url"] == "http://localhost:11434"
    assert result["ollama"]["parallel_slots"] == "4"


def test_parse_simple_yaml_ignores_comments_and_blanks():
    text = """
# This is a comment
anthropic:
  # another comment
  api_key: sk-ant-456

"""
    result = _parse_simple_yaml(text)
    assert result["anthropic"]["api_key"] == "sk-ant-456"
    assert "# another comment" not in str(result)


def test_parse_simple_yaml_empty():
    assert _parse_simple_yaml("") == {}
    assert _parse_simple_yaml("# just a comment\n") == {}


def test_parse_simple_yaml_value_with_colon():
    text = "anthropic:\n  url: https://api.anthropic.com/v1\n"
    result = _parse_simple_yaml(text)
    assert result["anthropic"]["url"] == "https://api.anthropic.com/v1"


# ---------------------------------------------------------------------------
# resolve_url
# ---------------------------------------------------------------------------

def test_resolve_url_explicit_wins():
    url = resolve_url("ollama", "http://myhost:11434")
    assert url == "http://myhost:11434"


def test_resolve_url_trailing_slash_stripped():
    url = resolve_url("ollama", "http://myhost:11434/")
    assert url == "http://myhost:11434"


def test_resolve_url_from_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("ollama:\n  url: http://custom:11434\n")
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    _clear_cache()
    assert resolve_url("ollama", "") == "http://custom:11434"


def test_resolve_url_from_legacy_keys_yaml(tmp_path, monkeypatch):
    (tmp_path / "keys.yaml").write_text("ollama:\n  url: http://legacy:11434\n")
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", tmp_path)
    _clear_cache()
    assert resolve_url("ollama", "") == "http://legacy:11434"


def test_resolve_url_app_config_beats_global(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    app_dir    = tmp_path / "app"
    global_dir.mkdir()
    app_dir.mkdir()
    (global_dir / "config.yaml").write_text("ollama:\n  url: http://global:11434\n")
    (app_dir / "config.yaml").write_text("ollama:\n  url: http://app:11434\n")
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", global_dir)
    monkeypatch.setattr(config_mod, "_config_dir", app_dir)
    _clear_cache()
    assert resolve_url("ollama", "") == "http://app:11434"


def test_resolve_url_default_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", tmp_path)
    _clear_cache()
    assert resolve_url("ollama", "") == "http://localhost:11434"
    assert resolve_url("anthropic", "") == "https://api.anthropic.com"
    assert resolve_url("openai", "") == "https://api.openai.com"
    assert resolve_url("openrouter", "") == "https://openrouter.ai/api"


# ---------------------------------------------------------------------------
# resolve_api_key
# ---------------------------------------------------------------------------

def test_resolve_api_key_explicit_wins():
    assert resolve_api_key("anthropic", "sk-explicit") == "sk-explicit"


def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert resolve_api_key("anthropic", "") == "sk-from-env"


def test_resolve_api_key_from_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("anthropic:\n  api_key: sk-from-file\n")
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _clear_cache()
    assert resolve_api_key("anthropic", "") == "sk-from-file"


def test_resolve_api_key_env_beats_file(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("anthropic:\n  api_key: sk-from-file\n")
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    _clear_cache()
    assert resolve_api_key("anthropic", "") == "sk-from-env"


def test_resolve_api_key_empty_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(keys_mod, "_keychain_get", lambda service, account: "")
    _clear_cache()
    assert resolve_api_key("anthropic", "") == ""


# ---------------------------------------------------------------------------
# resolve_api_key — Keychain step
# ---------------------------------------------------------------------------

def _fake_keychain(mapping):
    def _get(service, account):
        return mapping.get((service, account), "")
    return _get


def test_resolve_api_key_from_keychain_default_account(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({("llmclient:openrouter", "default"): "sk-or-default"}),
    )
    assert resolve_api_key("openrouter", "") == "sk-or-default"


def test_resolve_api_key_keychain_beats_config_file(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "openrouter:\n  api_key: sk-from-file\n"
    )
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({("llmclient:openrouter", "default"): "sk-or-keychain"}),
    )
    _clear_cache()
    assert resolve_api_key("openrouter", "") == "sk-or-keychain"


def test_resolve_api_key_env_beats_keychain(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({("llmclient:openrouter", "default"): "sk-or-keychain"}),
    )
    assert resolve_api_key("openrouter", "") == "sk-or-env"


def test_resolve_api_key_named_key_wins_over_default(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({
            ("llmclient:openrouter", "default"):  "sk-or-default",
            ("llmclient:openrouter", "bouncer"):   "sk-or-bouncer",
        }),
    )
    assert resolve_api_key("openrouter", "", key_name="bouncer") == "sk-or-bouncer"


def test_resolve_api_key_named_key_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({("llmclient:openrouter", "default"): "sk-or-default"}),
    )
    assert resolve_api_key("openrouter", "", key_name="bouncer") == "sk-or-default"


def test_resolve_api_key_configure_app_used_as_key_name(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        keys_mod, "_keychain_get",
        _fake_keychain({("llmclient:openrouter", "bouncer"): "sk-or-bouncer"}),
    )
    monkeypatch.setattr(config_mod, "_app", "bouncer")
    assert resolve_api_key("openrouter", "") == "sk-or-bouncer"


def test_keychain_get_returns_empty_on_missing_security_binary(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no security binary")
    monkeypatch.setattr(keys_mod.subprocess, "run", _raise)
    assert keys_mod._keychain_get("llmclient:openrouter", "default") == ""


def test_keychain_get_returns_empty_on_nonzero_exit(monkeypatch):
    class _Result:
        returncode = 44
        stdout = ""
    monkeypatch.setattr(keys_mod.subprocess, "run", lambda *a, **k: _Result())
    assert keys_mod._keychain_get("llmclient:openrouter", "default") == ""


# ---------------------------------------------------------------------------
# get_parallel_slots
# ---------------------------------------------------------------------------

def test_get_parallel_slots_from_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("ollama:\n  parallel_slots: 8\n")
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    _clear_cache()
    assert get_parallel_slots() == 8


def test_get_parallel_slots_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", tmp_path)
    _clear_cache()
    assert get_parallel_slots() == 4


# ---------------------------------------------------------------------------
# layering: global fills in keys absent from app config
# ---------------------------------------------------------------------------

def test_layering_app_partial_global_fills_rest(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    app_dir    = tmp_path / "app"
    global_dir.mkdir()
    app_dir.mkdir()
    (global_dir / "config.yaml").write_text(
        "anthropic:\n  api_key: sk-global\n  url: https://global.api\n"
    )
    (app_dir / "config.yaml").write_text(
        "anthropic:\n  url: https://app.api\n"
    )
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_DIR", global_dir)
    monkeypatch.setattr(config_mod, "_config_dir", app_dir)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _clear_cache()
    # app overrides url
    assert resolve_url("anthropic", "") == "https://app.api"
    # global fills in api_key absent from app config
    assert resolve_api_key("anthropic", "") == "sk-global"
