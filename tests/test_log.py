"""Tests for _log.py — central JSONL call logging."""
import json
from pathlib import Path

import pytest

import llmclient._config as config_mod
import llmclient._log as _log_mod
from llmclient._log import write_log
from llmclient import EmbedResult, LLMResult
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


def _embed_result(**kwargs) -> EmbedResult:
    defaults = dict(
        vector=[0.1, 0.2, 0.3],
        outcome="success",
        total_s=0.06,
        queue_wait_s=0.001,
        call_s=0.059,
        load_s=0.03,
        prompt_chars=28,
        prompt_tokens=9,
    )
    defaults.update(kwargs)
    return EmbedResult(**defaults)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_data_dir", tmp_path)
    monkeypatch.setattr(config_mod, "_log_level", "all")


def _log_file(tmp_path: Path) -> Path:
    return tmp_path / "llmclient_log.jsonl"


def _read_entries(tmp_path: Path) -> list[dict]:
    """Entries only — skips the "#" header, as every real reader does."""
    return [json.loads(l)
            for l in _log_file(tmp_path).read_text().splitlines()
            if l.strip() and not l.startswith("#")]


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


# ---------------------------------------------------------------------------
# Embed results
#
# EmbedResult carries generation-only fields as zero/None defaults purely so
# write_log() can build one entry shape for both result types.  These tests
# pin that down: any field write_log reads must survive an EmbedResult, or
# the bare `except Exception: pass` turns the whole embed call into a silent
# logging no-op.
# ---------------------------------------------------------------------------

def test_embed_result_is_logged(tmp_path):
    cfg = make_cfg(log_caller="squirrel", provider="ollama",
                   model="nomic-embed-text")
    write_log(cfg, "embed", _embed_result(), None)

    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["caller"] == "squirrel"
    assert e["operation"] == "embed"
    assert e["model"] == "nomic-embed-text"
    assert e["outcome"] == "success"
    assert e["elapsed_s"] == 0.06
    assert e["queue_wait_s"] == 0.001
    assert e["call_s"] == 0.059
    assert e["load_s"] == 0.03
    assert e["prompt_chars"] == 28
    assert e["prompt_tokens"] == 9
    assert "timestamp" in e
    assert "prompt_tokens_est" in e


def test_embed_entry_has_same_keys_as_generation_entry(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)
    write_log(cfg, "embed", _embed_result(), None)
    gen, emb = _read_entries(tmp_path)
    assert emb.keys() == gen.keys()


def test_embed_entry_generation_only_fields_are_empty(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "embed", _embed_result(), None)
    e = _read_entries(tmp_path)[0]
    assert e["response_chars"] == 0
    assert e["response_tokens"] is None
    assert e["inference_s"] == 0.0
    assert e["num_ctx"] is None
    assert e["num_ctx_want"] is None


def test_embed_failure_logged_at_errors_level(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_log_level", "errors")
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "embed", _embed_result(outcome="success"), None)
    write_log(cfg, "embed",
              _embed_result(vector=None, outcome="error:unreachable"), None)
    entries = _read_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "error:unreachable"


# ---------------------------------------------------------------------------
# Self-describing header
#
# At the default log level the file records only failures, so a reader who
# counts lines reads a failure count as a call count.  A "#" header written
# at file creation says so in-band.  It must be written exactly once, must
# not be JSON, and must not survive into the entry stream of any reader.
# ---------------------------------------------------------------------------

def _raw_lines(tmp_path: Path) -> list[str]:
    return _log_file(tmp_path).read_text().splitlines()


def test_header_written_on_file_creation(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)
    lines = _raw_lines(tmp_path)
    assert lines[0].startswith("#")
    assert "NOT a record of every call" in "\n".join(lines)
    # ...and the entry still lands.
    assert len(_read_entries(tmp_path)) == 1


def test_header_written_only_once(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)
    write_log(cfg, "call", _result(), None)
    write_log(cfg, "call", _result(), None)
    lines = _raw_lines(tmp_path)
    assert sum(1 for l in lines if l.startswith("#")) == len(
        [l for l in _log_mod._HEADER.splitlines() if l])
    assert len(_read_entries(tmp_path)) == 3


def test_header_not_prepended_to_existing_file(tmp_path):
    """Append-only: a pre-existing log is never rewritten to gain a header."""
    existing = _log_file(tmp_path)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('{"outcome": "timeout:generation"}\n')
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)
    lines = _raw_lines(tmp_path)
    assert not lines[0].startswith("#")
    assert len(lines) == 2


def test_header_lines_are_not_json(tmp_path):
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(), None)
    for line in _raw_lines(tmp_path):
        if line.startswith("#"):
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)


def test_cli_log_reader_skips_header(tmp_path):
    from llmclient.cli._log import _read_log
    cfg = make_cfg(log_caller="app")
    write_log(cfg, "call", _result(outcome="timeout:generation"), None)
    entries = _read_log(_log_file(tmp_path))
    assert len(entries) == 1
    assert entries[0]["outcome"] == "timeout:generation"
