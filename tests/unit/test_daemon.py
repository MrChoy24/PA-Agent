"""Unit tests for the headless daemon (no network, no PyQt6, no AI calls)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pa_agent.config.settings import Settings
from pa_agent.daemon.config import ConfigError, DaemonConfig, _timeframe_seconds
from pa_agent.daemon.event_bus import LoggingEventBus
from pa_agent.data.base import KlineBar


@pytest.fixture(autouse=True)
def _isolate_disk_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tick() 会落盘下单记录：全部改到 tmp_path，别污染项目里的 trade_records/。

    同时禁用「跨所价差保护」的取价请求：那是唯一会在 evaluate() 里
    发起网络调用的闸门，单测里必须保持完全离线。
    """
    import pa_agent.records.trade_logger as trade_logger
    import pa_agent.trading.auto_trader as auto_trader

    monkeypatch.setattr(trade_logger, "_TRADE_RECORDS_DIR", tmp_path / "trade_records")
    monkeypatch.setattr(
        auto_trader,
        "_DEFAULT_LOG_PATH",
        str(tmp_path / "trade_records" / "auto_orders.jsonl"),
    )
    # 取价恒为 0.0 → 调用方按「无法校验」放行，等价于该闸门关闭
    monkeypatch.setattr(
        auto_trader.AutoTrader,
        "_venue_reference_price",
        lambda self, symbol: 0.0,
    )


def _settings() -> Settings:
    return Settings()


def _config(**overrides: Any) -> DaemonConfig:
    cfg = DaemonConfig(
        settings=_settings(),
        settings_path=Path("config/settings.json"),
        symbol="BTCUSDT",
        timeframe="15m",
        data_source="binance",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ── config ───────────────────────────────────────────────────────────────────


def test_daemon_config_rejects_non_crypto_source() -> None:
    cfg = _config(data_source="ashare")
    with pytest.raises(ConfigError, match="binance / okx"):
        cfg.validate()


def test_daemon_config_normalizes_source_aliases() -> None:
    cfg = _config(data_source="BINANCE")
    cfg.validate()
    assert cfg.data_source == "binance"


def test_daemon_config_rejects_too_few_bars() -> None:
    cfg = _config(bar_count=3)
    with pytest.raises(ConfigError, match="至少 10 根"):
        cfg.validate()


def test_daemon_config_rejects_fast_polling() -> None:
    cfg = _config(poll_interval_s=0.2)
    with pytest.raises(ConfigError, match="至少 1 秒"):
        cfg.validate()


def test_daemon_config_rejects_negative_cooldown() -> None:
    cfg = _config(analysis_cooldown_s=-1.0)
    with pytest.raises(ConfigError, match="不能为负"):
        cfg.validate()


def test_daemon_config_falls_back_on_unknown_log_level() -> None:
    cfg = _config(log_level="LOUD")
    cfg.validate()
    assert cfg.log_level == "INFO"


def test_daemon_describe_never_leaks_secrets() -> None:
    cfg = _config()
    cfg.settings.provider.api_key = "sk-super-secret-value"
    text = cfg.describe()
    assert "sk-super-secret-value" not in text
    assert "BTCUSDT" in text


def test_timeframe_seconds_falls_back_for_unknown() -> None:
    assert _timeframe_seconds("15m") == 900
    assert _timeframe_seconds("4h") == 14400
    # "3M" is parsed case-insensitively as 3 minutes, not month.
    assert _timeframe_seconds("3M") == 180
    # Truly unparseable → conservative 1h fallback.
    assert _timeframe_seconds("1mon") == 3600


def test_config_from_args_reads_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pa_agent.daemon.config import config_from_args

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(Settings().model_dump(), ensure_ascii=False), encoding="utf-8"
    )
    cfg, args = config_from_args(
        [
            "--settings",
            str(settings_path),
            "--symbol",
            "ETHUSDT",
            "--timeframe",
            "1h",
            "--data-source",
            "okx",
            "--bar-count",
            "120",
            "--poll-interval",
            "30",
        ]
    )
    assert cfg.symbol == "ETHUSDT"
    assert cfg.timeframe == "1h"
    assert cfg.data_source == "okx"
    assert cfg.bar_count == 120
    assert cfg.poll_interval_s == 30.0
    assert args.once is False


