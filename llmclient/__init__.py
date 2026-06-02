from dataclasses import dataclass, field, replace as _dc_replace
import threading

from ._config import configure

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
    queue_mode:           str        = "cooperative"
    queue_timeout:        float|None = None
    queue_stall_timeout:  float|None = None
    priority:             int        = 50
    caller_max:          int        = 4
    first_token_timeout: int|None   = None
    generation_timeout:  int|None   = None
    retries:             int        = 0
    retry_delay:         int        = 15
    circuit_n:           int        = 0
    circuit_key:         str        = ""
    circuit_cooldown_s:  float      = 120.0
    circuit_triggers:    tuple      = (
        "timeout:first_token",
        "error:unreachable",
    )
    # Futility circuit breaker (opt-in; see docs/futility-circuit-breaker.md).
    # circuit_mode="count" (default) keeps the consecutive-failure counter
    # above.  "futility" uses the leaky-LLR breaker driven by grace_s /
    # deadline_s and a per-provider FutilitySensor.
    circuit_mode:        str        = "count"
    grace_s:             float      = 0.0
    deadline_s:          float|None = None
    ps_probe:            bool       = False
    ps_url:              str        = ""
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
    queue_snapshot:  list[dict] | None = None

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
        from ._queue import (
            acquire, release, circuit_check, circuit_record,
            futility_check, futility_update,
        )
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

        # Circuit breaker check — once before all attempts.  Two modes:
        # "count" (consecutive-failure counter) and "futility" (leaky-LLR).
        use_futility = (
            getattr(cfg, "circuit_mode", "count") == "futility"
            and bool(cfg.log_caller)
        )
        use_count = (
            not use_futility and cfg.circuit_n > 0 and bool(cfg.log_caller)
        )

        sensor   = None
        is_probe = False
        if use_futility:
            from ._sensor import get_sensor
            sensor = get_sensor(cfg.provider, cfg)
            check  = futility_check(cfg, sensor)
            if check == "open":
                result = LLMResult(
                    text=None, outcome="circuit_futile",
                    total_s=0.0, queue_wait_s=0.0, call_s=0.0,
                    inference_s=0.0, load_s=0.0,
                    prompt_chars=prompt_chars, response_chars=0,
                    prompt_tokens=None, response_tokens=None,
                )
                write_log(cfg, operation, result, context)
                return result
            is_probe = (check == "probe")
        elif use_count:
            check = circuit_check(cfg)
            if check == "open":
                result = LLMResult(
                    text=None, outcome="circuit_open",
                    total_s=0.0, queue_wait_s=0.0, call_s=0.0,
                    inference_s=0.0, load_s=0.0,
                    prompt_chars=prompt_chars, response_chars=0,
                    prompt_tokens=None, response_tokens=None,
                )
                write_log(cfg, operation, result, context)
                return result
            is_probe = (check == "probe")

        # In futility mode, deadline_s caps the queue-wait phase (reusing the
        # queue_timeout ceiling) and grace_s gates the stall ("not advancing")
        # bail.  The active call is still bounded by first_token/generation
        # timeouts.
        acquire_cfg   = cfg
        acquire_grace = 0.0
        if use_futility:
            acquire_grace = cfg.grace_s or 0.0
            if cfg.deadline_s is not None:
                qt = (cfg.deadline_s if cfg.queue_timeout is None
                      else min(cfg.queue_timeout, cfg.deadline_s))
                acquire_cfg = _dc_replace(cfg, queue_timeout=qt)

        result = None
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(cfg.retry_delay)

            queue_wait_s = 0.0
            queue_id     = None
            if cfg.queue_mode == "cooperative" and cfg.provider == "ollama":
                queue_id, queue_wait_s, queue_reason, queue_snap = acquire(
                    acquire_cfg, self._abort, grace_s=acquire_grace)
                if queue_id is None:
                    outcome = (
                        "aborted"             if queue_reason == "aborted"
                        else "timeout:queue_stall" if queue_reason == "queue_stalled"
                        else "timeout:queue_wait"
                    )
                    result = LLMResult(
                        text=None, outcome=outcome,
                        total_s=round(queue_wait_s, 3),
                        queue_wait_s=round(queue_wait_s, 3),
                        call_s=0.0, inference_s=0.0, load_s=0.0,
                        prompt_chars=prompt_chars, response_chars=0,
                        prompt_tokens=None, response_tokens=None,
                        queue_snapshot=queue_snap,
                    )
                    break

            try:
                pr = dispatch(
                    system, user, cfg, self._url, self._api_key, self._abort
                )
            finally:
                if queue_id is not None:
                    release(queue_id, cfg.model)

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

        if result is not None:
            if use_futility:
                from ._sensor import CallContext
                snap    = getattr(result, "queue_snapshot", None)
                running = (
                    sum(1 for r in snap if r.get("status") == "running")
                    if snap else 0
                )
                ctx = CallContext(
                    elapsed_s=result.total_s,
                    time_remaining=(
                        None if cfg.deadline_s is None
                        else max(0.0, cfg.deadline_s - result.total_s)
                    ),
                    last_outcome=result.outcome,
                    running_jobs=running,
                )
                futility_update(cfg, sensor, result.outcome, ctx, is_probe)
            elif use_count:
                circuit_record(cfg, result.outcome, is_probe=is_probe)

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
            queue_id, queue_wait_s, queue_reason, _snap = acquire(cfg, self._abort)
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
                write_log(cfg, operation, result, context)
                return result

        try:
            pr = dispatch_embed(text, cfg, self._url, self._api_key)
        finally:
            if queue_id is not None:
                release(queue_id, cfg.model)

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
    def claude_p(
        cls,
        model: str = "",
        *,
        abort_event: threading.Event | None = None,
        **kwargs,
    ) -> "LLMClient":
        """Alias for claude_code(); preferred name — 'claude -p' (print mode)."""
        return cls(
            LLMConfig(provider="claude_p", model=model, queue_mode="off", **kwargs),
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
        """Kept for backwards compatibility; prefer claude_p()."""
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
