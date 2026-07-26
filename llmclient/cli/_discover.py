"""`llmc providers` and `llmc models` — what can this machine call?

Intended as much for an agent as for a human: `--json` on either command
emits the whole picture in one shot.  API keys are never printed; only
where a key came from.
"""
import json
import sys

from .._discovery import (
    all_provider_status,
    catalog,
    configured_models,
    list_provider_models,
    provider_status,
)


def _key_cell(status: dict) -> str:
    if not status["needs_key"]:
        return "none needed"
    if status["has_key"]:
        return status["key_source"]
    return "MISSING"


def cmd_providers(args) -> None:
    statuses = all_provider_status()
    if args.json:
        print(json.dumps(statuses, indent=2))
        return

    width = max(len(s["provider"]) for s in statuses)
    url_w = max(len(s["url"]) for s in statuses) or 3
    print(f"  {'provider':<{width}}  {'url':<{url_w}}  key")
    for s in statuses:
        mark = "*" if s["usable"] else " "
        url  = s["url"] or "(unset)"
        print(
            f"{mark} {s['provider']:<{width}}  {url:<{url_w}}  {_key_cell(s)}"
        )
    print()
    usable = [s["provider"] for s in statuses if s["usable"]]
    print(f"* usable now: {', '.join(usable) if usable else '(none)'}")

    missing = [
        s for s in statuses
        if s["url"] and s["needs_key"] and not s["has_key"]
    ]
    for s in missing:
        env = s["env_var"] or "—"
        print(
            f"  {s['provider']}: no key. Set {env}, or add one with "
            f"`security add-generic-password -s llmclient:{s['provider']} "
            f"-a default -w`"
        )


def _print_named(named: dict) -> None:
    if not named:
        print("Named models: (none — add a `models:` section to "
              "~/.config/llmclient/config.yaml)")
        return
    width = max(len(n) for n in named)
    print("Named models:")
    for name, stanza in sorted(named.items()):
        provider = stanza.get("provider", "") or "(inferred)"
        knobs = ", ".join(
            f"{k}={v}" for k, v in sorted(stanza.items())
            if k not in ("provider", "model")
        )
        line = f"  {name:<{width}}  {provider:<18} {stanza.get('model', '')}"
        print(f"{line}   [{knobs}]" if knobs else line)


def _print_catalog(models: list[dict], limit: int, pattern: str) -> None:
    shown = models if limit <= 0 else models[:limit]
    width = max((len(m["id"]) for m in shown), default=0)
    for m in shown:
        flag   = "*" if m["loaded"] else " "
        detail = f"  {m['detail']}" if m["detail"] else ""
        print(f"  {flag} {m['id']:<{width}}{detail}")
    hidden = len(models) - len(shown)
    if hidden > 0:
        hint = "--all" if not pattern else "--all (with the current filter)"
        print(f"  ... {hidden} more — {hint} to see them")


def cmd_models(args) -> None:
    pattern = (args.filter or "").lower()

    if args.json:
        data = catalog(timeout=args.timeout, live=not args.no_probe)
        if pattern:
            for p in data["providers"]:
                p["models"] = [
                    m for m in p["models"] if pattern in m["id"].lower()
                ]
        print(json.dumps(data, indent=2))
        return

    # One provider: full catalog.
    if args.provider:
        status = provider_status(args.provider)
        print(f"{args.provider}  {status['url'] or '(no url)'}  "
              f"key: {_key_cell(status)}")
        models, error = list_provider_models(
            args.provider, timeout=args.timeout
        )
        if error:
            print(f"  cannot list: {error}", file=sys.stderr)
            raise SystemExit(1)
        if pattern:
            models = [m for m in models if pattern in m["id"].lower()]
        print(f"  {len(models)} model(s)"
              f"{' matching ' + repr(args.filter) if pattern else ''}:")
        _print_catalog(models, 0 if args.all else args.limit, pattern)
        return

    # Overview: named models, then a per-provider count.
    _print_named(configured_models())
    print()
    statuses = all_provider_status()
    width = max(len(s["provider"]) for s in statuses)
    print("Providers:")
    for s in statuses:
        if not s["usable"]:
            why = "not configured (no url)" if not s["url"] else "no key"
            print(f"  {s['provider']:<{width}}  — {why}")
            continue
        if args.no_probe:
            print(f"  {s['provider']:<{width}}  ready ({_key_cell(s)})")
            continue
        models, error = list_provider_models(
            s["provider"], timeout=args.timeout
        )
        if error:
            print(f"  {s['provider']:<{width}}  ! {error}")
            continue
        loaded = sum(1 for m in models if m["loaded"])
        extra  = f" ({loaded} loaded)" if loaded else ""
        print(f"  {s['provider']:<{width}}  {len(models)} models{extra}")
    print()
    print("`llmc models -p <provider>` lists one catalog; "
          "add --filter TEXT to narrow it.")
