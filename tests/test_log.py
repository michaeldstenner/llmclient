"""Tests for _log.py — central JSONL call logging."""
import json
from pathlib import Path

import pytest

import llmclient._config as config_mod
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


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_data_dir", tmp_path)
    monkeypatch.setattr(config_mod, "_log_level", "all")


def _log_file(tmp_path: Path) -> Path:
    return tmp_path / "llmclient_log.jsonl"


def _read_entries(tmp_path: Path) -> list[dict]:
    return [json.loads(l) for l in _log_file(tmp_path).read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Basic write
# ---------------------------------------------------------------------------

def test_write_log_creates_file_and_entry(tmp_path):
    cfg = make_cfg(log_caller="myapp", provider="ollama", model="qwen3:14b")
    write_log(cfg, "classify", _result(), {"cwd": "/tmp"})

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["caller"] == "myapp"
    assert e["operation"] == "classify"
    assert e["provider"] == "ollama"
    assert e["model"] == "qwen3:14b"
    assert e["outcome"] == "success"
    assert e["prompt_chars"] == 100
    assert e["response_chars"] == 5
    assert e["prompt_tokens"] == 30
    assert e["response_tokens"] == 3
    assert e["queue_wait_s"] == 0.1
    assert e["call_s"] == 1.4
    assert e["inference_s"] == 1.2
    assert e["load_s"] == 0.0
    assert e["elapsed_s"] == 1.5
    assert e["context"] == {"cwd": "/tmp"}
    assert "timestamp" in e
    assert "prompt_tokens_est" in e


def test_write_log_no_caller_field_still_writes(tmp_path):
    cfg = make_cfg(log_caller="")
    write_log(cfg, "classify", _result(), None)
    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["caller"] == ""


def test_write_log_appends_multiple_entries(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "op1", _result(outcome="success"), None)
    write_log(cfg, "op2", _result(outcome="timeout:generation"), None)
    entries = _read_entries(tmp_path)
    assert len(entries) == 2
    assert entries[0]["operation"] == "op1"
    assert entries[1]["outcome"] == "timeout:generation"


def test_write_log_null_tokens(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(prompt_tokens=None, response_tokens=None), None)
    e = _read_entries(tmp_path)[0]
    assert e["prompt_tokens"] is None
    assert e["response_tokens"] is None


def test_write_log_silent_on_write_error(monkeypatch):
    monkeypatch.setattr(config_mod, "_data_dir", Path("/no/such/dir/hopefully"))
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)  # must not raise


# ---------------------------------------------------------------------------
# Log levels
# ---------------------------------------------------------------------------

def test_level_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_log_level", "off")
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(outcome="timeout:generation"), None)
    assert not _log_file(tmp_path).exists()


def test_level_errors_skips_success(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_log_level", "errors")
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "ok_call", _result(outcome="success"), None)
    write_log(cfg, "bad_call", _result(outcome="timeout:generation"), None)
    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["operation"] == "bad_call"


def test_level_all_writes_success(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_log_level", "all")
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "ok_call", _result(outcome="success"), None)
    entries = _read_entries(tmp_path)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Queue snapshot
# ---------------------------------------------------------------------------

def test_snapshot_included_when_present(tmp_path):
    snap = [{"id": 1, "caller": "other", "status": "running", "model": "qwen3"}]
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(outcome="timeout:queue_wait", queue_snapshot=snap), None)
    e = _read_entries(tmp_path)[0]
    assert e["queue_snapshot"] == snap
