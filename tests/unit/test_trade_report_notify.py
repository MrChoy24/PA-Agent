"""钉钉实盘成交回报（notify/trade_report.py）的单元测试。

覆盖三条硬约束：
1. 试运行（dry_run=True）永不推送 —— 这是红线；
2. 只有 submitted / pending-trigger 两种 reason 才推；
3. 推送内容和「交易所已成交」的事实一致（成交均价 / 金额 / 委托号 / 保护单）。
"""
from __future__ import annotations

import io

import pytest

from pa_agent.notify import dingtalk_notifier, trade_report
from pa_agent.trading.auto_trader import AutoTradeOutcome
from pa_agent.trading.common import (
    PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2,
    ProtectionOrder,
    TradeResult,
)


# ── 夹具 ──────────────────────────────────────────────────────────────────────
def _result(
    *,
    protections: tuple[ProtectionOrder, ...] = (),
    raw: dict | None = None,
) -> TradeResult:
    return TradeResult(
        ok=True,
        venue="okx",
        order_id="4095060557604589568",
        symbol="ETH-USDT-SWAP",
        side="BUY",
        order_type="MARKET",
        qty=0.0078,
        price=1931.44,
        message="OKX 合约 MARKET BUY ETH-USDT-SWAP 已提交",
        raw=raw or {},
        market_type="futures",
        leverage=10,
        margin_mode="cross",
        margin_usdt=1.5,
        notional_usdt=15.0,
        protections=protections,
    )


def _outcome(
    *,
    executed: bool = True,
    dry_run: bool = False,
    reason: str = "submitted",
    result: TradeResult | None = None,
) -> AutoTradeOutcome:
    return AutoTradeOutcome(
        executed=executed,
        dry_run=dry_run,
        reason=reason,
        result=result if result is not None else _result(),
        leverage=10,
        notional_usdt=15.0,
        margin_usdt=1.5,
    )


@pytest.fixture()
def captured(monkeypatch):
    """拦住真正的 HTTP，记录发出的 markdown 正文."""
    sent: list[dict] = []

    def fake_post(payload, cfg, *, action="钉钉通知"):
        sent.append({"payload": payload, "action": action})
        return True, "ok"

    monkeypatch.setattr(dingtalk_notifier, "_post", fake_post)
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: True
    )
    monkeypatch.setattr(
        dingtalk_notifier,
        "_dingtalk_config_dict",
        lambda settings=None: {
            "enabled": True,
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x",
            "secret": "",
            "at_mobiles": [],
            "at_all": False,
            "notify_on_order_only": True,
            "notify_on_trade_filled": True,
            "msg_type": "markdown",
        },
    )
    return sent


def _body(sent: list[dict]) -> str:
    return sent[0]["payload"]["markdown"]["text"]


# ── should_report：三条硬约束 ─────────────────────────────────────────────────
def test_dry_run_is_never_reported():
    assert trade_report.should_report(_outcome(dry_run=True)) is False


def test_not_executed_is_not_reported():
    assert trade_report.should_report(_outcome(executed=False)) is False


@pytest.mark.parametrize("reason", ["submitted", "pending-trigger"])
def test_real_fills_are_reported(reason):
    assert trade_report.should_report(_outcome(reason=reason)) is True


@pytest.mark.parametrize(
    "reason",
    ["disabled: 自动下单未启用", "cooldown", "max-daily-orders", "pending-limit", ""],
)
def test_other_reasons_are_not_reported(reason):
    assert trade_report.should_report(_outcome(reason=reason)) is False


def test_none_outcome_is_not_reported():
    assert trade_report.should_report(None) is False


# ── 内容 ──────────────────────────────────────────────────────────────────────
def test_report_contains_every_field_the_user_asked_for(captured):
    assert trade_report.notify_trade_outcome(_outcome(), symbol="ETH-USDT-SWAP") is True
    assert len(captured) == 1
    assert captured[0]["action"] == "钉钉成交回报"

    body = _body(captured)
    assert "实盘成交" in body
    assert "OKX" in body and "合约" in body and "全仓 10x" in body
    assert "ETH-USDT-SWAP" in body
    assert "做多" in body
    # 开仓价（成交均价，不是决策入场价）
    assert "1931.44" in body
    # 下单金额：名义 + 保证金
    assert "15" in body and "1.5" in body
    # 下单时间 / 委托号
    assert "下单时间" in body
    assert "4095060557604589568" in body


def test_report_lists_protection_orders(captured):
    legs = (
        ProtectionOrder(kind=PROTECT_STOP_LOSS, ok=True, trigger_price=1906.96,
                        qty=0.0078, order_id="111", message=""),
        ProtectionOrder(kind=PROTECT_TAKE_PROFIT_1, ok=True, trigger_price=1945.48,
                        qty=0.0039, order_id="222", message=""),
        ProtectionOrder(kind=PROTECT_TAKE_PROFIT_2, ok=False, trigger_price=0.0,
                        qty=0.0, order_id="", message="止盈两段里有张数低于最小下单张数"),
    )
    assert trade_report.notify_trade_outcome(
        _outcome(result=_result(protections=legs))
    ) is True

    body = _body(captured)
    assert "止损：1906.96 已挂（111）" in body
    assert "TP1：1945.48 已挂（222）" in body
    assert "TP2" in body and "未挂" in body and "最小下单张数" in body


