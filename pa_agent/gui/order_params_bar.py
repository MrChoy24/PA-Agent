"""下单参数条：交易所 / 市场 / 每单保证金 / 杠杆 / 订单类型 / 保证金模式.

仿交易终端的「下单参数」面板——所有改动**即时生效**（直接写回
``settings.auto_trade`` 并落盘 ``config/settings.json``），不需要重启程序。

这里只负责「展示 + 采集」，真正的下单判定仍在
:class:`pa_agent.trading.auto_trader.AutoTrader` 的九道闸门里完成。
"""
from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from pa_agent.gui.theme import tokens as T

logger = logging.getLogger(__name__)

#: 杠杆取值区间（与交易所上限对齐：Binance/OKX 常见最高 125x）
MIN_LEVERAGE = 1
MAX_LEVERAGE = 125

_VENUE_LABELS = {"binance": "币安", "okx": "OKX"}
_MARKET_LABELS = (("现货", "spot"), ("合约", "futures"))
_ORDER_TYPE_LABELS = (("市价", "MARKET"), ("限价", "LIMIT"))
_MARGIN_MODE_LABELS = (("逐仓", "isolated"), ("全仓", "cross"))


def _combo(pairs: tuple[tuple[str, str], ...], value: str) -> QComboBox:
    """Build a combo box from ``(label, data)`` pairs and select ``value``."""
    box = QComboBox()
    for label, data in pairs:
        box.addItem(label, data)
    index = box.findData(value)
    if index >= 0:
        box.setCurrentIndex(index)
    return box


