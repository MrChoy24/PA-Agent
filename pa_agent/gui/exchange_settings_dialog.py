"""交易所 API 与自动交易设置对话框.

集中提供两件「按需开启」的配置：
1. Binance / OKX 的 API Key（行情走公开接口不需要 Key，Key 只用于账户查询与下单）
2. 自动交易的各项闸门（总开关、试运行、交易所、置信度、单笔上限、每日单数、白名单、止损要求）

实现上直接复用 SettingsDialog：它是唯一覆盖全部 Settings 字段的表单，
这里只负责把首屏滚动到「交易所 API」分组并改写标题，避免出现第二套控件。
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import QWidget

from pa_agent.config.settings import Settings
from pa_agent.gui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)


class ExchangeSettingsDialog(SettingsDialog):
    """「交易所与自动交易」入口：复用 SettingsDialog，聚焦到对应分组."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(settings, parent)
        self.setWindowTitle("交易所与自动交易设置")
        self.setMinimumWidth(600)
        self.setMinimumHeight(760)
        self._scrolled_once = False

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt 命名
        """首次显示后再滚动：布局尚未计算时 ensureWidgetVisible 不生效."""
        super().showEvent(event)
        if not self._scrolled_once:
            self._scrolled_once = True
            # 延后一拍，等滚动区拿到正确的 viewport 尺寸
            QTimer.singleShot(0, self._scroll_to_exchange_group)

    def _scroll_to_exchange_group(self) -> None:
        """把滚动区定位到「交易所 API」分组，省得用户自己往下找."""
        group = getattr(self, "_exchange_group", None)
        scroll = getattr(self, "_scroll_area", None)
        if group is None or scroll is None:
            return
        try:
            bar = scroll.verticalScrollBar()
            if bar is None:
                return
            # ensureWidgetVisible 只保证「可见」，当分组已在视口内时它不会滚动；
            # 这里直接按分组的 y 坐标定位，让分组标题落到视口上方。
            target = max(0, group.pos().y() - 10)
            bar.setValue(min(target, bar.maximum()))
        except Exception as exc:  # noqa: BLE001 - 纯装饰性动作，失败无影响
            logger.debug("滚动到交易所分组失败: %s", exc)
