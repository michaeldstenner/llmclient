"""
Tests for provider HTTP implementations (urllib mocked).

Ollama, Anthropic, and OpenAI-compatible providers are tested for:
  - success path with correct field extraction
  - timeout (model loaded vs not loaded for Ollama)
  - HTTP errors
  - URL/connection errors
  - abort signalling (Ollama only)
"""
import json
import socket
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from llmclient.providers.ollama import call_ollama
from llmclient.providers.anthropic import call_anthropic
from llmclient.providers.openai import call_openai, _extract_text
from tests.conftest import make_cfg, mock_urlopen, mock_urlopen_timeout
from tests.conftest import mock_urlopen_http_error, mock_urlopen_url_error


# ===========================================================================
# Ollama
# ===========================================================================

OLLAMA_SUCCESS_BODY = {
    "response": "  4  ",
    "done": True,
    "load_duration":        5_000_000_000,   # 5 s
    "prompt_eval_duration": 500_000_000,     # 0.5 s
    "eval_duration":        200_000_000,     # 0.2 s
    "total_duration":       5_700_000_000,
    "prompt_eval_count": 36,
    "eval_count": 2,
}


def _ollama_cfg(**kw):
    return make_cfg(provider="ollama", model="test:7b", url="http://localhost:11434", **kw)


def test_ollama_success_extracts_timing():
    cfg = _ollama_cfg()
    with mock_urlopen(OLLAMA_SUCCESS_BODY):
        r = call_ollama("sys", "user", cfg, "http://localhost:11434", None)
    assert r.outcome == "success"
    assert r.text == "4"
    assert r.load_s == pytest.approx(5.0, abs=0.01)
    assert r.inference_s == pytest.approx(0.7, abs=0.01)
    assert r.prompt_tokens == 36
    assert r.response_tokens == 2
    assert r.call_s > 0


def test_ollama_response_is_stripped():
    body = {**OLLAMA_SUCCESS_BODY, "response": "  hello world  "}
    with mock_urlopen(body):
        r = call_ollama("", "q", _ollama_cfg(), "http://localhost:11434", None)
    assert r.text == "hello world"


