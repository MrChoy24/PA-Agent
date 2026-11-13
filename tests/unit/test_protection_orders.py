"""开仓后立即挂止损 / TP1 / TP2 保护单（纯单元，全程离线）。

覆盖四层：

1. :func:`pa_agent.trading.common.plan_protections` 的价格几何与数量拆分
2. :meth:`AutoTrader._place_protections` 的容错（异常不外抛、开关生效）
3. 真实 Gate 10 链路：开仓成交后必须调用 ``place_protections``，
   且结果写进 ``TradeResult.protections`` 与 JSONL 的 ``protections`` 字段
4. 两个交易所客户端的请求体形状（用假 ``_request`` 截获，不发网络）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from pa_agent.trading.auto_trader import AutoTrader
from pa_agent.trading.binance_trader import BinanceTrader
from pa_agent.trading.common import (
    PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2,
    ProtectionOrder,
    TradeResult,
    plan_protections,
    protection_label,
)
from pa_agent.trading.okx_trader import OKXTrader


# ── 1. plan_protections ──────────────────────────────────────────────────────


def test_long_plan_orders_stop_below_and_targets_above() -> None:
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.04,
    )
    kinds = [kind for kind, _p, _q, _problem in plans]
    assert kinds == [PROTECT_STOP_LOSS, PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2]
    assert [problem for *_rest, problem in plans] == ["", "", ""]
    stop_qty = plans[0][2]
    tp1_qty = plans[1][2]
    tp2_qty = plans[2][2]
    # 止损保护全量，两个止盈各分一半
    assert stop_qty == pytest.approx(0.04)
    assert tp1_qty == pytest.approx(0.02)
    assert tp2_qty == pytest.approx(0.02)
    assert tp1_qty + tp2_qty == pytest.approx(stop_qty)


def test_short_plan_mirrors_the_geometry() -> None:
    plans = plan_protections(
        position_side="short",
        entry_price=2000.0,
        stop_loss=2010.0,
        take_profit_1=1990.0,
        take_profit_2=1980.0,
        total_qty=1.0,
    )
    assert all(problem == "" for *_rest, problem in plans)
    assert [round(price, 2) for _k, price, _q, _p in plans] == [2010.0, 1990.0, 1980.0]


def test_long_plan_rejects_stop_above_entry() -> None:
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=2010.0,
        take_profit_1=2020.0,
        take_profit_2=2030.0,
        total_qty=1.0,
    )
    stop = plans[0]
    assert stop[0] == PROTECT_STOP_LOSS
    assert "没有低于入场价" in stop[3]


def test_short_plan_rejects_target_below_entry() -> None:
    plans = plan_protections(
        position_side="short",
        entry_price=2000.0,
        stop_loss=2010.0,
        take_profit_1=2005.0,
        take_profit_2=1990.0,
        total_qty=1.0,
    )
    tp1 = plans[1]
    assert tp1[0] == PROTECT_TAKE_PROFIT_1
    assert "没有低于入场价" in tp1[3]


def test_missing_prices_are_reported_not_raised() -> None:
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=None,
        take_profit_1=None,
        take_profit_2=None,
        total_qty=1.0,
    )
    assert all(problem == "决策没有给出有效价格" for *_rest, problem in plans)


def test_tp1_fraction_out_of_range_falls_back_to_half() -> None:
    plans = plan_protections(
        position_side="long",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit_1=101.0,
        take_profit_2=102.0,
        total_qty=2.0,
        tp1_fraction=5.0,
    )
    assert plans[1][2] == pytest.approx(1.0)
    assert plans[2][2] == pytest.approx(1.0)


def test_protection_labels_are_chinese() -> None:
    assert protection_label(PROTECT_STOP_LOSS) == "止损"
    assert protection_label(PROTECT_TAKE_PROFIT_1) == "TP1"
    assert protection_label(PROTECT_TAKE_PROFIT_2) == "TP2"
    assert ProtectionOrder(kind=PROTECT_STOP_LOSS).label == "止损"


# ── 2/3. AutoTrader 链路 ─────────────────────────────────────────────────────


class _FakeClient:
    """记录 place_order / place_protections 的调用，不触网。"""

    def __init__(self, *, protection_error: Exception | None = None) -> None:
        self.orders: list[dict[str, Any]] = []
        self.protections: list[dict[str, Any]] = []
        self._protection_error = protection_error

    def place_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> TradeResult:
        self.orders.append(
            {"symbol": symbol, "side": side, "order_type": order_type, **kwargs}
        )
        return TradeResult(
            ok=True,
            venue="binance",
            order_id="ENTRY-1",
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=0.04,
            price=2000.0,
            message="ok",
            market_type="futures",
            leverage=10,
        )

    def place_protections(
        self, symbol: str, *, position_side: str, plans: list, market_type: str = ""
    ) -> list[ProtectionOrder]:
        self.protections.append(
            {
                "symbol": symbol,
                "position_side": position_side,
                "plans": plans,
                "market_type": market_type,
            }
        )
        if self._protection_error is not None:
            raise self._protection_error
        out: list[ProtectionOrder] = []
        for kind, price, qty, problem in plans:
            if problem:
                out.append(
                    ProtectionOrder(
                        kind=kind, trigger_price=price, qty=qty, message=problem
                    )
                )
            else:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        ok=True,
                        trigger_price=price,
                        qty=qty,
                        order_id=f"PROT-{kind}",
                    )
                )
        return out


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


def test_entry_triggers_protections_after_fill(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="binance")
    client = _FakeClient()
    trader._trader = client

    out = trader.evaluate(
        _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    assert out.executed is True and out.dry_run is False
    assert len(client.protections) == 1, "开仓成交后必须挂一次保护单"
    assert client.protections[0]["position_side"] == "long"
    assert out.result is not None
    kinds = [p.kind for p in out.result.protections]
    assert kinds == [PROTECT_STOP_LOSS, PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2]
    assert all(p.ok for p in out.result.protections)


def test_protections_are_written_to_the_jsonl(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="binance")
    trader._trader = _FakeClient()

    trader.evaluate(_decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0)

    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert [row["kind"] for row in record["protections"]] == [
        PROTECT_STOP_LOSS,
        PROTECT_TAKE_PROFIT_1,
        PROTECT_TAKE_PROFIT_2,
    ]
    assert record["protections"][0]["ok"] is True
    assert record["protections"][0]["trigger_price"] == pytest.approx(1990.0)


def test_protection_switch_off_skips_them(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path, protect_after_entry=False), venue="binance")
    client = _FakeClient()
    trader._trader = client

    out = trader.evaluate(
        _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    assert out.executed is True
    assert client.protections == []
    assert out.result is not None
    assert out.result.protections == ()


def test_protection_exception_never_breaks_the_entry(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="binance")
    trader._trader = _FakeClient(protection_error=RuntimeError("boom"))

    out = trader.evaluate(
        _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    # 开仓已经成交，保护单异常只能降级成失败记录
    assert out.executed is True
    assert out.result is not None
    assert len(out.result.protections) == 3
    assert all(p.ok is False for p in out.result.protections)
    assert all("boom" in p.message for p in out.result.protections)


def test_invalid_stop_is_reported_but_targets_still_placed(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="binance")
    trader._trader = _FakeClient()

    out = trader.evaluate(
        _decision(stop_loss_price=2010.0),  # 做多却把止损放在入场价上方
        symbol="ETHUSDT",
        timeframe="15m",
        last_price=2000.0,
    )

    assert out.executed is True
    assert out.result is not None
    stop = out.result.protections[0]
    assert stop.kind == PROTECT_STOP_LOSS
    assert stop.ok is False
    assert "没有低于入场价" in stop.message
    # 两个止盈仍然照挂
    assert out.result.protections[1].ok is True
    assert out.result.protections[2].ok is True


def test_dry_run_previews_protection_prices(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path, dry_run=True), venue="binance")
    client = _FakeClient()
    trader._trader = client

    out = trader.evaluate(
        _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
    )

    assert out.executed is True and out.dry_run is True
    assert client.protections == [], "dry-run 不得真的发保护单"
    assert out.result is not None
    assert "止损@1990" in out.result.message
    assert "TP1@2010" in out.result.message
    assert "TP2@2020" in out.result.message


# ── 4. 交易所客户端请求体 ────────────────────────────────────────────────────


def test_binance_futures_sends_stop_and_take_profit_market_orders() -> None:
    client = BinanceTrader(
        api_key="k", api_secret="s", market_type="futures", leverage=10
    )
    client._symbol_filters = lambda symbol, market=None: {  # type: ignore[assignment]
        "step": 0.001,
        "tick": 0.01,
        "min_qty": 0.001,
        "max_qty": 1000.0,
        "min_notional": 5.0,
    }
    client._is_hedge_mode = lambda: False  # type: ignore[assignment]
    sent: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, *, signed=True, market=None):
        sent.append({"method": method, "path": path, "params": dict(params or {})})
        return {"orderId": f"OID-{len(sent)}", "status": "NEW"}, ""

    client._request = _fake_request  # type: ignore[assignment]

    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.04,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    assert len(sent) == 3
    stop_req, tp1_req, tp2_req = (item["params"] for item in sent)

    # 止损：反向市价条件单 + 全量平仓
    assert stop_req["type"] == "STOP_MARKET"
    assert stop_req["side"] == "SELL"
    assert stop_req["closePosition"] == "true"
    assert stop_req["stopPrice"] == "1990"
    assert "quantity" not in stop_req

    # 止盈：反向市价条件单 + 分批数量 + reduceOnly
    assert tp1_req["type"] == "TAKE_PROFIT_MARKET"
    assert tp1_req["reduceOnly"] == "true"
    assert tp1_req["stopPrice"] == "2010"
    assert tp1_req["quantity"] == "0.02"
    assert tp2_req["stopPrice"] == "2020"
    assert tp2_req["quantity"] == "0.02"
    # 3 次都打到同一个 endpoint
    assert {item["path"] for item in sent} == {"/fapi/v1/order"}


def test_binance_short_protections_use_buy_side() -> None:
    client = BinanceTrader(
        api_key="k", api_secret="s", market_type="futures", leverage=10
    )
    client._symbol_filters = lambda symbol, market=None: {  # type: ignore[assignment]
        "step": 0.001,
        "tick": 0.01,
        "min_qty": 0.001,
        "max_qty": 1000.0,
        "min_notional": 5.0,
    }
    client._is_hedge_mode = lambda: False  # type: ignore[assignment]
    sent: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, *, signed=True, market=None):
        sent.append(dict(params or {}))
        return {"orderId": "OID", "status": "NEW"}, ""

    client._request = _fake_request  # type: ignore[assignment]

    plans = plan_protections(
        position_side="short",
        entry_price=2000.0,
        stop_loss=2010.0,
        take_profit_1=1990.0,
        take_profit_2=1980.0,
        total_qty=0.04,
    )
    client.place_protections("ETHUSDT", position_side="short", plans=plans)

    assert {item["side"] for item in sent} == {"BUY"}


def test_binance_spot_reports_no_native_protection() -> None:
    client = BinanceTrader(api_key="k", api_secret="s", market_type="spot")
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=1.0,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert len(orders) == 3
    assert all(o.ok is False for o in orders)
    assert all("现货不支持" in o.message for o in orders)


def test_binance_without_credentials_refuses_to_place() -> None:
    client = BinanceTrader(market_type="futures")
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=1.0,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)
    assert all(o.ok is False for o in orders)
    assert all("API Key" in o.message for o in orders)


def test_okx_futures_sends_conditional_algo_orders() -> None:
    client = OKXTrader(
        api_key="k",
        api_secret="s",
        passphrase="p",
        market_type="futures",
        leverage=10,
    )
    client._instrument_rules = lambda inst_id, inst_type=None: {  # type: ignore[assignment]
        "tick": 0.01,
        "ct_val": 0.01,
        "ct_mult": 1.0,
    }
    client._is_long_short_mode = lambda: False  # type: ignore[assignment]
    sent: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        sent.append({"method": method, "path": path, "body": dict(body or {})})
        return {"code": "0", "data": [{"algoId": f"A{len(sent)}", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]

    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.04,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    # 挂单前会先 GET 一次在挂条件单做幂等判断，这里只看 POST 的请求体
    posts = [item for item in sent if item["method"] == "POST"]
    assert {item["path"] for item in posts} == {"/api/v5/trade/order-algo"}
    stop_body = posts[0]["body"]
    assert stop_body["ordType"] == "conditional"
    assert stop_body["side"] == "sell"
    assert stop_body["slTriggerPx"] == "1990"
    assert stop_body["slOrdPx"] == "-1"
    assert stop_body["reduceOnly"] is True
    # 止损用「全平」语义：只给 closeFraction，不给 sz（同时给会报 50024）
    assert stop_body["closeFraction"] == "1"
    assert "sz" not in stop_body
    tp1_body = posts[1]["body"]
    assert tp1_body["tpTriggerPx"] == "2010"
    assert tp1_body["tpOrdPx"] == "-1"
    assert "closeFraction" not in tp1_body
    # 0.04 基础币 / ctVal 0.01 → 4 张；TP1 拿一半 = 2 张
    assert tp1_body["sz"] == "2"


def test_okx_spot_reports_no_native_protection() -> None:
    client = OKXTrader(api_key="k", api_secret="s", passphrase="p", market_type="spot")
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=1.0,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)
    assert all(o.ok is False for o in orders)
    assert all("现货不支持" in o.message for o in orders)


# ── 真实资金保护（用户 2026-11-13 明确要求：测试不得动用真实账户资金）────────


def test_force_dry_run_env_pulls_a_live_config_back_to_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PA_AGENT_FORCE_DRY_RUN=1`` 必须能把 ``dry_run=False`` 强制改成试运行。

    这是「只想验证一遍流程、不想碰钱」时的唯一保险丝，一旦失灵就是真实下单。
    """
    monkeypatch.setenv("PA_AGENT_FORCE_DRY_RUN", "1")
    trader = AutoTrader(
        {"enabled": True, "dry_run": False, "max_notional_usdt": 200.0},
        venue="okx",
    )
    assert trader.dry_run is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " on "])
