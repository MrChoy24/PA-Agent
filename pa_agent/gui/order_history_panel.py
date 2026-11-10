"""历史下单面板：查看系统分析后自动下单/试运行/被拦截的记录.

数据来源是 ``trade_records/auto_orders.jsonl``——``AutoTrader`` 每评估一次决策
就追加一行，因此这里能同时看到：

* **已提交** — 真实发到交易所并成功
* **试运行** — 只记录意图、没有发单（``dry_run=True``）
* **被拦截** — 被九道闸门之一挡下（置信度不足、无止损、白名单、每日上限…）

面板只读，不写任何文件（导出 CSV 除外）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.gui.theme import tokens as T
from pa_agent.records.order_history import (
    STATUS_DRY_RUN,
    STATUS_EXECUTED,
    STATUS_SKIPPED,
    OrderHistoryRow,
    export_csv,
    filter_rows,
    load_order_history,
    resolve_history_path,
    summarize,
)

logger = logging.getLogger(__name__)

_COLUMNS = (
    "时间",
    "交易所",
    "品种",
    "方向",
    "杠杆",
    "名义",
    "状态",
    "原因",
    "周期",
    "市场",
    "保证金",
    "价格",
    "订单号",
    "数量",
)

_BUCKET_LABELS = (
    ("全部", ""),
    ("已提交", STATUS_EXECUTED),
    ("试运行", STATUS_DRY_RUN),
    ("被拦截", STATUS_SKIPPED),
)

# 列宽（px）。右侧面板只有约 830px，14 列放不下，所以按「先看什么」排序：
# 时间/品种/方向/杠杆/名义/状态/原因 放前面，一屏内可见；
# 周期/市场/保证金/价格/订单号/数量 排在后面，需要时横向滚动。
_COLUMN_WIDTHS = (
    138,  # 时间
    56,   # 交易所
    96,   # 品种
    50,   # 方向
    46,   # 杠杆
    76,   # 名义
    64,   # 状态
    200,  # 原因
    46,   # 周期
    50,   # 市场
    76,   # 保证金
    84,   # 价格
    84,   # 订单号
    84,   # 数量
)

# 数量与「名义 ÷ 价格」等价，默认不显示（CSV 导出与 tooltip 仍完整给出）。
_HIDDEN_COLUMNS = (_COLUMNS.index("数量"),)

_VENUE_LABELS = (("全部", ""), ("币安", "binance"), ("OKX", "okx"))


class OrderHistoryPanel(QWidget):
    """「历史下单」标签页：表格 + 状态筛选 + 关键字搜索 + 导出."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[OrderHistoryRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 顶部：标题 + 汇总 ─────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("历史下单")
        title.setStyleSheet(f"color: {T.ACCENT}; font-weight: 600; font-size: 13px;")
        header.addWidget(title)

        self._summary_label = QLabel("—")
        self._summary_label.setObjectName("mutedLabel")
        self._summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header.addWidget(self._summary_label, 1)
        layout.addLayout(header)

        # ── 工具行：筛选 ──────────────────────────────────────────────────────
        tools = QHBoxLayout()
        tools.setSpacing(6)

        self._bucket_combo = QComboBox()
        for label, data in _BUCKET_LABELS:
            self._bucket_combo.addItem(label, data)
        self._bucket_combo.setToolTip("按状态筛选：已提交 / 试运行 / 被拦截")
        self._bucket_combo.setMinimumWidth(96)
        tools.addWidget(QLabel("状态:"))
        tools.addWidget(self._bucket_combo)

        self._venue_combo = QComboBox()
        for label, data in _VENUE_LABELS:
            self._venue_combo.addItem(label, data)
        self._venue_combo.setMinimumWidth(88)
        tools.addWidget(QLabel("交易所:"))
        tools.addWidget(self._venue_combo)

        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("搜索品种 / 原因 / 订单号…")
        self._keyword_edit.setClearButtonEnabled(True)
        tools.addWidget(self._keyword_edit, 1)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setToolTip("重新读取 trade_records/auto_orders.jsonl")
        self._refresh_btn.clicked.connect(self.reload)
        tools.addWidget(self._refresh_btn)

        self._export_btn = QPushButton("导出 CSV")
        self._export_btn.setToolTip("把当前筛选结果导出为 CSV（Excel 可直接打开）")
        self._export_btn.clicked.connect(self._on_export)
        tools.addWidget(self._export_btn)

        layout.addLayout(tools)

        # ── 表格 ──────────────────────────────────────────────────────────────
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setObjectName("orderHistoryTable")
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, width in enumerate(_COLUMN_WIDTHS):
            header_view.resizeSection(index, width)
        for index in _HIDDEN_COLUMNS:
            self._table.setColumnHidden(index, True)
        layout.addWidget(self._table, 1)

        # ── 空状态提示 ────────────────────────────────────────────────────────
        self._empty_label = QLabel("")
        self._empty_label.setObjectName("mutedLabel")
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._empty_label)

        # 筛选变化 → 重新渲染（不重读文件）
        self._bucket_combo.currentIndexChanged.connect(self._render)
        self._venue_combo.currentIndexChanged.connect(self._render)
        self._keyword_edit.textChanged.connect(self._render)

        self.reload()

    # ── 数据加载 ──────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """重新读取历史文件（切到本标签页时也会自动调用一次）."""
        self._rows = load_order_history()
        self._render()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """每次切到本页时自动刷新一次，避免盯着旧数据。"""
        super().showEvent(event)
        QTimer.singleShot(0, self.reload)

    def _visible_rows(self) -> list[OrderHistoryRow]:
        return filter_rows(
            self._rows,
            bucket=str(self._bucket_combo.currentData() or ""),
            venue=str(self._venue_combo.currentData() or ""),
            keyword=self._keyword_edit.text() or "",
        )

    # ── 渲染 ──────────────────────────────────────────────────────────────────

    def _render(self, *_args: Any) -> None:
        rows = self._visible_rows()
        stats = summarize(self._rows)
        shown_stats = summarize(rows)

        self._summary_label.setText(
            f"共 {stats['total']} 条：已提交 {stats['executed']} · "
            f"试运行 {stats['dry_run']} · 被拦截 {stats['skipped']} · "
            f"真实成交名义 {stats['real_notional_usdt']:,.2f} USDT"
            + (
                ""
                if len(rows) == stats["total"]
                else f"　|　当前筛选 {len(rows)} 条（名义 {shown_stats['real_notional_usdt']:,.2f}）"
            )
        )

        self._table.setUpdatesEnabled(False)
        try:
            self._table.clearContents()
            self._table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for col_index, value in enumerate(self._cells(row)):
                    item = QTableWidgetItem(value)
                    # 列宽有限，长文本（品种 / 原因 / 订单号）靠 tooltip 补全
                    if value:
                        item.setToolTip(value)
                    if col_index in (4, 5, 10, 11, 13):
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    self._apply_colour(item, col_index, row)
                    self._table.setItem(row_index, col_index, item)
        finally:
            self._table.setUpdatesEnabled(True)

        if rows:
            self._empty_label.setText("")
            self._empty_label.hide()
        else:
            self._empty_label.setText(self._empty_hint())
            self._empty_label.show()

    @staticmethod
    def _cells(row: OrderHistoryRow) -> tuple[str, ...]:
        leverage = f"{row.leverage}x" if row.market_type == "futures" else "—"
        return (
            row.time_label,
            "币安" if row.venue == "binance" else ("OKX" if row.venue == "okx" else row.venue or "—"),
            row.symbol or "—",
            row.side_label,
            leverage,
            f"{row.notional_usdt:,.2f}" if row.notional_usdt else "—",
            row.status_label,
            row.reason or "",
            row.timeframe or "—",
            row.market_label,
            f"{row.margin_usdt:,.2f}" if row.margin_usdt else "—",
            f"{row.price:,.6f}".rstrip("0").rstrip(".") if row.price else "—",
            row.order_id or "—",
            f"{row.qty:.8f}".rstrip("0").rstrip(".") if row.qty else "—",
        )

    @staticmethod
    def _apply_colour(item: QTableWidgetItem, column: int, row: OrderHistoryRow) -> None:
        """给方向与状态上色，让真实单在长列表里一眼可辨。"""
        if column == 3:  # 方向
            lowered = row.side.strip().lower()
            if lowered in ("buy", "long", "做多"):
                item.setForeground(QColor(T.SUCCESS))
            elif lowered in ("sell", "short", "做空"):
                item.setForeground(QColor(T.DANGER))
        elif column == 6:  # 状态
            bucket = row.status_bucket
            if bucket == STATUS_EXECUTED:
                item.setForeground(QColor(T.SUCCESS))
            elif bucket == STATUS_DRY_RUN:
                item.setForeground(QColor(T.WARNING))
            else:
                item.setForeground(QColor(T.FG_3))
        elif column == 7 and row.status_bucket == STATUS_SKIPPED:
            item.setForeground(QColor(T.FG_3))

    def _empty_hint(self) -> str:
        path = resolve_history_path()
        if not self._rows:
            return (
                "还没有任何下单记录。\n\n"
                "这里的数据来自自动交易的每一次评估：\n"
                "· 先打开「交易所与自动交易」→ 勾选「启用自动交易」与「试运行」\n"
                "· 每完成一次分析、出现下单机会时，就会追加一行（试运行也会记录）\n\n"
                f"记录文件：{path}"
            )
        return "当前筛选条件下没有记录，换个状态或关键字试试。"

    # ── 导出 ──────────────────────────────────────────────────────────────────

    def _on_export(self) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出下单历史",
            str(Path("trade_records") / "auto_orders_export.csv"),
            "CSV 文件 (*.csv)",
        )
        if not path:
            return
        if export_csv(rows, path):
            self._summary_label.setText(f"已导出 {len(rows)} 条到 {path}")
        else:
            self._summary_label.setText("导出失败，请检查目标路径是否可写")


__all__ = ["OrderHistoryPanel"]
