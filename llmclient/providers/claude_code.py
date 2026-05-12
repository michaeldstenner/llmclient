import json
import os
import shutil
import subprocess
import time
import threading

from . import _ProviderResult

_POLL_S = 0.5


def _find_claude() -> str:
    """Return absolute path to the claude binary."""
    # Honour an explicit override first
    override = os.environ.get("LLMCLIENT_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    # Common install location when PATH may be minimal (launchd)
    fallback = os.path.expanduser("~/.local/bin/claude")
    if os.path.isfile(fallback):
        return fallback
    raise FileNotFoundError(
        "claude binary not found; set LLMCLIENT_CLAUDE_BIN or add it to PATH"
    )


def call_claude_code(
    system: str,
    user: str,
    cfg,
    abort_event: threading.Event | None,
) -> _ProviderResult:
    try:
        claude = _find_claude()
    except FileNotFoundError as exc:
        return _ProviderResult(
            text=None, outcome=f"error:{exc}",
            call_s=0.0, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    cmd = [
        claude,
        "--print",
        "--no-session-persistence",
        "--output-format", "json",
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]
    if system:
        cmd += ["--system-prompt", system]

    # Allow callers to restrict/expand tools via extra_params
    tools = cfg.extra_params.get("tools")
    if tools is not None:
        cmd += ["--tools", tools]

    cmd.append(user)

    timeout = int(cfg.extra_params.get("timeout", cfg.timeout))
    t0 = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        return _ProviderResult(
            text=None, outcome=f"error:{exc}",
            call_s=0.0, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    # Poll so we can honour abort_event and enforce timeout
    while proc.poll() is None:
        if abort_event is not None and abort_event.is_set():
            proc.terminate()
            proc.wait()
            return _ProviderResult(
                text=None, outcome="aborted",
                call_s=time.monotonic() - t0,
                inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if time.monotonic() - t0 > timeout:
            proc.terminate()
            proc.wait()
            return _ProviderResult(
                text=None, outcome="timeout",
                call_s=time.monotonic() - t0,
                inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        time.sleep(_POLL_S)

    call_s = time.monotonic() - t0
    stdout, stderr = proc.communicate()

    if proc.returncode != 0:
        detail = stderr.strip()[:120] if stderr.strip() else f"exit {proc.returncode}"
        return _ProviderResult(
            text=None, outcome=f"error:{detail}",
            call_s=call_s, inference_s=call_s, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        # Unexpected: fall back to raw stdout as the response
        text = stdout.strip()
        return _ProviderResult(
            text=text or None,
            outcome="success" if text else "error:empty_response",
            call_s=call_s, inference_s=call_s, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    if body.get("is_error"):
        detail = body.get("result", "unknown error")[:120]
        return _ProviderResult(
            text=None, outcome=f"error:{detail}",
            call_s=call_s, inference_s=call_s, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    text = (body.get("result") or "").strip()

    # claude --output-format json exposes timing and usage when available
    duration_api_ms   = body.get("duration_api_ms", 0)
    inference_s       = duration_api_ms / 1000.0 if duration_api_ms else call_s
    usage             = body.get("usage", {})
    prompt_tokens     = usage.get("input_tokens")
    response_tokens   = usage.get("output_tokens")

    return _ProviderResult(
        text=text or None,
        outcome="success" if text else "error:empty_response",
        call_s=call_s, inference_s=inference_s, load_s=0.0,
        prompt_tokens=prompt_tokens, response_tokens=response_tokens,
    )
