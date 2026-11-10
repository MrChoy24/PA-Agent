"""Tests for the data source factory and related settings.

MT5 / TradingView sources were removed; the UI now offers Binance and OKX only.
"""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    normalize_data_source_kind,
)
from pa_agent.data.okx_source import OKXSource
from pa_agent.data.tushare_source import TushareSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "binance"
    assert normalize_data_source_kind(None) == "binance"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"
    # eastmoney 已并入 UI 可见的 ashare，不再是独立 kind
    assert normalize_data_source_kind("eastmoney") == "binance"


def test_removed_sources_fall_back_to_binance():
    """MT5 / TradingView 已从项目移除，历史 kind 一律回退 Binance。"""
    assert normalize_data_source_kind("mt5") == "binance"
    assert normalize_data_source_kind("tradingview") == "binance"


def test_binance_is_first_ui_choice():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "binance" in ui_kinds
    assert "okx" in ui_kinds
    assert DATA_SOURCE_CHOICES[0][0] == "binance"
    assert "akshare" not in ui_kinds
    # 已删除的数据源不得再出现在 UI 列表中
    assert "mt5" not in ui_kinds
    assert "tradingview" not in ui_kinds


def test_ashare_hidden_in_ui_by_default():
    """A股数据源默认不在界面显示：开关是代码常量，UI 里不暴露。"""
    import pa_agent.data.factory as factory

    assert factory.SHOW_ASHARE_DATA_SOURCE is False
    assert "ashare" not in {k for k, _ in DATA_SOURCE_CHOICES}
    assert [k for k, _ in DATA_SOURCE_CHOICES] == ["binance", "okx"]
    # 只是不显示，kind 本身仍然合法：脚本/后端照样能创建
    assert normalize_data_source_kind("ashare") == "ashare"
    assert isinstance(create_data_source("ashare"), EastMoneySource)


def test_ashare_shown_when_code_switch_is_on():
    from pa_agent.data.factory import ASHARE_SOURCE_LABEL, data_source_choices

    choices = data_source_choices(show_ashare=True)
    assert choices[-1] == ("ashare", ASHARE_SOURCE_LABEL)
    assert [k for k, _ in choices] == ["binance", "okx", "ashare"]
    assert [k for k, _ in data_source_choices(show_ashare=False)] == ["binance", "okx"]


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_ashare_label_uses_shared_constant():
    """状态栏文案与下拉文案共用 ASHARE_SOURCE_LABEL，避免两处写死不同名。"""
    from pa_agent.data.factory import (
        ASHARE_SOURCE_LABEL,
        data_source_choices,
        data_source_label,
    )

    assert ASHARE_SOURCE_LABEL == "A股（专属数据源）"
    assert data_source_label("ashare") == ASHARE_SOURCE_LABEL
    assert dict(data_source_choices(show_ashare=True))["ashare"] == ASHARE_SOURCE_LABEL


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("binance"), BinanceSource)
    assert isinstance(create_data_source("okx"), OKXSource)
    assert isinstance(create_data_source("ashare"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("binance") == "BTCUSDT"
    assert default_symbol_for_kind("okx") == "BTCUSDT"
    assert default_symbol_for_kind("ashare") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "binance"
    assert g.last_symbol == "BTCUSDT"


def test_legacy_data_source_values_are_coerced():
    """老版本 settings.json（mt5 / tradingview）必须能顺利加载。"""
    assert GeneralSettings(last_data_source="mt5").last_data_source == "binance"
    assert GeneralSettings(last_data_source="tradingview").last_data_source == "binance"
    assert GeneralSettings(last_data_source="yfinance").last_data_source == "ashare"
