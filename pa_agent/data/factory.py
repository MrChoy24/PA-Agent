"""Construct :class:`DataSource` implementations by kind id.

当前只保留两个纯 REST 行情源：Binance 与 OKX。MT5、TradingView 以及
A 股（东方财富 / AkShare / Tushare / YFinance / 东财期货）数据源已全部移除。
"""
from __future__ import annotations

from typing import Literal

from pa_agent.data.base import DataSource

DataSourceKind = Literal["binance", "okx"]

#: 出厂下拉框内容：Binance 为默认数据源，排在首位。
#: Binance / OKX 均为纯 REST 行情源，直连交易所公开接口，无需任何本地终端。
_BASE_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = (
    ("binance", "Binance"),
    ("okx", "OKX"),
)

#: 供界面/测试直接使用的可选列表。
DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = _BASE_SOURCE_CHOICES

#: Binance / OKX 默认品种（中性写法，两端都能归一）。
CRYPTO_DEFAULT_SYMBOL = "BTCUSDT"

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    "binance": CRYPTO_DEFAULT_SYMBOL,
    "okx": CRYPTO_DEFAULT_SYMBOL,
}


def normalize_data_source_kind(kind: str | None) -> DataSourceKind:
    """Return a supported data-source kind, defaulting to Binance.

    已移除的数据源（mt5 / tradingview / ashare / akshare / tushare /
    yfinance / eastmoney_futures）与任何未知值一律回退 ``binance``。
    """
    if kind in _DEFAULT_SYMBOLS:
        return kind  # type: ignore[return-value]
    return "binance"


def data_source_label(kind: str | None) -> str:
    """Human-readable label for *kind*."""
    normalized = normalize_data_source_kind(kind)
    for key, label in DATA_SOURCE_CHOICES:
        if key == normalized:
            return label
    return "Binance"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def create_data_source(kind: str | None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected)."""
    normalized = normalize_data_source_kind(kind)
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings

    try:
        settings = load_settings(SETTINGS_JSON_PATH)
    except Exception:  # noqa: BLE001 - 设置读取失败不应阻断行情
        settings = None
    if normalized == "okx":
        from pa_agent.data.okx_source import OKXSource

        cfg = getattr(settings, "okx", None)
        return OKXSource(
            api_key=str(getattr(cfg, "api_key", "") or ""),
            api_secret=str(getattr(cfg, "api_secret", "") or ""),
            passphrase=str(getattr(cfg, "passphrase", "") or ""),
            demo=bool(getattr(cfg, "demo", False)),
        )
    from pa_agent.data.binance_source import BinanceSource

    cfg = getattr(settings, "binance", None)
    return BinanceSource(
        api_key=str(getattr(cfg, "api_key", "") or ""),
        api_secret=str(getattr(cfg, "api_secret", "") or ""),
    )
