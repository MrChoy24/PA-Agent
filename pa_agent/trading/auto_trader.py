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
from dataclasses import dataclass
from datetime import datetime, timezone
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
    POSITION_LONG,
    POSITION_SHORT,
    TradeResult,
    clamp_leverage,
    format_decimal,
    normalize_margin_mode,
    normalize_market_type,
    normalize_order_type,
    normalize_position_side,
    to_float,
    to_text,
)
from pa_agent.trading.okx_trader import OKXTrader

logger = logging.getLogger(__name__)

# 与 pa_agent.gui.order_opportunity.ORDER_OPPORTUNITY_TYPES 保持一致
# （不能 import gui：那是 PyQt 层）
ORDER_OPPORTUNITY_TYPES: frozenset[str] = frozenset({"限价单", "突破单", "市价单"})

_DEFAULT_LOG_PATH = os.path.join("trade_records", "auto_orders.jsonl")

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
    """Map 中文下单方式 to ``MARKET``/``LIMIT`` (``""`` when not an order)."""
    raw = str(decision.get("order_type") or "").strip()
    if raw == "市价单":
        return "MARKET"
    if raw in ("限价单", "突破单"):
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

        # 交易客户端凭据（可选）
        self._api_key: str = str(opts.get("api_key") or "")
        self._api_secret: str = str(opts.get("api_secret") or "")
        self._passphrase: str = str(opts.get("passphrase") or "")
        self._testnet: bool = bool(opts.get("testnet", False))
        self._demo: bool = bool(opts.get("demo", False))
        self._timeout_s: float = to_float(opts.get("timeout_s"), 10.0)

        self._trader = None   # 延迟创建，避免未配置凭据时也建连接

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

    def _venue_reference_price(self, symbol: str) -> float:
        """下单交易所的公开最新价；取不到返回 ``0.0``（调用方按「无法校验」处理）。"""
        if self.max_price_deviation_pct <= 0:
            return 0.0
        client = self._client()
        if client is None:
            return 0.0
        try:
            return float(client.last_price(self._symbol_for_venue(symbol)))
        except Exception as exc:  # noqa: BLE001 — 取价失败不能抛
            logger.debug("AutoTrader 取 %s 参考价失败: %s", self.venue, exc)
            return 0.0

    @staticmethod
    def _looks_like_symbol(neutral: str) -> bool:
        """``BTCUSDT`` 这种「≥5 位大写字母数字」才认为是交易对。"""
        return bool(_SYMBOL_RE.match(neutral or ""))

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
        """Append one JSON line; never raises."""
        try:
            path = os.path.abspath(self.log_path)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
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
                # 逐仓 + 面板填了「每单保证金」：名义 = 保证金 × 杠杆，不被上限二次收敛
                notional = self.margin_per_trade_usdt * float(self.leverage)
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
            if use_margin:
                # 逐仓以面板保证金为准，避免夹取杠杆后出现浮点漂移
                margin_usdt = self.margin_per_trade_usdt
                notional = margin_usdt * float(leverage)

            qty = notional / price
            venue_symbol = self._symbol_for_venue(neutral)
            order_price = price if order_type == ORDER_TYPE_LIMIT else None
            position_side = self._resolve_position_side(side) if is_futures else POSITION_LONG

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
                    )
                else:
                    result = client.place_order(
                        venue_symbol,
                        side,
                        order_type,
                        notional_usdt=notional,
                        quantity=qty,
                        price=order_price,
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


__all__ = ["AutoTradeOutcome", "AutoTrader", "ORDER_OPPORTUNITY_TYPES"]
