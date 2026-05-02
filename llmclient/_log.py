"""JSONL call log — one entry per LLM call, per llm-call-logging.md schema."""
import json
from datetime import datetime
from pathlib import Path


def _log_path(caller: str) -> Path:
    return Path.home() / ".local" / "share" / caller / "llm_calls.jsonl"


def write_log(cfg, operation: str, result, context: dict | None) -> None:
    """Append one JSONL line. Silently drops on any error."""
    if not cfg.log_caller:
        return
    try:
        entry = {
            "timestamp":         datetime.now().isoformat(timespec="milliseconds"),
            "caller":            cfg.log_caller,
            "operation":         operation,
            "provider":          cfg.provider,
            "model":             cfg.model,
            "prompt_chars":      result.prompt_chars,
            "prompt_tokens_est": int(result.prompt_chars / 3.5),
            "prompt_tokens":     result.prompt_tokens,
            "response_tokens":   result.response_tokens,
            "queue_wait_s":      result.queue_wait_s,
            "call_s":            result.call_s,
            "inference_s":       result.inference_s,
            "load_s":            result.load_s,
            "elapsed_s":         result.total_s,
            "outcome":           result.outcome,
            "response_chars":    result.response_chars,
            "context":           context or {},
        }
        log_path = _log_path(cfg.log_caller)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