def test_force_dry_run_env_accepts_common_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("PA_AGENT_FORCE_DRY_RUN", raw)
    trader = AutoTrader({"enabled": True, "dry_run": False}, venue="okx")
    assert trader.dry_run is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off"])
def test_force_dry_run_env_ignores_falsy_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """非真值不该把配置改掉，也不该假装生效。"""
    monkeypatch.setenv("PA_AGENT_FORCE_DRY_RUN", raw)
    trader = AutoTrader({"enabled": True, "dry_run": False}, venue="okx")
    assert trader.dry_run is False


def test_live_trading_emits_a_loud_warning(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """``dry_run=False`` + ``enabled=True`` 时必须留下一条显眼的实盘告警。"""
    monkeypatch.delenv("PA_AGENT_FORCE_DRY_RUN", raising=False)
    with caplog.at_level(logging.WARNING):
        AutoTrader(
            {"enabled": True, "dry_run": False, "max_notional_usdt": 200.0},
            venue="okx",
        )
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "真实下单" in text
    assert "会动用真实资金" in text


def test_dry_run_config_does_not_warn(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """默认的试运行不该刷实盘告警，否则真出事时没人会看。"""
    monkeypatch.delenv("PA_AGENT_FORCE_DRY_RUN", raising=False)
    with caplog.at_level(logging.WARNING):
        AutoTrader({"enabled": True, "dry_run": True}, venue="okx")
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "会动用真实资金" not in text


def test_disabled_auto_trade_does_not_warn(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """开关没打开时不可能下单，不该误报。"""
    monkeypatch.delenv("PA_AGENT_FORCE_DRY_RUN", raising=False)
    with caplog.at_level(logging.WARNING):
        AutoTrader({"enabled": False, "dry_run": False}, venue="okx")
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "会动用真实资金" not in text


# ── 小数张数（OKX lotSz=0.01，用户 2026-11-13 指出 0.01 张即可下单）──────────

_ETH_RULES_SMALL_LOT: dict[str, Any] = {
    "tick": 0.01,
    "ct_val": 0.1,
    "ct_mult": 1.0,
    "step": 0.01,
    "min_qty": 0.01,
}


def _okx_client(rules: dict[str, Any]) -> OKXTrader:
    client = OKXTrader(
        api_key="k", api_secret="s", passphrase="p", market_type="futures", leverage=10
    )
    client._instrument_rules = lambda inst_id, inst_type=None: dict(rules)  # type: ignore[assignment]
    client._is_long_short_mode = lambda: False  # type: ignore[assignment]
    return client


@pytest.mark.parametrize(
    "base_qty, expected",
    [
        (0.005, 0.05),      # ≈9.6 USDT
        (0.007762, 0.07),   # ≈15 USDT（曾因整数取整被放大成 1 张）
        (0.05, 0.5),        # 半张
        (0.1, 1.0),         # 整张
        (0.35, 3.5),        # 3.5 张
    ],
)
def test_contracts_honour_the_lot_step(base_qty: float, expected: float) -> None:
    """``lotSz=0.01`` 时张数必须是小数，不能向下取整成整数。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    assert client._contracts_for(base_qty, _ETH_RULES_SMALL_LOT) == pytest.approx(expected)


def test_contracts_round_down_to_the_lot_step() -> None:
    """不足一个 lot 的零头要抹掉，但保留整数个 lot。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    # 0.1 ETH = 1 张；再多 0.0004 ETH（0.004 张）不足以凑 0.01 张
    assert client._contracts_for(0.1004, _ETH_RULES_SMALL_LOT) == pytest.approx(1.0)


def test_contracts_fall_back_to_whole_contracts_without_a_lot_step() -> None:
    """没有 ``step`` 时按整张处理（老合约/接口缺字段时的保守行为）。"""
    rules = {"ct_val": 0.1, "ct_mult": 1.0}
    client = _okx_client(rules)
    assert client._contracts_for(0.35, rules) == pytest.approx(3.0)


def test_one_contract_position_splits_take_profits_into_two_orders() -> None:
    """1 张仓位（0.1 ETH）必须能拆成两份 0.5 张的止盈，不再降级合并。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    sent: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "POST":
            sent.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": f"A{len(sent)}", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]

    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    # 止损走全平语义（closeFraction=1，无 sz），两段止盈各自 0.5 张
    assert "closeFraction" not in sent[0] or sent[0].get("closeFraction") == "1"
    assert [body.get("sz") for body in sent[1:]] == ["0.5", "0.5"]


def test_take_profit_legs_below_min_size_still_merge() -> None:
    """真正小到某一段低于 ``minSz`` 时才合并——否则那段根本挂不上。"""
    rules = dict(_ETH_RULES_SMALL_LOT, min_qty=1.0, step=0.01)
    client = _okx_client(rules)
    sent: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        sent.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": f"A{len(sent)}", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]

    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,   # 0.5 张的止盈腿 < minSz 1 张
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    by_kind = {o.kind: o for o in orders}
    # 止损 1 张照挂；一段止盈合并到另一段，另一段按全量挂
    assert by_kind[PROTECT_STOP_LOSS].ok is True
    assert sum(1 for o in orders if o.ok) == 2
    assert any("已合并" in o.message for o in orders)


# ── 突破单走计划委托（OKX trigger）＋ 滑点告警 ───────────────────────────────


class _TriggerClient(_FakeClient):
    """模拟 OKX：突破单直接返回 pending=True（还没触发、没持仓）。"""

    def __init__(self, *, pending: bool = True, fill_price: float = 2005.0) -> None:
        super().__init__()
        self._pending = pending
        self._fill_price = fill_price

    def place_order(self, symbol: str, side: str, order_type: str, **kwargs: Any) -> TradeResult:
        self.orders.append(
            {"symbol": symbol, "side": side, "order_type": order_type, **kwargs}
        )
        if self._pending:
            return TradeResult(
                ok=True,
                venue="okx",
                order_id="ALGO-1",
                symbol=symbol,
                side=side,
                order_type=order_type,
                qty=0.04,
                price=kwargs.get("trigger_price") or 0.0,
                message="计划委托已挂",
                raw={"pending": True, "algo_id": "ALGO-1"},
            )
        return TradeResult(
            ok=True,
            venue="okx",
            order_id="ENTRY-2",
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=0.04,
            price=self._fill_price,
            message="已成交",
        )


def test_breakout_decision_uses_a_trigger_order(log_path: Path) -> None:
    """突破单 → 计划委托（order_type 传 trigger，触发价 = 决策入场价）。"""
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _TriggerClient()
    trader._trader = client

    trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    assert client.orders, "必须真的调用下单"
    call = client.orders[0]
    assert call["order_type"] == "trigger"
    assert call["trigger_price"] == 2000.0


def test_breakout_plan_does_not_place_protections_before_the_fill(log_path: Path) -> None:
    """计划委托还没成交时不能挂保护单 —— 那会挂到不存在的仓位上。"""
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _TriggerClient(pending=True)
    trader._trader = client

    out = trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    assert out.reason == "pending-trigger"
    assert client.protections == [], "未成交就挂保护单是错的"
    assert out.result is not None and out.result.raw.get("pending") is True


def test_pending_trigger_is_recorded_in_the_jsonl(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")
    trader._trader = _TriggerClient(pending=True)

    trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["pending_trigger"] is True
    assert record["trigger_price"] == 2000.0


def test_limit_and_market_decisions_are_unchanged(log_path: Path) -> None:
    """回归：限价单/市价单仍然走原来的类型，不能被突破单改动带跑。"""
    for cn, expected in (("限价单", "limit"), ("市价单", "market")):
        trader = AutoTrader(_opts(log_path), venue="okx")
        client = _TriggerClient()
        trader._trader = client
        trader.evaluate(
            _decision(order_type=cn), symbol="ETHUSDT", timeframe="15m",
            last_price=2000.0,
        )
        assert client.orders[0]["order_type"] == expected


def test_breakout_on_binance_falls_back_to_market(log_path: Path) -> None:
    """Binance 没有计划委托 → 突破单退化成市价单，不能传 trigger 过去。"""
    trader = AutoTrader(_opts(log_path), venue="binance")
    client = _FakeClient()
    trader._trader = client

    trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    assert client.orders[0]["order_type"] == "MARKET"


def test_dry_run_mentions_the_trigger_price(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path, dry_run=True), venue="okx")

    out = trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    assert out.dry_run is True
    assert "计划委托" in out.result.message
    assert "触发价 2000" in out.result.message


@pytest.mark.parametrize(
    ("decision_price", "fill_price", "expected"),
    [(2000.0, 2005.0, 25.0), (2000.0, 2000.0, 0.0), (2000.0, 1998.0, -10.0)],
)
def test_slippage_is_computed_in_basis_points(
    log_path: Path, decision_price: float, fill_price: float, expected: float
) -> None:
    trader = AutoTrader(_opts(log_path), venue="binance")

    got = trader._slippage_bps(decision_price, fill_price)

    assert got is not None and round(got, 6) == expected


def test_slippage_above_threshold_warns_but_still_trades(
    log_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """滑点超限只告警：单已成交，拒单会让仓位裸奔。"""
    trader = AutoTrader(_opts(log_path, max_slippage_bps=10.0), venue="binance")
    trader._trader = _TriggerClient(pending=False, fill_price=2005.0)

    with caplog.at_level(logging.WARNING):
        out = trader.evaluate(
            _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
        )

    assert out.executed is True, "滑点大也不能拒绝已成交的单"
    assert any("滑点偏大" in r.getMessage() for r in caplog.records)


def test_slippage_within_threshold_does_not_warn(
    log_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trader = AutoTrader(_opts(log_path, max_slippage_bps=30.0), venue="binance")
    trader._trader = _TriggerClient(pending=False, fill_price=2000.5)

    with caplog.at_level(logging.WARNING):
        trader.evaluate(
            _decision(), symbol="ETHUSDT", timeframe="15m", last_price=2000.0
        )

    assert not any("滑点偏大" in r.getMessage() for r in caplog.records)


def test_slippage_check_can_be_disabled(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path, max_slippage_bps=0.0), venue="binance")

    assert trader._slippage_bps(2000.0, 2100.0) is None


# ── 计划委托触发后的补偿：补挂保护单 ────────────────────────────────────────


class _ResumeClient(_FakeClient):
    """可配置持仓状态的假客户端，用于测「触发后补挂」。"""

    def __init__(self, *, positions: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self._positions = positions or []

    def list_positions(self):
        return True, "ok", list(self._positions)


def _write_pending(log_path: Path, **overrides: Any) -> None:
    record = {
        "ts": "2026-11-13T10:00:00+00:00",
        "venue": "okx",
        "symbol": "ETHUSDT",
        "order_id": "ALGO-9",
        "pending_trigger": True,
        "trigger_price": 2000.0,
        "position_side": "long",
        "protection_plan": [
            {"kind": PROTECT_STOP_LOSS, "trigger_price": 1990.0, "qty": 0.04, "problem": ""},
            {"kind": PROTECT_TAKE_PROFIT_1, "trigger_price": 2010.0, "qty": 0.02, "problem": ""},
            {"kind": PROTECT_TAKE_PROFIT_2, "trigger_price": 2020.0, "qty": 0.02, "problem": ""},
        ],
    }
    record.update(overrides)
    log_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_resume_skips_when_there_is_nothing_pending(log_path: Path) -> None:
    trader = AutoTrader(_opts(log_path), venue="okx")
    trader._trader = _ResumeClient()

    handled, _ = trader.resume_pending_trigger()

    assert handled is False


def test_resume_waits_while_the_position_does_not_exist_yet(log_path: Path) -> None:
    """计划委托还没触发（无持仓）→ 不能挂保护单。"""
    _write_pending(log_path)
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _ResumeClient(positions=[])
    trader._trader = client

    handled, _ = trader.resume_pending_trigger()

    assert handled is False
    assert client.protections == []


def test_resume_places_protections_once_the_position_exists(log_path: Path) -> None:
    _write_pending(log_path)
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _ResumeClient(
        positions=[
            {
                "symbol": "ETH-USDT-SWAP",
                "position_side": "long",
                "size": 0.04,
                "entry_price": 2001.5,
                "mark_price": 2002.0,
                "unrealized_pnl": 0.02,
                "leverage": 10,
                "margin": 8.0,
            }
        ]
    )
    trader._trader = client

    handled, message = trader.resume_pending_trigger()

    assert handled is True
    assert "补挂保护单" in message
    assert len(client.protections) == 1
    assert client.protections[0]["symbol"] == "ETH-USDT-SWAP"
    kinds = [p[0] for p in client.protections[0]["plans"]]
    assert kinds == [PROTECT_STOP_LOSS, PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2]


def test_resume_is_idempotent_and_appends_a_marker(log_path: Path) -> None:
    """补挂一次后必须落一条 trigger-resumed 标记，否则每轮都会重复挂。"""
    _write_pending(log_path)
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _ResumeClient(
        positions=[{"symbol": "ETH-USDT-SWAP", "position_side": "long", "size": 0.04,
                    "entry_price": 2001.5, "mark_price": 2002.0, "unrealized_pnl": 0.0,
                    "leverage": 10, "margin": 8.0}]
    )
    trader._trader = client

    first, _ = trader.resume_pending_trigger()
    second, _ = trader.resume_pending_trigger()

    assert first is True
    assert second is False, "补挂过就不该再来一次"
    assert len(client.protections) == 1
    last = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["kind"] == "trigger-resumed"
    assert last["trigger_resumed"] is True


def test_resume_ignores_a_record_that_was_already_resumed(log_path: Path) -> None:
    _write_pending(log_path)
    # 真实写出的补挂标记带着 order_id，靠它把「哪一单补过了」对上号
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"kind": "trigger-resumed", "order_id": "ALGO-9", "trigger_resumed": True}
            )
            + "\n"
        )
    trader = AutoTrader(_opts(log_path), venue="okx")
    client = _ResumeClient(
        positions=[{"symbol": "ETH-USDT-SWAP", "position_side": "long", "size": 0.04,
                    "entry_price": 2001.5, "mark_price": 2002.0, "unrealized_pnl": 0.0,
                    "leverage": 10, "margin": 8.0}]
    )
    trader._trader = client

    handled, _ = trader.resume_pending_trigger()

    assert handled is False
    assert client.protections == []


def test_resume_never_raises_when_the_client_explodes(log_path: Path) -> None:
    class _Boom(_ResumeClient):
        def list_positions(self):
            raise RuntimeError("network down")

    _write_pending(log_path)
    trader = AutoTrader(_opts(log_path), venue="okx")
    trader._trader = _Boom()

    handled, _ = trader.resume_pending_trigger()

    assert handled is False


def test_pending_record_keeps_the_protection_plan(log_path: Path) -> None:
    """下单时就要把「触发后挂哪几条」写进 JSONL，否则重启后无从补挂。"""
    trader = AutoTrader(_opts(log_path), venue="okx")
    trader._trader = _TriggerClient(pending=True)

    trader.evaluate(
        _decision(order_type="突破单"), symbol="ETHUSDT", timeframe="15m",
        last_price=2000.0,
    )

    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    plan = record.get("protection_plan")
    assert isinstance(plan, list) and len(plan) == 3
    assert [p["kind"] for p in plan] == [
        PROTECT_STOP_LOSS, PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2
    ]


def test_slippage_env_override_is_wired_through_the_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """daemon 侧也要能改阈值，否则容器里只能挂保护单不能调告警。"""
    from pa_agent.config.settings import Settings
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    monkeypatch.setenv("PA_DAEMON_MAX_SLIPPAGE_BPS", "12.5")
    applied = apply_auto_trade_env_overrides(settings)

    assert "PA_DAEMON_MAX_SLIPPAGE_BPS" in applied
    assert settings.auto_trade.max_slippage_bps == 12.5


def test_slippage_env_override_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.config.settings import Settings
    from pa_agent.daemon.config import apply_auto_trade_env_overrides

    settings = Settings()
    before = settings.auto_trade.max_slippage_bps
    for bad in ("abc", "-1", "-0.01"):
        settings.auto_trade.max_slippage_bps = before
        monkeypatch.setenv("PA_DAEMON_MAX_SLIPPAGE_BPS", bad)
        applied = apply_auto_trade_env_overrides(settings)
        assert "PA_DAEMON_MAX_SLIPPAGE_BPS" not in applied, bad
        assert settings.auto_trade.max_slippage_bps == before, bad


def test_live_trading_warning_is_announced_once_per_process(caplog) -> None:
    """守护进程每轮 tick 都会新建 AutoTrader，告警不能跟着刷屏。

    2026-11-13 的容器日志里 100 行有 34 行是同一条「真实下单模式」WARNING，
    每 20 秒一次 —— 正是 tick 里为「待触发补偿」新建实例造成的。
    """
    from pa_agent.trading import auto_trader as mod

    mod._LIVE_WARNED.clear()
    opts = {
        "enabled": True,
        "dry_run": False,
        "market_type": "futures",
        "margin_mode": "cross",
        "leverage": 10,
        "max_notional_usdt": 15.0,
        "min_confidence": 60,
        "max_daily_orders": 0,
        "require_stop_loss": True,
    }
    with caplog.at_level(logging.WARNING, logger="pa_agent.trading.auto_trader"):
        for _ in range(5):
            mod.AutoTrader(opts, venue="okx")
    hits = [r for r in caplog.records if "真实下单】模式启动" in r.getMessage()]
    assert len(hits) == 1

    # 换了杠杆就是另一份配置，应该再喊一次
    with caplog.at_level(logging.WARNING, logger="pa_agent.trading.auto_trader"):
        mod.AutoTrader({**opts, "leverage": 5}, venue="okx")
    hits = [r for r in caplog.records if "真实下单】模式启动" in r.getMessage()]
    assert len(hits) == 2


def test_fallback_pool_is_announced_once(monkeypatch, caplog) -> None:
    """备用池内容是配置说明，不该每轮分析都打一条。"""
    from pa_agent.ai import extra_providers as mod

    mod._ANNOUNCED_SPECS.clear()
    spec = "甲|m1|https://a.example/v1|k1;乙|m2|https://b.example/v1|k2"
    monkeypatch.setenv(mod.FALLBACK_SPEC_ENV, spec)
    with caplog.at_level(logging.INFO, logger="pa_agent.ai.extra_providers"):
        for _ in range(3):
            mod.env_fallbacks()
    hits = [r for r in caplog.records if "提供了 2 条备用接口" in r.getMessage()]
    assert len(hits) == 1


# ── 止损腿用 closeFraction=1「全平」（用户 2026-11-14 提出）────────────────
#
# 背景：固定 sz 的止损单在分批止盈后会「多」出来 —— 开 0.4 张、TP1 成交
# 0.2 张后持仓只剩 0.2 张，止损单却仍写 sz=0.4，触发时一次全平。OKX 的
# closeFraction=1 表示「平掉触发那一刻的全部剩余持仓」，天然免疫这个问题。
# 四条硬约束均为 2026-11-14 模拟盘实测所得。


def test_stop_loss_uses_close_fraction_instead_of_size() -> None:
    """净持仓模式下止损腿只给 ``closeFraction``，不能同时给 ``sz``（50024）。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)  # _okx_client 已设 _is_long_short_mode False
    posts: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "POST":
            posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": f"A{len(posts)}", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    stop_body = posts[0]
    assert stop_body["closeFraction"] == "1"
    assert "sz" not in stop_body
    assert stop_body["reduceOnly"] is True  # 51328：不带 reduceOnly 会被拒
    # 止盈腿仍按计划量分批，不受影响
    assert [b.get("sz") for b in posts[1:]] == ["0.5", "0.5"]
    assert all("closeFraction" not in b for b in posts[1:])


def test_stop_loss_falls_back_to_size_in_hedge_mode() -> None:
    """双向持仓模式下 closeFraction 不可用（51329），必须退回固定张数。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    client._is_long_short_mode = lambda: True  # type: ignore[assignment]
    posts: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "POST":
            posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": f"A{len(posts)}", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    stop_body = posts[0]
    assert "closeFraction" not in stop_body
    assert stop_body["sz"] == "1"
    assert stop_body["posSide"] == "long"  # 双向持仓必须指方向


def test_repeated_place_protections_does_not_duplicate() -> None:
    """重复补挂（例如容器重启）不能把止盈叠成双份。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    existing: list[dict[str, Any]] = [
        {"algoId": "EX1", "closeFraction": "1", "posSide": "net", "slTriggerPx": "1990"},
        {"algoId": "EX2", "tpTriggerPx": "2010", "sz": "0.5"},
        {"algoId": "EX3", "tpTriggerPx": "2020", "sz": "0.5"},
    ]
    posts: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "POST":
            posts.append(dict(body or {}))
        return {"code": "0", "data": existing if method == "GET" else []}, ""

    client._request = _fake_request  # type: ignore[assignment]
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert all(o.ok for o in orders)
    assert posts == []  # 一条都不该再发
    assert "全平止损条件单" in orders[0].message
    assert "已有止盈条件单" in orders[1].message


def test_pending_algo_query_failure_does_not_block_placing() -> None:
    """查询在挂条件单失败时，宁可重复挂也不能让保护单完全挂不上。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)
    posts: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "GET":
            return None, "boom"
        posts.append(dict(body or {}))
        return {"code": "0", "data": [{"algoId": "A1", "sCode": "0"}]}, ""

    client._request = _fake_request  # type: ignore[assignment]
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.0,
        take_profit_2=2020.0,
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert [o.ok for o in orders] == [True, True, True]
    assert len(posts) == 3


def test_take_profit_dedup_tolerates_tick_rounding() -> None:
    """交易所回显的是按 tick 取整后的价，判重必须带容差，否则会再叠一条。"""
    client = _okx_client(_ETH_RULES_SMALL_LOT)  # tick=0.01
    # 场上已有一条触发价被取整过的止盈
    existing = [{"algoId": "EX1", "tpTriggerPx": "2010", "sz": "0.5"}]
    posts: list[dict[str, Any]] = []

    def _fake_request(method, path, params=None, body=None, *, signed=True):
        if method == "POST":
            posts.append(dict(body or {}))
        return {"code": "0", "data": existing if method == "GET" else []}, ""

    client._request = _fake_request  # type: ignore[assignment]
    plans = plan_protections(
        position_side="long",
        entry_price=2000.0,
        stop_loss=1990.0,
        take_profit_1=2010.004,  # 与 2010 只差 0.004 < tick/2
        take_profit_2=2100.0,  # 全新价位，应该照挂
        total_qty=0.1,
    )
    orders = client.place_protections("ETHUSDT", position_side="long", plans=plans)

    assert all(o.ok for o in orders)
    assert len(posts) == 2  # 止损 + TP2（TP1 被容差判重跳过）
    assert any("已有止盈条件单" in o.message for o in orders)
