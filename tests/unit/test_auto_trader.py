"""Unit tests for the auto-trading gate chain (pure unit, no network)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pa_agent.trading.auto_trader import AutoTrader
from pa_agent.trading.common import TradeResult, floor_to_step, format_decimal


def _opts(**overrides: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "enabled": True,
        "dry_run": True,
        "min_confidence": 60,
        "max_notional_usdt": 50.0,
        "max_daily_orders": 10,
        "allowed_symbols": (),
        "require_stop_loss": True,
    }
    opts.update(overrides)
    return opts


def _decision(**overrides: Any) -> dict[str, Any]:
    dec: dict[str, Any] = {
        "trade_confidence": 75,
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 60000.0,
        "stop_loss_price": 58000.0,
        "take_profit_price": 64000.0,
    }
    dec.update(overrides)
    return dec


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "auto_orders.jsonl"


def _trader(log_path: Path, **overrides: Any) -> AutoTrader:
    return AutoTrader(_opts(log_path=str(log_path), **overrides), venue="binance")


# ── Gate 1: 总开关 ───────────────────────────────────────────────────────────
def test_disabled_blocks_everything(log_path: Path) -> None:
    out = _trader(log_path, enabled=False).evaluate(
        _decision(), symbol="BTCUSDT", timeframe="15m", last_price=60000.0
    )
    assert out.executed is False
    assert out.reason.startswith("disabled")


# ── Gate 2: 置信度 ───────────────────────────────────────────────────────────
def test_low_confidence_is_rejected(log_path: Path) -> None:
    out = _trader(log_path).evaluate(
        _decision(trade_confidence=10), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is False
    assert out.reason.startswith("low-confidence")


def test_missing_confidence_is_rejected(log_path: Path) -> None:
    dec = _decision()
    dec.pop("trade_confidence")
    out = _trader(log_path).evaluate(dec, symbol="BTCUSDT", timeframe="15m")
    assert out.executed is False
    assert "trade_confidence" in out.reason


# ── Gate 3: 下单意图 ─────────────────────────────────────────────────────────
def test_no_order_type_is_rejected(log_path: Path) -> None:
    out = _trader(log_path).evaluate(
        _decision(order_type="不下单"), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is False
    assert out.reason.startswith("no-order")


def test_unknown_direction_is_rejected(log_path: Path) -> None:
    out = _trader(log_path).evaluate(
        _decision(order_direction="观望"), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is False
    assert out.reason.startswith("no-order")


# ── Gate 4: 止损 ─────────────────────────────────────────────────────────────
def test_missing_stop_loss_is_rejected(log_path: Path) -> None:
    dec = _decision()
    dec.pop("stop_loss_price")
    out = _trader(log_path).evaluate(dec, symbol="BTCUSDT", timeframe="15m")
    assert out.executed is False
    assert out.reason.startswith("no-stop-loss")


def test_stop_loss_not_required_when_disabled(log_path: Path) -> None:
    dec = _decision()
    dec.pop("stop_loss_price")
    out = _trader(log_path, require_stop_loss=False).evaluate(
        dec, symbol="BTCUSDT", timeframe="15m", last_price=60000.0
    )
    assert out.executed is True


# ── Gate 5: 白名单 ───────────────────────────────────────────────────────────
def test_symbol_outside_whitelist_is_rejected(log_path: Path) -> None:
    out = _trader(log_path, allowed_symbols=["ETHUSDT"]).evaluate(
        _decision(), symbol="BTCUSDT", timeframe="15m", last_price=60000.0
    )
    assert out.executed is False
    assert out.reason.startswith("symbol-not-allowed")


def test_whitelisted_symbol_passes(log_path: Path) -> None:
    out = _trader(log_path, allowed_symbols=["BTCUSDT"]).evaluate(
        _decision(), symbol="btc/usdt", timeframe="15m", last_price=60000.0
    )
    assert out.executed is True


# ── Gate 6: 每日次数 ─────────────────────────────────────────────────────────
def test_daily_limit_counts_only_real_orders(log_path: Path) -> None:
    # dry-run 不计入当日已下单数，避免试运行把自己锁死
    trader = _trader(log_path, max_daily_orders=1)
    for _ in range(5):
        out = trader.evaluate(
            _decision(), symbol="BTCUSDT", timeframe="15m", last_price=60000.0
        )
        assert out.executed is True


# ── Gate 7/8: 金额收敛 + dry-run ─────────────────────────────────────────────
def test_notional_is_clamped_not_rejected(log_path: Path) -> None:
    out = _trader(log_path, max_notional_usdt=30.0).evaluate(
        _decision(entry_price=60000.0, position_size_usdt=900.0),
        symbol="BTCUSDT",
        timeframe="15m",
        last_price=60000.0,
    )
    assert out.executed is True
    assert out.result is not None
    assert out.result.qty * 60000.0 <= 30.5


def test_dry_run_never_calls_exchange(log_path: Path) -> None:
    trader = _trader(log_path)
    trader._trader = None  # 确保没有客户端被创建
    out = trader.evaluate(
        _decision(), symbol="BTCUSDT", timeframe="15m", last_price=60000.0
    )
    assert out.executed is True and out.dry_run is True
    assert trader._trader is None


def test_every_call_appends_a_log_line(log_path: Path) -> None:
    trader = _trader(log_path)
    trader.evaluate(_decision(), symbol="BTCUSDT", timeframe="15m", last_price=60000.0)
    trader.evaluate(_decision(trade_confidence=1), symbol="BTCUSDT", timeframe="15m")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["symbol"] == "BTCUSDT" and first["dry_run"] is True


# ── 鲁棒性 ───────────────────────────────────────────────────────────────────
def test_never_raises_on_garbage_input(log_path: Path) -> None:
    trader = _trader(log_path)
    for bad in (None, {}, {"order_type": None}, {"trade_confidence": "abc"}):
        out = trader.evaluate(bad, symbol="BTCUSDT", timeframe="15m")
        assert out.executed is False


def test_bad_symbol_shape_is_rejected(log_path: Path) -> None:
    out = _trader(log_path).evaluate(
        _decision(), symbol="!!!", timeframe="15m", last_price=60000.0
    )
    assert out.executed is False


# ── 跨所价差保护 ─────────────────────────────────────────────────────────────
class _FakePriceClient:
    """最小替身：只实现 ``last_price``（AutoTrader 只调这一个方法）。"""

    def __init__(self, price: float) -> None:
        self.price = price
        self.calls = 0

    def last_price(self, symbol: str, market: str | None = None) -> float:
        self.calls += 1
        return self.price


def _trader_with_price(log_path: Path, price: float, **overrides: Any) -> Any:
    trader = AutoTrader(
        _opts(log_path=str(log_path), **overrides), venue="okx"
    )
    trader._trader = _FakePriceClient(price)
    return trader


def test_price_deviation_within_limit_passes(log_path: Path) -> None:
    # 76000 → 76004，偏离 0.005%，低于默认 0.3% 容忍度
    trader = _trader_with_price(log_path, 76004.0, max_price_deviation_pct=0.3)
    out = trader.evaluate(
        _decision(entry_price=76000.0), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is True, out.reason


def test_price_deviation_beyond_limit_blocks(log_path: Path) -> None:
    # 76000 → 77000，偏离 1.32%，远超 0.3%
    trader = _trader_with_price(log_path, 77000.0, max_price_deviation_pct=0.3)
    out = trader.evaluate(
        _decision(entry_price=76000.0), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is False
    assert out.reason.startswith("price-deviation")


def test_price_deviation_disabled_when_zero(log_path: Path) -> None:
    client = _FakePriceClient(99999.0)
    trader = AutoTrader(
        _opts(log_path=str(log_path), max_price_deviation_pct=0.0), venue="okx"
    )
    trader._trader = client
    out = trader.evaluate(
        _decision(entry_price=76000.0), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is True
    assert client.calls == 0, "关闭校验时不应发起取价请求"


def test_price_deviation_skipped_when_price_unavailable(log_path: Path) -> None:
    # 交易所取不到价（离线/接口异常）时不能误拦单
    trader = _trader_with_price(log_path, 0.0, max_price_deviation_pct=0.3)
    out = trader.evaluate(
        _decision(entry_price=76000.0), symbol="BTCUSDT", timeframe="15m"
    )
    assert out.executed is True, out.reason


def test_price_deviation_defaults_to_off(log_path: Path) -> None:
    # 交易层默认关闭（0.0），保证单测与老调用方完全离线、行为不变
    trader = AutoTrader(_opts(log_path=str(log_path)), venue="okx")
    assert trader.max_price_deviation_pct == 0.0


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def test_floor_to_step_rounds_down() -> None:
    assert floor_to_step(0.000123456, 1e-05) == pytest.approx(0.00012)


def test_format_decimal_strips_trailing_zeros() -> None:
    assert format_decimal(1e-05) == "0.00001"
    assert format_decimal(1.5) == "1.5"


def test_trade_result_defaults() -> None:
    r = TradeResult(ok=False, message="boom")
    assert r.ok is False and r.qty == 0.0 and r.raw == {}
