"""
Tests for LLMClient — integration of config, providers, queue, and log.

HTTP is mocked; the queue and log are redirected to temp paths.
"""
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llmclient import LLMClient, LLMConfig, LLMResult
from tests.conftest import make_cfg, mock_urlopen


OLLAMA_BODY = {
    "response": "42",
    "done": True,
    "load_duration": 0,
    "prompt_eval_duration": 100_000_000,
    "eval_duration": 200_000_000,
    "prompt_eval_count": 10,
    "eval_count": 3,
}

ANTHROPIC_BODY = {
    "content": [{"type": "text", "text": "42"}],
    "usage": {"input_tokens": 10, "output_tokens": 2},
}

OPENAI_BODY = {
    "choices": [{"message": {"content": "42"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
}


def _make_client(provider="ollama", model="test:7b", **kwargs):
    defaults = dict(
        url="http://localhost:11434",
        api_key="test-key",
        queue_mode="off",
        log_caller="",
    )
    defaults.update(kwargs)
    cfg = LLMConfig(provider=provider, model=model, **defaults)
    with patch("llmclient.LLMClient.__init__.__wrapped__", None, create=True):
        pass
    # Bypass key resolution so tests don't touch disk
    client = object.__new__(LLMClient)
    client._cfg       = cfg
    client._abort     = None
    client._url       = cfg.url
    client._api_key   = cfg.api_key
    return client


# ---------------------------------------------------------------------------
# LLMResult assembly
# ---------------------------------------------------------------------------

def test_call_ollama_result_fields():
    client = _make_client()
    with mock_urlopen(OLLAMA_BODY):
        result = client.call("What is 2+2?", system="You are a calculator.")

    assert isinstance(result, LLMResult)
    assert result.outcome == "success"
    assert result.text == "42"
    assert result.inference_s == pytest.approx(0.3, abs=0.01)
    assert result.load_s == 0.0
    assert result.prompt_tokens == 10
    assert result.response_tokens == 3
    assert result.prompt_chars == len("You are a calculator.") + len("What is 2+2?")
    assert result.response_chars == 2
    assert result.queue_wait_s == 0.0   # queue_mode="off"
    assert result.total_s == result.call_s


def test_call_anthropic_result_fields():
    client = _make_client(
        provider="anthropic", url="https://api.anthropic.com", model="claude-haiku"
    )
    with mock_urlopen(ANTHROPIC_BODY):
        result = client.call("user msg", system="system msg")

    assert result.outcome == "success"
    assert result.text == "42"
    assert result.prompt_tokens == 10
    assert result.response_tokens == 2


def test_call_openai_compatible():
    client = _make_client(provider="openai_compatible", url="https://api.openai.com")
    with mock_urlopen(OPENAI_BODY):
        result = client.call("user msg")

    assert result.outcome == "success"
    assert result.text == "42"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def test_call_writes_log_when_caller_set(tmp_path):
    import llmclient._log as log_mod
    log_entries = []

    def capture_log(cfg, operation, result, context):
        log_entries.append((cfg.log_caller, operation, result.outcome))

    cfg = LLMConfig(
        provider="ollama", model="test:7b",
        url="http://localhost:11434", api_key="",
        queue_mode="off", log_caller="myapp",
    )
    client = object.__new__(LLMClient)
    client._cfg   = cfg
    client._abort = None
    client._url   = cfg.url
    client._api_key = ""

    with mock_urlopen(OLLAMA_BODY):
        with patch("llmclient._log.write_log", side_effect=capture_log):
            client.call("hello", operation="test_op")

    assert len(log_entries) == 1
    assert log_entries[0] == ("myapp", "test_op", "success")


def test_call_skips_log_when_no_caller():
    cfg = LLMConfig(
        provider="ollama", model="test:7b",
        url="http://localhost:11434", api_key="",
        queue_mode="off", log_caller="",
    )
    client = object.__new__(LLMClient)
    client._cfg   = cfg
    client._abort = None
    client._url   = cfg.url
    client._api_key = ""

    with mock_urlopen(OLLAMA_BODY):
        with patch("llmclient._log.write_log") as mock_log:
            client.call("hello")

    mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# Queue integration
# ---------------------------------------------------------------------------

def test_call_skips_queue_when_mode_off():
    client = _make_client(queue_mode="off")
    with mock_urlopen(OLLAMA_BODY):
        with patch("llmclient._queue.acquire") as mock_acquire:
            result = client.call("hello")

    mock_acquire.assert_not_called()
    assert result.queue_wait_s == 0.0


def test_call_uses_queue_for_ollama_cooperative(queue_db, monkeypatch):
    cfg = LLMConfig(
        provider="ollama", model="test:7b",
        url="http://localhost:11434", api_key="",
        queue_mode="cooperative", log_caller="",
        priority=50, caller_max=4,
    )
    client = object.__new__(LLMClient)
    client._cfg   = cfg
    client._abort = None
    client._url   = cfg.url
    client._api_key = ""

    with mock_urlopen(OLLAMA_BODY):
        with patch("llmclient._keys.get_parallel_slots", return_value=4):
            result = client.call("hello")

    assert result.outcome == "success"
    assert result.queue_wait_s >= 0.0


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def test_ollama_constructor_sets_provider():
    with patch("llmclient._keys.resolve_url", return_value="http://localhost:11434"):
        with patch("llmclient._keys.resolve_api_key", return_value=""):
            c = LLMClient.ollama("mymodel:7b", queue_mode="off")
    assert c._cfg.provider == "ollama"
    assert c._cfg.model == "mymodel:7b"
    assert c._cfg.queue_mode == "off"


def test_anthropic_constructor_defaults_queue_off():
    with patch("llmclient._keys.resolve_url", return_value="https://api.anthropic.com"):
        with patch("llmclient._keys.resolve_api_key", return_value=""):
            c = LLMClient.anthropic("claude-haiku")
    assert c._cfg.provider == "anthropic"
    assert c._cfg.queue_mode == "off"


def test_from_profile_ollama_sets_cooperative():
    with patch("llmclient._keys.resolve_url", return_value="http://localhost:11434"):
        with patch("llmclient._keys.resolve_api_key", return_value=""):
            c = LLMClient.from_profile("ollama", "qwen3:14b")
    assert c._cfg.queue_mode == "cooperative"


def test_from_profile_anthropic_sets_off():
    with patch("llmclient._keys.resolve_url", return_value="https://api.anthropic.com"):
        with patch("llmclient._keys.resolve_api_key", return_value=""):
            c = LLMClient.from_profile("anthropic", "claude-haiku")
    assert c._cfg.queue_mode == "off"
