import json
import sqlite3
import subprocess
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

def _db_path() -> Path:
    from .._config import get_db_path
    return get_db_path()


def _ollama_ps(url: str) -> list[dict] | None:
    """None = unreachable; [] = reachable but no models loaded."""
    try:
        with urllib.request.urlopen(
            url + "/api/ps", timeout=3
        ) as r:
            return json.loads(r.read()).get("models", [])
    except Exception:
        return None


def _ollama_num_parallel() -> int | None:
    """Read OLLAMA_NUM_PARALLEL from the homebrew launchd plist."""
    plist = (
        Path.home()
        / "Library/LaunchAgents/homebrew.mxcl.ollama.plist"
    )
    try:
        import plistlib
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        env = data.get("EnvironmentVariables", {})
        val = env.get("OLLAMA_NUM_PARALLEL")
        return int(val) if val is not None else None
    except Exception:
        return None


def _direct_connections(
    port: int = 11434,
) -> dict[tuple[str, int], int]:
    """
    Return {(cmd, pid): count} for processes with an ESTABLISHED TCP
    connection to :port.  Excludes the server process itself.
    """
    try:
        r = subprocess.run(
            ["lsof", "-i", f":{port}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {}
    clients: dict[tuple[str, int], int] = defaultdict(int)
    target = f"->localhost:{port}"
    for line in r.stdout.splitlines()[1:]:
        if target not in line:
            continue
        if "(ESTABLISHED)" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            clients[(parts[0], int(parts[1]))] += 1
        except ValueError:
            continue
    return dict(clients)


def _queue_rows() -> list[dict]:
    db = _db_path()
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("""
                SELECT id, caller, status, pid, model,
                       CAST(strftime('%s','now') AS INTEGER)
                       - CAST(submitted_at AS INTEGER) AS age_s,
                       CASE WHEN started_at IS NOT NULL
                            THEN CAST(strftime('%s','now') AS INTEGER)
                                 - CAST(started_at AS INTEGER)
                            ELSE NULL
                       END AS running_s
                FROM   queue
                ORDER  BY submitted_at
            """).fetchall()
            result = [
                {
                    "id":       r[0],
                    "caller":   r[1],
                    "status":   r[2],
                    "pid":      r[3],
                    "model":    r[4] or "",
                    "age_s":    r[5],
                    "running_s": r[6],
                }
                for r in rows
            ]
        except Exception:
            # Old schema without model column
            rows = conn.execute("""
                SELECT id, caller, status, pid,
                       CAST(strftime('%s','now') AS INTEGER)
                       - CAST(submitted_at AS INTEGER) AS age_s
                FROM   queue
                ORDER  BY submitted_at
            """).fetchall()
            result = [
                {
                    "id":       r[0],
                    "caller":   r[1],
                    "status":   r[2],
                    "pid":      r[3],
                    "model":    "",
                    "age_s":    r[4],
                    "running_s": None,
                }
                for r in rows
            ]
        conn.close()
        return result
    except Exception:
        return []


def _ppid(pid: int) -> int | None:
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True, text=True,
        )
        return int(r.stdout.strip())
    except Exception:
        return None


def _process_detail(pid: int, _depth: int = 0) -> str:
    """
    Return a human-readable description of what a PID is running.
    For Python/bash/zsh: show the script, not the interpreter.
    For ollama run: show the model name.
    Falls back to the raw command line (truncated).
    """
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True,
        )
        cmd = r.stdout.strip()
    except Exception:
        return ""
    if not cmd:
        return ""

    parts = cmd.split()
    exe   = Path(parts[0]).name if parts else ""

    # Python: handles lowercase (python, python3, python3.X) and the
    # macOS framework binary named "Python" (capital P).
    _is_python = (
        exe in ("python", "python3", "Python")
        or exe.startswith("python3.")
        or exe.startswith("Python3.")
    )
    if _is_python:
        rest = parts[1:]
        if not rest and _depth < 2:
            ppid = _ppid(pid)
            return _process_detail(ppid, _depth + 1) if ppid else cmd
        if rest and rest[0] == "-c":
            snippet = rest[1][:60] if len(rest) > 1 else ""
            return f"-c {snippet!r}"
        if rest and rest[0] == "-m":
            return " ".join(rest[:2])
        if rest and rest[0].startswith("-"):
            # flags before script; find first non-flag arg
            for a in rest:
                if not a.startswith("-"):
                    return Path(a).name
            if _depth < 2:
                ppid = _ppid(pid)
                return (
                    _process_detail(ppid, _depth + 1) if ppid else cmd
                )
            return cmd
        if rest:
            return Path(rest[0]).name
        return cmd

    # bash / zsh / sh: show -c snippet or script name
    if exe in ("bash", "zsh", "sh"):
        rest = parts[1:]
        if rest and rest[0] == "-c" and len(rest) > 1:
            snippet = rest[1][:60]
            return f"-c {snippet!r}"
        for a in rest:
            if not a.startswith("-"):
                return Path(a).name
        return cmd

    # ollama run: show model name
    if exe == "ollama" and len(parts) >= 3 and parts[1] == "run":
        return f"ollama run {parts[2]}"

    # fallback: first 72 chars of full command
    return cmd[:72]


