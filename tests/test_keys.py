"""Tests for _keys.py — YAML parsing and resolution chain."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import llmclient._keys as keys_mod
from llmclient._keys import _parse_simple_yaml, resolve_url, resolve_api_key, get_parallel_slots


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

def _clear_keys_cache():
    keys_mod._load_keys.cache_clear()


def test_resolve_url_explicit_wins(tmp_path):
    _clear_keys_cache()
    url = resolve_url("ollama", "http://myhost:11434")
    assert url == "http://myhost:11434"


def test_resolve_url_trailing_slash_stripped(tmp_path):
    _clear_keys_cache()
    url = resolve_url("ollama", "http://myhost:11434/")
    assert url == "http://myhost:11434"


def test_resolve_url_from_keys_yaml(tmp_path, monkeypatch):
    _clear_keys_cache()
    keys_file = tmp_path / "keys.yaml"
    keys_file.write_text("ollama:\n  url: http://custom:11434\n")
    monkeypatch.setattr(keys_mod, "_KEYS_PATH", keys_file)
    _clear_keys_cache()
    assert resolve_url("ollama", "") == "http://custom:11434"
    _clear_keys_cache()


def test_resolve_url_default_fallback():
    _clear_keys_cache()
    with patch.object(keys_mod, "_KEYS_PATH", Path("/nonexistent/keys.yaml")):
        _clear_keys_cache()
        assert resolve_url("ollama", "") == "http://localhost:11434"
        assert resolve_url("anthropic", "") == "https://api.anthropic.com"
        assert resolve_url("openai", "") == "https://api.openai.com"
        _clear_keys_cache()


# ---------------------------------------------------------------------------
# resolve_api_key
# ---------------------------------------------------------------------------

def test_resolve_api_key_explicit_wins():
    assert resolve_api_key("anthropic", "sk-explicit") == "sk-explicit"


def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert resolve_api_key("anthropic", "") == "sk-from-env"


def test_resolve_api_key_from_keys_yaml(tmp_path, monkeypatch):
    _clear_keys_cache()
    keys_file = tmp_path / "keys.yaml"
    keys_file.write_text("anthropic:\n  api_key: sk-from-file\n")
    monkeypatch.setattr(keys_mod, "_KEYS_PATH", keys_file)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _clear_keys_cache()
    assert resolve_api_key("anthropic", "") == "sk-from-file"
    _clear_keys_cache()


def test_resolve_api_key_env_beats_file(tmp_path, monkeypatch):
    _clear_keys_cache()
    keys_file = tmp_path / "keys.yaml"
    keys_file.write_text("anthropic:\n  api_key: sk-from-file\n")
    monkeypatch.setattr(keys_mod, "_KEYS_PATH", keys_file)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    _clear_keys_cache()
    assert resolve_api_key("anthropic", "") == "sk-from-env"
    _clear_keys_cache()


def test_resolve_api_key_empty_when_nothing_set(monkeypatch):
    _clear_keys_cache()
    with patch.object(keys_mod, "_KEYS_PATH", Path("/nonexistent/keys.yaml")):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _clear_keys_cache()
        assert resolve_api_key("anthropic", "") == ""
        _clear_keys_cache()


# ---------------------------------------------------------------------------
# get_parallel_slots
# ---------------------------------------------------------------------------

def test_get_parallel_slots_from_keys_yaml(tmp_path, monkeypatch):
    _clear_keys_cache()
    keys_file = tmp_path / "keys.yaml"
    keys_file.write_text("ollama:\n  parallel_slots: 8\n")
    monkeypatch.setattr(keys_mod, "_KEYS_PATH", keys_file)
    _clear_keys_cache()
    assert get_parallel_slots() == 8
    _clear_keys_cache()


def test_get_parallel_slots_default():
    _clear_keys_cache()
    with patch.object(keys_mod, "_KEYS_PATH", Path("/nonexistent/keys.yaml")):
        _clear_keys_cache()
        assert get_parallel_slots() == 4
        _clear_keys_cache()
