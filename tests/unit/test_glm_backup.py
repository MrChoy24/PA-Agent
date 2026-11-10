"""glm-4-flash backup channel wiring (key comes from user settings)."""
from __future__ import annotations

import json

import pytest

from pa_agent.ai.glm_backup import (
    GLM_BASE_URL, GLM_MODEL, ensure_glm_fallback, glm_fallback_available,
    is_sensenova_base,
)
from pa_agent.config.settings import AIProviderSettings, Settings

SENSENOVA = "https://token.sensenova.cn/v1"


def _provider(**kw) -> AIProviderSettings:
    base = dict(model="deepseek-v4-flash", base_url=SENSENOVA, api_key="sk-primary")
    base.update(kw)
    return AIProviderSettings(**base)


def test_glm_fallback_added_when_sensenova_and_key_present():
    p = _provider(backup_glm_api_key="glm-key-123")
    assert ensure_glm_fallback(p) is True
    assert len(p.fallbacks) == 1
    fb = p.fallbacks[0]
    assert fb.model == GLM_MODEL
    assert fb.base_url == GLM_BASE_URL
    assert fb.api_key == "glm-key-123"
    assert fb.thinking is False


def test_idempotent_and_fills_missing_key():
    p = _provider(backup_glm_api_key="glm-key-123")
    ensure_glm_fallback(p)
    assert ensure_glm_fallback(p) is False
    assert len(p.fallbacks) == 1
    p2 = _provider(backup_glm_api_key="glm-key-456")
    p2.fallbacks = [AIProviderSettings(model=GLM_MODEL, base_url=GLM_BASE_URL, api_key="")]
    assert ensure_glm_fallback(p2) is False
    assert p2.fallbacks[0].api_key == "glm-key-456"


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://open.bigmodel.cn/api/paas/v4", ""])
def test_not_added_for_other_providers(base_url):
    p = _provider(base_url=base_url, backup_glm_api_key="glm-key-123")
    assert ensure_glm_fallback(p) is False
    assert p.fallbacks == []
    assert is_sensenova_base(base_url) is False


def test_no_key_no_fallback():
    p = _provider(backup_glm_api_key="   ")
    assert ensure_glm_fallback(p) is False
    assert glm_fallback_available(p) is False
    p.backup_glm_api_key = "k"
    assert glm_fallback_available(p) is True


def test_settings_roundtrip_keeps_key(tmp_path):
    from pa_agent.config.settings import load_settings, save_settings
    settings = Settings()
    settings.provider = _provider(backup_glm_api_key="glm-key-123")
    ensure_glm_fallback(settings.provider)
    path = tmp_path / "settings.json"
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == "glm-key-123"
    assert loaded.provider.fallbacks[0].model == GLM_MODEL


def test_orchestrator_uses_glm_fallback_on_auth_failure(monkeypatch):
    from unittest.mock import MagicMock
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken
    from tests.integration.conftest import make_frame
    from tests.unit.test_glm_flash_lite import payload, reply

    settings = Settings()
    settings.provider = _provider(backup_glm_api_key="glm-key-123")
    settings.validation.retry_enabled = False
    settings.general.enable_next_bar_prediction = True
    client = MagicMock()
    client.stream_chat.side_effect = [
        RuntimeError("Error code: 401 - Forbidden"), reply(payload())]
    o = TwoStageOrchestrator(client=client, assembler=MagicMock(), router=lambda _: [],
        validator=__import__("pa_agent.ai.json_validator", fromlist=["JsonValidator"]).JsonValidator(),
        pending_writer=MagicMock(), exp_reader=MagicMock(), settings=settings)
    record = o.submit(frame=make_frame(), cancel_token=CancelToken(), on_event=lambda e: None)
    assert record.exception is None, record.exception
    # The saved configuration must stay exactly what the user set.
    assert settings.provider.model == "deepseek-v4-flash"
    assert settings.provider.base_url == SENSENOVA
    assert settings.provider.api_key == "sk-primary"
    # The switch only affects this run.
    assert o._provider().model == GLM_MODEL
    assert record.stage2_decision["trace_source"] == "local_rules"  # lite contract engaged
    assert client.stream_chat.call_count == 2
    client.update_provider.assert_called_once()
def test_rotated_key_overwrites_stale_chain_entry():
    """换了 Key 必须覆盖备用链里的旧副本。

    回归：settings.json 会把 fallbacks 整条落盘，用户在输入框换 Key 后，
    链里的 glm 条目仍是旧 Key；旧实现「已存在就不动」导致每次请求都用失效
    的 Key 撞 401，换了 Key 也毫无效果。
    """
    p = _provider(backup_glm_api_key="new-key-789")
    p.fallbacks = [
        AIProviderSettings(model=GLM_MODEL, base_url=GLM_BASE_URL, api_key="old-key-123"),
    ]
    assert ensure_glm_fallback(p) is False  # 不新增条目
    assert len(p.fallbacks) == 1
    assert p.fallbacks[0].api_key == "new-key-789"  # 但必须刷新


def test_unchanged_key_leaves_entry_untouched():
    """Key 没变时不能无意义地改写条目（保持幂等，不产生多余写回）。"""
    p = _provider(backup_glm_api_key="same-key")
    p.fallbacks = [
        AIProviderSettings(model=GLM_MODEL, base_url=GLM_BASE_URL, api_key="same-key"),
    ]
    assert ensure_glm_fallback(p) is False
    assert p.fallbacks[0].api_key == "same-key"