def _fmt_expires(iso: str) -> str:
    try:
        exp  = datetime.fromisoformat(iso)
        now  = datetime.now(tz=exp.tzinfo or timezone.utc)
        secs = (exp - now).total_seconds()
        if secs < 0:
            return "expired"
        mins = int(secs / 60)
        return f"expires in {mins}m"
    except Exception:
        return ""


def cmd_status(args) -> None:
    from .._keys import resolve_url

    url          = resolve_url("ollama", "")
    num_parallel = _ollama_num_parallel()
    cap_str      = str(num_parallel) if num_parallel is not None else "?"

    rows       = _queue_rows()
    queue_pids = {r["pid"] for r in rows}

    # Group all queue rows by model
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    # TCP connections not accounted for by the queue
    conns     = _direct_connections(11434)
    unmanaged = {
        (cmd, pid): n
        for (cmd, pid), n in conns.items()
        if pid not in queue_pids
    }

    # ── CONNECTIONS ───────────────────────────────────────────────────
    print("CONNECTIONS  :11434")
    any_output = False

    for model in sorted(by_model):
        model_rows = by_model[model]
        running_n  = sum(1 for r in model_rows if r["status"] == "running")
        label      = model or "(unknown model)"
        print(f"\n  {label}  ({running_n}/{cap_str} slots)")

        def _sort_key(r):
            if r["status"] == "running":
                return (0, -(r["running_s"] or 0))
            return (1, r["age_s"] or 0)

        for r in sorted(model_rows, key=_sort_key):
            age = (
                r["running_s"]
                if r["status"] == "running" and r["running_s"] is not None
                else r["age_s"]
            )
            age_str = f"{age}s" if age is not None else "?"
            print(
                f"    {r['caller']:<16} pid:{r['pid']:<8}"
                f" {r['status']:<8} {age_str:>6}"
            )
        any_output = True

    if unmanaged:
        print("\n  unmanaged  (not in queue; may be idle keep-alive)")
        for (cmd, pid), n in sorted(unmanaged.items(), key=lambda kv: -kv[1]):
            pl     = "s" if n != 1 else ""
            warn   = "  ← saturating" if n >= 4 else ""
            detail = _process_detail(pid)
            print(f"    {cmd:<22} pid:{pid:<8} {n} conn{pl}{warn}")
            if detail:
                print(f"      → {detail}")
        any_output = True

    if not any_output:
        print("  (none)")

    # ── OLLAMA ────────────────────────────────────────────────────────
    print()
    par_str = (
        f"  (NUM_PARALLEL={num_parallel})"
        if num_parallel is not None else ""
    )
    print(f"OLLAMA  {url}{par_str}")
    models = _ollama_ps(url)
    if models is None:
        print("  unreachable")
    elif not models:
        print("  (no models loaded)")
    else:
        for m in models:
            name = m.get("model", "?")
            ctx  = m.get("context_length", "?")
            exp  = _fmt_expires(m.get("expires_at", ""))
            print(f"  {name:<32} ctx:{ctx:<6}  {exp}")

    # ── QUEUE ─────────────────────────────────────────────────────────
    print()
    cmd_queue(args, rows=rows)


def cmd_reset(args) -> None:
    from .._config import get_db_path
    from .._queue import _open
    db = get_db_path()
    if not db.exists():
        print("queue.db not found — nothing to reset")
        return
    # _open() applies all schema migrations (incl. the circuit_key re-key),
    # so reset works against a DB created by any llmclient version.
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT circuit_key, caller, consecutive_n, llr FROM circuit_state"
            " WHERE tripped_at IS NOT NULL"
        ).fetchall()
        if not rows:
            print("No tripped circuits.")
            conn.close()
            return
        conn.execute(
            "UPDATE circuit_state"
            " SET consecutive_n=0, tripped_at=NULL, probe_pid=NULL,"
            "     llr=0.0, llr_updated_at=NULL"
            " WHERE tripped_at IS NOT NULL"
        )
        conn.commit()
        for key, caller, n, llr in rows:
            detail = f"consecutive_n was {n}" if (llr or 0.0) == 0.0 \
                else f"llr was {llr:.2f}"
            label = key if (not caller or caller == key) else f"{key} ({caller})"
            print(f"  reset  {label}  ({detail})")
    finally:
        conn.close()


def cmd_queue(args, rows: list[dict] | None = None) -> None:
    if rows is None:
        rows = _queue_rows()
    print("LLMCLIENT QUEUE")
    if not rows:
        msg = "(queue.db not found)" if not _db_path().exists() else "(empty)"
        print(f"  {msg}")
        return
    print(
        f"  {'id':<5} {'caller':<16} {'model':<28}"
        f" {'status':<10} {'age':>6}  pid"
    )
    for r in rows:
        age   = f"{r['age_s']}s" if r["age_s"] is not None else "?"
        model = r.get("model") or "(unknown)"
        print(
            f"  {r['id']:<5} {r['caller']:<16} {model:<28}"
            f" {r['status']:<10} {age:>6}  {r['pid']}"
        )
