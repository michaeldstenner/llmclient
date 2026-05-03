"""Tests for _log.py — JSONL call logging."""
import json
from pathlib import Path

import pytest

from llmclient._log import write_log
from llmclient import LLMResult
from tests.conftest import make_cfg


def _result(**kwargs) -> LLMResult:
    defaults = dict(
        text="hello",
        outcome="success",
        total_s=1.5,
        queue_wait_s=0.1,
        call_s=1.4,
        inference_s=1.2,
        load_s=0.0,
        prompt_chars=100,
        response_chars=5,
        prompt_tokens=30,
        response_tokens=3,
    )
    defaults.update(kwargs)
    return LLMResult(**defaults)


def test_write_log_creates_file_and_entry(tmp_path, monkeypatch):
    import llmclient._log as log_mod
    monkeypatch.setattr(
        log_mod, "_log_path",
        lambda caller: tmp_path / caller / "llm_calls.jsonl"
    )
    cfg = make_cfg(log_caller="myapp", provider="ollama", model="qwen3:14b")
    write_log(cfg, "classify", _result(), {"cwd": "/tmp"})

    log_file = tmp_path / "myapp" / "llm_calls.jsonl"
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())

    assert entry["caller"] == "myapp"
    assert entry["operation"] == "classify"
    assert entry["provider"] == "ollama"
    assert entry["model"] == "qwen3:14b"
    assert entry["outcome"] == "success"
    assert entry["prompt_chars"] == 100
    assert entry["response_chars"] == 5
    assert entry["prompt_tokens"] == 30
    assert entry["response_tokens"] == 3
    assert entry["queue_wait_s"] == 0.1
    assert entry["call_s"] == 1.4
    assert entry["inference_s"] == 1.2
    assert entry["load_s"] == 0.0
    assert entry["elapsed_s"] == 1.5
    assert entry["context"] == {"cwd": "/tmp"}
    assert "timestamp" in entry
    assert "prompt_tokens_est" in entry


def test_write_log_skips_when_no_caller(tmp_path, monkeypatch):
    import llmclient._log as log_mod
    monkeypatch.setattr(
        log_mod, "_log_path",
        lambda caller: tmp_path / caller / "llm_calls.jsonl"
    )
    cfg = make_cfg(log_caller="")
    write_log(cfg, "classify", _result(), None)
    assert not any(tmp_path.iterdir())


def test_write_log_appends_multiple_entries(tmp_path, monkeypatch):
    import llmclient._log as log_mod
    monkeypatch.setattr(
        log_mod, "_log_path",
        lambda caller: tmp_path / caller / "llm_calls.jsonl"
    )
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "op1", _result(outcome="success"), None)
    write_log(cfg, "op2", _result(outcome="timeout"), None)

    lines = (tmp_path / "app" / "llm_calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["operation"] == "op1"
    assert json.loads(lines[1])["outcome"] == "timeout"


def test_write_log_null_tokens(tmp_path, monkeypatch):
    import llmclient._log as log_mod
    monkeypatch.setattr(
        log_mod, "_log_path",
        lambda caller: tmp_path / caller / "llm_calls.jsonl"
    )
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(prompt_tokens=None, response_tokens=None), None)
    entry = json.loads((tmp_path / "app" / "llm_calls.jsonl").read_text())
    assert entry["prompt_tokens"] is None
    assert entry["response_tokens"] is None


def test_write_log_silent_on_write_error(tmp_path, monkeypatch):
    import llmclient._log as log_mod
    monkeypatch.setattr(log_mod, "_log_path", lambda caller: Path("/no/permission/here.jsonl"))
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)  # must not raise
