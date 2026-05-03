import sys
import threading
import time
from .. import LLMClient, LLMConfig


def cmd_parallel(args) -> None:
    prompt   = " ".join(args.prompt)
    system   = args.system
    provider = args.provider
    model    = args.model
    n        = args.n
    timeout  = args.timeout

    results: list = [None] * n
    errors:  list = [None] * n

    def do_call(i: int) -> None:
        cfg    = LLMConfig(
            provider=provider, model=model,
            timeout=timeout, queue_mode="off",
        )
        client = LLMClient(cfg)
        t0 = time.monotonic()
        try:
            results[i] = (
                client.call(prompt, system=system),
                time.monotonic() - t0,
            )
        except Exception as exc:
            errors[i] = exc

    print(
        f"sending {n} concurrent requests"
        f" → {provider}/{model}  (queue bypassed)",
        flush=True,
    )
    wall_t0 = time.monotonic()
    threads = [
        threading.Thread(target=do_call, args=(i,), daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_s = time.monotonic() - wall_t0

    print()
    serial_est = 0.0
    all_ok     = True
    for i in range(n):
        if errors[i]:
            print(f"  #{i+1}  error  {errors[i]}", file=sys.stderr)
            all_ok = False
            continue
        result, wall_i = results[i]
        serial_est += result.call_s
        tok = ""
        if result.prompt_tokens is not None:
            tok = (
                f"  {result.prompt_tokens}"
                f"+{result.response_tokens} tok"
            )
        print(
            f"  #{i+1}  {result.outcome:<10}"
            f"  {wall_i:.1f}s wall"
            f"  {result.inference_s:.1f}s inf{tok}"
        )

    if not all_ok:
        return

    print()
    print(f"wall clock:  {wall_s:.1f}s")
    print(f"serial est:  {serial_est:.1f}s  (sum of call times)")
    if serial_est > 0 and wall_s > 0:
        speedup = serial_est / wall_s
        print(f"speedup:     {speedup:.2f}x")
        if speedup < 1.5:
            print("  → requests appear to be serialized")
        else:
            print("  → genuine parallelism confirmed")
