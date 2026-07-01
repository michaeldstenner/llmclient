import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from llmclient._keys import _load_config


_SECTIONS = ("aip", "openai_compatible", "openai")


@dataclass(frozen=True)
class AIPConfig:
    base_url: str
    api_key: str
    source: str


@dataclass(frozen=True)
class ModelTest:
    model: str
    ok: bool
    outcome: str
    elapsed_s: float


def _api_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _endpoint(base_url: str, path: str) -> str:
    return f"{_api_root(base_url)}/{path.lstrip('/')}"


def _resolve_config(args) -> AIPConfig:
    if args.url:
        base_url = args.url.rstrip("/")
        source = "--url"
    else:
        env_url = ""
        source = ""
        for name in ("AIP_BASE_URL", "AIP_URL", "OPENAI_BASE_URL"):
            env_url = os.environ.get(name, "")
            if env_url:
                source = name
                break
        if env_url:
            base_url = env_url.rstrip("/")
        else:
            cfg = _load_config()
            base_url = ""
            source = ""
            for section in _SECTIONS:
                value = cfg.get(section, {}).get("url", "")
                if value:
                    base_url = value.rstrip("/")
                    source = f"config:{section}.url"
                    break

    if not base_url:
        raise SystemExit(
            "No AIP URL configured. Set --url, AIP_BASE_URL, or "
            "aip.url/openai_compatible.url in llmclient config.yaml."
        )

    credential = args.api_key or os.environ.get("AIP_" + "API_KEY", "")
    if not credential:
        cfg = _load_config()
        for section in _SECTIONS:
            value = cfg.get(section, {}).get("api_key", "")
            if value:
                credential = value
                break
    if not credential:
        credential = os.environ.get("OPENAI_" + "API_KEY", "")

    return AIPConfig(base_url=base_url, api_key=credential, source=source)


def _headers(cfg: AIPConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return headers


def _read_json(req: urllib.request.Request, timeout: int) -> dict:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def list_models(cfg: AIPConfig, timeout: int) -> list[str]:
    req = urllib.request.Request(
        _endpoint(cfg.base_url, "models"),
        headers=_headers(cfg),
        method="GET",
    )
    body = _read_json(req, timeout)
    models: list[str] = []
    for item in body.get("data", []):
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return sorted(set(models))


def test_model(cfg: AIPConfig, model: str, timeout: int) -> ModelTest:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "temperature": 0,
        "max_tokens": 8,
    }
    req = urllib.request.Request(
        _endpoint(cfg.base_url, "chat/completions"),
        data=json.dumps(payload).encode(),
        headers=_headers(cfg),
    )
    t0 = time.monotonic()
    try:
        body = _read_json(req, timeout)
        elapsed_s = time.monotonic() - t0
    except (TimeoutError, socket.timeout):
        return ModelTest(model, False, "timeout", time.monotonic() - t0)
    except urllib.error.HTTPError as exc:
        return ModelTest(model, False, f"http_{exc.code}", time.monotonic() - t0)
    except urllib.error.URLError:
        return ModelTest(model, False, "error:unreachable", time.monotonic() - t0)
    except Exception as exc:
        return ModelTest(model, False, f"error:{exc}", time.monotonic() - t0)

    choices = body.get("choices") or []
    if not choices:
        return ModelTest(model, False, "error:no_choices", elapsed_s)
    return ModelTest(model, True, "success", elapsed_s)


def _print_urls(cfg: AIPConfig) -> None:
    print(f"base URL:   {cfg.base_url}  ({cfg.source})")
    print(f"models URL: {_endpoint(cfg.base_url, 'models')}")
    print(f"chat URL:   {_endpoint(cfg.base_url, 'chat/completions')}")


def _cmd_list(args) -> int:
    cfg = _resolve_config(args)
    _print_urls(cfg)
    print()
    try:
        models = list_models(cfg, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"list failed: http_{exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError:
        print("list failed: error:unreachable", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"list failed: error:{exc}", file=sys.stderr)
        return 1

    if not models:
        print("No models returned.")
        return 1

    print(f"Offered models ({len(models)}):")
    for model in models:
        print(f"  {model}")
    return 0


def _cmd_test(args) -> int:
    cfg = _resolve_config(args)
    _print_urls(cfg)
    print()

    if args.test == "all":
        try:
            models = list_models(cfg, args.timeout)
        except Exception as exc:
            print(f"could not list models for --test all: {exc}", file=sys.stderr)
            return 1
    else:
        models = [args.test]

    if not models:
        print("No models to test.")
        return 1

    print(f"Testing {len(models)} model(s):")
    results = [test_model(cfg, model, args.timeout) for model in models]
    width = max(len(r.model) for r in results)
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(
            f"  {status:<4} {result.model:<{width}}  "
            f"{result.outcome:<18} {result.elapsed_s:.2f}s"
        )
    return 0 if all(r.ok for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aip_models",
        description="List and test AIP/OpenAI-compatible models.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-l", "--list", action="store_true", help="list offered models")
    mode.add_argument(
        "-t", "--test", metavar="MODEL|all",
        help="test one model, or all offered models",
    )
    parser.add_argument("--url", help="AIP base URL; may include /v1")
    parser.add_argument("--api-key", help="AIP API key; defaults to config/env")
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="HTTP timeout in seconds (default: 30)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        return _cmd_list(args)
    return _cmd_test(args)


if __name__ == "__main__":
    raise SystemExit(main())
