import json
import sqlite3
import subprocess
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_DB = Path.home() / ".local" / "share" / "llmclient" / "queue.db"


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


def _active_connection_count(port: int = 11434) -> int:
    """Total number of client connections currently open to port."""
    return sum(_direct_connections(port).values())


def _direct_connections(
    port: int = 11434,
) -> dict[tuple[str, int], int]:
    """
    Return {(cmd, pid): count} for processes with a TCP connection
    TO :port — i.e., clients bypassing nothing, hitting the server
    directly.  Excludes the server process itself.
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
    if not _DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(_DB))
        rows = conn.execute("""
            SELECT id, caller, status, pid,
                   CAST(strftime('%s','now') AS INTEGER)
                   - CAST(submitted_at AS INTEGER) AS age_s
            FROM   queue
            ORDER  BY submitted_at
        """).fetchall()
        conn.close()
        return [
            {
                "id":     r[0],
                "caller": r[1],
                "status": r[2],
                "pid":    r[3],
                "age_s":  r[4],
            }
            for r in rows
        ]
    except Exception:
        return []


def _process_detail(pid: int) -> str:
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

    # Python: show the script / -m module, strip the interpreter
    if exe in ("python", "python3") or exe.startswith("python3."):
        rest = parts[1:]
        if not rest:
            return cmd
        if rest[0] == "-m":
            return " ".join(rest[:2])
        if rest[0].startswith("-"):
            # flags before script; find first non-flag arg
            for i, a in enumerate(rest):
                if not a.startswith("-"):
                    return " ".join([a] + rest[i+1:i+4])
            return cmd
        # rest[0] is the script path
        script = rest[0]
        trailer = " ".join(rest[1:4])
        label = str(Path(script))
        return f"{label} {trailer}".strip()

    # bash / zsh / sh: show -c snippet or script name
    if exe in ("bash", "zsh", "sh"):
        rest = parts[1:]
        if rest and rest[0] == "-c" and len(rest) > 1:
            snippet = rest[1][:60]
            return f"-c {snippet!r}"
        for a in rest:
            if not a.startswith("-"):
                return a
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
    url = resolve_url("ollama", "")

    num_parallel = _ollama_num_parallel()
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
        active = _active_connection_count(11434)
        cap    = num_parallel or "?"
        for m in models:
            name = m.get("model", "?")
            ctx  = m.get("context_length", "?")
            exp  = _fmt_expires(m.get("expires_at", ""))
            print(
                f"  {name:<32} ctx:{ctx:<6}"
                f"  {active}/{cap} slots  {exp}"
            )

    print()
    print("CONNECTIONS  :11434  (direct — outside llmclient queue)")
    conns     = _direct_connections(11434)
    queue_pids = {r["pid"] for r in _queue_rows()}
    if not conns:
        print("  (none)")
    else:
        for (cmd, pid), n in sorted(
            conns.items(), key=lambda kv: -kv[1]
        ):
            via    = "  [via queue]" if pid in queue_pids else ""
            warn   = "  ← saturating" if n >= 4 else ""
            pl     = "s" if n != 1 else ""
            detail = _process_detail(pid)
            print(
                f"  {cmd:<22} pid:{pid:<8} "
                f"{n} conn{pl}{via}{warn}"
            )
            if detail:
                print(f"    → {detail}")

    print()
    cmd_queue(args)


def cmd_queue(args) -> None:
    rows = _queue_rows()
    print("LLMCLIENT QUEUE")
    if not rows:
        msg = "(queue.db not found)" if not _DB.exists() else "(empty)"
        print(f"  {msg}")
        return
    print(f"  {'id':<5} {'caller':<18} {'status':<10} {'age':>6}  pid")
    for r in rows:
        age = f"{r['age_s']}s" if r["age_s"] is not None else "?"
        print(
            f"  {r['id']:<5} {r['caller']:<18} "
            f"{r['status']:<10} {age:>6}  {r['pid']}"
        )
