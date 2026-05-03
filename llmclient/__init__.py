from dataclasses import dataclass, field
import threading


@dataclass(frozen=True)
class LLMConfig:
    provider:     str
    model:        str
    url:          str  = ""
    timeout:      int  = 60
    api_key:      str  = ""
    keep_alive:   str  = "60m"
    num_ctx_auto: bool = True
    log_caller:   str  = ""
    queue_mode:   str  = "cooperative"
    priority:     int  = 50
    caller_max:   int  = 4
    extra_params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResult:
    text:            str | None
    outcome:         str        # "success"|"timeout"|"http_NNN"|"error"|"aborted"
    total_s:         float      # queue_wait_s + call_s
    queue_wait_s:    float
    call_s:          float
    inference_s:     float      # token generation (Ollama body field; else == call_s)
    load_s:          float      # model cold-start (Ollama body field; else 0.0)
    prompt_chars:    int
    response_chars:  int
    prompt_tokens:   int | None
    response_tokens: int | None


class LLMClient:
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        abort_event: threading.Event | None = None,
    ) -> None:
        self._cfg   = cfg
        self._abort = abort_event
        from ._keys import resolve_url, resolve_api_key
        self._url     = resolve_url(cfg.provider, cfg.url)
        self._api_key = resolve_api_key(cfg.provider, cfg.api_key)

    def call(
        self,
        user: str,
        system: str = "",
        *,
        operation: str = "call",
        context: dict | None = None,
    ) -> LLMResult:
        from ._queue import acquire, release
        from .providers import dispatch
        from ._log import write_log

        cfg          = self._cfg
        prompt_chars = len(system) + len(user)

        queue_wait_s = 0.0
        queue_id     = None
        if cfg.queue_mode == "cooperative" and cfg.provider == "ollama":
            queue_id, queue_wait_s = acquire(cfg, self._abort)
            if queue_id is None:
                result = LLMResult(
                    text=None, outcome="aborted",
                    total_s=round(queue_wait_s, 3),
                    queue_wait_s=round(queue_wait_s, 3),
                    call_s=0.0, inference_s=0.0, load_s=0.0,
                    prompt_chars=prompt_chars, response_chars=0,
                    prompt_tokens=None, response_tokens=None,
                )
                if cfg.log_caller:
                    write_log(cfg, operation, result, context)
                return result

        try:
            pr = dispatch(system, user, cfg, self._url, self._api_key, self._abort)
        finally:
            if queue_id is not None:
                release(queue_id)

        result = LLMResult(
            text=pr.text,
            outcome=pr.outcome,
            total_s=round(queue_wait_s + pr.call_s, 3),
            queue_wait_s=round(queue_wait_s, 3),
            call_s=round(pr.call_s, 3),
            inference_s=round(pr.inference_s, 3),
            load_s=round(pr.load_s, 3),
            prompt_chars=prompt_chars,
            response_chars=len(pr.text) if pr.text else 0,
            prompt_tokens=pr.prompt_tokens,
            response_tokens=pr.response_tokens,
        )
        if cfg.log_caller:
            write_log(cfg, operation, result, context)
        return result

    # ---------- convenience constructors ----------

    @classmethod
    def ollama(
        cls,
        model: str,
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        return cls(
            LLMConfig(provider="ollama", model=model, **kwargs),
            abort_event=abort_event,
        )

    @classmethod
    def anthropic(
        cls,
        model: str,
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        return cls(
            LLMConfig(provider="anthropic", model=model, queue_mode="off", **kwargs),
            abort_event=abort_event,
        )

    @classmethod
    def openai_compatible(
        cls,
        model: str,
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        return cls(
            LLMConfig(provider="openai_compatible", model=model, queue_mode="off", **kwargs),
            abort_event=abort_event,
        )

    @classmethod
    def from_profile(
        cls,
        provider: str,
        model: str,
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        queue_mode = "cooperative" if provider == "ollama" else "off"
        return cls(
            LLMConfig(provider=provider, model=model, queue_mode=queue_mode, **kwargs),
            abort_event=abort_event,
        )
