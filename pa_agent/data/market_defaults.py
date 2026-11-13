"""Default symbol identifiers for the supported data sources.

本模块只保留 Binance / OKX（加密货币）的品种归一逻辑。MT5、TradingView 以及
A 股（东方财富 / AkShare / Tushare）数据源已从本项目移除。
"""
from __future__ import annotations

#: Binance / OKX 默认品种（中性写法，两端都会归一成各自格式）。
CRYPTO_DEFAULT_SYMBOL = "BTCUSDT"

#: 常见计价资产（用于判断用户输入是否已经是一个完整交易对）。
_CRYPTO_QUOTES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "FDUSD",
    "TUSD",
    "BUSD",
    "USD",
    "BTC",
    "ETH",
    "EUR",
    "TRY",
    "BRL",
    "OKB",
)

#: 贵金属/外汇基础代码：老配置里的 XAUUSD / XAGUSD 不算加密交易对。
_METAL_BASES: frozenset[str] = frozenset(
    {"XAU", "XAG", "XPT", "XPD", "GLD", "SLV", "GOLD", "SILVER"}
)


def normalize_crypto_symbol(symbol: str) -> str:
    """Normalize a crypto pair to the neutral ``BASEQUOTE`` form.

    ``btc/usdt``、``BTC-USDT``、``BTCUSDT.P`` → ``BTCUSDT``。空值或无法识别成
    交易对（例如残留的 ``XAUUSDm``）时回退 :data:`CRYPTO_DEFAULT_SYMBOL`，
    保证 Binance / OKX 数据源总能拿到一个合法品种。
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        return CRYPTO_DEFAULT_SYMBOL
    try:
        from pa_agent.data.crypto_common import normalize_neutral

        neutral = normalize_neutral(raw)
    except Exception:  # noqa: BLE001 - crypto_common 缺失时退回本地极简归一
        neutral = raw
        for ch in ("/", "-", "_", ":"):
            neutral = neutral.replace(ch, "")
        for suffix in (".P", "PERP", "SWAP"):
            if neutral.endswith(suffix):
                neutral = neutral[: -len(suffix)]
    if not neutral:
        return CRYPTO_DEFAULT_SYMBOL
    for quote in _CRYPTO_QUOTES:
        if neutral.endswith(quote) and len(neutral) > len(quote):
            base = neutral[: -len(quote)]
            # 贵金属代码（XAU/XAG/…）不是加密货币交易对：
            # 老配置里的 XAUUSD 会被误判成有效 pair，这里显式拒绝。
            if base in _METAL_BASES:
                return CRYPTO_DEFAULT_SYMBOL
            return neutral
    return CRYPTO_DEFAULT_SYMBOL


def normalize_gold_symbol_for_kind(kind: str, symbol: str) -> str:
    """Normalize *symbol* for data-source *kind*.

    函数名保留历史叫法（调用方在 GUI 里已按此名接线），但已不含黄金/MT5 逻辑。
    现在只支持加密货币交易所，任何 kind 都按交易对归一。
    """
    return normalize_crypto_symbol(symbol)


def migrate_general_gold_defaults(general: dict) -> None:
    """In-place migration of persisted ``general`` symbol fields.

    历史配置里可能残留 ``XAUUSDm`` / ``XAUUSD``（MT5 / TradingView 品种），
    ``000001``（A 股代码），或已删除的 ``last_tradingview_exchange`` 字段
    —— 这里统一清洗。
    """
    sym = str(general.get("last_symbol", ""))
    general["last_symbol"] = normalize_crypto_symbol(sym)
    # TradingView 数据源已移除，残留的交易所字段一并丢弃。
    general.pop("last_tradingview_exchange", None)
