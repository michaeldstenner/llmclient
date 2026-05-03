import sys
from .. import LLMClient, LLMConfig


def cmd_call(args) -> None:
    prompt   = " ".join(args.prompt)
    provider = args.provider
    model    = args.model
    system   = args.system
    timeout  = args.timeout
    no_queue = getattr(args, "no_queue", False)

    queue_mode = "off" if no_queue else (
        "cooperative" if provider == "ollama" else "off"
    )

    cfg    = LLMConfig(
        provider=provider, model=model,
        timeout=timeout, queue_mode=queue_mode,
    )
    client = LLMClient(cfg)

    print(f"calling {provider}/{model}...", flush=True)
    result = client.call(prompt, system=system)

    print()
    if result.outcome == "success":
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
