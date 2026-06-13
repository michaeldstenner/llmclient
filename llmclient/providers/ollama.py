import json
import math
import queue as _queue_lib
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


def _stream_request(
    req: urllib.request.Request,
    first_token_timeout: float,
    generation_timeout: float,
    abort_event: threading.Event | None,
) -> dict:
    """
    Returns dict with keys:
      chunks        — list of parsed JSON objects received
      error         — exception or None
      timeout_phase — None | "first_token" | "generation" | "aborted"
    """
    chunk_q: _queue_lib.Queue = _queue_lib.Queue()
    exc_holder: list = [None]
    resp_holder: list = [None]
    closed: list = [False]

    def _cancel_upstream() -> None:
        """Tear down the HTTP connection so Ollama sees the client disconnect
        and CANCELS the generation, freeing the slot promptly.  Without this,
        an abandoned / timed-out request keeps generating server-side and
        holds a slot — the zombie-slot spiral that starves every other
        caller.  Must be called on every bail path."""
        if closed[0]:
            return
        closed[0] = True
        resp = resp_holder[0]
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _reader() -> None:
        try:
            urlopen_timeout = first_token_timeout + generation_timeout + 5
            resp = urllib.request.urlopen(req, timeout=urlopen_timeout)
            resp_holder[0] = resp
            if closed[0]:          # bailed during connect — abort immediately
                resp.close()
                return
            try:
                for raw in resp:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        chunk_q.put(json.loads(stripped))
                    except json.JSONDecodeError:
                        pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except Exception as exc:
            exc_holder[0] = exc
        finally:
            chunk_q.put(None)  # sentinel

    threading.Thread(target=_reader, daemon=True).start()

    try:
        first = chunk_q.get(timeout=first_token_timeout)
    except _queue_lib.Empty:
        _cancel_upstream()
        return {"chunks": [], "error": None, "timeout_phase": "first_token"}

    if first is None:
        return {"chunks": [], "error": exc_holder[0], "timeout_phase": None}

    chunks = [first]
    if first.get("done"):
        return {"chunks": chunks, "error": None, "timeout_phase": None}

    gen_deadline = time.monotonic() + generation_timeout

    while True:
        if abort_event is not None and abort_event.is_set():
            _cancel_upstream()
            return {"chunks": chunks, "error": None, "timeout_phase": "aborted"}

        remaining = gen_deadline - time.monotonic()
        if remaining <= 0:
            _cancel_upstream()
            return {"chunks": chunks, "error": None, "timeout_phase": "generation"}

        try:
            chunk = chunk_q.get(timeout=min(remaining, _ABORT_CHECK_S))
        except _queue_lib.Empty:
            if time.monotonic() >= gen_deadline:
                _cancel_upstream()
                return {"chunks": chunks, "error": None, "timeout_phase": "generation"}
            continue

        if chunk is None:
            if exc_holder[0] is not None:
                return {"chunks": chunks, "error": exc_holder[0], "timeout_phase": None}
            break

        chunks.append(chunk)
        if chunk.get("done"):
            break

    return {"chunks": chunks, "error": None, "timeout_phase": None}


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

    ftt_raw = cfg.extra_params.get("first_token_timeout", cfg.first_token_timeout)
    first_token_timeout = int(ftt_raw) if ftt_raw is not None else None

    gtt_raw = cfg.extra_params.get("generation_timeout", cfg.generation_timeout)
    generation_timeout  = int(gtt_raw) if gtt_raw is not None else timeout

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
        "stream":     False,   # overridden to True in streaming path below
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

    req = urllib.request.Request(
        base_url + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()

    # --- streaming path ---
    if first_token_timeout is not None:
        payload["stream"] = True
        sr = _stream_request(req, first_token_timeout, generation_timeout, abort_event)
        call_s = time.monotonic() - t0

        phase = sr["timeout_phase"]
        if phase == "aborted":
            return _ProviderResult(
                text=None, outcome="aborted",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if phase == "first_token":
            return _ProviderResult(
                text=None, outcome="timeout:first_token",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if phase == "generation":
            return _ProviderResult(
                text=None, outcome="timeout:generation",
                call_s=call_s, inference_s=0.0, load_s=0.0,
                prompt_tokens=None, response_tokens=None,
            )
        if sr["error"] is not None:
            exc = sr["error"]
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

        chunks = sr["chunks"]
        text = "".join(c.get("response", "") for c in chunks).strip()
        done = chunks[-1] if chunks else {}
        ns = 1e9
        load_s      = done.get("load_duration", 0) / ns
        inference_s = (
            done.get("prompt_eval_duration", 0) + done.get("eval_duration", 0)
        ) / ns
        return _ProviderResult(
            text=text, outcome="success",
            call_s=call_s, inference_s=inference_s, load_s=load_s,
            prompt_tokens=done.get("prompt_eval_count"),
            response_tokens=done.get("eval_count"),
        )

    # --- non-streaming path (original) ---
    result: dict = {}

    def _do_request() -> None:
        try:
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
            return _ProviderResult(
                text=None, outcome="timeout:generation",
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

    ns = 1e9
    load_s      = body.get("load_duration", 0) / ns
    inference_s = (
        body.get("prompt_eval_duration", 0) + body.get("eval_duration", 0)
    ) / ns

    return _ProviderResult(
        text=text, outcome="success",
        call_s=call_s, inference_s=inference_s, load_s=load_s,
        prompt_tokens=body.get("prompt_eval_count"),
        response_tokens=body.get("eval_count"),
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
