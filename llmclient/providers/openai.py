import json
import socket
import time
import urllib.request
import urllib.error

from . import _ProviderResult


def _extract_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("OpenAI response missing choices")

    choice  = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str) and part.strip():
                parts.append(part.strip())
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts)

    for candidate in (choice.get("text"), body.get("output_text")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    raise ValueError("OpenAI response missing textual content")


def call_openai(
    system: str,
    user: str,
    cfg,
    base_url: str,
    api_key: str,
) -> _ProviderResult:
    model       = cfg.model
    timeout     = int(cfg.extra_params.get("timeout", cfg.timeout))
    max_tokens  = int(cfg.extra_params.get("max_tokens", 1000))
    temperature = float(cfg.extra_params.get("temperature", 0))

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        call_s = time.monotonic() - t0

        text  = _extract_text(body)
        usage = body.get("usage", {})
        prompt_tokens   = usage.get("prompt_tokens")
        response_tokens = usage.get("completion_tokens")

        return _ProviderResult(
            text=text, outcome="success",
            call_s=call_s, inference_s=call_s, load_s=0.0,
            prompt_tokens=prompt_tokens, response_tokens=response_tokens,
        )
    except (TimeoutError, socket.timeout):
        call_s = time.monotonic() - t0
        return _ProviderResult(
            text=None, outcome="timeout",
            call_s=call_s, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )
    except urllib.error.HTTPError as exc:
        call_s = time.monotonic() - t0
        return _ProviderResult(
            text=None, outcome=f"http_{exc.code}",
            call_s=call_s, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )
    except urllib.error.URLError:
        call_s = time.monotonic() - t0
        return _ProviderResult(
            text=None, outcome="error:unreachable",
            call_s=call_s, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )
    except Exception as exc:
        call_s = time.monotonic() - t0
        return _ProviderResult(
            text=None, outcome=f"error:{exc}",
            call_s=call_s, inference_s=0.0, load_s=0.0,
            prompt_tokens=None, response_tokens=None,
        )
