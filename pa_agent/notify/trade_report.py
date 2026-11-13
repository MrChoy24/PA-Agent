"""实盘成交回报：把「交易所已经成交」的事实推送到钉钉。

与 :func:`pa_agent.notify.dingtalk_notifier.send_order_signal` 的区别
--------------------------------------------------------------
``send_order_signal`` 推的是**决策意图**（分析出「可以做多 ETH，入场 1930」），
它发生在下单之前，可能根本不会成交。本模块推的是**成交事实**：
真实成交均价、真实数量、名义金额与占用保证金、下单时间、委托号，
以及止损 / TP1 / TP2 有没有真的挂到交易所侧。

触发条件（三条硬约束）
----------------------
1. ``outcome.executed`` 必须为 True（真的提交了单）；
2. ``outcome.dry_run`` 必须为 False（试运行一律不推，绝不制造「我开仓了」的错觉）；
3. ``outcome.reason`` 必须是 ``submitted``（已成交）或 ``pending-trigger``
   （计划委托已挂、等触发）。

任何异常都在本模块内吞掉：通知失败不能影响交易主流程，也不能让已经
成交的仓位因为「推送没发出去」而被上层当成失败处理。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from pa_agent.config.settings import Settings
    from pa_agent.trading.auto_trader import AutoTradeOutcome

logger = logging.getLogger(__name__)

#: 会触发成交回报的 reason（其余 reason 都代表「没有真实成交」）
_REPORTABLE_REASONS = frozenset({"submitted", "pending-trigger"})


def _now_text() -> str:
    """本地时区的下单时间文本（与日志、流水记录同一口径）."""
    try:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover - 极端环境
        return ""


def _is_pending_trigger(outcome: "AutoTradeOutcome") -> bool:
    """True 表示这是「计划委托已挂、尚未成交」而不是「已经成交」."""
    if (outcome.reason or "") == "pending-trigger":
        return True
    raw = getattr(outcome.result, "raw", None)
    return bool(isinstance(raw, dict) and raw.get("pending"))


def should_report(outcome: "AutoTradeOutcome | None") -> bool:
    """判断一条交易结果是否值得推送成交回报（纯函数，无副作用）."""
    if outcome is None:
        return False
    if not getattr(outcome, "executed", False):
        return False
    # 试运行永不推送 —— 这是红线，不是配置项。
    if getattr(outcome, "dry_run", True):
        return False
    return str(getattr(outcome, "reason", "") or "") in _REPORTABLE_REASONS


def notify_trade_outcome(
    outcome: "AutoTradeOutcome | None",
    *,
    settings: "Settings | None" = None,
    venue: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> bool:
    """把一条真实成交结果推送到钉钉，返回是否发送成功。

    调用方（守护进程 tick / 桌面版分析完成）只需在 ``evaluate()`` 之后
    无脑调用；本函数自己判断该不该发、该发什么。
    """
    try:
        if not should_report(outcome):
            return False

        from pa_agent.notify import dingtalk_notifier

        # 未启用/未配置时早点返回，省掉构建 markdown 的开销。
        if not dingtalk_notifier.dingtalk_is_active(settings):
            return False

        result = getattr(outcome, "result", None)
        pending = _is_pending_trigger(outcome)

        side = str(getattr(result, "side", "") or "")
        order_type = str(getattr(result, "order_type", "") or "")
        order_id = str(getattr(result, "order_id", "") or "")
        qty = getattr(result, "qty", 0.0)
        fill_price = getattr(result, "price", 0.0)
        market_type = str(getattr(result, "market_type", "") or "")
        margin_mode = str(getattr(result, "margin_mode", "") or "")
        protections = getattr(result, "protections", ()) or ()

        trigger_price = 0.0
        if pending:
            raw = getattr(result, "raw", None)
            if isinstance(raw, dict):
                trigger_price = raw.get("trigger_price") or 0.0
            if not trigger_price:
                trigger_price = fill_price

        return dingtalk_notifier.send_trade_report(
            venue=venue or str(getattr(result, "venue", "") or ""),
            symbol=symbol or str(getattr(result, "symbol", "") or ""),
            order_id=order_id,
            side=side,
            order_type=order_type,
            fill_price=fill_price,
            qty=qty,
            notional_usdt=getattr(outcome, "notional_usdt", 0.0),
            margin_usdt=getattr(outcome, "margin_usdt", 0.0),
            leverage=getattr(outcome, "leverage", 1),
            margin_mode=margin_mode,
            market_type=market_type,
            executed_at=_now_text(),
            protections=protections,
            pending_trigger=pending,
            trigger_price=trigger_price,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不影响交易
        logger.warning("实盘成交回报推送失败（忽略）：%s", exc)
        return False


def notify_trigger_resumed(
    report: Any,
    *,
    settings: "Settings | None" = None,
    venue: str = "",
) -> bool:
    """突破单触发成交、补偿逻辑补挂完保护单之后，推一条成交回报。

    ``report`` 是 :attr:`pa_agent.trading.auto_trader.AutoTrader.last_resume_report`
    （一个 dict）。传 None 时直接返回。
    """
    try:
        if not isinstance(report, dict) or not report:
            return False

        from pa_agent.notify import dingtalk_notifier

        if not dingtalk_notifier.dingtalk_is_active(settings):
            return False

        return dingtalk_notifier.send_trade_report(
            venue=venue,
            symbol=str(report.get("symbol") or ""),
            order_id=str(report.get("order_id") or ""),
            side="BUY" if str(report.get("position_side") or "") == "long" else "SELL",
            order_type=str(report.get("order_type") or "TRIGGER"),
            fill_price=report.get("entry_price") or 0.0,
            qty=report.get("qty") or 0.0,
            notional_usdt=report.get("notional_usdt") or 0.0,
            margin_usdt=report.get("margin_usdt") or 0.0,
            leverage=report.get("leverage") or 1,
            margin_mode=str(report.get("margin_mode") or ""),
            market_type=str(report.get("market_type") or ""),
            executed_at=str(report.get("executed_at") or _now_text()),
            protections=report.get("protections") or (),
            pending_trigger=False,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不影响交易
        logger.warning("突破单成交回报推送失败（忽略）：%s", exc)
        return False