class OrderParamsBar(QFrame):
    """主界面顶部的一行「下单参数」，改动立即持久化.

    信号 :attr:`changed` 在任一控件变化后发出，携带 :meth:`values` 的结果，
    方便主窗口据此刷新提示或写日志。
    """

    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        #: 载入/同步期间抑制信号回写，避免「读配置」被当成「用户改配置」
        self._loading = False

        self.setObjectName("orderParamsBar")
        self.setStyleSheet(
            f"QFrame#orderParamsBar {{"
            f" background-color: {T.SURFACE_1};"
            f" border: 1px solid {T.BORDER_SUBTLE};"
            f" border-radius: {T.RADIUS_SM}px;"
            f" }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self._title = QLabel("币安下单参数")
        self._title.setObjectName("orderParamsTitle")
        self._title.setStyleSheet(
            f"color: {T.ACCENT}; font-weight: 600; font-size: 12px;"
            " background: transparent; border: none;"
        )
        layout.addWidget(self._title)

        # ── 交易所 ────────────────────────────────────────────────────────────
        layout.addWidget(self._muted("交易所:"))
        self._venue_combo = _combo(
            (("币安", "binance"), ("OKX", "okx")), "binance"
        )
        self._venue_combo.setToolTip(
            "自动下单走哪家交易所（与「数据来源」相互独立：可以用 Binance 看行情、用 OKX 下单）"
        )
        self._venue_combo.setMinimumWidth(88)
        layout.addWidget(self._venue_combo)

        # ── 市场类型 ──────────────────────────────────────────────────────────
        layout.addWidget(self._muted("市场:"))
        self._market_combo = _combo(_MARKET_LABELS, "spot")
        self._market_combo.setToolTip(
            "现货 = /api/v3（Binance）或 SPOT（OKX），杠杆不生效；\n"
            "合约 = U 本位永续（/fapi 或 SWAP），杠杆生效。"
        )
        self._market_combo.setMinimumWidth(76)
        layout.addWidget(self._market_combo)

        # ── 每单保证金 ────────────────────────────────────────────────────────
        layout.addWidget(self._muted("每单保证金:"))
        self._margin_spin = QDoubleSpinBox()
        self._margin_spin.setRange(0.0, 1_000_000.0)
        self._margin_spin.setDecimals(2)
        self._margin_spin.setSingleStep(1.0)
        self._margin_spin.setSuffix(" U")
        self._margin_spin.setMinimumWidth(104)
        self._margin_spin.setToolTip(
            "逐仓：每一单投入的保证金（USDT），名义金额 = 保证金 × 杠杆；\n"
            "全仓：没有「每单保证金」这个概念，保证金来自整个账户可用余额，此栏不生效。"
        )
        layout.addWidget(self._margin_spin)

        # ── 杠杆 ──────────────────────────────────────────────────────────────
        layout.addWidget(self._muted("杠杆:"))
        self._leverage_spin = QSpinBox()
        self._leverage_spin.setRange(MIN_LEVERAGE, MAX_LEVERAGE)
        self._leverage_spin.setSingleStep(1)
        self._leverage_spin.setSuffix("x")
        self._leverage_spin.setMinimumWidth(78)
        self._leverage_spin.setToolTip(
            f"杠杆倍数 {MIN_LEVERAGE}~{MAX_LEVERAGE}。仅「合约」市场生效；现货固定 1 倍。"
        )
        layout.addWidget(self._leverage_spin)

        # ── 订单类型 ──────────────────────────────────────────────────────────
        layout.addWidget(self._muted("订单类型:"))
        self._order_type_combo = _combo(_ORDER_TYPE_LABELS, "MARKET")
        self._order_type_combo.setToolTip(
            "市价 = 立即成交；限价 = 用决策给出的 entry_price 挂单排队。"
        )
        self._order_type_combo.setMinimumWidth(76)
        layout.addWidget(self._order_type_combo)

        # ── 保证金模式 ────────────────────────────────────────────────────────
        layout.addWidget(self._muted("保证金模式:"))
        self._margin_mode_combo = _combo(_MARGIN_MODE_LABELS, "isolated")
        self._margin_mode_combo.setToolTip(
            "逐仓 = 亏损只吃掉该单保证金；全仓 = 动用到账户可用余额。仅「合约」市场生效。"
        )
        self._margin_mode_combo.setMinimumWidth(76)
        layout.addWidget(self._margin_mode_combo)

        # ── 预计名义金额（只读提示）───────────────────────────────────────────
        self._notional_label = QLabel("")
        self._notional_label.setStyleSheet(
            f"color: {T.FG_2}; font-size: 11px;"
            " background: transparent; border: none;"
        )
        layout.addWidget(self._notional_label)

        layout.addStretch()

        self._instant_label = QLabel("即时生效")
        self._instant_label.setStyleSheet(
            f"color: {T.SUCCESS}; font-size: 11px;"
            " background: transparent; border: none;"
        )
        self._instant_label.setToolTip(
            "这里的改动会立刻写入 config/settings.json，下一笔自动下单就按新参数执行。"
        )
        layout.addWidget(self._instant_label)

        # ── 接线 ──────────────────────────────────────────────────────────────
        self._venue_combo.currentIndexChanged.connect(self._on_any_changed)
        self._market_combo.currentIndexChanged.connect(self._on_any_changed)
        self._margin_spin.valueChanged.connect(self._on_any_changed)
        self._leverage_spin.valueChanged.connect(self._on_any_changed)
        self._order_type_combo.currentIndexChanged.connect(self._on_any_changed)
        self._margin_mode_combo.currentIndexChanged.connect(self._on_any_changed)

        self.load_from_settings(settings)

    # ── 构建辅助 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _muted(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {T.FG_3}; font-size: 11px;"
            " background: transparent; border: none;"
        )
        return label

    # ── 配置 <-> 控件 ─────────────────────────────────────────────────────────

    def load_from_settings(self, settings: Any) -> None:
        """按 ``settings.auto_trade`` 回填控件（不会触发回写）。"""
        self._settings = settings
        cfg = getattr(settings, "auto_trade", None) if settings is not None else None
        self._loading = True
        try:
            if cfg is not None:
                venue = str(getattr(cfg, "venue", "binance") or "binance").lower()
                idx = self._venue_combo.findData(venue)
                self._venue_combo.setCurrentIndex(idx if idx >= 0 else 0)

                market = str(getattr(cfg, "market_type", "spot") or "spot").lower()
                idx = self._market_combo.findData(market)
                self._market_combo.setCurrentIndex(idx if idx >= 0 else 0)

                margin = float(getattr(cfg, "margin_per_trade_usdt", 10.0) or 0.0)
                self._margin_spin.setValue(max(0.0, margin))

                leverage = int(getattr(cfg, "leverage", 1) or 1)
                self._leverage_spin.setValue(
                    max(MIN_LEVERAGE, min(MAX_LEVERAGE, leverage))
                )

                order_type = str(getattr(cfg, "order_type", "MARKET") or "MARKET").upper()
                idx = self._order_type_combo.findData(order_type)
                self._order_type_combo.setCurrentIndex(idx if idx >= 0 else 0)

                margin_mode = str(
                    getattr(cfg, "margin_mode", "isolated") or "isolated"
                ).lower()
                idx = self._margin_mode_combo.findData(margin_mode)
                self._margin_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._loading = False
        self._refresh_visuals()

    def values(self) -> dict[str, Any]:
        """当前控件值（可直接塞进 ``settings.auto_trade``）。"""
        return {
            "venue": str(self._venue_combo.currentData() or "binance"),
            "market_type": str(self._market_combo.currentData() or "spot"),
            "margin_per_trade_usdt": round(float(self._margin_spin.value()), 2),
            "leverage": int(self._leverage_spin.value()),
            "order_type": str(self._order_type_combo.currentData() or "MARKET"),
            "margin_mode": str(self._margin_mode_combo.currentData() or "isolated"),
        }

    def apply_to_settings(self, settings: Any) -> bool:
        """把控件值写进 ``settings.auto_trade``；返回是否真的改动了值。"""
        cfg = getattr(settings, "auto_trade", None) if settings is not None else None
        if cfg is None:
            return False
        payload = self.values()
        dirty = False
        for key, value in payload.items():
            try:
                if getattr(cfg, key, None) != value:
                    setattr(cfg, key, value)
                    dirty = True
            except Exception as exc:  # noqa: BLE001 — 单个字段失败不影响其它字段
                logger.debug("下单参数写回失败（%s）: %s", key, exc)
        return dirty

    # ── 交互 ──────────────────────────────────────────────────────────────────

    def _on_any_changed(self, *_args: Any) -> None:
        self._refresh_visuals()
        if self._loading:
            return
        payload = self.values()
        settings = self._settings
        if settings is not None:
            # 只改内存里的 settings，落盘由持有方（MainWindow）负责。
            # 控件自己写 config/settings.json 会让单元测试在改控件时
            # 顺手改掉真实配置，也会让「界面组件」隐式依赖磁盘路径。
            self.apply_to_settings(settings)
        self.changed.emit(payload)

    def _refresh_visuals(self) -> None:
        """同步标题、杠杆可用性、预计名义金额。"""
        venue = str(self._venue_combo.currentData() or "binance")
        self._title.setText(f"{_VENUE_LABELS.get(venue, venue)}下单参数")

        is_futures = str(self._market_combo.currentData() or "spot") == "futures"
        is_cross = is_futures and str(self._margin_mode_combo.currentData() or "isolated") == "cross"
        self._leverage_spin.setEnabled(is_futures)
        self._margin_mode_combo.setEnabled(is_futures)
        # 全仓没有「每单固定保证金」：该输入框不参与计算，置灰避免误导。
        self._margin_spin.setEnabled(not is_cross)
        if not is_futures:
            self._leverage_spin.setToolTip(
                "现货市场不使用杠杆（固定 1 倍）。切换到「合约」后才能设置。"
            )
        else:
            self._leverage_spin.setToolTip(
                f"杠杆倍数 {MIN_LEVERAGE}~{MAX_LEVERAGE}；下单前会自动同步到交易所。"
            )
        if is_cross:
            self._margin_spin.setToolTip(
                "全仓模式没有「每单保证金」：保证金来自整个账户可用余额，"
                "此栏不生效，名义金额由「单笔上限」控制。"
            )

        margin = float(self._margin_spin.value())
        leverage = int(self._leverage_spin.value()) if is_futures else 1
        if is_cross:
            self._notional_label.setText(
                "全仓模式：名义金额 = 决策/单笔上限，保证金由账户余额扣除"
            )
        elif margin <= 0:
            self._notional_label.setText("名义 ≈ —（保证金为 0）")
        elif is_futures:
            self._notional_label.setText(
                f"名义 ≈ {margin * leverage:,.2f} USDT（{margin:,.2f} × {leverage}x）"
            )
        else:
            self._notional_label.setText(f"名义 = {margin:,.2f} USDT（现货）")