def test_config_from_args_rejects_unknown_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pa_agent.daemon.config import config_from_args

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(Settings().model_dump(), ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        DaemonConfig.from_env(
            settings_path=settings_path, overrides={"nope": 1}
        )


def test_provider_env_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.daemon.config import apply_provider_env_overrides

    settings = Settings()
    settings.provider.api_key = "from-json"
    monkeypatch.setenv("PA_AGENT_API_KEY", "from-env")
    monkeypatch.setenv("PA_AGENT_MODEL", "deepseek-chat")
    applied = apply_provider_env_overrides(settings)
    assert settings.provider.api_key == "from-env"
    assert settings.provider.model == "deepseek-chat"
    assert "PA_AGENT_API_KEY" in applied
    assert "FROM-ENV" not in " ".join(applied).upper().replace("FROM-ENV", "FROM-ENV")


def test_glm_backup_key_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """备用 GLM Key 也应能只靠 .env 配置，无需改 settings.json。"""
    from pa_agent.ai.glm_backup import (
        GLM_BASE_URL,
        GLM_MODEL,
        ensure_glm_fallback,
        is_glm_backup_eligible,
    )
    from pa_agent.daemon.config import apply_provider_env_overrides

    for name in ("PA_AGENT_GLM_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()
    settings.provider.base_url = "https://ark.cn-beijing.volces.com/api/v3"
    settings.provider.fallbacks = []
    assert apply_provider_env_overrides(settings) == []
    assert settings.provider.backup_glm_api_key == ""

    monkeypatch.setenv("PA_AGENT_GLM_API_KEY", "glm-from-env")
    applied = apply_provider_env_overrides(settings)
    assert applied == ["PA_AGENT_GLM_API_KEY"]
    assert settings.provider.backup_glm_api_key == "glm-from-env"

    assert is_glm_backup_eligible(settings.provider.base_url) is True
    assert ensure_glm_fallback(settings.provider) is True
    assert ensure_glm_fallback(settings.provider) is False  # 幂等
    assert len(settings.provider.fallbacks) == 1
    entry = settings.provider.fallbacks[0]
    assert entry.model == GLM_MODEL
    assert entry.base_url == GLM_BASE_URL
    assert entry.api_key == "glm-from-env"


def test_glm_backup_key_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLM_API_KEY / ZHIPU_API_KEY 作为旧名别名同样生效。"""
    from pa_agent.daemon.config import apply_provider_env_overrides

    for name in ("PA_AGENT_GLM_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-alias")

    settings = Settings()
    assert apply_provider_env_overrides(settings) == ["ZHIPU_API_KEY"]
    assert settings.provider.backup_glm_api_key == "zhipu-alias"


def test_glm_backup_not_attached_for_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    """主通道是 DeepSeek 官方时不挂 GLM 兜底（官方通道本身够稳）。"""
    from pa_agent.ai.glm_backup import ensure_glm_fallback

    settings = Settings()
    settings.provider.base_url = "https://api.deepseek.com"
    settings.provider.backup_glm_api_key = "glm-key"
    assert ensure_glm_fallback(settings.provider) is False
    assert settings.provider.fallbacks == []


def test_exchange_env_overrides_fill_both_venues(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container should be configurable with env vars alone."""
    from pa_agent.daemon.config import apply_exchange_env_overrides

    settings = Settings()
    monkeypatch.setenv("BINANCE_API_KEY", "bn-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "bn-secret")
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "okx-secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "okx-pass")
    monkeypatch.setenv("OKX_DEMO", "1")

    applied = apply_exchange_env_overrides(settings)

    assert settings.binance.api_key == "bn-key"
    assert settings.binance.api_secret == "bn-secret"
    assert settings.binance.testnet is True
    assert settings.okx.api_key == "okx-key"
    assert settings.okx.api_secret == "okx-secret"
    assert settings.okx.passphrase == "okx-pass"
    assert settings.okx.demo is True
    assert "BINANCE_API_KEY" in applied and "OKX_PASSPHRASE" in applied


def test_exchange_env_only_overrides_when_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty variable must not wipe the value already in settings.json."""
    from pa_agent.daemon.config import apply_exchange_env_overrides

    settings = Settings()
    settings.okx.api_key = "from-file"
    monkeypatch.setenv("OKX_API_KEY", "")
    apply_exchange_env_overrides(settings)
    assert settings.okx.api_key == "from-file"

    monkeypatch.setenv("OKX_API_KEY", "from-env")
    apply_exchange_env_overrides(settings)
    assert settings.okx.api_key == "from-env"


def test_auto_trade_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    assert settings.auto_trade.enabled is False
    monkeypatch.setenv("PA_DAEMON_AUTO_TRADE", "true")
    monkeypatch.setenv("PA_DAEMON_DRY_RUN", "false")
    monkeypatch.setenv("PA_DAEMON_VENUE", "okx")

    applied = apply_auto_trade_env_overrides(settings)

    assert settings.auto_trade.enabled is True
    assert settings.auto_trade.dry_run is False
    assert settings.auto_trade.venue == "okx"
    assert set(applied) == {"PA_DAEMON_AUTO_TRADE", "PA_DAEMON_DRY_RUN", "PA_DAEMON_VENUE"}


def test_auto_trade_env_rejects_unknown_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    settings.auto_trade.venue = "binance"
    monkeypatch.setenv("PA_DAEMON_VENUE", "kraken")
    applied = apply_auto_trade_env_overrides(settings)
    assert settings.auto_trade.venue == "binance"
    assert "PA_DAEMON_VENUE" not in applied



def test_auto_trade_env_overrides_order_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """杠杆 / 开单金额 / 订单类型都能从 .env 下来（服务器部署的核心诉求）。"""
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    monkeypatch.setenv("PA_DAEMON_MARGIN_PER_TRADE", "10")
    monkeypatch.setenv("PA_DAEMON_MAX_NOTIONAL", "20")
    monkeypatch.setenv("PA_DAEMON_LEVERAGE", "10")
    monkeypatch.setenv("PA_DAEMON_MARKET_TYPE", "futures")
    monkeypatch.setenv("PA_DAEMON_ORDER_TYPE", "limit")
    monkeypatch.setenv("PA_DAEMON_MARGIN_MODE", "cross")

    applied = apply_auto_trade_env_overrides(settings)

    assert settings.auto_trade.margin_per_trade_usdt == 10.0
    assert settings.auto_trade.max_notional_usdt == 20.0
    assert settings.auto_trade.leverage == 10
    assert settings.auto_trade.market_type == "futures"
    assert settings.auto_trade.order_type == "LIMIT"
    assert settings.auto_trade.margin_mode == "cross"
    assert set(applied) == {
        "PA_DAEMON_MARGIN_PER_TRADE",
        "PA_DAEMON_MAX_NOTIONAL",
        "PA_DAEMON_LEVERAGE",
        "PA_DAEMON_MARKET_TYPE",
        "PA_DAEMON_ORDER_TYPE",
        "PA_DAEMON_MARGIN_MODE",
    }


def test_auto_trade_env_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法值必须被忽略并保留原配置，不能污染风控。"""
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    settings.auto_trade.leverage = 3
    settings.auto_trade.margin_per_trade_usdt = 7.0
    monkeypatch.setenv("PA_DAEMON_LEVERAGE", "500")
    monkeypatch.setenv("PA_DAEMON_MARGIN_PER_TRADE", "-5")
    monkeypatch.setenv("PA_DAEMON_MARKET_TYPE", "margin")
    monkeypatch.setenv("PA_DAEMON_ORDER_TYPE", "STOP")
    monkeypatch.setenv("PA_DAEMON_MARGIN_MODE", "portfolio")
    monkeypatch.setenv("PA_DAEMON_MAX_NOTIONAL", "abc")

    applied = apply_auto_trade_env_overrides(settings)

    assert settings.auto_trade.leverage == 3
    assert settings.auto_trade.margin_per_trade_usdt == 7.0
    assert settings.auto_trade.market_type == "spot"
    assert settings.auto_trade.order_type == "MARKET"
    assert settings.auto_trade.margin_mode == "isolated"
    assert applied == []


def test_auto_trade_env_cannot_loosen_safety_ceilings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风控上限（置信度 / 每日单数 / 止损）刻意不开放环境变量通道。"""
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    before = (
        settings.auto_trade.min_confidence,
        settings.auto_trade.max_daily_orders,
        settings.auto_trade.require_stop_loss,
    )
    monkeypatch.setenv("PA_DAEMON_MIN_CONFIDENCE", "0")
    monkeypatch.setenv("PA_DAEMON_MAX_DAILY_ORDERS", "999")
    monkeypatch.setenv("PA_DAEMON_REQUIRE_STOP_LOSS", "false")

    apply_auto_trade_env_overrides(settings)

    after = (
        settings.auto_trade.min_confidence,
        settings.auto_trade.max_daily_orders,
        settings.auto_trade.require_stop_loss,
    )
    assert after == before


# ── event bus ───────────────────────────────────────────────────────────────


def test_logging_event_bus_never_raises() -> None:
    bus = LoggingEventBus()
    bus.emit("disk_error", {"path": "x"})
    bus.emit("status", {"text": "hi"})
    bus.emit_exception(ValueError("boom"))


# ── runner (with a fake source + fake orchestrator) ─────────────────────────


class _FakeSource:
    """Minimal DataSource double returning a fixed bar list."""

    def __init__(self, bars: list[KlineBar]) -> None:
        self._bars = bars
        self.connected = False
        self._connected = False

    def connect(self) -> None:
        self.connected = True
        self._connected = True

    def disconnect(self) -> None:
        self.connected = False
        self._connected = False

    def list_symbols(self) -> list[str]:
        return ["BTCUSDT"]

    def supported_timeframes(self) -> list[str]:
        return ["15m"]

    def subscribe(self, symbol: str, timeframe: str) -> None:
        pass

    def unsubscribe(self) -> None:
        pass

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        return list(self._bars[:n])


def _make_bars(closed_count: int, *, forming: bool = True) -> list[KlineBar]:
    """Build newest-first bars with 15m spacing ending 'now'."""
    import time

    now_ms = int(time.time() * 1000)
    duration_ms = 15 * 60 * 1000
    base = now_ms - (now_ms % duration_ms)
    bars: list[KlineBar] = []
    idx = -1 if forming else 0
    if forming:
        bars.append(
            KlineBar(
                seq=0,
                ts_open=float(base),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1.0,
                closed=False,
            )
        )
    for i in range(closed_count):
        ts = base - duration_ms * (i + 1 - idx)
        bars.append(
            KlineBar(
                seq=i + 1,
                ts_open=float(ts),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=1.0 + i,
                closed=True,
            )
        )
    return bars


class _FakeRecord:
    def __init__(self, stage2: dict | None, stage1: dict | None = None) -> None:
        self.stage1_diagnosis = stage1 or {"cycle_position": "上涨宽通道"}
        self.stage2_decision = stage2
        self.exception = None


class _FakeOrchestrator:
    def __init__(self, stage2: dict | None) -> None:
        self._stage2 = stage2
        self.calls = 0
        self.last_kwargs: dict = {}

    def submit(self, frame, cancel_token, on_event, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return _FakeRecord(self._stage2)


def _runner_with(bars, stage2) -> Any:
    from pa_agent.daemon.runner import DaemonRunner

    cfg = _config()
    cfg.validate()
    runner = DaemonRunner(cfg)
    runner._source = _FakeSource(bars)  # type: ignore[attr-defined]
    runner._orchestrator = _FakeOrchestrator(stage2)  # type: ignore[attr-defined]
    runner._pending_writer = None  # type: ignore[attr-defined]
    return runner


def test_tick_skips_when_not_enough_closed_bars() -> None:
    runner = _runner_with(_make_bars(5), {})
    outcome = runner.tick(force=True)
    assert outcome.ran is False
    assert "不足" in outcome.reason


def test_tick_analyses_and_extracts_decision() -> None:
    decision = {
        "decision": {
            "order_type": "限价单",
            "direction": "bullish",
            "trade_confidence": 72,
            "entry_price": 100.5,
            "stop_loss_price": 99.0,
            "take_profit_price": 103.0,
        },
        "diagnosis_summary": {"cycle_position": "上涨宽通道", "direction": "bullish"},
    }
    runner = _runner_with(_make_bars(140), decision)
    outcome = runner.tick(force=True)
    assert outcome.ran is True
    assert outcome.decision is not None
    assert outcome.decision.get("order_type") == "限价单"
    assert runner._orchestrator.calls == 1  # type: ignore[attr-defined]


def test_tick_skips_duplicate_bar() -> None:
    decision = {"decision": {"order_type": "观望", "trade_confidence": 0}}
    runner = _runner_with(_make_bars(140), decision)
    first = runner.tick(force=True)
    assert first.ran is True
    second = runner.tick(force=False)
    assert second.ran is False
    assert "已分析过" in second.reason


def test_tick_respects_cooldown() -> None:
    decision = {"decision": {"order_type": "观望", "trade_confidence": 0}}
    runner = _runner_with(_make_bars(140), decision)
    cfg = runner._cfg  # type: ignore[attr-defined]
    cfg.analysis_cooldown_s = 3600.0
    cfg.skip_duplicate_bars = False
    runner.tick(force=True)
    outcome = runner.tick(force=False)
    assert outcome.ran is False
    assert "冷却" in outcome.reason


def test_tick_reports_missing_decision() -> None:
    runner = _runner_with(_make_bars(140), None)
    outcome = runner.tick(force=True)
    assert outcome.ran is True
    assert "决策" in outcome.reason


def test_has_order_uses_configured_threshold() -> None:
    runner = _runner_with(_make_bars(140), {})
    inner = {"order_type": "限价单", "trade_confidence": 40}
    runner._cfg.settings.general.decision_confidence_threshold = 0  # type: ignore[attr-defined]
    assert runner._has_order(inner) is True
    runner._cfg.settings.general.decision_confidence_threshold = 80  # type: ignore[attr-defined]
    assert runner._has_order(inner) is False
    assert runner._has_order({"order_type": "观望"}) is False


def test_auto_trade_disabled_returns_empty() -> None:
    runner = _runner_with(_make_bars(140), {})
    runner._cfg.settings.auto_trade.enabled = False  # type: ignore[attr-defined]
    assert runner._maybe_auto_trade({"order_type": "限价单"}) == ""


def test_auto_trade_dry_run_records_reason(tmp_path: Path) -> None:
    runner = _runner_with(_make_bars(140), {})
    cfg = runner._cfg  # type: ignore[attr-defined]
    cfg.settings.auto_trade.enabled = True
    cfg.settings.auto_trade.dry_run = True
    cfg.settings.auto_trade.min_confidence = 50
    inner = {
        "order_type": "限价单",
        "direction": "bullish",
        "trade_confidence": 75,
        "entry_price": 60000.0,
        "stop_loss_price": 58000.0,
        "take_profit_price": 64000.0,
    }
    reason = runner._maybe_auto_trade(inner)
    assert "dry-run" in reason


def test_health_check_reports_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PA_AGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    runner = _runner_with(_make_bars(10), {})
    runner._cfg.settings.provider.api_key = ""  # type: ignore[attr-defined]
    ok, problems = runner.health_check()
    assert ok is False
    assert any("API Key" in item for item in problems)


def test_daemon_package_does_not_import_pyqt6() -> None:
    """The headless entry point must stay importable on a server without Qt."""
    import builtins

    real_import = builtins.__import__
    hits: list[str] = []

    def guard(name, *args, **kwargs):
        if name.split(".")[0] == "PyQt6":
            hits.append(name)
            raise AssertionError(f"PyQt6 imported by daemon: {name}")
        return real_import(name, *args, **kwargs)

    saved = {k: v for k, v in sys.modules.items() if k.startswith("pa_agent.daemon")}
    for key in list(saved):
        sys.modules.pop(key, None)
    builtins.__import__ = guard
    try:
        import pa_agent.daemon.__main__  # noqa: F401
        import pa_agent.daemon.config  # noqa: F401
        import pa_agent.daemon.runner  # noqa: F401
    finally:
        builtins.__import__ = real_import
    assert hits == []


# ── 配置来源可诊断（用户改 .env 却「没生效」时的定位依据）─────────────────


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PA_DAEMON_DATA_SOURCE",
        "PA_DAEMON_SYMBOL",
        "PA_DAEMON_TIMEFRAME",
        "PA_DAEMON_BAR_COUNT",
        "PA_DAEMON_POLL_INTERVAL",
        "PA_DAEMON_ANALYSIS_COOLDOWN",
        "PA_DAEMON_WAIT_BAR_CLOSE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_describe_sources_marks_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """改了 .env 里的周期，来源必须显示 env —— 否则用户无从判断是否读到。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("PA_DAEMON_TIMEFRAME", "30m")
    monkeypatch.setenv("PA_DAEMON_POLL_INTERVAL", "45")

    cfg = DaemonConfig.from_env(settings_path=tmp_path / "settings.json")

    assert cfg.timeframe == "30m"
    assert cfg.bar_seconds == 1800
    assert cfg.sources["timeframe"] == "env"
    assert cfg.sources["poll_interval_s"] == "env"
    text = cfg.describe_sources()
    assert "timeframe=env" in text
    assert "poll_interval_s=env" in text


def test_describe_sources_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """没设 env 时，来源标成 settings，而不是默认值 —— 两者含义完全不同。"""
    _clean_env(monkeypatch)
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"general": {"last_timeframe": "4h", "last_symbol": "BTCUSDT"}}),
        encoding="utf-8",
    )

    cfg = DaemonConfig.from_env(settings_path=path)

    assert cfg.timeframe == "4h"
    assert cfg.sources["timeframe"] == "settings"
    assert cfg.sources["symbol"] == "settings"
    # 这个没配过，应落在默认值上。
    assert cfg.sources["poll_interval_s"] == "default"


