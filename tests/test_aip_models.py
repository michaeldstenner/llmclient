import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

import llmclient._config as config_mod
import llmclient._keys as keys_mod
from llmclient.cli import _aip_models as aip


def _response(body: dict) -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = json.dumps(body).encode()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    monkeypatch.setattr(config_mod, "_config_dir", None)
    monkeypatch.setattr(config_mod, "_data_dir", None)
    monkeypatch.setattr(config_mod, "_log_level", "errors")
    monkeypatch.delenv("AIP_BASE_URL", raising=False)
    monkeypatch.delenv("AIP_URL", raising=False)
    monkeypatch.delenv("AIP_API_KEY", raising=False)
    keys_mod._cached_config = None
    yield
    keys_mod._cached_config = None


def test_endpoint_does_not_double_v1():
    assert aip._endpoint("https://aip.example/v1", "models") == "https://aip.example/v1/models"
    assert aip._endpoint("https://aip.example", "models") == "https://aip.example/v1/models"


def test_list_models_uses_models_url_and_bearer_auth():
    captured = {}

    def urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["timeout"] = timeout
        return _response({"data": [{"id": "z-model"}, {"id": "a-model"}]})

    cfg = aip.AIPConfig("https://aip.example/v1", "sk-test", "test")
    with patch("urllib.request.urlopen", side_effect=urlopen):
        assert aip.list_models(cfg, 7) == ["a-model", "z-model"]

    assert captured == {
        "url": "https://aip.example/v1/models",
        "auth": "Bearer sk-test",
        "timeout": 7,
    }


def test_test_model_posts_to_chat_completions():
    captured = {}

    def urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data)
        return _response({"choices": [{"message": {"content": "ok"}}]})

    cfg = aip.AIPConfig("https://aip.example", "", "test")
    with patch("urllib.request.urlopen", side_effect=urlopen):
        result = aip.test_model(cfg, "model-a", 30)

    assert result.ok is True
    assert result.outcome == "success"
    assert captured["url"] == "https://aip.example/v1/chat/completions"
    assert captured["payload"]["model"] == "model-a"


def test_resolve_config_prefers_aip_section(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "openai_compatible:\n  url: https://compat.example\n"
        "aip:\n  url: https://aip.example/v1\n  api_key: sk-aip\n"
    )
    monkeypatch.setattr(config_mod, "_config_dir", tmp_path)
    keys_mod._cached_config = None

    cfg = aip._resolve_config(Namespace(url=None, api_key=None))

    assert cfg.base_url == "https://aip.example/v1"
    assert cfg.api_key == "sk-aip"
    assert cfg.source == "config:aip.url"
