import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _is_error(outcome: str) -> bool:
    return outcome not in ("success", "aborted")


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


def _format_entry(entry: dict) -> str:
    ts      = entry.get("timestamp", "")
    caller  = entry.get("caller", "?") or "?"
    outcome = entry.get("outcome", "?")
    label   = "ERR" if _is_error(outcome) else "ok"

    try:
        dt  = datetime.fromisoformat(ts).astimezone()
        now = datetime.now(timezone.utc).astimezone()
        if dt.date() == now.date():
            ts_str = dt.strftime("%H:%M:%S")
        else:
            ts_str = dt.strftime("%m-%d %H:%M:%S")
    except Exception:
        ts_str = ts[:19]

    wait_s = entry.get("queue_wait_s", 0.0) or 0.0
    call_s = entry.get("call_s", 0.0) or 0.0
    model  = entry.get("model", "")
    model  = model.split(":")[0] if model else ""

    timing = f"{wait_s:.1f}s queue + {call_s:.1f}s call"
    line = (
        f"{ts_str}  {caller:<12} {label:<4}  {outcome:<28}  "
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
    from llmclient._config import get_log_path
    show_all   = getattr(args, "level", "errors") == "all"
    last_n     = args.last
    caller_flt = getattr(args, "caller", None)
    emit_json  = getattr(args, "json", False)

    log_path = get_log_path()
    if not log_path.exists():
        print(f"no log found at {log_path}", file=sys.stderr)
        return

    entries = _read_log(log_path)
    entries.sort(key=lambda e: e.get("timestamp", ""))

    if not show_all:
        entries = [e for e in entries if _is_error(e.get("outcome", ""))]
    if caller_flt:
        entries = [e for e in entries if e.get("caller") == caller_flt]
    if last_n > 0:
        entries = entries[-last_n:]

    if not entries:
        print("(no matching log entries)")
        return

    if emit_json:
        print(json.dumps(entries, indent=2))
        return

    for entry in entries:
        print(_format_entry(entry))