def test_cli_override_wins_and_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI 优先级最高，来源要能看出是被 CLI 覆盖的。"""
    _clean_env(monkeypatch)
    monkeypatch.setenv("PA_DAEMON_TIMEFRAME", "30m")

    cfg = DaemonConfig.from_env(
        settings_path=tmp_path / "settings.json",
        overrides={"timeframe": "1h"},
    )

    assert cfg.timeframe == "1h"
    assert cfg.sources["timeframe"] == "cli"
    assert "cli_overrides=timeframe" in cfg.describe_sources()


def test_describe_shows_dingtalk_on_when_env_provides_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings 里 enabled=False，但 .env 提供了 webhook：实际会发，日志必须说 on。

    真实日志（2026-11-12 容器）：describe() 打 `dingtalk=off`，
    同一轮末尾却是「钉钉下单信号发送成功」，自相矛盾，让运维误判通知坏了。
    """
    cfg = _config()
    cfg.settings.dingtalk.enabled = False
    cfg.settings.dingtalk.webhook_url = ""
    cfg.settings.dingtalk.secret = ""
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=abc")
    monkeypatch.setenv("DINGTALK_SECRET", "SECabc")

    assert "dingtalk=on" in cfg.describe()


def test_describe_shows_dingtalk_off_without_any_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config()
    cfg.settings.dingtalk.enabled = False
    cfg.settings.dingtalk.webhook_url = ""
    cfg.settings.dingtalk.secret = ""
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)

    assert "dingtalk=off" in cfg.describe()


