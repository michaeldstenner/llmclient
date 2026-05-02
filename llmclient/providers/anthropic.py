import json
import socket
import time
import urllib.request
import urllib.error

from . import _ProviderResult


def call_anthropic(
    system: str,
    user: str,
    cfg,
    base_url: str,
    api_key: str,
) -> _ProviderResult:
    model      = cfg.model
    timeout    = int(cfg.extra_params.get("timeout", cfg.timeout))
    max_tokens = int(cfg.extra_params.get("max_tokens", 1024))

    payload = {
        "model":      model,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   [{"role": "user", "content": user}],
    }
    headers = {
        "Content-Type":    "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            base_url + "/v1/messages",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        call_s = time.monotonic() - t0

        text            = body["content"][0]["text"].strip()
        usage           = body.get("usage", {})
        prompt_tokens   = usage.get("input_tokens")
        response_tokens = usage.get("output_tokens")

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
