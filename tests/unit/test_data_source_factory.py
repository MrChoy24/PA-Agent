"""Tests for the data source factory and related settings.

MT5 / TradingView / A 股（东方财富 / AkShare / Tushare / YFinance）数据源均已
移除；界面与工厂现在只提供 Binance 与 OKX。
"""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    DataSourceKind,
    create_data_source,
    data_source_label,
    default_symbol_for_kind,
    normalize_data_source_kind,
)
from pa_agent.data.okx_source import OKXSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "binance"
    assert normalize_data_source_kind(None) == "binance"
    assert normalize_data_source_kind("") == "binance"


def test_removed_sources_fall_back_to_binance():
    """已移除的数据源（含历史遗留的 A 股 / MT5 / TradingView kind）一律回退 Binance。"""
    for legacy in (
        "mt5",
        "tradingview",
        "ashare",
        "akshare",
        "tushare",
        "yfinance",
        "eastmoney",
        "eastmoney_futures",
        "adata",
        "a_share",
    ):
        assert normalize_data_source_kind(legacy) == "binance", legacy


def test_ui_offers_only_binance_and_okx():
    assert [k for k, _ in DATA_SOURCE_CHOICES] == ["binance", "okx"]
    assert DATA_SOURCE_CHOICES[0][0] == "binance"


def test_data_source_kind_literal_is_narrowed():
    """DataSourceKind 只剩两个成员，避免调用方再写 A 股分支。"""
    args = DataSourceKind.__args__  # type: ignore[attr-defined]
    assert args == ("binance", "okx")


def test_labels_are_stable():
    assert data_source_label("binance") == "Binance"
    assert data_source_label("okx") == "OKX"
    # 已移除的 kind 也回退到 Binance 文案
    assert data_source_label("ashare") == "Binance"
    assert data_source_label(None) == "Binance"


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("binance"), BinanceSource)
    assert isinstance(create_data_source("okx"), OKXSource)
    # 已移除的 kind 不再抛异常，而是安静地回退 Binance
    assert isinstance(create_data_source("ashare"), BinanceSource)
    assert isinstance(create_data_source("tushare"), BinanceSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("binance") == "BTCUSDT"
    assert default_symbol_for_kind("okx") == "BTCUSDT"
    assert default_symbol_for_kind("ashare") == "BTCUSDT"


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "binance"
    assert g.last_symbol == "BTCUSDT"


def test_legacy_data_source_values_are_coerced():
    """老版本 settings.json（mt5 / tradingview / A 股 kind）必须能顺利加载。"""
    assert GeneralSettings(last_data_source="mt5").last_data_source == "binance"
    assert GeneralSettings(last_data_source="tradingview").last_data_source == "binance"
    assert GeneralSettings(last_data_source="yfinance").last_data_source == "binance"
    assert GeneralSettings(last_data_source="ashare").last_data_source == "binance"
    assert GeneralSettings(last_data_source="akshare").last_data_source == "binance"
    assert GeneralSettings(last_data_source="tushare").last_data_source == "binance"
    assert (
        GeneralSettings(last_data_source="eastmoney_futures").last_data_source
        == "binance"
    )


def test_general_settings_drops_removed_kline_adjust():
    """``kline_adjust`` 只服务于已删除的 A 股源，模型里不再声明（且 extra=ignore）。"""
    g = GeneralSettings.model_validate({"kline_adjust": "hfq"})
    assert not hasattr(g, "kline_adjust")