def test_compose_does_not_shadow_dotenv_keys() -> None:
    """compose 的 environment 优先级高于 env_file。

    曾经两个 compose 都硬写了 PA_DAEMON_POLL_INTERVAL /
    PA_DAEMON_ANALYSIS_COOLDOWN / PA_DAEMON_WAIT_BAR_CLOSE，
    用户在 .env 里怎么改都不会生效。这里把这条红线钉死。
    """
    root = Path(__file__).resolve().parents[2]
    shadowing = (
        "PA_DAEMON_POLL_INTERVAL",
        "PA_DAEMON_ANALYSIS_COOLDOWN",
        "PA_DAEMON_WAIT_BAR_CLOSE",
    )
    for name in ("docker-compose.yml", "docker-compose.1panel.yml"):
        text = (root / name).read_text(encoding="utf-8")
        # 只看非注释行：注释里提到这些名字是允许且必要的说明。
        live = [
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        for key in shadowing:
            offenders = [line for line in live if key in line]
            assert offenders == [], f"{name} 仍然硬写了 {key}：{offenders}"


def test_dotenv_files_use_lf_line_endings() -> None:
    """CRLF 会被 docker 的 env_file 解析器带进值里（如 '30m\\r'）。

    代码层 _env_str 会 strip()，但 compose env_file 与别家工具未必，
    统一成 LF 才稳。
    """
    root = Path(__file__).resolve().parents[2]
    for name in (".env", ".env.example"):
        path = root / name
        if not path.exists():
            continue
        assert b"\r\n" not in path.read_bytes(), f"{name} 含 CRLF 行尾"


# ── graceful stop ────────────────────────────────────────────────────────────


class _CancelAwareOrchestrator:
    """卡在一次分析里，直到 cancel_token 被点掉才抛 CancelledError。"""

    def __init__(self) -> None:
        self.calls = 0
        self.token_seen: Any = None

    def submit(self, frame, cancel_token, on_event, **kwargs):
        from pa_agent.ai.deepseek_client import CancelledError

        self.calls += 1
        self.token_seen = cancel_token
        raise CancelledError("cancelled by test")


def test_request_stop_cancels_the_inflight_analysis_token() -> None:
    """docker stop 只置 _stop 没用：正在跑的 AI 请求也必须能中断。"""
    from pa_agent.util.threading import CancelToken

    runner = _runner_with(_make_bars(140), {})
    token = CancelToken()
    runner._cancel_token = token  # type: ignore[attr-defined]

    runner.request_stop()

    assert runner.stopped is True
    assert token.is_set() is True


def test_request_stop_is_safe_without_an_inflight_analysis() -> None:
    runner = _runner_with(_make_bars(140), {})
    assert runner._cancel_token is None  # type: ignore[attr-defined]
    runner.request_stop()
    assert runner.stopped is True


def test_tick_reports_cancelled_instead_of_raising() -> None:
    """取消不是异常：tick 要返回 ran=False / reason=已取消。"""
    runner = _runner_with(_make_bars(140), {})
    runner._orchestrator = _CancelAwareOrchestrator()  # type: ignore[attr-defined]

    outcome = runner.tick(force=True)

    assert outcome.ran is False
    assert outcome.reason == "已取消"
    assert runner._cancel_token is None  # type: ignore[attr-defined]


def test_tick_still_raises_ordinary_errors() -> None:
    """非取消类异常不能被吞掉，否则轮询循环看不到数据源故障。"""

    class _Boom:
        calls = 0

        def submit(self, frame, cancel_token, on_event, **kwargs):
            self.calls += 1
            raise RuntimeError("boom")

    runner = _runner_with(_make_bars(140), {})
    runner._orchestrator = _Boom()  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="boom"):
        runner.tick(force=True)
