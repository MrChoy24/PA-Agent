"""Shared helpers for centralized crypto exchanges (Binance / OKX).

Neutral symbol form used across these sources is ``BTCUSDT`` — uppercase,
no separators, quote asset appended.  Exchange-specific forms are derived
from it: Binance keeps the neutral form, OKX uses ``BTC-USDT``.

Ownership note: this module is pure-Python (no PyQt, no network); it only
normalizes symbols and provides the curated symbol universe.
"""
from __future__ import annotations

import re

# 默认计价币种
DEFAULT_QUOTE = "USDT"

# 常见计价币种（用于从无分隔符的币对中切出 base/quote）
QUOTE_ASSETS: tuple[str, ...] = (
    "USDT",
    "USDC",
    "FDUSD",
    "TUSD",
    "BUSD",
    "BTC",
    "ETH",
    "EUR",
    "TRY",
    "BRL",
    "OKB",
)

# 精选主流币种（币安/OKX 均有现货交易对）
BASE_ASSETS: tuple[str, ...] = (
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "DOT",
    "TRX",
    "TON",
    "MATIC",
    "LTC",
    "BCH",
    "NEAR",
    "APT",
    "ARB",
    "OP",
    "SUI",
    "PEPE",
    "SHIB",
    "ETC",
    "FIL",
    "ATOM",
    "UNI",
    "INJ",
    "TIA",
    "SEI",
    "WLD",
)

# 按长度降序匹配，保证 FDUSD 不会被 USDT 抢先切分
_QUOTES_BY_LEN: tuple[str, ...] = tuple(
    sorted(QUOTE_ASSETS, key=len, reverse=True)
)

_SEPARATORS_RE = re.compile(r"[\s/:_,\-]+")

# 合约/永续后缀：BTCUSDT.P、BTC-USDT-SWAP、BTCUSDT-PERP
_SUFFIX_RE = re.compile(r"(?:\.P|[-_]?(?:PERP|SWAP))$", re.IGNORECASE)

_MAX_SUFFIX_STRIPS = 4


def normalize_neutral(symbol: str) -> str:
    """Return the neutral symbol form, e.g. ``btc/usdt`` → ``BTCUSDT``.

    去空格、转大写、去掉分隔符，并剥离常见合约后缀（.P / PERP / -SWAP）。
    """
    text = (symbol or "").strip().upper()
    if not text:
        return ""
    # 反复剥离后缀，处理 "BTC-USDT-SWAP" 这类组合写法
    for _ in range(_MAX_SUFFIX_STRIPS):
        stripped = _SUFFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    return _SEPARATORS_RE.sub("", text)


def split_base_quote(symbol: str) -> tuple[str, str]:
    """Split a neutral symbol into ``(base, quote)``.

    以最长匹配的计价币种切分；无法识别时返回 ``("", neutral)``。
    """
    neutral = normalize_neutral(symbol)
    if not neutral:
        return "", ""
    for quote in _QUOTES_BY_LEN:
        if neutral.endswith(quote) and len(neutral) > len(quote):
            return neutral[: -len(quote)], quote
    return "", neutral


def to_binance_symbol(symbol: str) -> str:
    """Binance REST symbol form (identical to the neutral form)."""
    return normalize_neutral(symbol)


def to_okx_inst_id(symbol: str) -> str:
    """OKX instrument id, e.g. ``BTCUSDT`` → ``BTC-USDT``; unchanged if unsplittable."""
    base, quote = split_base_quote(symbol)
    if not base or not quote:
        return normalize_neutral(symbol)
    return f"{base}-{quote}"


def is_valid_exchange_symbol(symbol: str) -> bool:
    """True when *symbol* can be split into a plausible base + quote pair."""
    base, quote = split_base_quote(symbol)
    if not base or not quote:
        return False
    if not (2 <= len(base) <= 15):
        return False
    return base.isalnum() and quote.isalnum()


def _build_common_symbols() -> tuple[str, ...]:
    """BTC/ETH 优先，其余按 BASE_ASSETS 顺序拼 USDT 计价对。"""
    ordered: list[str] = [f"BTC{DEFAULT_QUOTE}", f"ETH{DEFAULT_QUOTE}"]
    seen: set[str] = set(ordered)
    for base in BASE_ASSETS:
        pair = f"{base}{DEFAULT_QUOTE}"
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return tuple(ordered)


# 精选交易对（中性写法，无分隔符），顺序固定 → 便于 GUI 下拉框稳定展示
COMMON_SYMBOLS: tuple[str, ...] = _build_common_symbols()
