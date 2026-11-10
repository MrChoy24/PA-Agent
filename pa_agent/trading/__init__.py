"""交易执行层：把「阶段二决策」安全地变成一个真实的现货下单请求。

自包含的交易所执行包，与数据层（:mod:`pa_agent.data`）解耦：

* :class:`~pa_agent.trading.common.TradeResult` —— 统一的下单/撤单结果
* :class:`~pa_agent.trading.binance_trader.BinanceTrader` —— 币安 SPOT
* :class:`~pa_agent.trading.okx_trader.OKXTrader` —— OKX SPOT（tdMode=cash）
* :class:`~pa_agent.trading.auto_trader.AutoTrader` —— 带风控闸门的自动下单

本包内 **不导入 PyQt，也不导入 pa_agent.gui**：可以放心在后台线程、
脚本或测试中直接使用。所有公开方法都不会抛异常，失败时返回
``ok=False`` 并带上交易所自身的报错文本。

Usage:
    from pa_agent.trading import BinanceTrader, AutoTrader

    ok, msg = BinanceTrader().test_connection()
    trader = AutoTrader({"enabled": True, "dry_run": True})
    print(trader.evaluate(decision, symbol="BTCUSDT", timeframe="15m").reason)
"""
from __future__ import annotations

from pa_agent.trading.auto_trader import AutoTradeOutcome, AutoTrader
from pa_agent.trading.binance_trader import BinanceTrader
from pa_agent.trading.common import (
    TradeResult,
    floor_to_precision,
    floor_to_step,
    format_decimal,
)
from pa_agent.trading.okx_trader import OKXTrader

__all__ = [
    "TradeResult",
    "BinanceTrader",
    "OKXTrader",
    "AutoTrader",
    "AutoTradeOutcome",
    "floor_to_step",
    "floor_to_precision",
    "format_decimal",
]
