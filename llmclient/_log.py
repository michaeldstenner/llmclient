"""
Central JSONL call log — one entry per LLM call.

All callers sharing a data_dir write to the same llmclient_log.jsonl.
fcntl.flock serialises concurrent writes from separate processes.

Log levels (set via llmclient.configure(log_level=...)):
  "off"    — nothing written
  "errors" — non-success outcomes only
  "all"    — every call; queue snapshot always included
"""
import fcntl
import json
from datetime import datetime, timezone


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
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
