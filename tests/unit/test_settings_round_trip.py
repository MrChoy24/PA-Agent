"""Unit tests for settings load/save round-trip (task 2.4)."""
from __future__ import annotations
import json
from unittest.mock import patch

import pytest
from pathlib import Path
from pa_agent.config.settings import Settings, load_settings, save_settings


def test_defaults(tmp_path):
    """load_settings on a missing file returns defaults and creates the file."""
    p = tmp_path / "settings.json"
    s = load_settings(p)
    assert s.provider.model == "deepseek-flash"
    assert s.provider.base_url == "https://api.deepseek.com"
    assert s.provider.thinking is True
    # Default is "low" (settings.py: ProviderSettings.reasoning_effort) —
    # conservative cost/latency default for first-run users.
    assert s.provider.reasoning_effort == "low"
    assert s.provider.context_window == 2_000_000
    assert s.general.analysis_bar_count == 100
    # 默认数据源为 Binance，默认品种为 BTCUSDT（MT5/TradingView 已移除）
    assert s.general.last_data_source == "binance"
    assert s.general.last_symbol == "BTCUSDT"
    assert s.general.last_timeframe == "15m"
    assert s.general.decision_stance == "balanced"
    assert s.general.decision_flow_auto_play is True
    assert s.general.auto_resume_chart_after_analysis is False
    assert p.exists(), "defaults should be written to disk"


def test_round_trip(tmp_path):
    """save → load preserves all fields."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.provider.api_key = "sk-test-1234"
    original.general.last_symbol = "BTCUSDT"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.provider.api_key == "sk-test-1234"
    # 默认数据源为 binance，加密货币代码 BTCUSDT 原样保留
    assert loaded.general.last_symbol == "BTCUSDT"
    assert loaded.provider.model == original.provider.model


def test_api_key_present_on_disk(tmp_path):
    """The saved JSON contains the plaintext API key."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.provider.api_key = "sk-super-secret-key"
    save_settings(s, p)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["provider"]["api_key"] == "sk-super-secret-key"


def test_corrupt_json_returns_defaults(tmp_path):
    """Corrupt settings.json falls back to defaults without raising."""
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = load_settings(p)
    assert s.provider.model == "deepseek-flash"


def test_missing_api_key_leaves_api_key_blank(tmp_path):
    """If api_key is absent, api_key stays empty string."""
    p = tmp_path / "settings.json"
    data = Settings().model_dump()
    data["provider"].pop("api_key", None)
    data["provider"].pop("api_key_encrypted", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    s = load_settings(p)
    assert s.provider.api_key == ""


def test_dingtalk_round_trip(tmp_path):
    """save → load preserves dingtalk settings (飞书通道已被钉钉取代)."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.dingtalk.enabled = True
    original.dingtalk.webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=t"
    original.dingtalk.secret = "SEC-test"
    original.dingtalk.at_mobiles = ["13800000000"]
    original.dingtalk.at_all = True
    original.dingtalk.msg_type = "text"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.dingtalk.enabled is True
    assert loaded.dingtalk.webhook_url.endswith("access_token=t")
    assert loaded.dingtalk.secret == "SEC-test"
    assert loaded.dingtalk.at_mobiles == ["13800000000"]
    assert loaded.dingtalk.at_all is True
    assert loaded.dingtalk.msg_type == "text"


def test_pushplus_round_trip(tmp_path):
    """save → load preserves pushplus settings."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.pushplus.token = "pp-test-token"
    original.pushplus.enabled = False
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.pushplus.token == "pp-test-token"
    assert loaded.pushplus.enabled is False


def test_removed_tushare_section_is_dropped(tmp_path):
    """A 股 / Tushare 段已从 Settings 移除；历史 settings.json 里的残留段应被忽略，
    且下次保存时不再写回。"""
    p = tmp_path / "settings.json"
    p.write_text(
        '{"tushare": {"token": "ts-test-token"}, "general": {"last_symbol": "BTCUSDT"}}',
        encoding="utf-8",
    )
    loaded = load_settings(p)
    assert not hasattr(loaded, "tushare")
    save_settings(loaded, p)
    assert "tushare" not in p.read_text(encoding="utf-8")


def test_pushplus_auto_disabled_when_enabled_without_token(tmp_path):
    """load_settings disables pushplus when enabled but token empty."""
    p = tmp_path / "settings.json"
    p.write_text(
        '{"pushplus": {"enabled": true, "token": ""}}',
        encoding="utf-8",
    )
    with patch.dict("os.environ", {}, clear=True):
        loaded = load_settings(p)
    assert loaded.pushplus.enabled is False
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["pushplus"]["enabled"] is False


def test_legacy_feishu_section_dropped_on_load(tmp_path):
    """老版本 settings.json 里的 feishu 段在 load 时被清理（通道已改为钉钉）."""
    p = tmp_path / "settings.json"
    save_settings(Settings(), p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["feishu"] = {
        "enabled": True,
        "webhook_url": "https://example.com/legacy-hook",
        "secret": "legacy-secret",
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_settings(p)
    assert not hasattr(loaded, "feishu")
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert "feishu" not in saved
