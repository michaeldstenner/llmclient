from dataclasses import dataclass, field, replace as _dc_replace
import threading

_RETRYABLE = {
    "timeout:generation", "error:unreachable",
    # legacy names kept for any surviving callers
    "timeout", "timeout:model_loaded_but_slow", "timeout:model_not_loaded",
}


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
    queue_mode:          str        = "cooperative"
    queue_timeout:       float|None = None
    priority:            int        = 50
    caller_max:          int        = 4
    first_token_timeout: int|None   = None
    generation_timeout:  int|None   = None
    retries:             int        = 0
    retry_delay:         int        = 15
    circuit_n:           int        = 0
    circuit_cooldown_s:  float      = 120.0
    circuit_triggers:    tuple      = (
        "timeout:queue_wait",
        "timeout:first_token",
        "error:unreachable",
    )
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

    @property
    def is_success(self) -> bool:
        return self.outcome == "success"


@dataclass(frozen=True)
class EmbedResult:
    vector:          list[float] | None
    outcome:         str
    total_s:         float
    queue_wait_s:    float
    call_s:          float
    load_s:          float
    prompt_chars:    int
    prompt_tokens:   int | None
    # write_log() compatibility fields
    response_chars:  int = 0
    inference_s:     float = 0.0
    response_tokens: int | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome == "success"


def _infer_provider(url: str) -> str:
    u = url.lower()
    if not u or "11434" in u or "ollama" in u:
        return "ollama"
    if "anthropic.com" in u:
        return "anthropic"
    return "openai_compatible"


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

    @property
    def cfg(self) -> LLMConfig:
        return self._cfg

    def call(
        self,
        user: str,
        system: str = "",
        *,
        operation: str = "call",
        context: dict | None = None,
        extra_params: dict | None = None,
    ) -> LLMResult:
        import time
        from ._queue import acquire, release, circuit_check, circuit_record
        from .providers import dispatch
        from ._log import write_log

        cfg = (
            _dc_replace(
                self._cfg,
                extra_params={**self._cfg.extra_params, **extra_params},
            )
            if extra_params
            else self._cfg
        )
        prompt_chars = len(system) + len(user)
        attempts     = cfg.retries + 1

        # Circuit breaker check — once before all attempts.
        is_probe = False
        if cfg.circuit_n > 0 and cfg.log_caller:
            check = circuit_check(cfg)
            if check == "open":
                result = LLMResult(
                    text=None, outcome="circuit_open",
                    total_s=0.0, queue_wait_s=0.0, call_s=0.0,
                    inference_s=0.0, load_s=0.0,
                    prompt_chars=prompt_chars, response_chars=0,
                    prompt_tokens=None, response_tokens=None,
                )
                if cfg.log_caller:
                    write_log(cfg, operation, result, context)
                return result
            is_probe = (check == "probe")

        result = None
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(cfg.retry_delay)

            queue_wait_s = 0.0
            queue_id     = None
            if cfg.queue_mode == "cooperative" and cfg.provider == "ollama":
                queue_id, queue_wait_s, queue_reason = acquire(cfg, self._abort)
                if queue_id is None:
                    outcome = (
                        "aborted" if queue_reason == "aborted"
                        else "timeout:queue_wait"
                    )
                    result = LLMResult(
                        text=None, outcome=outcome,
                        total_s=round(queue_wait_s, 3),
                        queue_wait_s=round(queue_wait_s, 3),
                        call_s=0.0, inference_s=0.0, load_s=0.0,
                        prompt_chars=prompt_chars, response_chars=0,
                        prompt_tokens=None, response_tokens=None,
                    )
                    break

            try:
                pr = dispatch(
                    system, user, cfg, self._url, self._api_key, self._abort
                )
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
            if result.outcome not in _RETRYABLE:
                break

        if cfg.circuit_n > 0 and cfg.log_caller and result is not None:
            circuit_record(cfg, result.outcome, is_probe=is_probe)

        if cfg.log_caller:
            write_log(cfg, operation, result, context)
        return result

    def embed(
        self,
        text: str,
        *,
        operation: str = "embed",
        context: dict | None = None,
    ) -> EmbedResult:
        from ._queue import acquire, release
        from .providers import dispatch_embed
        from ._log import write_log

        cfg          = self._cfg
        prompt_chars = len(text)

        queue_wait_s = 0.0
        queue_id     = None
        if cfg.queue_mode == "cooperative" and cfg.provider == "ollama":
            queue_id, queue_wait_s, queue_reason = acquire(cfg, self._abort)
            if queue_id is None:
                outcome = (
                    "aborted" if queue_reason == "aborted"
                    else "timeout:queue_wait"
                )
                result = EmbedResult(
                    vector=None, outcome=outcome,
                    total_s=round(queue_wait_s, 3),
                    queue_wait_s=round(queue_wait_s, 3),
                    call_s=0.0, load_s=0.0,
                    prompt_chars=prompt_chars, prompt_tokens=None,
                )
                if cfg.log_caller:
                    write_log(cfg, operation, result, context)
                return result

        try:
            pr = dispatch_embed(text, cfg, self._url, self._api_key)
        finally:
            if queue_id is not None:
                release(queue_id)

        result = EmbedResult(
            vector=pr.vector,
            outcome=pr.outcome,
            total_s=round(queue_wait_s + pr.call_s, 3),
            queue_wait_s=round(queue_wait_s, 3),
            call_s=round(pr.call_s, 3),
            load_s=round(pr.load_s, 3),
            prompt_chars=prompt_chars,
            prompt_tokens=pr.prompt_tokens,
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
    def claude_code(
        cls,
        model: str = "",
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        return cls(
            LLMConfig(provider="claude_code", model=model, queue_mode="off", **kwargs),
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

    @classmethod
    def from_dict(
        cls,
        d: dict,
        *,
        abort_event: threading.Event | None = None,
    ) -> "LLMClient":
        """Build a client from a flat config dict (e.g. a YAML stanza).

        `provider` is optional and inferred from `url` if absent.
        `queue_mode` defaults to "cooperative" for ollama, "off" otherwise.
        All other LLMConfig fields can be supplied as keys.
        """
        d        = dict(d)
        url      = d.get("url", "")
        provider = d.pop("provider", None) or _infer_provider(url)
        model    = d.pop("model")
        d.setdefault("queue_mode", "cooperative" if provider == "ollama" else "off")
        return cls(
            LLMConfig(provider=provider, model=model, **d),
            abort_event=abort_event,
        )
