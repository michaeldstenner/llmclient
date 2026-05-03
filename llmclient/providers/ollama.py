import json
import math
import socket
import threading
import time
import urllib.request
import urllib.error

from . import _ProviderResult, _EmbedProviderResult

_ABORT_CHECK_S = 1.0   # patched to a small value in tests


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


def _get_loaded_ctx(base_url: str, model: str) -> int | None:
    """Return the context_length of the currently loaded model, or None."""
    try:
        req = urllib.request.Request(base_url + "/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        prefix = model.split(":")[0]
        for m in data.get("models", []):
            name = m.get("model", "")
            if name == model or name.startswith(prefix):
                ctx = m.get("context_length")
                return int(ctx) if ctx is not None else None
    except Exception:
        pass
    return None


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

    # Keys explicitly handled; not passed through below
    _HANDLED = {"temperature", "num_predict", "num_ctx",
                "timeout", "keep_alive", "think"}
    # extra_params keys that belong in the Ollama options dict
    _OPTIONS_KEYS = {
        "top_k", "top_p", "seed", "repeat_penalty",
        "mirostat", "mirostat_eta", "mirostat_tau",
        "num_batch", "num_thread", "num_keep", "stop",
        "tfs_z", "typical_p", "penalize_newline",
    }

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

    # Upward-only ratchet: never request a smaller context than what's
    # already loaded — a smaller request forces Ollama to reload the
    # model, blocking behind any in-progress inference at the larger size.
    if "num_ctx" in options:
        loaded_ctx = _get_loaded_ctx(base_url, model)
        if loaded_ctx is not None:
            options["num_ctx"] = max(options["num_ctx"], loaded_ctx)

    think = cfg.extra_params.get("think", False)

    payload = {
        "model":      model,
        "prompt":     full_prompt,
        "stream":     False,
        "keep_alive": keep_alive,
        "think":      think,
        "options":    options,
    }

    # Pass unrecognized extra_params to the right level:
    # known options keys → options{}; anything else is a top-level
    # Ollama payload key (e.g. chat_template_kwargs, format, system).
    for key, val in cfg.extra_params.items():
        if key in _HANDLED:
            continue
        if key in _OPTIONS_KEYS:
            options[key] = val
        else:
            payload[key] = val

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
        t.join(_ABORT_CHECK_S)
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
                outcome = "timeout:model_loaded_but_slow"
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


def embed_ollama(
    text: str,
    cfg,
    base_url: str,
) -> _EmbedProviderResult:
    model      = cfg.model
    timeout    = int(cfg.extra_params.get("timeout", cfg.timeout))
    keep_alive = cfg.extra_params.get("keep_alive", cfg.keep_alive)

    payload = {
        "model":      model,
        "input":      text,
        "keep_alive": keep_alive,
    }

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            base_url + "/api/embed",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (TimeoutError, socket.timeout):
        return _EmbedProviderResult(
            vector=None, outcome="timeout",
            call_s=time.monotonic() - t0, load_s=0.0, prompt_tokens=None,
        )
    except urllib.error.HTTPError as exc:
        return _EmbedProviderResult(
            vector=None, outcome=f"http_{exc.code}",
            call_s=time.monotonic() - t0, load_s=0.0, prompt_tokens=None,
        )
    except urllib.error.URLError:
        return _EmbedProviderResult(
            vector=None, outcome="error:unreachable",
            call_s=time.monotonic() - t0, load_s=0.0, prompt_tokens=None,
        )
    except Exception as exc:
        return _EmbedProviderResult(
            vector=None, outcome=f"error:{exc}",
            call_s=time.monotonic() - t0, load_s=0.0, prompt_tokens=None,
        )

    call_s = time.monotonic() - t0
    embeddings = body.get("embeddings", [])
    vector = embeddings[0] if embeddings else None
    load_s = body.get("load_duration", 0) / 1e9
    prompt_tokens = body.get("prompt_eval_count")

    return _EmbedProviderResult(
        vector=vector,
        outcome="success" if vector is not None else "error:empty_response",
        call_s=call_s, load_s=load_s, prompt_tokens=prompt_tokens,
    )
