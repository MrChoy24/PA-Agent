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

#: 保护单种类的中文标签（本地映射：notify 层不 import pa_agent.trading）
_PROTECT_KIND_LABELS = {
    "stop_loss": "止损",
    "take_profit_1": "TP1",
    "take_profit_2": "TP2",
    "take_profit": "止盈",
}


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

        # 没能从交易所确认这笔委托成交时，绝不能发「成交回报」——
        # 2026-11-17 04:10 事故：buy 限价单从未成交，查单失败后退回看持仓，
        # 把上一笔多头的均价当成本单成交价发给了钉钉。
        if report.get("fill_confirmed") is False:
            return _notify_unconfirmed_resume(report, settings=settings)

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


def _notify_unconfirmed_resume(
    report: Any,
    *,
    settings: "Settings | None" = None,
) -> bool:
    """补挂保护单完成、但**未能确认这笔委托真的成交**时发的核对提示。

    与 notify_trigger_resumed 的区别：那条是「成交回报」，可以据此认为
    仓位已经建立；这条明确写「未确认成交」，要求人工核对。宁可少一条成交喜报，
    也不能让用户以为一笔没成交的委托已经建仓。
    """
    try:
        from pa_agent.notify import dingtalk_notifier

        symbol = str(report.get("symbol") or "")
        side = "做多" if str(report.get("position_side") or "") == "long" else "做空"
        protections = report.get("protections") or ()
        total = len(protections)
        # 保护单可能是 dict，也可能是 ProtectionOrder 对象（notify 层不 import 交易层）
        ok_count = sum(
            1
            for item in protections
            if bool(item.get("ok") if isinstance(item, dict) else getattr(item, "ok", False))
        )
        if total == 0:
            ok_count = int(report.get("ok_count") or 0)
        lines = [
            f"### ⚠️ 保护单已补挂，但未确认成交（{symbol or '—'}）",
            "",
            "> 交易所没能回报这笔委托的成交状态，本次保护单是按**当前持仓**挂的。",
            "> 请到交易所核对：委托是否成交、持仓数量与止损价位是否匹配。",
            "",
            f"- 方向：{side}",
            f"- 持仓数量：{report.get('qty') or 0}",
            f"- 参考均价：{report.get('entry_price') or 0}",
            f"- 委托号：{report.get('order_id') or '—'}",
            f"- 保护单：{ok_count}/{total} 条已挂" if total else f"- 保护单：{ok_count} 条已挂",
        ]
        for item in protections:
            if isinstance(item, dict):
                kind = str(item.get("kind") or "")
                ok_flag = bool(item.get("ok"))
                price = item.get("trigger_price")
            else:  # ProtectionOrder 之类的对象
                kind = str(getattr(item, "kind", "") or "")
                ok_flag = bool(getattr(item, "ok", False))
                price = getattr(item, "trigger_price", None)
            label = _PROTECT_KIND_LABELS.get(kind, kind or "—")
            state = "已挂" if ok_flag else "失败"
            lines.append(f"    - {label}：{state} @ {price or '—'}")
        return dingtalk_notifier.send_markdown(
            f"未确认成交（{symbol or '—'}）",
            "\n".join(lines),
            settings=settings,
            # 2026-11-17 审计 P2-9：这是订单相关告警，必须打 order=True，
            # 否则 notify_on_order_only=true 时会被当普通文本吞掉。
            order=True,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不影响交易
        logger.warning("未确认成交提示推送失败（忽略）：%s", exc)
        return False


def notify_protection_failure(
    report: Any,
    *,
    settings: "Settings | None" = None,
    venue: str = "",
) -> bool:
    """仓位已成交、但保护单一条都没挂上时推一条告警。

    ``report`` 是 :attr:`pa_agent.trading.auto_trader.AutoTrader.last_resume_failure`
    （一个 dict）。这不是「成交回报」，而是**裸奔告警**：屏幕上写着止损 1844，
    交易所里其实一张条件单都没有，出问题就是无上限亏损，所以它必须比成交回报
    更响（标题带「⚠️ 保护单未挂上」）。

    放在本模块而不是 ``dingtalk_notifier``：判断「该不该发」的逻辑要和成交回报
    放一起，避免调用方各写一套。真正的发送仍走 ``send_markdown``。

    只依赖本模块与 ``dingtalk_notifier``，不 import ``pa_agent.trading``，也不碰 GUI 层。
    """
    try:
        if not isinstance(report, dict) or not report:
            return False

        from pa_agent.notify import dingtalk_notifier

        if not dingtalk_notifier.dingtalk_is_active(settings):
            return False

        symbol = str(report.get("symbol") or report.get("venue_symbol") or "—")
        side = str(report.get("position_side") or "")
        side_text = "做多" if side == "long" else ("做空" if side == "short" else "—")
        attempts = report.get("attempts") or 1
        final = bool(report.get("final"))
        reasons = [str(r) for r in (report.get("reasons") or []) if str(r).strip()]

        lines = [
            f"#### ⚠️ 保护单未挂上（{symbol}）",
            "",
            f"- 交易对：{symbol}（{venue or report.get('venue') or '—'}）",
            f"- 方向：{side_text}　数量：{dingtalk_notifier._fmt(report.get('qty'))}",
            f"- 成交均价：{dingtalk_notifier._fmt_price(report.get('entry_price'))}",
            f"- 委托号：{report.get('order_id') or '—'}",
            f"- 尝试次数：第 {attempts} 次" + ("（已放弃自动重试）" if final else "（下一轮继续重试）"),
            "",
        ]
        if reasons:
            lines.append("**交易所拒绝原因**")
            lines.extend(f"- {r}" for r in reasons[:6])
            lines.append("")
        if final:
            lines.append("> ❗ 仓位当前**没有止损/止盈**保护，请立即人工平仓或手动挂保护单。")
        else:
            lines.append("> 该仓位暂时没有保护单，程序会自动重试；若持续失败请人工介入。")

        return dingtalk_notifier.send_markdown(
            f"保护单未挂上（{symbol}）",
            "\n".join(lines),
            settings=settings,
            # 2026-11-17 审计 P2-9：裸奔告警属于订单类，必须打 order=True，
            # 否则 notify_on_order_only=true 时这条最重要的告警反而发不出去。
            order=True,
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不影响交易
        logger.warning("保护单失败告警推送失败（忽略）：%s", exc)
        return False
