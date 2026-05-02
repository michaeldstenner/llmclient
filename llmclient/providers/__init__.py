from dataclasses import dataclass


@dataclass
class _ProviderResult:
    text:            str | None
    outcome:         str        # "success"|"timeout"|"http_NNN"|"error"|"aborted"
    call_s:          float      # wall-clock from HTTP send to response
    inference_s:     float      # token generation (Ollama field; else == call_s)
    load_s:          float      # model cold-start (Ollama field; else 0.0)
    prompt_tokens:   int | None
    response_tokens: int | None


def dispatch(
    system: str,
    user: str,
    cfg,
    resolved_url: str,
    resolved_api_key: str,
    abort_event,
) -> _ProviderResult:
    provider = cfg.provider
    if provider == "ollama":
        from .ollama import call_ollama
        return call_ollama(system, user, cfg, resolved_url, abort_event)
    if provider in ("openai", "openai_compatible"):
        from .openai import call_openai
        return call_openai(system, user, cfg, resolved_url, resolved_api_key)
    if provider == "anthropic":
        from .anthropic import call_anthropic
        return call_anthropic(system, user, cfg, resolved_url, resolved_api_key)
    return _ProviderResult(
        text=None,
        outcome=f"unknown_provider:{provider!r}",
        call_s=0.0, inference_s=0.0, load_s=0.0,
        prompt_tokens=None, response_tokens=None,
    )
