"""三个真实缺陷的回归测试（2026-11-14 OKX 模拟盘实测发现）。

1. 限价单刚挂上、还没成交时**不能**去挂保护单（交易所必报「没有持仓」），
   要登记成 ``pending_fill``，由 ``resume_pending_trigger()`` 在成交后补挂。
2. 止盈止损几何矛盾（做多却把止损放在入场价上方）必须在**开仓之前**拦下，
   否则仓位照常开出来、三条腿全被拒 → 裸奔。
3. 加仓后保护单要按**整个持仓**重建：止损跟上新均价、止盈不叠单也不缩水。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pa_agent.trading.auto_trader import AutoTrader
from pa_agent.trading.common import (
    PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2,
    ProtectionOrder,
    TradeResult,
    plan_protections,
)
from pa_agent.trading.okx_trader import OKXTrader

# ── 夹具 ────────────────────────────────────────────────────────────────────


def _opts(log_path: Path, **overrides: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "enabled": True,
        "dry_run": False,
        "min_confidence": 60,
        "max_notional_usdt": 100.0,
        "max_daily_orders": 0,
        "require_stop_loss": True,
        "market_type": "futures",
        "margin_mode": "cross",
        "leverage": 10,
        "log_path": str(log_path),
    }
    opts.update(overrides)
    return opts


def _decision(**overrides: Any) -> dict[str, Any]:
    dec: dict[str, Any] = {
        "trade_confidence": 80,
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 2000.0,
        "stop_loss_price": 1990.0,
        "take_profit_price": 2010.0,
        "take_profit_price_2": 2020.0,
    }
    dec.update(overrides)
    return dec


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "auto_orders.jsonl"


class _LimitClient:
    """限价单：只挂上、不成交。``filled=False`` 复现真实回包。"""

    def __init__(self, *, filled: bool, positions: list[dict[str, Any]] | None = None) -> None:
        self.filled = filled
        self.orders: list[dict[str, Any]] = []
        self.protections: list[dict[str, Any]] = []
        self._positions = positions or []

    def place_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> TradeResult:
        self.orders.append({"symbol": symbol, "side": side, "order_type": order_type, **kwargs})
        return TradeResult(
            ok=True,
            venue="okx",
            order_id="LIMIT-1",
            symbol="ETH-USDT-SWAP",
            side=side,
            order_type=order_type,
            qty=0.01,
            price=2000.0 if self.filled else 2000.0,
            message="ok",
            market_type="futures",
            leverage=10,
            margin_mode="cross",
            filled=self.filled,
        )

    def place_protections(
        self,
        symbol: str,
        *,
        position_side: str,
        plans: list,
        market_type: str = "",
        replace_on_mismatch: bool = True,
    ) -> list[ProtectionOrder]:
        self.protections.append(
            {
                "symbol": symbol,
                "position_side": position_side,
                "plans": plans,
                "replace_on_mismatch": replace_on_mismatch,
            }
        )
        return [
            ProtectionOrder(kind=kind, ok=True, trigger_price=px, qty=q, order_id=f"P-{kind}")
            for kind, px, q, problem in plans
            if not problem
        ]

    def list_positions(self):
        return True, "ok", list(self._positions)


# ── 1. 限价单未成交 → 登记待补挂，绝不提前挂保护单 ─────────────────────────


def test_unfilled_limit_order_does_not_place_protections(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _LimitClient(filled=False)
    trader._trader = client

    out = trader.evaluate(_decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0)

    assert out.executed is True
    assert out.reason == "pending-fill"
    assert out.result is not None and out.result.filled is False
    assert client.protections == [], "限价单还没成交，此时挂保护单必被交易所拒绝"

    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["pending_fill"] is True
    assert len(record["protection_plan"]) == 3


def test_filled_market_order_still_places_protections(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _LimitClient(filled=True)
    trader._trader = client

    out = trader.evaluate(
        _decision(order_type="市价单"), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    assert out.executed is True
    assert out.reason == "submitted"
    assert len(client.protections) == 1


def test_resume_covers_a_pending_limit_fill(log_path: Path) -> None:
    """``pending_fill`` 记录必须被补偿链路认到，成交后才补挂。"""
    log_path.write_text(
        json.dumps(
            {
                "ts": "2026-11-14T10:00:00+00:00",
                "venue": "okx",
                "symbol": "ETHUSDT",
                "order_id": "LIMIT-1",
                "pending_fill": True,
                "trigger_price": 2000.0,
                "position_side": "long",
                "protection_plan": [
                    {"kind": PROTECT_STOP_LOSS, "trigger_price": 1990.0, "qty": 0.04, "problem": ""},
                    {"kind": PROTECT_TAKE_PROFIT_1, "trigger_price": 2010.0, "qty": 0.02, "problem": ""},
                    {"kind": PROTECT_TAKE_PROFIT_2, "trigger_price": 2020.0, "qty": 0.02, "problem": ""},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _LimitClient(
        filled=True,
        positions=[
            {"symbol": "ETH-USDT-SWAP", "position_side": "long", "size": 0.04, "entry_price": 2001.5}
        ],
    )
    trader._trader = client

    handled, message = trader.resume_pending_trigger()

    assert handled is True
    assert "限价单已成交" in message or "补挂保护单" in message
    assert len(client.protections) == 1
    assert client.protections[0]["replace_on_mismatch"] is False


# ── 2. 几何矛盾必须在开仓之前拦下 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "decision_kwargs, expected",
    [
        ({"stop_loss_price": 2010.0}, "没有低于入场价"),  # 做多止损在上方
        ({"take_profit_price": 1990.0}, "没有高过入场价"),  # 做多止盈在下方
    ],
)
def test_invalid_geometry_blocks_before_entry(
    log_path: Path, decision_kwargs: dict[str, Any], expected: str
) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _LimitClient(filled=True)
    trader._trader = client

    out = trader.evaluate(
        _decision(**decision_kwargs), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    assert out.executed is False
    assert out.reason.startswith("invalid-protection")
    assert expected in out.reason
    assert client.orders == [], "几何矛盾时不该发出任何开仓委托"


def test_missing_take_profit_2_is_not_a_geometry_error(log_path: Path) -> None:
    """TP2 本来就是可选的；缺价只会少挂一条腿，不算矛盾。"""
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _LimitClient(filled=True)
    trader._trader = client

    out = trader.evaluate(
        _decision(take_profit_price_2=None),
        symbol="ETHUSDT",
        timeframe="15m",
        last_price=2000.0,
    )

    assert out.executed is True, out.reason


# ── 3. 保护单按整个持仓重建 ────────────────────────────────────────────────


def test_add_position_rebuilds_stop_on_the_whole_position(log_path: Path) -> None:
    """加仓后止损要按整个持仓的均价重算，而不是沿用第一次的价。"""
    trader = AutoTrader(_opts(log_path), venue="okx")
    # 已经加过仓：持仓 0.04、均价 2003.0
    client = _LimitClient(
        filled=True,
        positions=[
            {"symbol": "ETH-USDT-SWAP", "position_side": "long", "size": 0.04, "entry_price": 2003.0}
        ],
    )
    trader._trader = client

    out = trader.evaluate(_decision(), symbol="ETHUSDT", timeframe="15m", last_price=2003.0)

    assert out.executed is True
    plans = client.protections[0]["plans"]
    # 止损价是决策给的绝对价，不会因为加仓而变形
    stop_px = next(px for kind, px, _q, _p in plans if kind == PROTECT_STOP_LOSS)
    assert stop_px == pytest.approx(1990.0)

    # 关键断言：止盈数量之和覆盖**整个持仓** 0.04，而不是本笔的 0.01
    tp_qty = sum(q for kind, _px, q, _p in plans if kind != PROTECT_STOP_LOSS)
    assert tp_qty == pytest.approx(0.04), "止盈只覆盖本笔成交的话，加仓后一半仓位没有止盈"

    # 止损腿永远是全量（先保命）
    stop_qty = next(q for kind, _px, q, _p in plans if kind == PROTECT_STOP_LOSS)
    assert stop_qty == pytest.approx(0.04)


def test_held_position_returns_zero_when_query_fails(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")

    class _Boom:
        def list_positions(self):
            raise RuntimeError("network down")

    assert trader._held_position(_Boom(), "ETH-USDT", "long") == (0.0, 0.0)


# ── 4. OKX 端：按价位逐条比对，只撤不一致的腿 ───────────────────────────────


def _okx_client() -> OKXTrader:
    client = OKXTrader(api_key="k", api_secret="s", passphrase="p", demo=True)
    client._instrument_rules = lambda inst, inst_type="": {  # type: ignore[assignment]
        "tick": 0.01,
        "ct_val": 0.1,
        "ct_mult": 1.0,
        "step": 0.01,
        "min_qty": 0.01,
    }
    client._is_long_short_mode = lambda: False  # type: ignore[assignment]
    return client


def _plans(entry: float, stop: float, tp1: float, tp2: float, qty: float):
    return plan_protections(
        position_side="long",
        entry_price=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        total_qty=qty,
    )


def test_add_position_cancels_the_stale_stop_and_rebuilds() -> None:
    """场上止损价已过时 → 撤掉旧的，按新价重挂。"""
    client = _okx_client()
    posts: list[dict[str, Any]] = []
    cancels: list[list[dict]] = []
    existing = [{"algoId": "SL-OLD", "slTriggerPx": "1816.52", "closeFraction": "1", "posSide": "net"}]

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "GET":
            return {"code": "0", "data": existing}, ""
        if "cancel-algos" in str(path):
            cancels.append(list(body or []))
            return {"code": "0", "data": [{"sCode": "0"}]}, ""
        posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": "NEW", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    orders = client.place_protections(
        "ETH-USDT-SWAP",
        position_side="long",
        plans=_plans(2003.0, 1874.24, 2013.0, 2023.0, 0.04),
        market_type="futures",
    )

    assert all(o.ok for o in orders)
    assert cancels, "旧止损必须被撤掉，否则止损会永远停在第一次的价"
    assert cancels[0][0]["algoId"] == "SL-OLD"


def test_identical_protections_are_not_replaced(log_path: Path) -> None:
    """价位完全一致 → 保持幂等，一条都不撤、一条都不重挂。"""
    client = _okx_client()
    posts: list[dict[str, Any]] = []
    cancels: list[list[dict]] = []
    existing = [
        {"algoId": "SL", "slTriggerPx": "1990", "closeFraction": "1", "posSide": "net"},
        {"algoId": "T1", "tpTriggerPx": "2010", "posSide": "net"},
        {"algoId": "T2", "tpTriggerPx": "2020", "posSide": "net"},
    ]

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "GET":
            return {"code": "0", "data": existing}, ""
        if "cancel-algos" in str(path):
            cancels.append(list(body or []))
            return {"code": "0", "data": [{"sCode": "0"}]}, ""
        posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": "NEW", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    orders = client.place_protections(
        "ETH-USDT-SWAP",
        position_side="long",
        plans=_plans(2000.0, 1990.0, 2010.0, 2020.0, 0.04),
        market_type="futures",
    )

    assert all(o.ok for o in orders)
    assert posts == [] and cancels == []
    assert any("已有" in o.message for o in orders)


def test_only_the_changed_take_profit_leg_is_replaced() -> None:
    """只改了 TP2 的价 → 只撤 TP2，止损与 TP1 原样保留。"""
    client = _okx_client()
    posts: list[dict[str, Any]] = []
    cancels: list[list[dict]] = []
    existing = [
        {"algoId": "SL", "slTriggerPx": "1990", "closeFraction": "1", "posSide": "net"},
        {"algoId": "T1", "tpTriggerPx": "2010", "posSide": "net"},
        {"algoId": "T2", "tpTriggerPx": "2020", "posSide": "net"},
    ]

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "GET":
            return {"code": "0", "data": existing}, ""
        if "cancel-algos" in str(path):
            cancels.append(list(body or []))
            return {"code": "0", "data": [{"sCode": "0"}]}, ""
        posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": "NEW", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    client.place_protections(
        "ETH-USDT-SWAP",
        position_side="long",
        plans=_plans(2000.0, 1990.0, 2010.0, 2050.0, 0.04),
        market_type="futures",
    )

    cancelled_ids = [row["algoId"] for row in (cancels[0] if cancels else [])]
    assert cancelled_ids == ["T2"]
    # 只重挂 TP2（止损与 TP1 判重跳过）
    assert len(posts) == 1
