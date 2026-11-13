"""自动下单编排：把「阶段二决策」变成一次经过风控阉割的下单尝试。

本模块**不导入 GUI**、不导入 PyQt：为了复用「这条决策是不是一个下单
机会」的判定，这里按 :mod:`pa_agent.gui.order_opportunity` 的规则重新
实现了一个最小版本（``order_type`` 属于 限价单/突破单/市价单）。

默认行为是最保守的：``enabled=False`` + ``dry_run=True`` —— 即使被
调用，也只是把**打算下的单**写进日志并返回 ``reason="dry-run"``。

风控闸门顺序（任一不通过即返回 ``executed=False`` 及其原因）：

1. ``enabled`` 为假 → ``disabled``
2. ``trade_confidence`` 低于 ``min_confidence`` → ``low-confidence``
3. 决策里没有下单意图 → ``no-order``
4. 要求止损但决策缺止损 → ``no-stop-loss``
5. 交易对不在白名单（白名单非空时） → ``symbol-not-allowed``
6. 当日成交/下单次数已达上限 → ``daily-limit``
7. 名义金额超过上限 → 收敛到 ``max_notional_usdt``（记为 ``clamped``，继续）
8. **杠杆夹取**（第 7 步之后、dry-run 之前）：``leverage`` 夹到 ``[1,125]``
   并据此重算名义金额 / 保证金；现货强制 ``leverage=1``
9. ``dry_run`` → 不发单，返回 ``executed=True, reason="dry-run"`` 与拟下单结果
10. 否则真正调用交易所客户端；失败不抛异常，只返回 ``ok=False`` 的结果

每次 :meth:`AutoTrader.evaluate` 都会尝试向 ``log_path`` 追加一行 JSON，
写日志失败只记录日志、绝不抛异常。

下单参数（对应交易终端的「下单参数」面板）全部来自 ``settings``，
缺省值保持原有的现货行为：

========================  ===========================================  ==========
键                      含义                                         默认值
========================  ===========================================  ==========
``market_type``         ``spot`` / ``futures``                       ``spot``
``leverage``            杠杆倍数（自动夹到 1~125）                    ``1``
``margin_per_trade_usdt`` 每单保证金；>0 时名义金额 = 保证金 × 杠杆   ``0.0``
                        （逐仓专用，且**仍受 ``max_notional_usdt`` 封顶**）
``order_type``          ``MARKET`` / ``LIMIT``（决策里没有可用值时）  ``MARKET``
``margin_mode``         ``isolated`` / ``cross``                     ``isolated``
``position_side``       ``long`` / ``short``；留空则由决策方向推导     ``""``
========================  ===========================================  ==========

Usage:
    trader = AutoTrader({"enabled": True, "dry_run": True, "min_confidence": 60})
    outcome = trader.evaluate(decision, symbol="BTCUSDT", timeframe="15m")
    print(outcome.executed, outcome.reason)

    # 合约：每单 10 USDT 保证金、20 倍杠杆、逐仓、跟随决策方向
    futures = AutoTrader({
        "enabled": True, "dry_run": True,
        "market_type": "futures", "leverage": 20,
        "margin_per_trade_usdt": 10.0, "margin_mode": "isolated",
    })
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pa_agent.data.crypto_common import normalize_neutral, to_okx_inst_id
from pa_agent.trading.binance_trader import BinanceTrader
from pa_agent.trading.common import (
    DEFAULT_MAX_PRICE_DEVIATION_PCT,
    MARKET_FUTURES,
    MARKET_SPOT,
    MARGIN_CROSS,
    MARGIN_ISOLATED,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_TRIGGER,
    POSITION_LONG,
    POSITION_SHORT,
    PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2,
    ProtectionOrder,
    TradeResult,
    clamp_leverage,
    format_decimal,
    normalize_margin_mode,
    normalize_market_type,
    normalize_order_type,
    normalize_position_side,
    plan_protections,
    protection_label,
    to_float,
    to_text,
)
from pa_agent.trading.okx_trader import OKXTrader
from pa_agent.util import filelock

logger = logging.getLogger(__name__)

# 与 pa_agent.gui.order_opportunity.ORDER_OPPORTUNITY_TYPES 保持一致
# （不能 import gui：那是 PyQt 层）
ORDER_OPPORTUNITY_TYPES: frozenset[str] = frozenset({"限价单", "突破单", "市价单"})

_DEFAULT_LOG_PATH = os.path.join("trade_records", "auto_orders.jsonl")

#: 成交价与决策入场价的默认容忍偏差（bps = 万分之一）。
#: 15 USDT 量级实测滑点约 0.0002 USDT（≈0.1 bps），30 bps 足够宽松；
#: 它只触发日志告警，不会拒单或平仓。
DEFAULT_MAX_SLIPPAGE_BPS = 30.0

#: JSONL 里存的 kind 就是 PROTECT_* 常量本身，这里只是显式声明依赖，
#: 已经打过「真实下单模式」告警的组合；同一个进程里只喊一次。
#: 守护进程每 20 秒就会新建一个 AutoTrader 做「待触发补偿」检查，
#: 不去重的话这条 WARNING 会把整个日志刷满（实测 100 行里 34 行是它）。
_LIVE_WARNED: set[tuple[str, str, int, float]] = set()

#: 避免以后有人改了常量值却忘了同步读取端。
PENDING_KIND_ALIASES: dict[str, str] = {
    PROTECT_STOP_LOSS: PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1: PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2: PROTECT_TAKE_PROFIT_2,
}

# 归一化后的交易对形状：BTCUSDT / 1000SHIBUSDT / ETHUSDT
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{5,24}$")

# 方向 → 交易所 side
_BUY_WORDS: frozenset[str] = frozenset(
    {"buy", "long", "做多", "多头", "看多", "bullish", "买", "买入"}
)
_SELL_WORDS: frozenset[str] = frozenset(
    {"sell", "short", "做空", "空头", "看空", "bearish", "卖", "卖出"}
)

# order_status 取值（写进 JSONL，供上层统计）
_STATUS_FILLED = "filled"
_STATUS_PENDING = "pending"
_STATUS_REJECTED = "rejected"
_STATUS_DRY_RUN = "dry-run"
_STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class AutoTradeOutcome:
    """一次自动下单评估的结果。

    ``executed=True`` 包含 dry-run（只记录、不真正发单）与真实成交提交。

    合约相关字段带默认值，现货调用方（含既有测试）不需要关心：
    ``leverage`` 是实际使用的杠杆（现货恒为 1），``notional_usdt`` 是名义
    金额，``margin_usdt`` 是每单保证金（= 名义金额 / 杠杆）。
    """

    executed: bool
    dry_run: bool
    reason: str
    result: TradeResult | None = None
    leverage: int = 1
    notional_usdt: float = 0.0
    margin_usdt: float = 0.0


def _parse_confidence(decision: Mapping[str, Any]) -> int | None:
    """Extract ``trade_confidence`` as a 0-100 int, or ``None`` if absent/invalid."""
    raw = decision.get("trade_confidence")
    if raw is None or raw == "":
        return None
    try:
        return max(0, min(100, int(float(str(raw).strip()))))
    except (ValueError, TypeError):
        return None


def _decide_side(decision: Mapping[str, Any]) -> str:
    """Map decision direction to a neutral ``BUY``/``SELL`` (``""`` if unknown)."""
    raw = decision.get("order_direction")
    if not raw:
        raw = decision.get("direction")
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in _BUY_WORDS:
        return "BUY"
    if text in _SELL_WORDS:
        return "SELL"
    # 「偏多/看跌」等复合写法
    for word in _BUY_WORDS:
        if word in text:
            return "BUY"
    for word in _SELL_WORDS:
        if word in text:
            return "SELL"
    return ""


def _decide_order_type(decision: Mapping[str, Any]) -> str:
    """Map 中文下单方式 to ``MARKET``/``LIMIT``/``TRIGGER``.

    突破单走 ``TRIGGER``（计划委托）：决策算出 entry_price 时价格可能还没到，
    分析本身又要跑几分钟；追市价滑点不可控，挂限价又常在突破瞬间不成交，
    所以先把触发价挂上去，等它被穿再按市价入场。
    ``""`` 表示这不是一个下单决策。
    """
    raw = str(decision.get("order_type") or "").strip()
    if raw == "市价单":
        return "MARKET"
    if raw == "突破单":
        return "TRIGGER"
    if raw == "限价单":
        return "LIMIT"
    return ""


def _has_stop_loss(decision: Mapping[str, Any]) -> bool:
    """True when the decision carries a usable stop-loss price."""
    for key in ("stop_loss_price", "stop_loss"):
        if key not in decision:
            continue
        if to_float(decision.get(key), 0.0) > 0:
            return True
    return False


class AutoTrader:
    """把决策转成下单动作的安全封装（默认禁用 + 试运行）。

    ``settings`` 是一个普通 dict，支持的键见模块文档；缺省值即最保守配置。
    额外的连接凭据键（``api_key`` / ``api_secret`` / ``passphrase`` /
    ``testnet`` / ``demo`` / ``timeout_s``）会被透传给交易所客户端，
    这样上层可以只构造一个 dict 就完成配置。

    新增的「下单参数面板」键（``market_type`` / ``leverage`` /
    ``margin_per_trade_usdt`` / ``order_type`` / ``margin_mode`` /
    ``position_side``）同样有默认值，不传时行为与旧版完全一致。
    """

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        venue: str = "binance",
    ) -> None:
        opts: dict[str, Any] = dict(settings or {})

        self.enabled: bool = bool(opts.get("enabled", False))
        self.dry_run: bool = bool(opts.get("dry_run", True))
        self.min_confidence: int = int(to_float(opts.get("min_confidence"), 60))
        self.max_notional_usdt: float = to_float(opts.get("max_notional_usdt"), 50.0)
        self.max_daily_orders: int = int(to_float(opts.get("max_daily_orders"), 10))
        self.allowed_symbols: tuple[str, ...] = tuple(
            text
            for text in (self._neutral_symbol(s) for s in (opts.get("allowed_symbols") or ()))
            if text
        )
        self.require_stop_loss: bool = bool(opts.get("require_stop_loss", True))
        self.log_path: str = str(opts.get("log_path") or _DEFAULT_LOG_PATH)
        self.venue: str = (venue or "binance").strip().lower()

        # ── 下单参数面板 ──
        self.market_type: str = normalize_market_type(opts.get("market_type"))
        # 注意：这里先按原始值夹取，evaluate 里还会再夹一次（Gate 8）
        self.leverage: int = clamp_leverage(opts.get("leverage"))
        self.margin_per_trade_usdt: float = to_float(
            opts.get("margin_per_trade_usdt"), 0.0
        )
        # 决策没有给出下单方式时的兜底类型
        self.order_type: str = normalize_order_type(opts.get("order_type"))
        self.margin_mode: str = normalize_margin_mode(opts.get("margin_mode"))
        # 显式配置的方向（留空 = 跟随决策：做多→long，做空→short）
        raw_position_side = to_text(opts.get("position_side"))
        self.position_side: str = (
            normalize_position_side(raw_position_side) if raw_position_side else ""
        )

        # ── 跨所价差保护 ──
        # 行情源与下单所不是同一家时（如 Binance 行情 + OKX 下单），
        # 决策里的 entry_price 来自行情源；下单前比一下下单所的真实价格。
        # <=0 表示关闭该校验。
        deviation = to_float(
            opts.get("max_price_deviation_pct"), DEFAULT_MAX_PRICE_DEVIATION_PCT
        )
        self.max_price_deviation_pct: float = max(0.0, deviation)

        # ── 开仓后立刻挂保护单（止损 / TP1 / TP2） ──
        # True = 开仓成交后马上把这三个价提交给交易所（条件单），
        # 这样本程序崩了、容器重启、服务器断连都不影响止盈止损生效。
        self.protect_after_entry: bool = bool(opts.get("protect_after_entry", True))
        # TP1 拿多少比例（0~1）；剩下的给 TP2。止损永远全量。
        self.tp1_fraction: float = to_float(opts.get("tp1_fraction"), 0.5)

        # ── 滑点告警 ──
        # 成交均价与决策入场价相差超过这个 bps（万分之一）就告警。
        # 刻意**只告警不拒绝**：拒单会让已经成交的仓位裸奔，
        # 而滑点本身是市场条件，不是配置错误。<=0 关闭。
        self.max_slippage_bps: float = to_float(
            opts.get("max_slippage_bps"), DEFAULT_MAX_SLIPPAGE_BPS
        )

        # 交易客户端凭据（可选）
        self._api_key: str = str(opts.get("api_key") or "")
        self._api_secret: str = str(opts.get("api_secret") or "")
        self._passphrase: str = str(opts.get("passphrase") or "")

        #: 最近一次「突破单触发后补挂保护单」的明细（供上层推送实盘成交回报）。
        #: 只在 resume_pending_trigger 真正补挂成功时被覆盖；None 表示没有新事件。
        self.last_resume_report: dict[str, Any] | None = None
        self._testnet: bool = bool(opts.get("testnet", False))
        self._demo: bool = bool(opts.get("demo", False))
        self._timeout_s: float = to_float(opts.get("timeout_s"), 10.0)

        self._trader = None   # 延迟创建，避免未配置凭据时也建连接

        # ── 真实资金保护 ──
        # 走到上面的 dry_run=False 说明会真的下单。这条日志的意义是：
        # 任何时候翻日志都能一眼看出「这次跑的是实盘」。
        # 另外 PA_AGENT_FORCE_DRY_RUN=1 可以把任何配置强制拉回试运行，
        # 用于「只想验证一遍流程、不想碰钱」的场景（测试/冒烟/回归）。
        if os.environ.get("PA_AGENT_FORCE_DRY_RUN", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            if not self.dry_run:
                logger.warning(
                    "环境变量 PA_AGENT_FORCE_DRY_RUN 生效：自动交易已从【真实下单】"
                    "强制改为试运行（原配置 dry_run=False）"
                )
            self.dry_run = True
        if not self.dry_run and self.enabled:
            # 每个「venue + 市场 + 杠杆 + 上限」组合每进程只喊一次：
            # 这条是给翻日志的人看的，不是每轮心跳。
            _warn_key = (
                str(self.venue),
                str(self.market_type),
                int(self.leverage),
                float(self.max_notional_usdt),
            )
            if _warn_key not in _LIVE_WARNED:
                _LIVE_WARNED.add(_warn_key)
                logger.warning(
                    "自动交易以【真实下单】模式启动：venue=%s 市场=%s 杠杆=%sx "
                    "单笔上限 %s USDT —— 会动用真实资金。只想验证流程请设 "
                    "PA_AGENT_FORCE_DRY_RUN=1 或把 dry_run 改回 true。",
                    self.venue,
                    self.market_type,
                    self.leverage,
                    self.max_notional_usdt,
                )

    # ── 交易所客户端 ──────────────────────────────────────────────────────────

    def _client(self):
        """Lazily build the venue client (``None`` for an unsupported venue)."""
        if self._trader is not None:
            return self._trader
        try:
            if self.venue == "okx":
                self._trader = OKXTrader(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    passphrase=self._passphrase,
                    demo=self._demo,
                    timeout_s=self._timeout_s,
                    market_type=self.market_type,
                    leverage=self.leverage,
                    margin_mode=self.margin_mode,
                )
            elif self.venue == "binance":
                self._trader = BinanceTrader(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    testnet=self._testnet,
                    timeout_s=self._timeout_s,
                    market_type=self.market_type,
                    leverage=self.leverage,
                    margin_mode=self.margin_mode,
                )
            else:
                logger.warning("AutoTrader: 未知交易所 venue=%r", self.venue)
                return None
        except Exception as exc:  # noqa: BLE001 — 构造失败也不能抛
            logger.warning("AutoTrader: 创建 %s 客户端失败: %s", self.venue, exc)
            return None
        return self._trader

    def _symbol_for_venue(self, symbol: str) -> str:
        """Neutral → venue-specific symbol form."""
        neutral = self._neutral_symbol(symbol)
        if not neutral:
            return ""
        if self.venue == "okx":
            return to_okx_inst_id(neutral)
        return neutral

    @staticmethod
    def _neutral_symbol(symbol: Any) -> str:
        """``BTC/USDT`` / ``btc-usdt`` / ``None`` → ``BTCUSDT``（失败返回 ``""``）。"""
        try:
            return normalize_neutral(to_text(symbol))
        except Exception as exc:  # noqa: BLE001 — 归一化失败不能抛
            logger.warning("AutoTrader 交易对归一化失败（%r）: %s", symbol, exc)
            return ""

    # ── 跨所价差保护 ──────────────────────────────────────────────────────────

    def _venue_reference_price(self, symbol: str, *, force: bool = False) -> float:
        """下单交易所的公开最新价；取不到返回 ``0.0``（调用方按「无法校验」处理）。

        ``force=True`` 时即使价差护栏关着也去取价 —— 数量换算需要真实价，
        和「要不要拦单」是两件独立的事。
        """
        if self.max_price_deviation_pct <= 0 and not force:
            return 0.0
        client = self._client()
        if client is None:
            return 0.0
        try:
            return float(client.last_price(self._symbol_for_venue(symbol)))
        except Exception as exc:  # noqa: BLE001 — 取价失败不能抛
            logger.debug("AutoTrader 取 %s 参考价失败: %s", self.venue, exc)
            return 0.0

    # ── 开仓后的保护单（止损 / TP1 / TP2） ────────────────────────────────

    def _slippage_bps(self, decision_price: float, fill_price: float) -> float | None:
        """成交价相对决策入场价的偏差（bps = 万分之一）；不可算时返回 None。

        只做**告警**用，不参与任何拒单/平仓判断：滑点是市场条件，
        不是配置错误，而单一旦成交就必须把保护单挂上去。
        负值表示成交更有利（买得更低/卖得更高），不告警。
        """
        if self.max_slippage_bps <= 0:
            return None
        base = to_float(decision_price, 0.0)
        got = to_float(fill_price, 0.0)
        if base <= 0 or got <= 0:
            return None
        return (got - base) / base * 10_000.0

    def _place_protections(
        self,
        client: Any,
        *,
        venue_symbol: str,
        decision: Mapping[str, Any],
        position_side: str,
        entry_price: float,
        qty: float,
    ) -> tuple[ProtectionOrder, ...]:
        """开仓成交后立刻把止损 / TP1 / TP2 挂到交易所侧。

        这一步存在的意义就是「不依赖本程序」：挂上之后即使容器被删除、
        服务器断电，交易所也会在触发价自己执行止盈止损。

        任何异常都被吞掉并降级成一条失败记录 —— 保护单挂不上不能反过来
        让已经成交的开仓失败。
        """
        if not self.protect_after_entry:
            return ()
        if not qty or qty <= 0:
            logger.warning("自动交易：成交数量为空，无法挂保护单")
            return ()

        plans = plan_protections(
            position_side=position_side,
            entry_price=entry_price,
            stop_loss=decision.get("stop_loss_price", decision.get("stop_loss")),
            take_profit_1=decision.get("take_profit_price"),
            take_profit_2=decision.get("take_profit_price_2"),
            total_qty=qty,
            tp1_fraction=self.tp1_fraction,
        )
        try:
            orders = client.place_protections(
                venue_symbol,
                position_side=position_side,
                plans=plans,
                market_type=self.market_type,
            )
        except Exception as exc:  # noqa: BLE001 — 绝不能让保护单拖垮开仓
            logger.exception("自动交易：挂保护单异常")
            return tuple(
                ProtectionOrder(
                    kind=kind,
                    trigger_price=round(to_float(price, 0.0), 8),
                    qty=round(to_float(leg_qty, 0.0), 8),
                    message=f"挂保护单异常: {exc}",
                )
                for kind, price, leg_qty, _problem in plans
            )

        orders = tuple(orders or ())
        for order in orders:
            if order.ok:
                logger.info("自动交易：%s %s", venue_symbol, order.describe())
            else:
                logger.warning("自动交易：%s %s", venue_symbol, order.describe())
        return orders

    @staticmethod
    def _looks_like_symbol(neutral: str) -> bool:
        """``BTCUSDT`` 这种「≥5 位大写字母数字」才认为是交易对。"""
        return bool(_SYMBOL_RE.match(neutral or ""))

    def resume_pending_trigger(self) -> tuple[bool, str]:
        """检查上一条「突破单计划委托」是否已触发，触发则补挂保护单。

        背景：计划委托挂上去时还没有持仓，当时的保护单价位已经写进
        `trade_records/auto_orders.jsonl` 的 `protection_plan` 字段
        （盘中改不动，也不依赖进程内存）。但**触发成交是交易所侧的异步动作**，
        本程序可能在那一刻正忙着分析别的、或者已经重启——
        这个函数就是那个补偿点：由调用方每轮轮询调用一次。

        返回 `(handled, message)`：`handled=False` 表示「没有待补挂的事」。
        绝不抛异常，也不撤单、不平仓。
        """
        try:
            pending = self._last_pending_trigger()
            if not pending:
                return False, ""
            neutral = self._neutral_symbol(pending.get("symbol", ""))
            if not neutral:
                return False, ""
            plans = pending.get("protection_plan") or []
            if not plans:
                self._mark_trigger_resumed(pending, "no-plan")
                return False, ""

            client = self._client()
            if client is None:
                return False, ""

            venue_symbol = self._symbol_for_venue(neutral)
            pos_ok, _pos_msg, positions = (
                client.list_positions()
                if hasattr(client, "list_positions")
                else (False, "", [])
            )
            if not pos_ok:
                return False, ""
            # 持仓返回的是交易所原始 id（OKX 是 ``ETH-USDT-SWAP``），
            # 而 _symbol_for_venue 只给到 ``ETH-USDT``，不能直接 == 比较。
            held = 0.0
            fill_price = 0.0
            held_symbol = venue_symbol
            for item in positions:
                raw_symbol = str(item.get("symbol", "") or "")
                if not self._position_matches(raw_symbol, venue_symbol):
                    continue
                # 用持仓自己的 instId（OKX 是 ETH-USDT-SWAP）去挂保护单，
                # 而不是推导出来的 ETH-USDT —— 后者是现货 id，会挂错市场。
                held_symbol = raw_symbol or venue_symbol
                held = to_float(item.get("size"), 0.0)
                # 实际成交均价：决策价只是触发价，用它做几何校验会误判。
                fill_price = to_float(item.get("entry_price"), 0.0)
                break
            if held <= 0:
                # 还没成交（或者已经手动平掉）→ 什么都不做
                return False, ""

            position_side = str(pending.get("position_side") or POSITION_LONG)
            if fill_price <= 0:
                fill_price = to_float(pending.get("trigger_price"), 0.0)

            tuple_plans = [
                (
                    PENDING_KIND_ALIASES.get(str(p.get("kind")), str(p.get("kind"))),
                    to_float(p.get("trigger_price"), 0.0),
                    to_float(p.get("qty"), 0.0),
                    str(p.get("problem") or ""),
                )
                for p in plans
                if isinstance(p, dict)
            ]
            protections = client.place_protections(
                held_symbol,
                position_side=position_side,
                plans=tuple_plans,
                market_type=self.market_type,
            )
            ok_count = sum(1 for o in protections if o.ok)
            logger.info(
                "自动交易：突破单已触发（%s 持仓 %s，成交均价 %s），"
                "补挂保护单 %d/%d 条",
                held_symbol, format_decimal(held), format_decimal(fill_price),
                ok_count, len(protections),
            )
            self._mark_trigger_resumed(pending, "resumed", protections)
            # 留一份本次补挂的明细给上层做通知（返回值签名保持不变，
            # 已有 7 处调用方按 (handled, message) 解包）。
            self.last_resume_report = {
                "symbol": held_symbol,
                "position_side": position_side,
                "entry_price": fill_price,
                "qty": held,
                "protections": tuple(protections),
                "ok_count": ok_count,
                "trigger_price": to_float(pending.get("trigger_price"), 0.0),
                "order_id": str(pending.get("order_id") or ""),
                "executed_at": str(pending.get("ts") or ""),
                "notional_usdt": to_float(pending.get("notional_usdt"), 0.0),
                "margin_usdt": to_float(pending.get("margin_usdt"), 0.0),
                "market_type": str(pending.get("market_type") or self.market_type),
                "margin_mode": str(pending.get("margin_mode") or self.margin_mode),
                "leverage": int(to_float(pending.get("leverage"), self.leverage)),
                "order_type": str(pending.get("order_type") or ORDER_TYPE_TRIGGER),
            }
            return True, f"补挂保护单 {ok_count}/{len(protections)} 条"
        except Exception as exc:  # noqa: BLE001 — 补偿逻辑绝不能影响主循环
            logger.warning("AutoTrader 补挂保护单失败: %s", exc)
            return False, ""

    @staticmethod
    def _position_matches(position_symbol: str, venue_symbol: str) -> bool:
        """持仓的 instId 是否属于这个交易对。

        OKX 合约的 instId 是 ``ETH-USDT-SWAP``，现货是 ``ETH-USDT``，
        而 ``_symbol_for_venue`` 只产出 ``ETH-USDT``；Binance 两者相同。
        所以按「前缀 + 分隔符」比较，避免把 ``ETH-USDT`` 误判成 ``ETH-USDTX``。
        """
        left = (position_symbol or "").strip().upper()
        right = (venue_symbol or "").strip().upper()
        if not left or not right:
            return False
        if left == right:
            return True
        return left.startswith(right + "-")

    def _last_pending_trigger(self) -> dict[str, Any] | None:
        """读 JSONL 里最后一条 `pending_trigger=True` 且还没补挂过的记录。

        补挂完成后会追加一条 ``kind="trigger-resumed"`` 的记录，带上原来那个
        ``order_id``。所以判断「已补挂」不能只看最新一行，而是：
        找到最新的 pending 记录 → 再看这个 ``order_id`` 有没有对应的 resumed 记录。
        """
        try:
            path = Path(os.path.abspath(self.log_path))
            if not path.exists():
                return None
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        pending: dict[str, Any] | None = None
        resumed_ids: set[str] = set()
        for line in reversed(lines[-80:]):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "trigger-resumed":
                oid = str(item.get("order_id") or "")
                if oid:
                    resumed_ids.add(oid)
                continue
            if not item.get("pending_trigger"):
                continue
            if pending is None:
                pending = item
        if pending is None:
            return None
        if str(pending.get("order_id") or "") in resumed_ids:
            return None
        return pending

    def _mark_trigger_resumed(
        self,
        pending: dict[str, Any],
        status: str,
        protections: Any = None,
    ) -> None:
        """在 JSONL 末尾追加一条「已补挂」记录（不改原行，保持流水不可变）。"""
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "trigger-resumed",
            "status": status,
            "venue": self.venue,
            "symbol": pending.get("symbol", ""),
            "order_id": pending.get("order_id", ""),
            "trigger_resumed": True,
        }
        if protections is not None:
            record["protections"] = [
                {
                    "kind": o.kind,
                    "ok": o.ok,
                    "trigger_price": o.trigger_price,
                    "qty": o.qty,
                    "order_id": o.order_id,
                    "message": o.message,
                }
                for o in protections
            ]
        self._log_record(record)

    def close(self) -> None:
        """释放底层 HTTP 连接（可重复调用）。"""
        if self._trader is not None:
            try:
                self._trader.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("AutoTrader close failed: %s", exc)
            finally:
                self._trader = None

    # ── 日志 / 计数 ───────────────────────────────────────────────────────────

    def _log_record(self, record: dict[str, Any]) -> None:
        """Append one JSON line; never raises.

        走 :func:`pa_agent.util.filelock.append_line`：加跨进程锁并 ``fsync``。
        自动下单流水是「谁在什么时候开了什么仓」的唯一凭据，
        GUI 与守护进程同时运行时不能出现两行交织的半截 JSON。
        """
        try:
            path = os.path.abspath(self.log_path)
            line = json.dumps(record, ensure_ascii=False, default=str)
            filelock.append_line(path, line)
        except Exception as exc:  # noqa: BLE001 — 日志失败不影响交易流程
            logger.warning("AutoTrader 写日志失败（%s）: %s", self.log_path, exc)

    def _count_today_orders(self) -> int:
        """Count today's executed (non-dry-run) records in the log file.

        兼容两类历史行：新增的 ``order_status`` 字段缺失时回落到
        ``executed``/``dry_run`` 两个老字段判断（老文件照常可读）。
        """
        try:
            path = os.path.abspath(self.log_path)
            if not os.path.isfile(path):
                return 0
            today = datetime.now(timezone.utc).astimezone().date()
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(item, dict):
                        continue
                    status = str(item.get("order_status", "") or "")
                    if status:
                        if status in (_STATUS_DRY_RUN, _STATUS_SKIPPED, _STATUS_REJECTED):
                            continue
                    else:
                        if item.get("dry_run"):
                            continue
                        if not item.get("executed"):
                            continue
                    ts = str(item.get("ts", "") or "")
                    if len(ts) >= 10:
                        if ts[:10] != today.isoformat():
                            continue
                    count += 1
            return count
        except Exception as exc:  # noqa: BLE001
            logger.warning("AutoTrader 读取历史订单失败: %s", exc)
            return 0

    # ── 决策 → 下单参数 ───────────────────────────────────────────────────────

    @staticmethod
    def _notional_from_decision(decision: Mapping[str, Any]) -> float:
        """Best-effort intended notional in USDT (``0`` when unstated)."""
        for key in (
            "notional_usdt",
            "position_size_usdt",
            "order_notional_usdt",
            "amount_usdt",
        ):
            value = to_float(decision.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _entry_price(decision: Mapping[str, Any], last_price: float | None) -> float:
        """Limit price from the decision, else the caller's last price."""
        for key in ("entry_price", "limit_price", "price"):
            value = to_float(decision.get(key), 0.0)
            if value > 0:
                return value
        return to_float(last_price, 0.0)

    def _resolve_position_side(self, side: str) -> str:
        """``self.position_side`` if configured, else derived from BUY/SELL."""
        if self.position_side:
            return self.position_side
        return POSITION_SHORT if side == "SELL" else POSITION_LONG

    @staticmethod
    def _order_status(outcome: "AutoTradeOutcome", result: TradeResult | None) -> str:
        """Map an outcome to one of the five ``order_status`` values."""
        if outcome.dry_run:
            return _STATUS_DRY_RUN
        if not outcome.executed:
            return _STATUS_REJECTED if result is not None else _STATUS_SKIPPED
        if result is not None:
            raw_status = ""
            try:
                raw_status = str((result.raw or {}).get("status", "") or "").upper()
            except Exception:  # noqa: BLE001 — raw 可能是任意对象
                raw_status = ""
            if raw_status and raw_status != "FILLED":
                return _STATUS_PENDING
        return _STATUS_FILLED

    # ── 主流程 ────────────────────────────────────────────────────────────────

    def evaluate(
        self,
        decision: dict,
        *,
        symbol: str,
        timeframe: str,
        last_price: float | None = None,
    ) -> AutoTradeOutcome:
        """Run the gate chain and (unless dry-run) submit one order.

        永远不会抛异常：任何内部错误都降级成 ``executed=False`` 的结果。
        """
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "venue": self.venue,
            "symbol": str(symbol or ""),
            "timeframe": str(timeframe or ""),
            "dry_run": self.dry_run,
            "executed": False,
            "reason": "",
            "order_id": "",
            "side": "",
            "order_type": "",
            "qty": 0.0,
            "price": 0.0,
            "message": "",
            # ── 下单参数面板快照（新增字段，老字段保持不变） ──
            "market_type": self.market_type,
            "leverage": self.leverage,
            "margin_usdt": 0.0,
            "notional_usdt": 0.0,
            "position_side": self.position_side or POSITION_LONG,
            "margin_mode": self.margin_mode,
            "order_status": _STATUS_SKIPPED,
        }

        def finish(outcome: AutoTradeOutcome, message: str = "") -> AutoTradeOutcome:
            result = outcome.result
            record["executed"] = outcome.executed
            record["reason"] = outcome.reason
            record["message"] = message or (result.message if result else "")
            if result is not None:
                record["order_id"] = result.order_id
                record["side"] = result.side
                record["order_type"] = result.order_type
                record["qty"] = result.qty
                record["price"] = result.price
            record["leverage"] = outcome.leverage
            record["margin_usdt"] = outcome.margin_usdt
            record["notional_usdt"] = outcome.notional_usdt
            record["order_status"] = self._order_status(outcome, result)
            self._log_record(record)
            return outcome

        try:
            # Gate 1 — 功能开关
            if not self.enabled:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="disabled: 自动下单未启用（enabled=False）",
                    )
                )

            if not isinstance(decision, dict):
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="no-order: 决策不是字典",
                    )
                )

            # Gate 2 — 置信度
            confidence = _parse_confidence(decision)
            if confidence is None:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="low-confidence: 决策缺少 trade_confidence",
                    )
                )
            if confidence < self.min_confidence:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=(
                            f"low-confidence: trade_confidence={confidence} "
                            f"< min_confidence={self.min_confidence}"
                        ),
                    )
                )

            # Gate 3 — 是否存在下单意图
            order_type_cn = str(decision.get("order_type") or "").strip()
            side = _decide_side(decision)
            order_type = _decide_order_type(decision)
            if order_type_cn not in ORDER_OPPORTUNITY_TYPES or not order_type or not side:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=(
                            f"no-order: order_type={order_type_cn or '—'}, "
                            f"direction={decision.get('order_direction') or '—'}"
                        ),
                    )
                )

            # Gate 4 — 止损（合约加杠杆时更严格，这里不做任何放宽）
            if self.require_stop_loss and not _has_stop_loss(decision):
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="no-stop-loss: 决策未给出有效 stop_loss_price（require_stop_loss=True）",
                    )
                )

            # Gate 5 — 白名单
            neutral = self._neutral_symbol(symbol)
            if not neutral:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="invalid-symbol: 交易对为空",
                    )
                )
            if not self._looks_like_symbol(neutral):
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=f"invalid-symbol: {neutral!r} 不是有效交易对",
                    )
                )
            if self.allowed_symbols and neutral not in self.allowed_symbols:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=f"symbol-not-allowed: {neutral} 不在白名单 {list(self.allowed_symbols)}",
                    )
                )

            # Gate 6 — 当日次数
            today_count = self._count_today_orders()
            if self.max_daily_orders > 0 and today_count >= self.max_daily_orders:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=(
                            f"daily-limit: 今日已下单 {today_count} 笔，"
                            f"达到 max_daily_orders={self.max_daily_orders}"
                        ),
                    )
                )

            # Gate 7 — 名义金额上限（超额收敛，不拒绝）
            price = self._entry_price(decision, last_price)
            if price <= 0:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="no-price: 决策与 last_price 都没有可用价格",
                    )
                )

            # Gate 8 — 跨所价差保护
            # 分析用的行情可能来自另一家交易所（如 Binance 行情 + OKX 下单），
            # 决策价格与下单所真实价格偏离过大时，宁可不下单。
            venue_price = self._venue_reference_price(symbol)
            if self.max_price_deviation_pct > 0 and venue_price > 0:
                deviation_pct = abs(venue_price - price) / price * 100.0
                if deviation_pct > self.max_price_deviation_pct:
                    return finish(
                        AutoTradeOutcome(
                            executed=False,
                            dry_run=self.dry_run,
                            reason=(
                                f"price-deviation: 下单所 {self.venue} 最新价 "
                                f"{format_decimal(venue_price)} 与决策价 "
                                f"{format_decimal(price)} 偏离 "
                                f"{deviation_pct:.2f}% > "
                                f"max_price_deviation_pct={self.max_price_deviation_pct}%"
                            ),
                        )
                    )

            is_futures = self.market_type == MARKET_FUTURES
            # 全仓（cross）没有「每单固定保证金」概念：保证金来自整个账户可用余额，
            # 所以必须忽略 margin_per_trade_usdt，只按决策给出的名义或单笔上限走。
            is_cross = is_futures and self.margin_mode == MARGIN_CROSS
            use_margin = is_futures and not is_cross and self.margin_per_trade_usdt > 0
            notional = self._notional_from_decision(decision)
            clamped = False
            if use_margin:
                # 逐仓 + 面板填了「每单保证金」：名义 = 保证金 × 杠杆。
                # 但**仍然受 max_notional_usdt 封顶**：否则「每单保证金 100 ×
                # 125 倍」会算出 12500 USDT 的名义，配置里那个 15 USDT 的上限
                # 形同虚设（2026-11-14 自检 P1-2）。收敛后保证金同步反推，
                # 日志与实际占用保持一致。
                notional = self.margin_per_trade_usdt * float(self.leverage)
                if self.max_notional_usdt > 0 and notional > self.max_notional_usdt:
                    logger.warning(
                        "自动交易：逐仓名义金额 %.2f USDT（保证金 %.2f × %sx）"
                        "超过单笔上限 %.2f USDT，已收敛到上限",
                        notional,
                        self.margin_per_trade_usdt,
                        self.leverage,
                        self.max_notional_usdt,
                    )
                    notional = self.max_notional_usdt
                    clamped = True
            else:
                # 现货 / 全仓 / 无保证金：用决策名义，缺省落上限，再被上限收敛
                if notional <= 0:
                    notional = self.max_notional_usdt
                if self.max_notional_usdt > 0 and notional > self.max_notional_usdt:
                    notional = self.max_notional_usdt
                    clamped = True
            if notional <= 0:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason="notional-invalid: 名义金额上限配置为 0，无法下单",
                    )
                )

            # Gate 8 — 杠杆夹取（金额收敛之后、dry-run 之前）
            # 现货没有杠杆概念，强制 1；合约夹到 [1,125]
            leverage = clamp_leverage(self.leverage) if is_futures else 1
            # 全仓：保证金不由我们指定，由交易所按「名义 ÷ 杠杆」从账户扣，
            # 这里只给出「预计占用保证金」用于日志与历史记录展示。
            margin_usdt = notional / float(leverage) if leverage > 0 else notional
            if use_margin and not clamped:
                # 逐仓以面板保证金为准，避免夹取杠杆后出现浮点漂移
                margin_usdt = self.margin_per_trade_usdt
                notional = margin_usdt * float(leverage)
            # 被上限收敛过的情况不再回推：此时应保持不变式
            # 名义 ≤ max_notional_usdt，保证金 = 名义 ÷ 杠杆（由上面那行算出）。

            # 数量按**下单所的真实价**算，不是决策价。
            # 分析耗时几分钟，市价单成交价早已不是决策里的 entry_price：
            # 用 2000 算出来的数量拿到 2005 成交，名义金额就超了 0.25%，
            # 杠杆越高、金额越大，偏差越明显。
            # 限价单不受影响 —— 它的成交价不会高于（买入）/低于（卖出）限价。
            sizing_price = price
            if order_type == ORDER_TYPE_MARKET:
                sizing_market = self._venue_reference_price(symbol, force=True)
                if sizing_market > 0:
                    sizing_price = sizing_market
                else:
                    logger.warning(
                        "自动交易：取不到 %s 的实时价，数量仍按决策价 %s 换算"
                        "（成交价可能偏离，名义金额会有偏差）",
                        self.venue,
                        format_decimal(price),
                    )
            qty = notional / sizing_price
            venue_symbol = self._symbol_for_venue(neutral)
            order_price = (
                price
                if order_type in (ORDER_TYPE_LIMIT, ORDER_TYPE_TRIGGER)
                else None
            )
            position_side = self._resolve_position_side(side) if is_futures else POSITION_LONG

            # 试运行也要展示「开仓后会挂哪几条保护单」，否则用户没法在
            # dry_run 阶段确认止盈止损价位是否会被正确提交。
            planned_protections = (
                plan_protections(
                    position_side=position_side,
                    entry_price=price,
                    stop_loss=decision.get("stop_loss_price", decision.get("stop_loss")),
                    take_profit_1=decision.get("take_profit_price"),
                    take_profit_2=decision.get("take_profit_price_2"),
                    total_qty=qty,
                    tp1_fraction=self.tp1_fraction,
                )
                if self.protect_after_entry
                else []
            )
            # 第 4 个元素是 problem（空串 = 这条腿可挂），只预览可挂的那几条
            protect_preview = "".join(
                f"，{protection_label(kind)}@{format_decimal(trigger)}"
                for kind, trigger, _leg_qty, problem in planned_protections
                if not problem
            )

            # Gate 9 — 试运行：不发单，但返回拟下单结果
            if self.dry_run:
                intended = TradeResult(
                    ok=True,
                    venue=self.venue,
                    order_id="",
                    symbol=venue_symbol,
                    side=side if self.venue == "binance" else side.lower(),
                    order_type=order_type if self.venue == "binance" else order_type.lower(),
                    qty=round(qty, 8),
                    price=round(price, 8),
                    message=(
                        f"[dry-run] 拟提交 {self.venue} "
                        f"{'合约' if is_futures else '现货'} {order_type} {side} "
                        f"{venue_symbol}，名义 ≈ {format_decimal(notional)} USDT"
                        + (
                            (
                                f"，{leverage}x 全仓，预计占用保证金 ≈ "
                                f"{format_decimal(margin_usdt)} USDT（由账户可用余额扣），方向 {position_side}"
                                if is_cross
                                else f"，{leverage}x {self.margin_mode}，保证金 ≈ "
                                f"{format_decimal(margin_usdt)} USDT，方向 {position_side}"
                            )
                            if is_futures
                            else ""
                        )
                        + ("（已收敛到 max_notional_usdt）" if clamped else "")
                        + (
                            f"；突破单将以计划委托挂出，触发价 {format_decimal(price)}，"
                            "触发后按市价成交"
                            if order_type == ORDER_TYPE_TRIGGER
                            else ""
                        )
                        + (
                            f"；开仓后将挂保护单{protect_preview}"
                            if protect_preview
                            else ""
                        )
                    ),
                    raw={
                        "notional_usdt": round(notional, 8),
                        "clamped": clamped,
                        "confidence": confidence,
                        "timeframe": str(timeframe or ""),
                        "stop_loss_price": to_float(
                            decision.get("stop_loss_price", decision.get("stop_loss")), 0.0
                        ),
                        "market_type": self.market_type,
                        "leverage": leverage,
                        "margin_usdt": round(margin_usdt, 8),
                        "position_side": position_side,
                        "margin_mode": self.margin_mode,
                    },
                    market_type=self.market_type,
                    leverage=leverage,
                    margin_usdt=round(margin_usdt, 8),
                    notional_usdt=round(notional, 8),
                    position_side=position_side,
                    margin_mode=self.margin_mode if is_futures else MARGIN_ISOLATED,
                )
                reason = "dry-run"
                if clamped:
                    reason = "dry-run (notional-clamped)"
                return finish(
                    AutoTradeOutcome(
                        executed=True,
                        dry_run=True,
                        reason=reason,
                        result=intended,
                        leverage=leverage,
                        notional_usdt=round(notional, 8),
                        margin_usdt=round(margin_usdt, 8),
                    )
                )

            # Gate 10 — 真实下单
            client = self._client()
            if client is None:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=False,
                        reason=f"unsupported-venue: {self.venue}",
                        leverage=leverage,
                        notional_usdt=round(notional, 8),
                        margin_usdt=round(margin_usdt, 8),
                    )
                )

            try:
                if self.venue == "okx":
                    result = client.place_order(
                        venue_symbol,
                        side.lower(),
                        order_type.lower(),
                        notional_usdt=notional,
                        quantity=qty,
                        price=order_price,
                        market_type=self.market_type,
                        leverage=leverage,
                        margin_mode=self.margin_mode,
                        position_side=position_side,
                        # 计划委托的触发价就是决策里的入场价
                        trigger_price=order_price,
                    )
                else:
                    # Binance 没有计划委托：突破单只能退化成市价（由客户端
                    # 的 STOP_MARKET 语义覆盖），这里显式传原类型，
                    # 让客户端按自己的口径处理并把自己的 message 返回来。
                    result = client.place_order(
                        venue_symbol,
                        side,
                        ORDER_TYPE_MARKET if order_type == ORDER_TYPE_TRIGGER else order_type,
                        notional_usdt=notional,
                        quantity=qty,
                        price=(
                            None if order_type == ORDER_TYPE_TRIGGER else order_price
                        ),
                        market_type=self.market_type,
                        leverage=leverage,
                        margin_mode=self.margin_mode,
                        position_side=position_side,
                    )
            except Exception as exc:  # noqa: BLE001 — 客户端应内部收敛，这里兜底
                logger.exception("AutoTrader 下单异常")
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=False,
                        reason=f"client-error: {exc}",
                        leverage=leverage,
                        notional_usdt=round(notional, 8),
                        margin_usdt=round(margin_usdt, 8),
                    ),
                    message=f"下单异常: {exc}",
                )

            if result is None or not getattr(result, "ok", False):
                message = getattr(result, "message", "") or "下单失败"
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=False,
                        reason=f"order-rejected: {message}",
                        result=result,
                        leverage=leverage,
                        notional_usdt=round(notional, 8),
                        margin_usdt=round(margin_usdt, 8),
                    )
                )

            # 计划委托只是挂上去等触发，此刻还没有持仓 —— 绝不能去挂保护单，
            # 那会挂到一个不存在的仓位上。等真正触发成交后，下一轮分析
            # （或人工）再补挂；这条状态如实写进记录，便于事后核对。
            pending_trigger = bool(
                isinstance(getattr(result, "raw", None), dict)
                and result.raw.get("pending")
            )
            if pending_trigger:
                record["pending_trigger"] = True
                record["trigger_price"] = to_float(order_price, 0.0)
                # 把「触发后该挂哪几条保护单」一并存下来：本程序的记忆里
                # 只有这里知道决策的止盈止损价，进程重启后靠它补挂。
                record["protection_plan"] = [
                    {
                        "kind": kind,
                        "trigger_price": trigger,
                        "qty": leg_qty,
                        "problem": problem,
                    }
                    for kind, trigger, leg_qty, problem in planned_protections
                ]
                logger.info(
                    "自动交易：%s 突破单已挂计划委托（触发价 %s），"
                    "触发成交后再挂保护单",
                    venue_symbol,
                    format_decimal(to_float(order_price, 0.0)),
                )
                return finish(
                    AutoTradeOutcome(
                        executed=True,
                        dry_run=False,
                        reason="pending-trigger",
                        result=result,
                        leverage=leverage,
                        notional_usdt=round(notional, 8),
                        margin_usdt=round(margin_usdt, 8),
                    ),
                    message=f"计划委托已挂，等待触发（触发价 {format_decimal(to_float(order_price, 0.0))}）",
                )

            # 开仓已成交 → 立刻把止损 / TP1 / TP2 挂到交易所侧。
            # 这一步是「程序挂了也能止盈止损」的关键，失败只告警不回滚开仓。
            fill_price = to_float(getattr(result, "price", 0.0), 0.0) or price
            fill_qty = to_float(getattr(result, "qty", 0.0), 0.0) or qty

            # 滑点核对：成交均价 vs 决策入场价。
            # 只告警不拒绝 —— 单已经成交了，拒单只会让仓位裸奔。
            slip_bps = self._slippage_bps(price, fill_price)
            if slip_bps is not None:
                record["slippage_bps"] = round(slip_bps, 2)
                if slip_bps > self.max_slippage_bps:
                    logger.warning(
                        "自动交易：%s 滑点偏大 —— 决策入场价 %s，实际成交 %s，"
                        "偏差 %.1f bps（阈值 %.0f bps）。"
                        "若经常触发，考虑改用突破单（计划委托）或压低单笔金额。",
                        venue_symbol,
                        format_decimal(price),
                        format_decimal(fill_price),
                        slip_bps,
                        self.max_slippage_bps,
                    )
            protect_orders = self._place_protections(
                client,
                venue_symbol=venue_symbol,
                decision=decision,
                position_side=position_side,
                entry_price=fill_price,
                qty=fill_qty,
            )
            if protect_orders:
                record["protections"] = [
                    {
                        "kind": o.kind,
                        "ok": o.ok,
                        "trigger_price": o.trigger_price,
                        "qty": o.qty,
                        "order_id": o.order_id,
                        "message": o.message,
                    }
                    for o in protect_orders
                ]
                result = dataclass_replace(result, protections=protect_orders)

            return finish(
                AutoTradeOutcome(
                    executed=True,
                    dry_run=False,
                    reason="submitted",
                    result=result,
                    leverage=leverage,
                    notional_usdt=round(notional, 8),
                    margin_usdt=round(margin_usdt, 8),
                )
            )

        except Exception as exc:  # noqa: BLE001 — evaluate 绝不抛异常
            logger.exception("AutoTrader.evaluate 未预期异常")
            try:
                return finish(
                    AutoTradeOutcome(
                        executed=False,
                        dry_run=self.dry_run,
                        reason=f"internal-error: {exc}",
                    )
                )
            except Exception:  # pragma: no cover — finish 本身也失败
                return AutoTradeOutcome(
                    executed=False,
                    dry_run=self.dry_run,
                    reason=f"internal-error: {exc}",
                )


__all__ = [
    "AutoTradeOutcome",
    "AutoTrader",
    "DEFAULT_MAX_SLIPPAGE_BPS",
    "ORDER_OPPORTUNITY_TYPES",
]
