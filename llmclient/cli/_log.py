import json
import sys
from datetime import datetime
from pathlib import Path


_LOG_ROOT = Path.home() / ".local" / "share"

_WARN_OUTCOMES = {
    "timeout:queue_wait",
    "timeout:queue_stall",
    "timeout:first_token",
    "timeout:generation",
    "circuit_open",
    "error:unreachable",
    # legacy names
    "timeout",
    "timeout:model_loaded_but_slow",
    "timeout:model_not_loaded",
}


def _outcome_level(outcome: str) -> str:
    if outcome == "success":
        return "ok"
    if outcome in ("aborted",):
        return "ok"
    if outcome in _WARN_OUTCOMES:
        return "warn"
    return "error"


def _level_value(level: str) -> int:
    return {"ok": 0, "warn": 1, "error": 2}.get(level, 1)


def _read_log(path: Path) -> list[dict]:
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return entries


def _all_log_files() -> list[Path]:
    if not _LOG_ROOT.exists():
        return []
    return sorted(_LOG_ROOT.glob("*/llm_calls.jsonl"))


def _format_entry(entry: dict) -> str:
    ts      = entry.get("timestamp", "")
    caller  = entry.get("caller", "?")
    outcome = entry.get("outcome", "?")
    level   = _outcome_level(outcome)
    label   = level.upper()

    # short timestamp: HH:MM:SS if today, else MM-DD HH:MM:SS
    try:
        dt  = datetime.fromisoformat(ts)
        now = datetime.now()
        if dt.date() == now.date():
            ts_str = dt.strftime("%H:%M:%S")
        else:
            ts_str = dt.strftime("%m-%d %H:%M:%S")
    except Exception:
        ts_str = ts[:19]

    wait_s  = entry.get("queue_wait_s", 0.0) or 0.0
    call_s  = entry.get("call_s", 0.0) or 0.0
    model   = entry.get("model", "")
    model   = model.split(":")[0] if model else ""   # strip tag

    timing = f"{wait_s:.1f}s queue + {call_s:.1f}s call"
    line = (
        f"{ts_str}  {caller:<12} {label:<5}  {outcome:<28}  "
        f"{timing}  [{model}]"
    )

    snap = entry.get("queue_snapshot")
    if snap:
        from collections import Counter
        running = Counter(r["caller"] for r in snap if r["status"] == "running")
        waiting = Counter(r["caller"] for r in snap if r["status"] == "waiting")
        parts = []
        if running:
            parts.append("running: " + ", ".join(
                f"{c}×{n}" if n > 1 else c for c, n in running.items()
            ))
        if waiting:
            parts.append("waiting: " + ", ".join(
                f"{c}×{n}" if n > 1 else c for c, n in waiting.items()
            ))
        if parts:
            line += "\n  queue: " + "; ".join(parts)

    return line


def cmd_log(args) -> None:
    min_level  = _level_value(args.level)
    last_n     = args.last
    caller_flt = getattr(args, "caller", None)
    emit_json  = getattr(args, "json", False)

    # collect log files
    if caller_flt:
        files = [_LOG_ROOT / caller_flt / "llm_calls.jsonl"]
    else:
        files = _all_log_files()

    if not files:
        print("no log files found under ~/.local/share/*/llm_calls.jsonl",
              file=sys.stderr)
        return

    # read and merge
    all_entries: list[dict] = []
    for path in files:
        all_entries.extend(_read_log(path))

    # sort by timestamp
    def _ts_key(e: dict) -> str:
        return e.get("timestamp", "")

    all_entries.sort(key=_ts_key)

    # filter by level
    filtered = [
        e for e in all_entries
        if _level_value(_outcome_level(e.get("outcome", ""))) >= min_level
    ]

    # take last N
    if last_n > 0:
        filtered = filtered[-last_n:]

    if not filtered:
        print("(no matching log entries)")
        return

    if emit_json:
        print(json.dumps(filtered, indent=2))
        return

    for entry in filtered:
        print(_format_entry(entry))
