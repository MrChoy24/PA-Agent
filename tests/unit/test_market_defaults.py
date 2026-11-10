"""Symbol normalization for the crypto + A-share data sources.

MT5 / TradingView sources were removed, so this suite only covers
Binance / OKX pair normalization and A-share code normalization.
"""
from __future__ import annotations

import pytest

from pa_agent.data.market_defaults import (
    A_SHARE_DEFAULT_SYMBOL,
    CRYPTO_DEFAULT_SYMBOL,
    migrate_general_gold_defaults,
    normalize_ashare_tv_code,
    normalize_crypto_symbol,
    normalize_gold_symbol_for_kind,
)


@pytest.mark.parametrize(
    "raw",
    ["BTCUSDT", "btcusdt", "BTC/USDT", "BTC-USDT", "BTC_USDT", "BTCUSDT.P", "BTC-USDT-SWAP"],
)
def test_crypto_symbol_normalizes_to_neutral_pair(raw: str) -> None:
    assert normalize_crypto_symbol(raw) == "BTCUSDT"


@pytest.mark.parametrize("raw", ["ETHUSDT", "SOLUSDT", "eth/usdc"])
def test_crypto_symbol_keeps_non_btc_pairs(raw: str) -> None:
    assert normalize_crypto_symbol(raw) == raw.upper().replace("/", "")


def test_crypto_symbol_empty_falls_back_to_default() -> None:
    assert normalize_crypto_symbol("") == CRYPTO_DEFAULT_SYMBOL
    assert normalize_crypto_symbol("   ") == CRYPTO_DEFAULT_SYMBOL


def test_legacy_gold_symbol_becomes_default_crypto_pair() -> None:
    # 历史配置里残留的 MT5 / TradingView 黄金品种名不应再进入交易所源。
    assert normalize_crypto_symbol("XAUUSDm") == CRYPTO_DEFAULT_SYMBOL
    assert normalize_crypto_symbol("XAUUSD") == CRYPTO_DEFAULT_SYMBOL


def test_binance_and_okx_kinds_share_crypto_normalization() -> None:
    assert normalize_gold_symbol_for_kind("binance", "eth/usdt") == "ETHUSDT"
    assert normalize_gold_symbol_for_kind("okx", "ETH-USDT") == "ETHUSDT"


def test_unknown_kind_treated_as_crypto() -> None:
    assert normalize_gold_symbol_for_kind("tradingview", "BTCUSDT") == "BTCUSDT"


def test_ashare_kinds_keep_six_digit_code() -> None:
    assert normalize_gold_symbol_for_kind("ashare", "600519") == "600519"
    assert normalize_gold_symbol_for_kind("akshare", "sh600519") == "600519"


def test_ashare_kind_falls_back_to_default_code() -> None:
    assert normalize_gold_symbol_for_kind("ashare", "BTCUSDT") == A_SHARE_DEFAULT_SYMBOL


def test_normalize_ashare_tv_code_strips_prefix_and_keeps_last_six() -> None:
    assert normalize_ashare_tv_code("sh600519") == "600519"
    assert normalize_ashare_tv_code("600519") == "600519"
    assert normalize_ashare_tv_code("") == ""


def test_migrate_general_cleans_legacy_mt5_symbol() -> None:
    general = {
        "last_data_source": "mt5",
        "last_symbol": "XAUUSDm",
        "last_tradingview_exchange": "TVC",
    }
    migrate_general_gold_defaults(general)
    assert general["last_symbol"] == CRYPTO_DEFAULT_SYMBOL
    # 已删除的 TradingView 交易所字段必须被清洗掉。
    assert "last_tradingview_exchange" not in general


def test_migrate_general_keeps_valid_crypto_symbol() -> None:
    general = {"last_data_source": "okx", "last_symbol": "BTC-USDT"}
    migrate_general_gold_defaults(general)
    assert general["last_symbol"] == "BTCUSDT"
