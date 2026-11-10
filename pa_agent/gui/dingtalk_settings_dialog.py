"""钉钉机器人设置对话框.

提供 GUI 界面填写并保存到 config/settings.json 的 dingtalk 段。
包含：Webhook 地址、加签密钥、@ 提醒设置、推送范围、消息格式，
以及启用/禁用开关，并带有一键发送测试消息（后台线程，不阻塞界面）。
"""
from __future__ import annotations

import copy
import logging

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings

logger = logging.getLogger(__name__)


class _DingTalkTestWorker(QThread):
    """在后台线程发送测试消息，避免 Webhook 超时冻结对话框."""

    finished_result = pyqtSignal(bool, str)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            from pa_agent.notify.dingtalk_notifier import send_test_message

            ok, msg = send_test_message(self._settings)
        except Exception as exc:  # noqa: BLE001 - 测试失败只提示，不抛异常
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        self.finished_result.emit(bool(ok), str(msg))


class DingTalkSettingsDialog(QDialog):
    """填写钉钉机器人 Webhook / 加签等配置的模态对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._test_workers: list[_DingTalkTestWorker] = []
        self.setWindowTitle("钉钉机器人设置")
        self.setMinimumWidth(560)
        self._setup_ui()
        self._load_values()

    # ── UI 搭建 ───────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ── 状态开关 ───────────────────────────────────────────────────────────
        self._enabled_check = QCheckBox("启用钉钉通知（下单信号推送到钉钉群）")
        self._enabled_check.setToolTip(
            "关闭后即使有下单决策也不发送钉钉消息，其余配置保留。"
        )
        root.addWidget(self._enabled_check)

        # ── 基础配置 ───────────────────────────────────────────────────────────
        basic_group = QGroupBox("群机器人（必填）")
        basic_form = QFormLayout(basic_group)
        basic_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        basic_hint = QLabel(
            "钉钉群 → 右上角设置 → 智能群助手 → 添加机器人 → 自定义 → "
            "安全设置勾选「加签」，复制 Webhook 与加签密钥。"
        )
        basic_hint.setWordWrap(True)
        basic_hint.setObjectName("mutedLabel")
        basic_form.addRow(basic_hint)

        self._webhook_edit = QLineEdit()
        self._webhook_edit.setPlaceholderText(
            "https://oapi.dingtalk.com/robot/send?access_token=xxxxxx"
        )
        self._webhook_edit.setToolTip(
            "钉钉群 → 智能群助手 → 机器人 → 复制 Webhook 地址"
        )
        basic_form.addRow("Webhook URL:", self._webhook_edit)

        secret_row = QHBoxLayout()
        self._secret_edit = QLineEdit()
        self._secret_edit.setPlaceholderText("留空则不启用加签校验")
        self._secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_edit.setToolTip(
            "机器人安全设置 → 加签 → 复制 SEC 开头的密钥（可选，留空不校验）"
        )
        secret_row.addWidget(self._secret_edit)
        self._show_secret_btn = QPushButton("显示")
        self._show_secret_btn.setCheckable(True)
        self._show_secret_btn.setFixedWidth(52)
        self._show_secret_btn.toggled.connect(self._toggle_secret_visibility)
        secret_row.addWidget(self._show_secret_btn)
        basic_form.addRow("加签密钥（Secret）:", secret_row)

        root.addWidget(basic_group)

        # ── 提醒与推送范围 ─────────────────────────────────────────────────────
        push_group = QGroupBox("提醒与推送")
        push_form = QFormLayout(push_group)
        push_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._at_all_check = QCheckBox("@所有人")
        self._at_all_check.setToolTip("勾选后每条消息都会 @所有人，谨慎使用。")
        push_form.addRow("提醒:", self._at_all_check)

        self._at_mobiles_edit = QLineEdit()
        self._at_mobiles_edit.setPlaceholderText("13800000000, 13900000000（可选）")
        self._at_mobiles_edit.setToolTip(
            "需要 @ 的手机号，多个用逗号分隔；留空则不 @ 具体的人。"
        )
        push_form.addRow("@手机号:", self._at_mobiles_edit)

        self._msg_type_combo = QComboBox()
        self._msg_type_combo.addItem("Markdown（推荐，带标题与表格感）", "markdown")
        self._msg_type_combo.addItem("纯文本（最兼容）", "text")
        push_form.addRow("消息格式:", self._msg_type_combo)

        self._order_only_check = QCheckBox("仅在下单机会时推送")
        self._order_only_check.setToolTip(
            "勾选后只推送含下单决策的信号；取消勾选则连普通分析状态一并推送。"
        )
        push_form.addRow("推送范围:", self._order_only_check)

        root.addWidget(push_group)

        # ── 测试按钮 ────────────────────────────────────────────────────────────
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("发送测试消息")
        self._test_btn.setToolTip(
            "使用当前填写的配置向钉钉群发送一条测试消息，验证 Webhook 与加签是否正常。"
        )
        self._test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self._test_btn)

        self._status_label = QLabel("")
        self._status_label.setObjectName("mutedLabel")
        self._status_label.setWordWrap(True)
        test_row.addWidget(self._status_label, 1)
        root.addLayout(test_row)

        # ── 确认 / 取消 ────────────────────────────────────────────────────────
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = btn_box.button(QDialogButtonBox.StandardButton.Save)
        if save_btn is not None:
            save_btn.setText("保存")
        cancel_btn = btn_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("取消")
        btn_box.accepted.connect(self._on_save)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        cfg = getattr(self._settings, "dingtalk", None)
        if cfg is None:
            return
        self._enabled_check.setChecked(bool(getattr(cfg, "enabled", False)))
        self._webhook_edit.setText(str(getattr(cfg, "webhook_url", "") or ""))
        self._secret_edit.setText(str(getattr(cfg, "secret", "") or ""))
        self._at_all_check.setChecked(bool(getattr(cfg, "at_all", False)))
        self._at_mobiles_edit.setText(
            ", ".join(getattr(cfg, "at_mobiles", []) or [])
        )
        self._order_only_check.setChecked(
            bool(getattr(cfg, "notify_on_order_only", True))
        )
        msg_type = str(getattr(cfg, "msg_type", "markdown") or "markdown")
        idx = self._msg_type_combo.findData(msg_type)
        self._msg_type_combo.setCurrentIndex(idx if idx >= 0 else 0)

    @staticmethod
    def _split_mobiles(text: str) -> list[str]:
        raw = (text or "").replace("，", ",").replace(";", ",").replace(" ", ",")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _apply_values_to_settings(self) -> None:
        cfg = getattr(self._settings, "dingtalk", None)
        if cfg is None:
            return
        cfg.enabled = self._enabled_check.isChecked()
        cfg.webhook_url = self._webhook_edit.text().strip()
        cfg.secret = self._secret_edit.text().strip()
        cfg.at_all = self._at_all_check.isChecked()
        cfg.at_mobiles = self._split_mobiles(self._at_mobiles_edit.text())
        cfg.notify_on_order_only = self._order_only_check.isChecked()
        cfg.msg_type = str(self._msg_type_combo.currentData() or "markdown")

    def _on_save(self) -> None:
        self._apply_values_to_settings()
        cfg = getattr(self._settings, "dingtalk", None)
        if cfg is not None and cfg.enabled and not cfg.webhook_url:
            QMessageBox.warning(
                self,
                "配置不完整",
                "已启用钉钉通知，但 Webhook URL 为空。\n请填写 Webhook URL 或关闭启用开关。",
            )
            return
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
            self.accept()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "保存失败",
                f"写入 config/settings.json 失败：\n{exc}",
            )

    # ── 显示 / 隐藏密钥 ───────────────────────────────────────────────────────

    def _toggle_secret_visibility(self, checked: bool) -> None:
        if checked:
            self._secret_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_secret_btn.setText("隐藏")
        else:
            self._secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_secret_btn.setText("显示")

    # ── 测试发送 ──────────────────────────────────────────────────────────────

    def _on_test(self) -> None:
        """用当前表单填写的值向钉钉群发送测试消息（后台线程）."""
        webhook_url = self._webhook_edit.text().strip()
        if not webhook_url:
            QMessageBox.warning(self, "缺少配置", "请先填写 Webhook URL 再测试。")
            return

        # 用当前界面的值构造一个临时 Settings 副本，避免「点测试就改内存配置」
        try:
            probe = copy.deepcopy(self._settings)
        except Exception:  # noqa: BLE001 - 极端情况下退回原对象
            probe = self._settings
        self._apply_values_to_settings()
        probe.dingtalk.enabled = True
        probe.dingtalk.webhook_url = webhook_url
        probe.dingtalk.secret = self._secret_edit.text().strip()
        probe.dingtalk.at_all = self._at_all_check.isChecked()
        probe.dingtalk.at_mobiles = self._split_mobiles(self._at_mobiles_edit.text())
        probe.dingtalk.msg_type = str(self._msg_type_combo.currentData() or "markdown")

        self._status_label.setText("正在发送…")
        self._test_btn.setEnabled(False)

        worker = _DingTalkTestWorker(probe, self)
        self._test_workers.append(worker)

        def _on_result(ok: bool, msg: str) -> None:
            self._status_label.setText(("成功：" if ok else "失败：") + msg)
            self._test_btn.setEnabled(True)

        worker.finished_result.connect(_on_result)
        worker.start()
