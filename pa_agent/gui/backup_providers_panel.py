"""「备用 AI 提供商」编辑面板：多组接口 + API Key，按顺序依次尝试。"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
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

from pa_agent.config.settings import AIProviderSettings

_COLUMNS = ("启用", "名称", "Base URL", "模型", "API Key")
_COL_ON = 0
_COL_NAME = 1
_COL_URL = 2
_COL_MODEL = 3
_COL_KEY = 4

#: 单次连通性测试的请求超时（秒）。
_TEST_TIMEOUT_S = 20.0


class _TestWorker(QThread):
    """后台跑一次最小 chat，避免测试时卡住对话框。"""

    finished_result = pyqtSignal(bool, str)

    def __init__(self, entry: AIProviderSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry

    def run(self) -> None:  # noqa: D102
        try:
            from pa_agent.ai.client_factory import create_ai_client

            client = create_ai_client(self._entry)
            reply = client.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                timeout_s=_TEST_TIMEOUT_S,
            )
            text = str(getattr(reply, "content", "") or "").strip()
            if text:
                self.finished_result.emit(True, f"可用：{text[:20]}")
            else:
                self.finished_result.emit(False, "接口无返回内容")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成状态文字
            self.finished_result.emit(False, f"{type(exc).__name__}: {exc}"[:160])


class BackupProvidersPanel(QGroupBox):
    """列出用户手填的备用接口，顺序就是尝试顺序。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("备用 AI 提供商（按顺序尝试）", parent)
        self._workers: list[_TestWorker] = []
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "主通道失败时，按这里的顺序逐个尝试，直到有一个能给出分析结果。"
            "只填了部分字段的行会被跳过（必须同时有 Base URL、模型、API Key）。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setObjectName("backupProvidersTable")
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        # 启用 / 名称 / 模型 / API Key 固定宽度，Base URL 自适应剩余空间，
        # 这样即使窗口不宽也不会出现横向滚动条把 Key 列挤出可视区。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_COL_URL, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self._table.setColumnWidth(_COL_ON, 46)
        self._table.setColumnWidth(_COL_NAME, 92)
        self._table.setColumnWidth(_COL_MODEL, 130)
        self._table.setColumnWidth(_COL_KEY, 140)
        self._table.setMinimumHeight(160)
        layout.addWidget(self._table)

        row = QHBoxLayout()
        self._add_btn = QPushButton("+ 添加")
        self._up_btn = QPushButton("上移")
        self._down_btn = QPushButton("下移")
        self._remove_btn = QPushButton("删除")
        self._test_btn = QPushButton("测试选中")
        self._status_label = QLabel("")
        self._status_label.setObjectName("mutedLabel")
        self._status_label.setWordWrap(True)

        self._add_btn.clicked.connect(self._on_add)
        self._up_btn.clicked.connect(lambda: self._move(-1))
        self._down_btn.clicked.connect(lambda: self._move(1))
        self._remove_btn.clicked.connect(self._on_remove)
        self._test_btn.clicked.connect(self._on_test)

        for btn in (self._add_btn, self._up_btn, self._down_btn,
                    self._remove_btn, self._test_btn):
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self._status_label)

    # ── 数据 ──────────────────────────────────────────────────────────────────

    def load_entries(self, entries: list[Any]) -> None:
        """用配置里的列表重建表格。"""
        self._table.setRowCount(0)
        for entry in entries or []:
            self._append_row(
                enabled=bool(getattr(entry, "enabled", True)),
                name=str(getattr(entry, "name", "") or ""),
                base_url=str(getattr(entry, "base_url", "") or ""),
                model=str(getattr(entry, "model", "") or ""),
                api_key=str(getattr(entry, "api_key", "") or ""),
            )

    def collect_entries(self) -> list[AIProviderSettings]:
        """读回表格，产出配置对象列表（顺序即表格顺序）。"""
        out: list[AIProviderSettings] = []
        for row in range(self._table.rowCount()):
            base_url = self._cell_text(row, _COL_URL)
            model = self._cell_text(row, _COL_MODEL)
            api_key = self._key_text(row)
            name = self._cell_text(row, _COL_NAME) or model
            if not (base_url or model or api_key):
                continue  # 整行空白，直接丢弃
            out.append(
                AIProviderSettings(
                    name=name,
                    enabled=self._cell_enabled(row),
                    base_url=base_url,
                    model=model or "deepseek-flash",
                    api_key=api_key,
                    thinking=True,
                    reasoning_effort="low",
                )
            )
        return out

    # ── 行操作 ────────────────────────────────────────────────────────────────

    def _append_row(
        self,
        *,
        enabled: bool = True,
        name: str = "",
        base_url: str = "",
        model: str = "",
        api_key: str = "",
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        check = QTableWidgetItem()
        check.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        check.setCheckState(
            Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        )
        self._table.setItem(row, _COL_ON, check)

        self._table.setItem(row, _COL_NAME, QTableWidgetItem(name))
        self._table.setItem(row, _COL_URL, QTableWidgetItem(base_url))
        self._table.setItem(row, _COL_MODEL, QTableWidgetItem(model))

        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("输入 API Key")
        key_edit.setText(api_key)
        self._table.setCellWidget(row, _COL_KEY, key_edit)

    def _cell_text(self, row: int, column: int) -> str:
        item = self._table.item(row, column)
        return item.text().strip() if item is not None else ""

    def _cell_enabled(self, row: int) -> bool:
        item = self._table.item(row, _COL_ON)
        return bool(item is not None and item.checkState() == Qt.CheckState.Checked)

    def _key_text(self, row: int) -> str:
        widget = self._table.cellWidget(row, _COL_KEY)
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return ""

    def _current_row(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        if rows:
            return rows[0].row()
        return self._table.currentRow()

    def _on_add(self) -> None:
        self._append_row()
        last = self._table.rowCount() - 1
        self._table.selectRow(last)
        self._table.editItem(self._table.item(last, _COL_URL))
        self._status_label.setText("已添加一行，请填写 Base URL、模型与 API Key。")

    def _on_remove(self) -> None:
        row = self._current_row()
        if row < 0:
            self._status_label.setText("请先选中要删除的行。")
            return
        self._table.removeRow(row)
        self._status_label.setText("已删除该行，保存后生效。")

    def _move(self, delta: int) -> None:
        row = self._current_row()
        target = row + delta
        if row < 0 or target < 0 or target >= self._table.rowCount():
            return
        values = (
            self._cell_enabled(row),
            self._cell_text(row, _COL_NAME),
            self._cell_text(row, _COL_URL),
            self._cell_text(row, _COL_MODEL),
            self._key_text(row),
        )
        self._table.removeRow(row)
        self._table.insertRow(target)
        check = QTableWidgetItem()
        check.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        check.setCheckState(
            Qt.CheckState.Checked if values[0] else Qt.CheckState.Unchecked
        )
        self._table.setItem(target, _COL_ON, check)
        self._table.setItem(target, _COL_NAME, QTableWidgetItem(values[1]))
        self._table.setItem(target, _COL_URL, QTableWidgetItem(values[2]))
        self._table.setItem(target, _COL_MODEL, QTableWidgetItem(values[3]))
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setText(values[4])
        self._table.setCellWidget(target, _COL_KEY, key_edit)
        self._table.selectRow(target)
        self._status_label.setText("顺序已调整，保存后生效。")

    # ── 测试 ──────────────────────────────────────────────────────────────────

    def _on_test(self) -> None:
        row = self._current_row()
        if row < 0:
            self._status_label.setText("请先选中要测试的行。")
            return
        base_url = self._cell_text(row, _COL_URL)
        model = self._cell_text(row, _COL_MODEL)
        api_key = self._key_text(row)
        missing = [
            label
            for label, value in (
                ("Base URL", base_url),
                ("模型", model),
                ("API Key", api_key),
            )
            if not value
        ]
        if missing:
            self._status_label.setText(
                f"第 {row + 1} 行缺少：{'、'.join(missing)}，无法测试。"
            )
            return
        entry = AIProviderSettings(
            name=self._cell_text(row, _COL_NAME) or model,
            base_url=base_url,
            model=model,
            api_key=api_key,
            thinking=True,
            reasoning_effort="low",
        )
        worker = _TestWorker(entry, self)
        self._test_btn.setEnabled(False)
        self._status_label.setText(f"正在测试第 {row + 1} 行…")
        worker.finished_result.connect(
            lambda ok, msg, r=row: self._on_test_result(r, ok, msg)
        )
        worker.finished.connect(lambda w=worker: self._cleanup_worker(w))
        self._workers.append(worker)
        worker.start()

    def _on_test_result(self, row: int, ok: bool, message: str) -> None:
        self._test_btn.setEnabled(True)
        prefix = "成功" if ok else "失败"
        self._status_label.setText(f"第 {row + 1} 行{prefix}：{message}")

    def _cleanup_worker(self, worker: _TestWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()