def test_ollama_num_ctx_auto_sizing():
    """Auto num_ctx should be max(4096, next-power-of-2(estimated+512))."""
    import math
    cfg = _ollama_cfg(num_ctx_auto=True)
    # 15000 chars / 3.5 ≈ 4285 tokens + 512 = 4797 → next pow2 = 8192
    system = "s" * 8000
    user   = "u" * 7000
    captured_payload = {}

    def capture_urlopen(req, timeout=None):
        captured_payload.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(OLLAMA_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        call_ollama(system, user, cfg, "http://localhost:11434", None)

    assert captured_payload["options"]["num_ctx"] == 8192


def test_ollama_num_ctx_not_set_when_disabled():
    cfg = _ollama_cfg(num_ctx_auto=False)
    captured_payload = {}

    def capture(req, timeout=None):
        captured_payload.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(OLLAMA_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    with patch("urllib.request.urlopen", side_effect=capture):
        call_ollama("s", "u", cfg, "http://localhost:11434", None)

    assert "num_ctx" not in captured_payload["options"]


def test_ollama_timeout_model_loaded():
    cfg = _ollama_cfg()
    with mock_urlopen_timeout():
        with patch("llmclient.providers.ollama._check_ollama_busy", return_value=True):
            r = call_ollama("s", "u", cfg, "http://localhost:11434", None)
    assert r.outcome == "timeout:model_loaded_but_slow"
    assert r.text is None


def test_ollama_timeout_model_not_loaded():
    cfg = _ollama_cfg()
    with mock_urlopen_timeout():
        with patch("llmclient.providers.ollama._check_ollama_busy", return_value=False):
            r = call_ollama("s", "u", cfg, "http://localhost:11434", None)
    assert r.outcome == "timeout:model_not_loaded"


def test_ollama_http_error():
    cfg = _ollama_cfg()
    with mock_urlopen_http_error(503):
        r = call_ollama("s", "u", cfg, "http://localhost:11434", None)
    assert r.outcome == "http_503"
    assert r.text is None


def test_ollama_url_error():
    cfg = _ollama_cfg()
    with mock_urlopen_url_error():
        r = call_ollama("s", "u", cfg, "http://localhost:11434", None)
    assert r.outcome == "error:unreachable"


def test_ollama_abort(monkeypatch):
    """Abort fires after the thread starts; call returns outcome=aborted."""
    import llmclient.providers.ollama as ollama_mod
    monkeypatch.setattr(ollama_mod, "_ABORT_CHECK_S", 0.05)

    abort = threading.Event()
    cfg   = _ollama_cfg(timeout=30)

    def slow_urlopen(*args, **kwargs):
        time.sleep(10)

    abort_timer = threading.Timer(0.07, abort.set)
    abort_timer.start()
    with patch("urllib.request.urlopen", side_effect=slow_urlopen):
        r = call_ollama("s", "u", cfg, "http://localhost:11434", abort)

    assert r.outcome == "aborted"
    assert r.text is None


def test_ollama_keep_alive_in_payload():
    cfg = _ollama_cfg(keep_alive="120m")
    captured: dict = {}

    def capture(req, timeout=None):
        captured.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(OLLAMA_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    with patch("urllib.request.urlopen", side_effect=capture):
        call_ollama("s", "u", cfg, "http://localhost:11434", None)

    assert captured["keep_alive"] == "120m"
    assert captured["think"] is False


# ===========================================================================
# Anthropic
# ===========================================================================

ANTHROPIC_SUCCESS_BODY = {
    "content": [{"type": "text", "text": "The answer is 42."}],
    "usage": {"input_tokens": 20, "output_tokens": 7},
    "model": "claude-3-haiku-20240307",
}


def _anthropic_cfg(**kw):
    return make_cfg(
        provider="anthropic", model="claude-3-haiku-20240307",
        url="https://api.anthropic.com", api_key="sk-ant-test", **kw
    )


def test_anthropic_success():
    cfg = _anthropic_cfg()
    with mock_urlopen(ANTHROPIC_SUCCESS_BODY):
        r = call_anthropic("sys", "user", cfg, "https://api.anthropic.com", "sk-ant-test")
    assert r.outcome == "success"
    assert r.text == "The answer is 42."
    assert r.prompt_tokens == 20
    assert r.response_tokens == 7
    assert r.load_s == 0.0
    assert r.inference_s == r.call_s


def test_anthropic_sends_separate_system_field():
    captured: dict = {}

    def capture(req, timeout=None):
        captured.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(ANTHROPIC_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    cfg = _anthropic_cfg()
    with patch("urllib.request.urlopen", side_effect=capture):
        call_anthropic("my system", "my user", cfg, "https://api.anthropic.com", "k")

    assert captured["system"] == "my system"
    assert captured["messages"][0]["role"] == "user"
    assert captured["messages"][0]["content"] == "my user"


def test_anthropic_timeout():
    cfg = _anthropic_cfg()
    with mock_urlopen_timeout():
        r = call_anthropic("s", "u", cfg, "https://api.anthropic.com", "k")
    assert r.outcome == "timeout"
    assert r.text is None


def test_anthropic_http_401():
    cfg = _anthropic_cfg()
    with mock_urlopen_http_error(401):
        r = call_anthropic("s", "u", cfg, "https://api.anthropic.com", "bad-key")
    assert r.outcome == "http_401"


def test_anthropic_url_error():
    cfg = _anthropic_cfg()
    with mock_urlopen_url_error():
        r = call_anthropic("s", "u", cfg, "https://api.anthropic.com", "k")
    assert r.outcome == "error:unreachable"


# ===========================================================================
# OpenAI / OpenAI-compatible
# ===========================================================================

OPENAI_SUCCESS_BODY = {
    "choices": [{
        "message": {"role": "assistant", "content": "Paris."},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 15, "completion_tokens": 3},
}


def _openai_cfg(**kw):
    return make_cfg(
        provider="openai_compatible", model="gpt-4o-mini",
        url="https://api.openai.com", api_key="sk-test", **kw
    )


def test_openai_success():
    cfg = _openai_cfg()
    with mock_urlopen(OPENAI_SUCCESS_BODY):
        r = call_openai("sys", "user", cfg, "https://api.openai.com", "sk-test")
    assert r.outcome == "success"
    assert r.text == "Paris."
    assert r.prompt_tokens == 15
    assert r.response_tokens == 3


def test_openai_includes_system_message():
    captured: dict = {}

    def capture(req, timeout=None):
        captured.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(OPENAI_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    cfg = _openai_cfg()
    with patch("urllib.request.urlopen", side_effect=capture):
        call_openai("be concise", "What is 2+2?", cfg, "https://api.openai.com", "k")

    assert captured["messages"][0] == {"role": "system", "content": "be concise"}
    assert captured["messages"][1] == {"role": "user", "content": "What is 2+2?"}


def test_openai_omits_system_when_empty():
    captured: dict = {}

    def capture(req, timeout=None):
        captured.update(json.loads(req.data))
        return MagicMock(
            read=lambda: json.dumps(OPENAI_SUCCESS_BODY).encode(),
            __enter__=lambda s: s,
            __exit__=MagicMock(return_value=False),
        )

    cfg = _openai_cfg()
    with patch("urllib.request.urlopen", side_effect=capture):
        call_openai("", "user prompt", cfg, "https://api.openai.com", "k")

    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["user"]


def test_openai_timeout():
    cfg = _openai_cfg()
    with mock_urlopen_timeout():
        r = call_openai("s", "u", cfg, "https://api.openai.com", "k")
    assert r.outcome == "timeout"


def test_openai_http_429():
    cfg = _openai_cfg()
    with mock_urlopen_http_error(429):
        r = call_openai("s", "u", cfg, "https://api.openai.com", "k")
    assert r.outcome == "http_429"


def test_openai_url_error():
    cfg = _openai_cfg()
    with mock_urlopen_url_error():
        r = call_openai("s", "u", cfg, "https://api.openai.com", "k")
    assert r.outcome == "error:unreachable"


# ---------------------------------------------------------------------------
# _extract_text edge cases
# ---------------------------------------------------------------------------

def test_extract_text_string_content():
    body = {"choices": [{"message": {"content": "hello"}}]}
    assert _extract_text(body) == "hello"


def test_extract_text_list_content():
    body = {"choices": [{"message": {"content": [
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ]}}]}
    assert _extract_text(body) == "part one\npart two"


def test_extract_text_fallback_to_choice_text():
    body = {"choices": [{"text": "fallback"}]}
    assert _extract_text(body) == "fallback"


def test_extract_text_missing_choices_raises():
    with pytest.raises(ValueError, match="missing choices"):
        _extract_text({"choices": []})
