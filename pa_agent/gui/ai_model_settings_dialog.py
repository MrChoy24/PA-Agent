"""AI 模型设置对话框 — 只包含 AI 提供商相关字段."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.settings import Settings, loaded_digest, save_settings
from pa_agent.gui.backup_providers_panel import BackupProvidersPanel
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.ai.qclaw_connector import (
    detect_qclaw,
    is_openclaw_model,
    should_use_qclaw_provider,
)
from pa_agent.ai.workbuddy_connector import (
    detect_workbuddy,
    is_openclaw_wb_model,
    should_use_workbuddy_provider,
)
from pa_agent.ai.trae_connector import (
    detect_trae_cn,
    is_openclaw_twc_model,
    should_use_trae_cn_provider,
)
from pa_agent.ai.qoder_connector import (
    detect_qoder_cn,
    is_openclaw_qc_model,
    should_use_qoder_cn_provider,
)

_AGENT_TUTORIAL_URL = (
    "https://my.feishu.cn/wiki/BEdFwGJhaiATbukuD2HccSXCnrb?from=from_copylink"
)


class AIModelSettingsDialog(QDialog):
    """AI 模型 / 提供商配置对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 模型设置")
        self.setMinimumWidth(720)
        self._settings = settings
        self._setup_ui()
        self._load_values()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        provider_group = QGroupBox("AI 提供商")
        form = QFormLayout(provider_group)

        self._model_edit = QLineEdit()
        form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("隐藏")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        form.addRow("API Key:", api_key_row)

        # GLM 备用的模型 / Base URL / API Key 三件套统一下方「备用 AI 提供商」
        # 面板的第 0 行（内置行），这里不再单独放一行 Key。

        self._thinking_check = QCheckBox("启用 Thinking")
        form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._agent_tutorial_btn = QPushButton("智能体使用教程及问题解决方法")
        self._agent_tutorial_btn.setToolTip(_AGENT_TUTORIAL_URL)
        self._agent_tutorial_btn.clicked.connect(self._open_agent_tutorial_url)
        form.addRow("", self._agent_tutorial_btn)

        root.addWidget(provider_group)

        self._backup_panel = BackupProvidersPanel()
        root.addWidget(self._backup_panel)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.setText("保存")
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 加载 / 保存 ────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        p = self._settings.provider
        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)
        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)
        self._backup_panel.load_entries(list(getattr(self._settings, "ai_backups", None) or []))
        self._backup_panel.load_glm_entry(
            model=str(getattr(p, "backup_glm_model", "") or ""),
            base_url=str(getattr(p, "backup_glm_base_url", "") or ""),
            api_key=str(getattr(p, "backup_glm_api_key", "") or ""),
            enabled=bool(getattr(p, "backup_glm_enabled", True)),
        )

    def _on_save(self) -> None:
        p = self._settings.provider
        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()

        # Explicit model aliases win over stale base_url (openclaw_wb before openclaw).
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_workbuddy_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "WorkBuddy 配置异常", err)
                return
        elif is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_trae_cn_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "TRAE Work CN 配置异常", err)
                return
        elif is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_qoder_cn_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "Qoder CN 配置异常", err)
                return
        elif is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            p.api_key = api_key
            err = self._apply_qclaw_provider(preferred_model=model)
            if err:
                QMessageBox.warning(self, "QClaw 配置异常", err)
                return
        else:
            field_err = self._validate_provider_fields(model, base_url)
            if field_err:
                QMessageBox.warning(self, "AI 提供商配置有误", field_err)
                return
            p.model = model
            p.base_url = base_url
            p.api_key = api_key

        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]

        self._apply_glm_backup(p)
        self._settings.ai_backups = self._backup_panel.collect_entries()

        if not save_settings(
            self._settings, SETTINGS_JSON_PATH, base_digest=loaded_digest()
        ):
            QMessageBox.warning(
                self,
                "配置已被外部修改",
                "config/settings.json 在对话框打开期间被其他程序修改过，"
                "本次改动未写入磁盘。\n\n请关闭对话框重新打开后再改一次。",
            )
            return
        self.accept()

    def _apply_glm_backup(self, p: Any) -> None:
        """写回 GLM 备用三件套，并同步维护 fallbacks 里的那条 GLM 条目。

        - 勾选且有 Key：挂载 / 就地刷新（model、base_url、api_key 全跟着走）。
        - 未勾选或 Key 为空：当次就从链里剔除，不等下一次分析。
        """
        from pa_agent.ai.glm_backup import drop_glm_fallback, ensure_glm_fallback

        # 改配置之前的旧身份：链里那条老条目可能还是用旧值写成的。
        old_identity = (
            str(getattr(p, "backup_glm_model", "") or "").strip(),
            str(getattr(p, "backup_glm_base_url", "") or "").strip(),
        )
        model, base_url, api_key, enabled = self._backup_panel.collect_glm_entry()
        p.backup_glm_model = model
        p.backup_glm_base_url = base_url
        p.backup_glm_api_key = api_key
        p.backup_glm_enabled = enabled
        if enabled and api_key:
            ensure_glm_fallback(p, extra_identities=[old_identity])
            return
        drop_glm_fallback(p, extra_identities=[old_identity])

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def focus_api_key_field(self) -> None:
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")

    def _apply_qclaw_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.qclaw_connector import apply_qclaw_provider_to_settings
        return apply_qclaw_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_workbuddy_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.workbuddy_connector import apply_workbuddy_provider_to_settings
        return apply_workbuddy_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_trae_cn_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.trae_connector import apply_trae_cn_provider_to_settings
        return apply_trae_cn_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    def _apply_qoder_cn_provider(self, *, preferred_model: str = "") -> str | None:
        from pa_agent.ai.qoder_connector import apply_qoder_cn_provider_to_settings
        return apply_qoder_cn_provider_to_settings(self._settings, preferred_model=preferred_model or None)

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        if is_openclaw_model(model) or should_use_qclaw_provider(model, base_url):
            return None
        if is_openclaw_wb_model(model) or should_use_workbuddy_provider(model, base_url):
            return None
        if is_openclaw_twc_model(model) or should_use_trae_cn_provider(model, base_url):
            return None
        if is_openclaw_qc_model(model) or should_use_qoder_cn_provider(model, base_url):
            return None
        if model.startswith(("http://", "https://")) and not base_url.startswith(("http://", "https://")):
            return (
                "「模型」与「Base URL」似乎填反了：\n"
                "• 模型应填模型名，如 deepseek-v4-pro 或 claude-sonnet-4-6\n"
                "• 使用 QClaw 时模型填 openclaw（或 openclaw/main）\n"
                "• 使用 WorkBuddy 时模型填 openclaw_wb\n"
                "• 使用 TRAE Work CN 时模型填 openclaw_twc\n"
                "• 使用 Qoder CN 时模型填 openclaw_qc\n"
                "• Base URL 应填接口地址，如 https://api.deepseek.com"
            )
        if base_url.startswith(("http://", "https://")):
            return None
        if not base_url:
            if detect_qclaw():
                return (
                    "请填写 Base URL，或使用 QClaw/WorkBuddy/TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw → QClaw\n"
                    "• 模型填 openclaw_wb → WorkBuddy\n"
                    "• 模型填 openclaw_twc → TRAE Work CN\n"
                    "• 模型填 openclaw_qc → Qoder CN"
                )
            if detect_workbuddy():
                return (
                    "请填写 Base URL，或使用 WorkBuddy/TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw_wb → WorkBuddy\n"
                    "• 模型填 openclaw_twc → TRAE Work CN\n"
                    "• 模型填 openclaw_qc → Qoder CN"
                )
            if detect_trae_cn():
                return (
                    "请填写 Base URL，或使用 TRAE Work CN/Qoder CN：\n"
                    "• 模型填 openclaw_twc → TRAE Work CN（保存时自动配置）\n"
                    "• 模型填 openclaw_qc → Qoder CN（保存时自动配置）"
                )
            if detect_qoder_cn():
                return (
                    "请填写 Base URL，或使用 Qoder CN：\n"
                    "• 模型填 openclaw_qc → Qoder CN（保存时自动配置）"
                )
            return "请填写 Base URL（API 接口地址）。"
        return (
            f"Base URL 不是有效网址（当前：{base_url}）。\n"
            "DeepSeek 示例：https://api.deepseek.com\n"
            "火山方舟示例：https://ark.cn-beijing.volces.com/api/v3\n"
            "PackyAPI 示例：https://www.packyapi.com/v1\n"
            "QClaw：模型填 openclaw 后点保存（自动配置本地网关）\n"
            "WorkBuddy：模型填 openclaw_wb 后点保存（自动配置 WorkBuddy）\n"
            "TRAE Work CN：模型填 openclaw_twc 后点保存（自动配置 TRAE Work CN）\n"
            "Qoder CN：模型填 openclaw_qc 后点保存（自动配置 Qoder CN）"
        )

    def _open_agent_tutorial_url(self) -> None:
        QDesktopServices.openUrl(QUrl(_AGENT_TUTORIAL_URL))