def test_report_warns_when_no_protection_orders(captured):
    assert trade_report.notify_trade_outcome(_outcome()) is True
    assert "未提交保护单" in _body(captured)


def test_pending_trigger_report_says_not_filled_yet(captured):
    result = TradeResult(
        ok=True,
        venue="okx",
        order_id="algo-1",
        symbol="ETH-USDT-SWAP",
        side="BUY",
        order_type="TRIGGER",
        qty=0.0,
        price=0.0,
        message="计划委托已挂",
        raw={"pending": True, "trigger_price": 1930.0},
        market_type="futures",
        leverage=10,
    )
    assert trade_report.notify_trade_outcome(
        _outcome(reason="pending-trigger", result=result)
    ) is True

    body = _body(captured)
    assert "计划委托已挂" in body
    assert "1930" in body
    assert "尚未成交" in body
    assert "触发成交后自动补挂" in body


# ── 开关 ──────────────────────────────────────────────────────────────────────
def test_trade_report_switch_off_skips(captured, monkeypatch):
    monkeypatch.setattr(
        dingtalk_notifier,
        "_dingtalk_config_dict",
        lambda settings=None: {
            "enabled": True,
            "webhook_url": "https://example.invalid/hook",
            "secret": "",
            "at_mobiles": [],
            "at_all": False,
            "notify_on_order_only": True,
            "notify_on_trade_filled": False,
            "msg_type": "markdown",
        },
    )
    assert trade_report.notify_trade_outcome(_outcome()) is False
    assert captured == []


def test_disabled_notifier_skips(captured, monkeypatch):
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: False
    )
    assert trade_report.notify_trade_outcome(_outcome()) is False
    assert captured == []


def test_settings_default_enables_trade_report():
    from pa_agent.config.settings import DingTalkSettings

    assert DingTalkSettings().notify_on_trade_filled is True


def test_config_dict_carries_trade_report_flag():
    cfg = dingtalk_notifier._normalize_config({})
    assert cfg["notify_on_trade_filled"] is True
    cfg = dingtalk_notifier._normalize_config({"notify_on_trade_filled": False})
    assert cfg["notify_on_trade_filled"] is False


# ── 突破单触发后的补偿推送 ────────────────────────────────────────────────────
def test_trigger_resumed_report_uses_position_fill_price(captured):
    report = {
        "symbol": "ETH-USDT-SWAP",
        "position_side": "long",
        "entry_price": 1925.99,
        "qty": 0.4,
        "protections": (
            ProtectionOrder(kind=PROTECT_STOP_LOSS, ok=True, trigger_price=1833.5,
                            qty=0.4, order_id="333", message=""),
        ),
        "order_id": "4095",
        "executed_at": "2026-11-14 01:00:00",
        "notional_usdt": 77.0,
        "margin_usdt": 7.7,
        "market_type": "futures",
        "margin_mode": "cross",
        "leverage": 10,
        "order_type": "TRIGGER",
    }
    assert trade_report.notify_trigger_resumed(report, venue="okx") is True

    body = _body(captured)
    assert "1925.99" in body
    assert "做多" in body
    assert "止损：1833.5 已挂（333）" in body
    assert "2026-11-14 01:00:00" in body


def test_trigger_resumed_report_ignores_empty_report(captured):
    assert trade_report.notify_trigger_resumed(None) is False
    assert trade_report.notify_trigger_resumed({}) is False
    assert captured == []


# ── 保护单未挂上：裸奔告警 ────────────────────────────────────────────────────
def _failure_report(**overrides):
    report = {
        "symbol": "ETH-USDT-SWAP",
        "venue": "okx",
        "order_id": "4095",
        "position_side": "short",
        "entry_price": 1819.47,
        "qty": 0.82,
        "attempts": 1,
        "final": False,
        "total": 3,
        "reasons": ["止损: 51023 Position doesnt exist", "TP1: 51279 invalid"],
        "protections": (
            ProtectionOrder(kind=PROTECT_STOP_LOSS, trigger_price=1844.52,
                            qty=0.82, message="51023 Position doesnt exist"),
        ),
    }
    report.update(overrides)
    return report


def _resume_report(**overrides):
    report = {
        "symbol": "ETH-USDT-SWAP",
        "position_side": "long",
        "entry_price": 1816.66,
        "qty": 0.055,
        "ok_count": 3,
        "order_id": "4105634441777123328",
        "fill_confirmed": False,
        "protections": (
            ProtectionOrder(kind=PROTECT_STOP_LOSS, ok=False, trigger_price=1800.63,
                            qty=0.055, message="51278"),
            ProtectionOrder(kind=PROTECT_TAKE_PROFIT_1, ok=True, trigger_price=1824.14,
                            qty=0.028, order_id="444", message=""),
        ),
    }
    report.update(overrides)
    return report


