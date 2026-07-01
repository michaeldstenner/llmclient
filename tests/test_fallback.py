from unittest.mock import patch

import pytest

from llmclient import FallbackLLMClient, LLMConfig
from llmclient.providers import _EmbedProviderResult, _ProviderResult


def _cfg(model: str) -> LLMConfig:
    return LLMConfig(provider="ollama", model=model, queue_mode="off")


def _provider_result(outcome: str, text: str | None = None) -> _ProviderResult:
    return _ProviderResult(
        text=text,
        outcome=outcome,
        call_s=0.0,
        inference_s=0.0,
        load_s=0.0,
        prompt_tokens=None,
        response_tokens=None,
    )


def _embed_result(outcome: str, vector: list[float] | None = None) -> _EmbedProviderResult:
    return _EmbedProviderResult(
        vector=vector,
        outcome=outcome,
        call_s=0.0,
        load_s=0.0,
        prompt_tokens=None,
    )


def test_fallback_call_tries_next_on_default_failure():
    seen = []

    def dispatch(system, user, cfg, resolved_url, resolved_api_key, abort_event):
        seen.append(cfg.model)
        if cfg.model == "primary":
            return _provider_result("http_500")
        return _provider_result("success", "ok")

    client = FallbackLLMClient([_cfg("primary"), _cfg("backup")])
    with patch("llmclient.providers.dispatch", side_effect=dispatch):
        result = client.call("hello")

    assert result.outcome == "success"
    assert result.text == "ok"
    assert seen == ["primary", "backup"]
    assert [a.cfg.model for a in client.last_attempts] == ["primary", "backup"]


def test_fallback_call_stops_on_non_fallback_failure():
    seen = []

    def dispatch(system, user, cfg, resolved_url, resolved_api_key, abort_event):
        seen.append(cfg.model)
        return _provider_result("http_404")

    client = FallbackLLMClient([_cfg("missing"), _cfg("backup")])
    with patch("llmclient.providers.dispatch", side_effect=dispatch):
        result = client.call("hello")

    assert result.outcome == "http_404"
    assert seen == ["missing"]
    assert [a.cfg.model for a in client.last_attempts] == ["missing"]


def test_fallback_conditions_are_configurable():
    seen = []

    def dispatch(system, user, cfg, resolved_url, resolved_api_key, abort_event):
        seen.append(cfg.model)
        if cfg.model == "primary":
            return _provider_result("http_404")
        return _provider_result("success", "ok")

    client = FallbackLLMClient(
        [_cfg("primary"), _cfg("backup")],
        fallback_on=("http_4*",),
    )
    with patch("llmclient.providers.dispatch", side_effect=dispatch):
        result = client.call("hello")

    assert result.outcome == "success"
    assert seen == ["primary", "backup"]


def test_fallback_condition_accepts_single_string():
    client = FallbackLLMClient([_cfg("primary")], fallback_on="http_5*")
    assert client.fallback_on == ("http_5*",)


def test_fallback_requires_at_least_one_config():
    with pytest.raises(ValueError, match="at least one config"):
        FallbackLLMClient([])


def test_fallback_embed_tries_next_on_default_failure():
    seen = []

    def dispatch_embed(text, cfg, resolved_url, resolved_api_key):
        seen.append(cfg.model)
        if cfg.model == "primary":
            return _embed_result("error:unreachable")
        return _embed_result("success", [1.0, 2.0])

    client = FallbackLLMClient([_cfg("primary"), _cfg("backup")])
    with patch("llmclient.providers.dispatch_embed", side_effect=dispatch_embed):
        result = client.embed("hello")

    assert result.outcome == "success"
    assert result.vector == [1.0, 2.0]
    assert seen == ["primary", "backup"]
