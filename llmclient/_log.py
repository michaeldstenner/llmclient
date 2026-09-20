"""
Central JSONL call log — one entry per LLM call *that the current log
level records*.

At the default level ("errors") successful calls are dropped, so the
file on disk is a failure log, not a call log: a quiet caller that is
succeeding constantly writes nothing at all.  Line counts therefore
measure failures, never traffic.  A "#"-prefixed header saying so is
written into each log file when it is first created (see _HEADER) —
readers must skip non-JSON lines.

All callers sharing a data_dir write to the same llmclient_log.jsonl.
fcntl.flock serialises concurrent writes from separate processes.

Log levels (set via llmclient.configure(log_level=...)):
  "off"    — nothing written
  "errors" — non-success outcomes only (default)
  "all"    — every call; queue snapshot always included
"""
import fcntl
import json
import os
from datetime import datetime, timezone

# Written once, when a log file is created.  Not JSON: readers of this
# file skip lines that fail to parse (llmc log, scripts/fit_breaker_
# params.py).  Kept as comments rather than an in-band JSON object so a
# reader counting entries cannot mistake it for a call.
_HEADER = (
    "# llmclient call log — JSONL, one object per line.\n"
    "# NOT a record of every call: at the default log level \"errors\"\n"
    "# only non-success outcomes are written, so successful calls leave\n"
    "# no trace here and the line count is a failure count, not a call\n"
    "# count.  Use llmclient.configure(log_level=\"all\") to log every\n"
    "# call.  Lines starting with # are not JSON; skip them.\n"
)


def write_log(cfg, operation: str, result, context: dict | None) -> None:
    """Append one JSONL line to the central log. Silently drops on error."""
    from ._config import get_log_path, get_log_level
    level = get_log_level()
    if level == "off":
        return
    if level == "errors" and result.outcome == "success":
        return
    try:
        snap = getattr(result, "queue_snapshot", None)
        if snap is None and level == "all":
            from ._queue import _read_queue_state
            snap = _read_queue_state()

        entry = {
            "timestamp":         datetime.now(timezone.utc).isoformat(
                                     timespec="milliseconds"),
            "caller":            cfg.log_caller or "",
            "operation":         operation,
            "provider":          cfg.provider,
            "model":             cfg.model,
            "outcome":           result.outcome,
            "elapsed_s":         result.total_s,
            "queue_wait_s":      result.queue_wait_s,
            "call_s":            result.call_s,
            "inference_s":       result.inference_s,
            "load_s":            result.load_s,
            "prompt_chars":      result.prompt_chars,
            "prompt_tokens_est": int(result.prompt_chars / 3.5),
            "prompt_tokens":     result.prompt_tokens,
            "response_chars":    result.response_chars,
            "response_tokens":   result.response_tokens,
            # Ollama only; None elsewhere. num_ctx is what was sent after
            # the high-water-mark ratchet, num_ctx_want what this call
            # alone needed. Logging both makes the ratchet's inflation
            # measurable per caller -- the number that decides whether
            # OLLAMA_CONTEXT_LENGTH can safely be capped.
            "num_ctx":           getattr(result, "num_ctx", None),
            "num_ctx_want":      getattr(result, "num_ctx_want", None),
        }
        if snap:
            entry["queue_snapshot"] = snap
        if context:
            entry["context"] = context

        log_path = get_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry) + "\n"
        with open(log_path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # Size is checked under the lock, not at open(), so two
                # processes racing to create the file cannot both write
                # a header.  Append-only: an existing file never gains
                # one retroactively.
                if os.fstat(f.fileno()).st_size == 0:
                    f.write(_HEADER)
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