def test_unconfirmed_resume_sends_a_check_reminder_not_a_fill_report(captured):
    """2026-11-17 事故：委托没成交却推了「成交回报」。

    fill_confirmed 为 False 时必须走核对提示，绝不能走 send_trade_report。
    """
    assert trade_report.notify_trigger_resumed(_resume_report(), venue="okx") is True

    assert captured[0]["action"] != "钉钉成交回报"
    body = _body(captured)
    assert "未确认成交" in body
    assert "ETH-USDT-SWAP" in body
    assert "4105634441777123328" in body
    assert "止损：失败" in body
    assert "TP1：已挂" in body


def test_confirmed_resume_still_sends_the_fill_report(captured):
    ok = trade_report.notify_trigger_resumed(
        _resume_report(fill_confirmed=True), venue="okx"
    )

    assert ok is True
    assert captured[0]["action"] == "钉钉成交回报"


def test_resume_report_without_the_flag_keeps_the_old_behaviour(captured):
    """旧流水没有 fill_confirmed 字段，不能因为缺字段就把成交回报吞掉。"""
    report = _resume_report()
    report.pop("fill_confirmed")

    assert trade_report.notify_trigger_resumed(report, venue="okx") is True
    assert captured[0]["action"] == "钉钉成交回报"


def test_unconfirmed_resume_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dingtalk_notifier, "send_markdown", boom)
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: True
    )
    assert trade_report.notify_trigger_resumed(_resume_report()) is False


def test_protection_failure_is_pushed_with_exchange_reasons(captured):
    """成交了但一条保护单都没挂上，必须推一条带交易所原因的告警。"""
    assert trade_report.notify_protection_failure(_failure_report(), venue="okx") is True

    body = _body(captured)
    assert "保护单未挂上" in body
    assert "ETH-USDT-SWAP" in body
    assert "做空" in body
    assert "51023" in body, "交易所拒绝原因必须原样带出"
    assert "第 1 次" in body
    assert "下一轮继续重试" in body


def test_protection_failure_final_asks_for_manual_action(captured):
    """达到重试上限的告警要说清「已放弃」，并明确要求人工介入。"""
    ok = trade_report.notify_protection_failure(
        _failure_report(final=True, attempts=3), venue="okx"
    )

    assert ok is True
    body = _body(captured)
    assert "已放弃自动重试" in body
    assert "人工" in body


def test_protection_failure_ignores_empty_report(captured):
    assert trade_report.notify_protection_failure(None) is False
    assert trade_report.notify_protection_failure({}) is False
    assert captured == []


def test_protection_failure_skips_when_notifier_disabled(captured, monkeypatch):
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: False
    )
    assert trade_report.notify_protection_failure(_failure_report()) is False
    assert captured == []


def test_protection_failure_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dingtalk_notifier, "send_markdown", boom)
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: True
    )
    assert trade_report.notify_protection_failure(_failure_report()) is False

def test_short_side_is_labelled():
    _, body = dingtalk_notifier._build_trade_markdown(
        venue="binance",
        symbol="BTCUSDT",
        order_id="x",
        side="SELL",
        order_type="MARKET",
        fill_price=68000.0,
        qty=0.001,
        notional_usdt=68.0,
        margin_usdt=0.0,
        leverage=1,
        margin_mode="isolated",
        market_type="spot",
        executed_at="2026-11-14 01:00:00",
    )
    assert "做空" in body
    assert "Binance" in body and "现货" in body


# ── 绝不抛异常 ────────────────────────────────────────────────────────────────
def test_sender_swallows_exceptions(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dingtalk_notifier, "_build_trade_markdown", boom)
    monkeypatch.setattr(
        dingtalk_notifier, "dingtalk_is_active", lambda settings=None: True
    )
    # 直接打 notifier 层：返回 False 而不是抛
    assert (
        dingtalk_notifier.send_trade_report(
            venue="okx",
            symbol="ETH-USDT-SWAP",
            order_id="1",
            side="BUY",
            order_type="MARKET",
            fill_price=1.0,
            qty=1.0,
            notional_usdt=1.0,
            margin_usdt=0.0,
            leverage=1,
            margin_mode="isolated",
            market_type="spot",
            executed_at="now",
        )
        is False
    )


# ── AutoTrader.last_resume_report 的形状 ──────────────────────────────────────
def test_auto_trader_exposes_resume_report_attribute():
    from pa_agent.trading.auto_trader import AutoTrader

    trader = AutoTrader({"enabled": False}, venue="okx")
    assert hasattr(trader, "last_resume_report")
    assert trader.last_resume_report is None


def test_trade_report_module_has_no_gui_or_qt_imports():
    """notify 层必须能在无头守护进程里导入（不能拉进 PyQt）."""
    src = io.open(trade_report.__file__, encoding="utf-8").read()
    assert "PyQt" not in src
    assert "pa_agent.gui" not in src
