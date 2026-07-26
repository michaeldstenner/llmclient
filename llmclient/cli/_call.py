import dataclasses
import json
import sys
from .. import LLMClient, LLMConfig


def cmd_call(args) -> None:
    from .._discovery import configured_models

    prompt    = " ".join(args.prompt)
    model     = args.model
    system    = args.system
    timeout   = args.timeout
    no_queue  = getattr(args, "no_queue", False)
    emit_json = getattr(args, "json", False)

    # A bare -m may name a stanza in config.yaml's `models:` section; an
    # explicit -p means the caller is naming a provider's model id.
    if args.provider is None and model in configured_models():
        overrides = {"log_caller": "llmc"}
        if timeout is not None:
            overrides["timeout"] = timeout
        if no_queue:
            overrides["queue_mode"] = "off"
        client   = LLMClient.from_name(model, **overrides)
        provider = client.cfg.provider
        model    = client.cfg.model
    else:
        provider   = args.provider or "ollama"
        queue_mode = "off" if no_queue else (
            "cooperative" if provider == "ollama" else "off"
        )
        cfg = LLMConfig(
            provider=provider, model=model,
            timeout=60 if timeout is None else timeout,
            queue_mode=queue_mode,
            log_caller="llmc",
        )
        client = LLMClient(cfg)

    print(f"calling {provider}/{model}...", flush=True)
    result = client.call(prompt, system=system)

    if emit_json:
        print(json.dumps(dataclasses.asdict(result), indent=2))
        return

    print()
    if result.is_success:
        print(result.text or "(empty response)")
    else:
        print(f"[{result.outcome}]", file=sys.stderr)

    print()
    tok_str = ""
    if result.prompt_tokens is not None:
        tok_str = (
            f"  ({result.prompt_tokens} prompt"
            f" + {result.response_tokens} response tokens)"
        )
    print(f"outcome:   {result.outcome}")
    print(
        f"timing:    {result.queue_wait_s:.2f}s queue"
        f"  +  {result.inference_s:.2f}s inference"
        f"  =  {result.total_s:.2f}s total{tok_str}"
    )
