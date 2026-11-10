"""下单参数条测试（Qt）：值读写、回填、脏判断、现货禁用杠杆."""
from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.config.settings import Settings
from pa_agent.gui.order_params_bar import MAX_LEVERAGE, MIN_LEVERAGE, OrderParamsBar


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


def _bar(qapp: QApplication, settings: Settings | None = None) -> OrderParamsBar:
    return OrderParamsBar(settings=settings or Settings())


def test_defaults_when_settings_empty(qapp: QApplication) -> None:
    bar = _bar(qapp)
    values = bar.values()
    assert values["venue"] == "binance"
    assert values["market_type"] == "spot"
    assert values["margin_per_trade_usdt"] == pytest.approx(10.0)
    assert values["leverage"] == 1
    assert values["order_type"] == "MARKET"
    assert values["margin_mode"] == "isolated"


def test_load_from_settings_backfills_all_fields(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.venue = "okx"
    settings.auto_trade.market_type = "futures"
    settings.auto_trade.margin_per_trade_usdt = 25.5
    settings.auto_trade.leverage = 20
    settings.auto_trade.order_type = "LIMIT"
    settings.auto_trade.margin_mode = "cross"

    bar = _bar(qapp, settings)
    values = bar.values()
    assert values["venue"] == "okx"
    assert values["market_type"] == "futures"
    assert values["margin_per_trade_usdt"] == pytest.approx(25.5)
    assert values["leverage"] == 20
    assert values["order_type"] == "LIMIT"
    assert values["margin_mode"] == "cross"


def test_apply_to_settings_returns_dirty_only_on_change(qapp: QApplication) -> None:
    settings = Settings()
    bar = _bar(qapp, settings)
    # 值没变 → 不算脏
    assert bar.apply_to_settings(settings) is False

    settings.auto_trade.leverage = 5  # 手工改成与控件不同
    assert bar.apply_to_settings(settings) is True
    assert settings.auto_trade.leverage == 1  # 被控件值覆盖回去

    assert bar.apply_to_settings(settings) is False  # 再来一次已经一致


def test_leverage_clamped_into_range(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.leverage = 9999
    bar = _bar(qapp, settings)
    assert bar.values()["leverage"] == MAX_LEVERAGE

    settings.auto_trade.leverage = 0
    bar.load_from_settings(settings)
    assert bar.values()["leverage"] == MIN_LEVERAGE


def test_spot_mode_disables_leverage_and_margin_mode(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.market_type = "spot"
    bar = _bar(qapp, settings)
    assert bar.values()["market_type"] == "spot"
    # 私有控件直接断言（UI 行为是产品需求的一部分）
    assert bar._leverage_spin.isEnabled() is False
    assert bar._margin_mode_combo.isEnabled() is False


def test_futures_mode_enables_leverage_and_margin_mode(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.market_type = "futures"
    settings.auto_trade.leverage = 10
    bar = _bar(qapp, settings)
    assert bar._leverage_spin.isEnabled() is True
    assert bar._margin_mode_combo.isEnabled() is True


def test_title_follows_venue(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.venue = "okx"
    bar = _bar(qapp, settings)
    assert "OKX" in bar._title.text()

    settings.auto_trade.venue = "binance"
    bar.load_from_settings(settings)
    assert "币安" in bar._title.text()


def test_notional_label_reflects_margin_times_leverage(qapp: QApplication) -> None:
    settings = Settings()
    settings.auto_trade.market_type = "futures"
    settings.auto_trade.margin_per_trade_usdt = 10.0
    settings.auto_trade.leverage = 20
    bar = _bar(qapp, settings)
    text = bar._notional_label.text()
    assert "200" in text
    assert "20x" in text


def test_changed_signal_emits_on_user_edit(qapp: QApplication) -> None:
    settings = Settings()
    bar = _bar(qapp, settings)
    captured: list[dict] = []
    bar.changed.connect(captured.append)

    bar._leverage_spin.setValue(7)
    bar._market_combo.setCurrentIndex(bar._market_combo.findData("futures"))

    assert captured, "用户改控件必须发出 changed 信号"
    assert captured[-1]["leverage"] == 7 or captured[-1]["market_type"] == "futures"


def test_set_object_name_for_qss(qapp: QApplication) -> None:
    bar = _bar(qapp)
    assert bar.objectName() == "orderParamsBar"
    assert bar._title.objectName() == "orderParamsTitle"
