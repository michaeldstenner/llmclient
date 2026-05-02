import json
import math
import socket
import threading
import time
import urllib.request
import urllib.error

from . import _ProviderResult


def _check_ollama_busy(base_url: str, model: str) -> bool:
    """True if Ollama is reachable and this model is currently loaded."""
    try:
        req = urllib.request.Request(base_url + "/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        loaded = [m.get("model", "") for m in data.get("models", [])]
        return any(m == model or m.startswith(model.split(":")[0]) for m in loaded)
    except Exception:
        return False


def call_ollama(
    system: str,
    user: str,
    cfg,
    base_url: str,
    abort_event: threading.Event | None,
) -> _ProviderResult:
    model       = cfg.model
    timeout     = int(cfg.extra_params.get("timeout", cfg.timeout))
    keep_alive  = cfg.extra_params.get("keep_alive", cfg.keep_alive)
    temperature = float(cfg.extra_params.get("temperature", 0))

    full_prompt = "\n\n---\n\n".join([system, user]) if system else user
    prompt_chars = len(system) + len(user)

    options: dict = {"temperature": temperature}

    # Temperature and other extra_params options
    for key in ("num_predict",):
        if key in cfg.extra_params:
            options[key] = cfg.extra_params[key]

    # Auto-size context window to avoid silent truncation
    if cfg.num_ctx_auto and "num_ctx" not in cfg.extra_params:
        estimated = int(prompt_chars / 3.5)
        required  = estimated + 512
        options["num_ctx"] = max(4096, 2 ** math.ceil(math.log2(max(required, 2))))
    elif "num_ctx" in cfg.extra_params:
        options["num_ctx"] = cfg.extra_params["num_ctx"]

    payload = {
        "model":      model,
        "prompt":     full_prompt,
        "stream":     False,
        "keep_alive": keep_alive,
        "think":      False,
        "options":    options,
    }

    result: dict = {}
    t0 = time.monotonic()

    def _do_request() -> None:
        try:
            req = urllib.request.Request(
                base_url + "/api/generate",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result["body"] = json.loads(resp.read())
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()
    while t.is_alive():
        t.join(1.0)
        if abort_event is not None and abort_event.is_set():
            call_s = time.monotonic() - t0
            return _ProviderResult(
                text=None, outcome="aborted",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )

    call_s = time.monotonic() - t0

    if "error" in result:
        exc = result["error"]
        if isinstance(exc, (TimeoutError, socket.timeout)):
            if _check_ollama_busy(base_url, model):
                outcome = f"timeout:model_loaded_but_slow"
            else:
                outcome = "timeout:model_not_loaded"
            return _ProviderResult(
                text=None, outcome=outcome,
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if isinstance(exc, urllib.error.HTTPError):
            return _ProviderResult(
                text=None, outcome=f"http_{exc.code}",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if isinstance(exc, urllib.error.URLError):
            return _ProviderResult(
                text=None, outcome="error:unreachable",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        return _ProviderResult(
            text=None, outcome=f"error:{exc}",
            call_s=call_s, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )

    body = result["body"]
    text = body.get("response", "").strip()

    # Ollama provides nanosecond timing fields in the response body.
    ns = 1e9
    load_s      = body.get("load_duration", 0) / ns
    inference_s = (
        body.get("prompt_eval_duration", 0) + body.get("eval_duration", 0)
    ) / ns

    prompt_tokens   = body.get("prompt_eval_count")
    response_tokens = body.get("eval_count")

    return _ProviderResult(
        text=text, outcome="success",
        call_s=call_s, inference_s=inference_s, load_s=load_s,
        prompt_tokens=prompt_tokens, response_tokens=response_tokens,
    )
