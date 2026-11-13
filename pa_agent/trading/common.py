"""交易层共用类型与数值工具（无网络、无 PyQt）。

这里的 :class:`TradeResult` 是 Binance / OKX 两个下单实现的统一返回类型，
上层（``AutoTrader``、GUI、日志）只依赖它，不关心交易所差异。

数量/价格取整统一用 ``Decimal`` 计算，避免 ``0.1 * 3`` 这类浮点误差
导致交易所拒单（LOT_SIZE / PRICE_FILTER 的经典坑）。

本模块同时定义「下单参数面板」用的市场类型 / 订单类型 / 保证金模式 /
持仓方向常量与归一化函数：**对外统一用这里的词**，各交易所内部各自翻译
（Binance ``CROSSED``、OKX ``cross``、现货 ``cash`` 等）。
所有归一化函数与 ``clamp_leverage`` 都不抛异常，非法输入回落到默认值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

# ── 市场类型 / 订单类型 / 保证金模式 / 持仓方向 ────────────────────────────────
# 现货：Binance ``/api/v3``，OKX ``instType=SPOT`` / ``tdMode=cash``
MARKET_SPOT = "spot"
# 合约：Binance U 本位永续 ``/fapi``，OKX ``instType=SWAP``
MARKET_FUTURES = "futures"

# 对外统一的订单类型（内部各自翻译成交易所口径）
ORDER_TYPE_MARKET = "MARKET"
ORDER_TYPE_LIMIT = "LIMIT"
# 计划委托：价格先触到触发价，再按市价送进撮合（仅 OKX 合约）。
# 用途是「突破单」——分析本身要跑几分钟，直接市价追进去滑点不可控，
# 挂限价又可能在突破瞬间不成交，所以先挂触发价等它被穿。
ORDER_TYPE_TRIGGER = "TRIGGER"

# 保证金模式（Binance 内部叫 CROSSED / ISOLATED）
MARGIN_ISOLATED = "isolated"
MARGIN_CROSS = "cross"

# 合约开仓方向（现货只有 long）
POSITION_LONG = "long"
POSITION_SHORT = "short"

# 杠杆范围：交易所允许 1~125，超出直接夹取而不是拒绝
DEFAULT_LEVERAGE = 1
MIN_LEVERAGE = 1
MAX_LEVERAGE = 125

# 跨所价差保护的默认阈值（百分比）。0 = 关闭该检查。
# 用法：行情源（如 Binance）做分析、到另一家（如 OKX）下单时，
# 若两边最新价偏离超过该比例，就跳过本次下单而不是在错误的价格上成交。
#
# 注意：交易层默认关闭（0），因为它是唯一会在 evaluate() 里发起网络请求的闸门。
# 需要它的上层（GUI / 守护进程）从 AutoTradeSettings.max_price_deviation_pct
# 显式传入（该配置默认 0.3），单元测试因此仍然完全离线。
DEFAULT_MAX_PRICE_DEVIATION_PCT = 0.0

_MARKET_FUTURES_WORDS: frozenset[str] = frozenset(
    {
        "futures",
        "future",
        "swap",
        "perpetual",
        "perp",
        "contract",
        "usdt-m",
        "usdm",
        "合约",
        "永续",
        "交割",
        "u本位",
    }
)
_MARKET_SPOT_WORDS: frozenset[str] = frozenset({"spot", "cash", "现货", "现物"})

_ORDER_LIMIT_WORDS: frozenset[str] = frozenset(
    {"limit", "限价", "限价单", "突破单", "挂单"}
)
_ORDER_MARKET_WORDS: frozenset[str] = frozenset(
    {"market", "市价", "市价单", "市价委托"}
)
_ORDER_TRIGGER_WORDS: frozenset[str] = frozenset(
    {"trigger", "计划委托", "触发单", "条件触发"}
)

_MARGIN_CROSS_WORDS: frozenset[str] = frozenset({"cross", "crossed", "全仓", "cross margin"})
_MARGIN_ISOLATED_WORDS: frozenset[str] = frozenset(
    {"isolated", "逐仓", "isolated margin"}
)

_POSITION_SHORT_WORDS: frozenset[str] = frozenset(
    {"short", "sell", "空", "做空", "空头", "卖出"}
)
_POSITION_LONG_WORDS: frozenset[str] = frozenset(
    {"long", "buy", "多", "做多", "多头", "买入"}
)


@dataclass(frozen=True)
class TradeResult:
    """一次交易操作的结果快照。

    ``ok=False`` 时 ``message`` 必须是可直接展示给人的可读文本
    （交易所原始报错也要保留在 ``message`` 里，便于排查）。

    合约相关字段（``market_type`` / ``leverage`` / ``margin_usdt`` /
    ``notional_usdt`` / ``position_side`` / ``margin_mode``）全部带默认值，
    现货调用方不需要关心它们。
    """

    ok: bool
    venue: str = ""          # "binance" / "okx"
    order_id: str = ""
    symbol: str = ""
    side: str = ""           # BUY/SELL（Binance）或 buy/sell（OKX）
    order_type: str = ""     # MARKET/LIMIT 或 market/limit
    qty: float = 0.0
    price: float = 0.0
    message: str = ""
    raw: dict = field(default_factory=dict)
    # ── 合约参数快照（向后兼容新增，全部有默认值） ──
    market_type: str = MARKET_SPOT
    leverage: int = DEFAULT_LEVERAGE
    margin_usdt: float = 0.0
    notional_usdt: float = 0.0
    position_side: str = POSITION_LONG
    margin_mode: str = MARGIN_ISOLATED
    # ── 开仓成功后立刻挂上的保护单（止损 / TP1 / TP2） ──
    #: 每条腿的结果快照；空元组表示「没有挂」或「本次没配置保护单」。
    protections: tuple["ProtectionOrder", ...] = ()


# 保护单的腿：止损 / 止盈一 / 止盈二
PROTECT_STOP_LOSS = "stop_loss"
PROTECT_TAKE_PROFIT_1 = "take_profit_1"
PROTECT_TAKE_PROFIT_2 = "take_profit_2"

_PROTECT_LABELS: dict[str, str] = {
    PROTECT_STOP_LOSS: "止损",
    PROTECT_TAKE_PROFIT_1: "TP1",
    PROTECT_TAKE_PROFIT_2: "TP2",
}


def protection_label(kind: str) -> str:
    """保护单腿的中文标签（止损 / TP1 / TP2）；未知类型原样返回。"""
    return _PROTECT_LABELS.get(kind, kind or "保护单")


@dataclass(frozen=True)
class ProtectionOrder:
    """一条保护单（止损或某个止盈）的下单结果。

    保护单是「开仓成交后立刻挂到交易所」的条件单，目的是一旦本程序崩了/
    服务器断连，交易所侧仍然会在触发价执行止盈止损，而不依赖本程序还活着。

    ``kind`` 取 :data:`PROTECT_STOP_LOSS` / :data:`PROTECT_TAKE_PROFIT_1` /
    :data:`PROTECT_TAKE_PROFIT_2`；``ok=False`` 时 ``message`` 是可直接展示
    给人看的中文原因（例如「现货无原生保护单」「触发价方向不对」）。
    """

    kind: str
    ok: bool = False
    trigger_price: float = 0.0
    qty: float = 0.0
    order_id: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """中文标签：止损 / TP1 / TP2。"""
        return protection_label(self.kind)

    def describe(self) -> str:
        """一行可读文本，直接进日志/JSONL。"""
        head = f"{self.label}@{format_decimal(self.trigger_price)}"
        if self.ok:
            oid = self.order_id or "—"
            return f"{head} 已挂（{oid}）"
        return f"{head} 未挂：{self.message or '未知原因'}"


# ── 参数归一化（永不抛异常） ──────────────────────────────────────────────────


def plan_protections(
    *,
    position_side: Any,
    entry_price: Any,
    stop_loss: Any,
    take_profit_1: Any,
    take_profit_2: Any,
    total_qty: Any,
    tp1_fraction: float = 0.5,
) -> list[tuple[str, float, float, str]]:
    """把决策里的止损/止盈价整理成待挂的保护单计划。

    返回 ``[(kind, trigger_price, qty, problem)]``：``problem`` 为空表示这条腿
    可以挂；非空是**中文原因**，调用方应把它写进日志而不是当作异常。

    校验规则（与决策侧 ``trade_metrics`` 的几何要求一致）：

    * 做多：``stop < entry < tp1 <= tp2``
    * 做空：``tp2 <= tp1 < entry < stop``

    价格缺失、非正数、方向反了都会被跳过（``problem`` 说明原因），
    **不会**因为某条腿不合法就整体放弃 —— 止损能挂就先挂止损。

    ``qty`` 按 ``tp1_fraction`` 拆分：TP1 拿 ``total_qty * tp1_fraction``，
    TP2 拿剩下的；止损永远用全量 ``total_qty``（先把保命那条挂稳）。
    """
    side = normalize_position_side(position_side)
    is_long = side == POSITION_LONG
    entry = to_float(entry_price, 0.0)
    qty_total = to_float(total_qty, 0.0)

    planned: list[tuple[str, float, float, str]] = []

    def _leg(kind: str, raw: Any, want_above: bool, leg_qty: float) -> None:
        price = to_float(raw, 0.0)
        if price <= 0:
            planned.append((kind, 0.0, leg_qty, "决策没有给出有效价格"))
            return
        if entry > 0:
            if want_above and price <= entry:
                planned.append((kind, price, leg_qty, "触发价没有高过入场价"))
                return
            if not want_above and price >= entry:
                planned.append((kind, price, leg_qty, "触发价没有低于入场价"))
                return
        planned.append((kind, price, leg_qty, ""))

    # 止损永远全量：先保命
    _leg(PROTECT_STOP_LOSS, stop_loss, not is_long, qty_total)

    fraction = to_float(tp1_fraction, 0.5)
    if fraction <= 0 or fraction >= 1:
        fraction = 0.5
    tp1_qty = qty_total * fraction if qty_total > 0 else 0.0
    tp2_qty = (qty_total - tp1_qty) if qty_total > 0 else 0.0

    _leg(PROTECT_TAKE_PROFIT_1, take_profit_1, is_long, tp1_qty)
    _leg(PROTECT_TAKE_PROFIT_2, take_profit_2, is_long, tp2_qty)
    return planned


def _match_word(value: Any, table: dict[str, str], default: str) -> str:
    """Case-insensitive exact/containment match over a {word: result} table."""
    if isinstance(value, bool):  # bool 是 int 的子类，但在参数语义上不可用
        return default
    text = ""
    if isinstance(value, str):
        text = value.strip().lower()
    elif isinstance(value, (int, float)):
        text = str(value).strip().lower()
    if not text:
        return default
    if text in table:
        return table[text]
    # 「FUTURES 永续」这类复合写法：包含关系也算
    for word, result in table.items():
        if word and word in text:
            return result
    return default


def normalize_market_type(value: Any) -> str:
    """把任意输入归一成 :data:`MARKET_SPOT` / :data:`MARKET_FUTURES`。

    非法值回落到 ``spot``（最保守：不会意外用到杠杆）。
    """
    table = {word: MARKET_FUTURES for word in _MARKET_FUTURES_WORDS}
    table.update({word: MARKET_SPOT for word in _MARKET_SPOT_WORDS})
    return _match_word(value, table, MARKET_SPOT)


def normalize_order_type(value: Any) -> str:
    """把任意输入归一成 :data:`ORDER_TYPE_MARKET` / :data:`ORDER_TYPE_LIMIT`
    / :data:`ORDER_TYPE_TRIGGER`。

    非法值回落到 ``MARKET``（``place_order`` 内部仍会做严格校验，
    避免把拼错类型当成市价单发出去）。
    """
    table = {word: ORDER_TYPE_LIMIT for word in _ORDER_LIMIT_WORDS}
    table.update({word: ORDER_TYPE_MARKET for word in _ORDER_MARKET_WORDS})
    table.update({word: ORDER_TYPE_TRIGGER for word in _ORDER_TRIGGER_WORDS})
    return _match_word(value, table, ORDER_TYPE_MARKET)


def normalize_margin_mode(value: Any) -> str:
    """把任意输入归一成 :data:`MARGIN_ISOLATED` / :data:`MARGIN_CROSS`。"""
    table = {word: MARGIN_CROSS for word in _MARGIN_CROSS_WORDS}
    table.update({word: MARGIN_ISOLATED for word in _MARGIN_ISOLATED_WORDS})
    return _match_word(value, table, MARGIN_ISOLATED)


def normalize_position_side(value: Any) -> str:
    """把任意输入归一成 :data:`POSITION_LONG` / :data:`POSITION_SHORT`。"""
    table = {word: POSITION_SHORT for word in _POSITION_SHORT_WORDS}
    table.update({word: POSITION_LONG for word in _POSITION_LONG_WORDS})
    return _match_word(value, table, POSITION_LONG)


def clamp_leverage(value: Any) -> int:
    """把任意输入安全转成 ``[1, 125]`` 的整数杠杆，永不抛异常。

    ``0`` / 负数 / ``None`` / ``"abc"`` → ``1``；``999`` → ``125``。
    """
    lev = int(to_float(value, float(DEFAULT_LEVERAGE)))
    return max(MIN_LEVERAGE, min(MAX_LEVERAGE, lev))


def notional_from_margin(margin_usdt: Any, leverage: Any) -> float:
    """名义金额 = 每单保证金 × 杠杆；非法输入返回 ``0.0``（永不抛异常）。

    例：``notional_from_margin(10, 100) == 1000.0``。
    """
    margin = to_float(margin_usdt, 0.0)
    lev = clamp_leverage(leverage)
    if margin <= 0:
        return 0.0
    return margin * float(lev)


# ── 数值工具 ──────────────────────────────────────────────────────────────────


def _to_decimal(value: Any) -> Decimal | None:
    """Best-effort Decimal conversion; ``None`` when the value is unusable."""
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, bool):
            result = Decimal(int(value))
        else:
            result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def to_decimal(value: Any) -> Decimal | None:
    """把任意数字/字符串转成有限 ``Decimal``；不可用时返回 ``None``。

    公开别名：**数量换算必须走 Decimal**，否则 ``0.3 / 0.1`` 这类二进制
    浮点误差会让 ``int()`` 少算一张合约（止损就会漏保护一部分仓位）。
    """
    return _to_decimal(value)


def to_float(value: Any, default: float = 0.0) -> float:
    """Convert to a finite float, falling back to *default* (never raises)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def to_text(value: Any) -> str:
    """Coerce *value* to a trimmed string without raising.

    对外部传入的 symbol / ccy / order_id 一律先过这一层：上层可能把它们
    写成 ``None``、数字或带空白的字符串，直接 ``.strip()`` 会抛异常。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 — 极端对象（__str__ 抛错）
        return ""


def _floor_to_unit(value: Any, unit: Any) -> float | None:
    """Floor *value* down to a whole multiple of *unit* (``None`` on bad input)."""
    amount = _to_decimal(value)
    step = _to_decimal(unit)
    if amount is None:
        return None
    if step is None or step <= 0:
        return float(amount)
    try:
        floored = (amount / step).to_integral_value(rounding=ROUND_DOWN) * step
    except (InvalidOperation, ZeroDivisionError, ValueError):
        return float(amount)
    return float(floored)


def floor_to_step(qty: Any, step: Any) -> float:
    """把数量向下取整到 ``step`` 的整数倍（LOT_SIZE.stepSize）。

    ``step <= 0`` 或无法解析时原样返回数量 —— 交易所仍然会做最终校验。
    """
    out = _floor_to_unit(qty, step)
    return 0.0 if out is None else out


def floor_to_precision(value: Any, tick: Any) -> float:
    """把价格向下取整到 ``tick`` 的整数倍（PRICE_FILTER.tickSize）。"""
    out = _floor_to_unit(value, tick)
    return 0.0 if out is None else out


def format_decimal(x: Any) -> str:
    """Render a number without scientific notation or trailing zeros.

    ``0.00100`` → ``"0.001"``；``100.0`` → ``"100"``；``1e-05`` → ``"0.00001"``。
    """
    dec = _to_decimal(x)
    if dec is None:
        return "0"
    try:
        text = format(dec.normalize(), "f")
    except (ValueError, InvalidOperation):  # pragma: no cover — 防御性
        text = str(dec)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "TradeResult",
    "ProtectionOrder",
    "PROTECT_STOP_LOSS",
    "PROTECT_TAKE_PROFIT_1",
    "PROTECT_TAKE_PROFIT_2",
    "plan_protections",
    "protection_label",
    "MARKET_SPOT",
    "MARKET_FUTURES",
    "ORDER_TYPE_MARKET",
    "ORDER_TYPE_LIMIT",
    "ORDER_TYPE_TRIGGER",
    "MARGIN_ISOLATED",
    "MARGIN_CROSS",
    "POSITION_LONG",
    "POSITION_SHORT",
    "DEFAULT_LEVERAGE",
    "MIN_LEVERAGE",
    "MAX_LEVERAGE",
    "clamp_leverage",
    "notional_from_margin",
    "normalize_market_type",
    "normalize_order_type",
    "normalize_margin_mode",
    "normalize_position_side",
    "to_decimal",
    "to_float",
    "to_text",
    "floor_to_step",
    "floor_to_precision",
    "format_decimal",
]
