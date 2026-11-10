"""用户手填的备用 AI 提供商：清洗、合并顺序、运行时依次尝试。"""
from __future__ import annotations

import pytest

from pa_agent.ai.extra_providers import (
    combined_fallback_chain,
    entry_key,
    extra_fallbacks,
    is_entry_usable,
    normalize_base_url,
)
from pa_agent.config.settings import AIProviderSettings, Settings


def _entry(**kw) -> AIProviderSettings:
    base = dict(
        name="backup",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        api_key="sk-backup",
    )
    base.update(kw)
    return AIProviderSettings(**base)


def test_normalize_base_url_strips_whitespace_and_trailing_slash():
    assert normalize_base_url("  https://a.com/v1/// ") == "https://a.com/v1"
    assert normalize_base_url(None) == ""
    assert normalize_base_url("") == ""


def test_is_entry_usable_requires_all_three_fields():
    assert is_entry_usable(_entry()) is True
    assert is_entry_usable(_entry(api_key="")) is False
    assert is_entry_usable(_entry(base_url="")) is False
    assert is_entry_usable(_entry(model="")) is False


def test_extra_fallbacks_skips_disabled_and_incomplete():
    settings = Settings()
    settings.ai_backups = [
        _entry(name="a"),
        _entry(name="b", enabled=False),
        _entry(name="c", api_key=""),
        _entry(name="d", model=""),
    ]
    out = extra_fallbacks(settings)
    assert [e.name for e in out] == ["a"]


def test_extra_fallbacks_keeps_declared_order():
    settings = Settings()
    settings.ai_backups = [
        _entry(name="first", model="m1"),
        _entry(name="second", model="m2"),
        _entry(name="third", model="m3"),
    ]
    assert [e.name for e in extra_fallbacks(settings)] == ["first", "second", "third"]


def test_combined_chain_puts_derived_first_then_user_backups():
    provider = AIProviderSettings(
        fallbacks=[_entry(name="ark", base_url="https://ark.x.com/v1", model="doubao")],
    )
    settings = Settings()
    settings.ai_backups = [_entry(name="mine", base_url="https://mine.com/v1", model="kimi")]
    chain = combined_fallback_chain(provider, settings)
    assert [e.name for e in chain] == ["ark", "mine"]


def test_combined_chain_dedupes_same_url_and_model():
    dup = _entry(name="dup")
    provider = AIProviderSettings(fallbacks=[dup])
    settings = Settings()
    settings.ai_backups = [_entry(name="dup2")]
    chain = combined_fallback_chain(provider, settings)
    assert len(chain) == 1
    assert entry_key(chain[0]) == entry_key(dup)


def test_combined_chain_without_settings_returns_derived_only():
    provider = AIProviderSettings(fallbacks=[_entry(name="ark")])
    chain = combined_fallback_chain(provider, None)
    assert [e.name for e in chain] == ["ark"]


def test_combined_chain_normalises_trailing_slash():
    provider = AIProviderSettings()
    settings = Settings()
    settings.ai_backups = [_entry(name="x", base_url="https://x.com/v1/")]
    chain = combined_fallback_chain(provider, settings)
    assert chain[0].base_url == "https://x.com/v1"


def test_settings_roundtrip_keeps_ai_backups(tmp_path):
    from pa_agent.config.settings import load_settings, save_settings

    settings = Settings()
    settings.ai_backups = [
        _entry(name="甲", model="m1", api_key="k1"),
        _entry(name="乙", model="m2", api_key="k2", enabled=False),
    ]
    path = tmp_path / "settings.json"
    save_settings(settings, path)
    loaded = load_settings(path)
    assert [(e.name, e.model, e.enabled) for e in loaded.ai_backups] == [
        ("甲", "m1", True),
        ("乙", "m2", False),
    ]


def test_orchestrator_tries_derived_then_user_backup_then_gives_up():
    """派生链走完后必须轮到自定义备用；全试完才返回 False。"""
    from unittest.mock import MagicMock

    from pa_agent.ai.json_validator import JsonValidator
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

    settings = Settings()
    settings.provider = AIProviderSettings(
        model="primary-model",
        base_url="https://primary.example.com/v1",
        api_key="sk-primary",
        fallbacks=[
            _entry(name="dead", base_url="https://dead.example.com/v1", model="dead-m"),
        ],
    )
    settings.ai_backups = [
        _entry(name="mine", base_url="https://mine.example.com/v1", model="mine-m"),
    ]

    orchestrator = TwoStageOrchestrator(
        client=MagicMock(),
        assembler=MagicMock(),
        router=lambda _: [],
        validator=JsonValidator(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )
    chain = combined_fallback_chain(settings.provider, settings)
    assert [c.model for c in chain] == ["dead-m", "mine-m"]

    tried: set[tuple[str, str]] = set()
    assert orchestrator._try_next_provider_fallback(
        tried_keys=tried, fallback_chain=chain, stage_label="Stage 1",
        exc=RuntimeError("boom"),
    ) is True
    assert orchestrator._active_provider.model == "dead-m"

    assert orchestrator._try_next_provider_fallback(
        tried_keys=tried, fallback_chain=chain, stage_label="Stage 1",
        exc=RuntimeError("boom"),
    ) is True
    assert orchestrator._active_provider.model == "mine-m"

    # 全试完：返回 False，交给免费池
    assert orchestrator._try_next_provider_fallback(
        tried_keys=tried, fallback_chain=chain, stage_label="Stage 1",
        exc=RuntimeError("boom"),
    ) is False


def test_empty_key_user_backup_is_skipped_by_the_chain():
    """没填 Key 的备用行不该进链——上去只会撞 401 白等。"""
    settings = Settings()
    settings.ai_backups = [
        _entry(name="no-key", model="m1", api_key=""),
        _entry(name="has-key", model="m2"),
    ]
    chain = combined_fallback_chain(AIProviderSettings(), settings)
    assert [c.model for c in chain] == ["m2"]
