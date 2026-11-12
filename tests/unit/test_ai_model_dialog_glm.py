"""AI 模型设置窗口里的 GLM 备用：三件套已并入「备用 AI 提供商」面板第 0 行。

原先 provider 表单里那行孤立的「GLM 备用 Key:」已删除，模型 / Base URL / API Key
统一在 `BackupProvidersPanel` 的内置行里编辑；下面的断言覆盖同样的业务语义。
"""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings

SENSENOVA = "https://token.sensenova.cn/v1"
ARK = "https://ark.cn-beijing.volces.com/api/v3"


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _dialog(settings: Settings):
    from pa_agent.gui.ai_model_settings_dialog import AIModelSettingsDialog
    dlg = AIModelSettingsDialog(settings)
    dlg.show()
    return dlg


def _glm_row_name(panel) -> str:
    from pa_agent.gui.backup_providers_panel import _COL_NAME
    item = panel._table.item(0, _COL_NAME)
    return item.text() if item is not None else ""


def test_glm_key_row_removed_from_provider_form(qapp):
    """provider 表单里不再有孤立的「GLM 备用 Key:」控件。"""
    s = Settings()
    s.provider.base_url = SENSENOVA
    dlg = _dialog(s)
    assert not hasattr(dlg, "_glm_key_edit")
    assert not hasattr(dlg, "_glm_key_label")
    assert not hasattr(dlg, "_refresh_glm_key_row")
    dlg.close()


def test_glm_builtin_row_present_for_any_gateway(qapp):
    """内置行常驻第 0 行，与主通道类型无关；挂载与否由 eligible 决定。"""
    s = Settings()
    s.provider.base_url = SENSENOVA
    dlg = _dialog(s)
    assert _glm_row_name(dlg._backup_panel) == "GLM 备用（内置）"

    from pa_agent.ai.glm_backup import is_glm_backup_eligible
    dlg._base_url_edit.setText("https://api.deepseek.com")
    qapp.processEvents()
    assert _glm_row_name(dlg._backup_panel) == "GLM 备用（内置）"
    assert is_glm_backup_eligible("https://api.deepseek.com") is False
    assert is_glm_backup_eligible(SENSENOVA) is True
    assert is_glm_backup_eligible(ARK) is True
    dlg.close()


def test_glm_row_loaded_from_settings(qapp):
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.backup_glm_api_key = "glm-key-loaded"
    s.provider.backup_glm_model = "glm-4-plus"
    s.provider.backup_glm_base_url = "https://example.invalid/v4"
    dlg = _dialog(s)
    model, base_url, api_key, enabled = dlg._backup_panel.collect_glm_entry()
    assert (model, base_url, api_key, enabled) == (
        "glm-4-plus", "https://example.invalid/v4", "glm-key-loaded", True,
    )
    dlg.close()


def test_save_writes_key_and_builds_fallback(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.model = "deepseek-v4-flash"
    s.provider.api_key = "sk-primary"
    dlg = _dialog(s)
    dlg._backup_panel.load_glm_entry(
        model=GLM_MODEL, base_url=GLM_BASE_URL, api_key="glm-key-新", enabled=True,
    )
    dlg._base_url_edit.setText(SENSENOVA)
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == "glm-key-新"
    assert loaded.provider.backup_glm_model == GLM_MODEL
    assert loaded.provider.backup_glm_base_url == GLM_BASE_URL
    entries = [fb for fb in loaded.provider.fallbacks
               if (fb.model, fb.base_url) == (GLM_MODEL, GLM_BASE_URL)]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.base_url == GLM_BASE_URL
    assert entry.api_key == "glm-key-新"
    assert entry.thinking is False


def test_save_writes_custom_model_and_base_url(qapp, monkeypatch, tmp_path):
    """改模型 / Base URL 后链里仍只有一条 GLM 条目，且值已更新。"""
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_BASE_URL, GLM_MODEL, ensure_glm_fallback
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.api_key = "sk-primary"
    s.provider.backup_glm_api_key = "glm-key-old"
    ensure_glm_fallback(s.provider)  # 先用默认常量写进一条旧条目
    dlg = _dialog(s)
    dlg._backup_panel.load_glm_entry(
        model="glm-4-plus",
        base_url="https://open.bigmodel.cn/api/paas/v4/x",
        api_key="glm-key-new",
        enabled=True,
    )
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_model == "glm-4-plus"
    assert loaded.provider.backup_glm_base_url == "https://open.bigmodel.cn/api/paas/v4/x"
    glm_entries = [
        fb for fb in loaded.provider.fallbacks
        if str(fb.model).startswith("glm-")
    ]
    assert len(glm_entries) == 1, [fb.model for fb in loaded.provider.fallbacks]
    assert glm_entries[0].model == "glm-4-plus"
    assert glm_entries[0].base_url == "https://open.bigmodel.cn/api/paas/v4/x"
    assert glm_entries[0].api_key == "glm-key-new"
    # 旧常量身份的那条不得残留。
    assert all(
        (fb.model, fb.base_url) != (GLM_MODEL, GLM_BASE_URL)
        for fb in loaded.provider.fallbacks
    )


def test_clearing_key_drops_glm_fallback(qapp, monkeypatch, tmp_path):
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_MODEL, ensure_glm_fallback
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.api_key = "sk-primary"
    s.provider.backup_glm_api_key = "glm-key-abc"
    ensure_glm_fallback(s.provider)
    dlg = _dialog(s)
    # 只清 Key，模型 / Base URL 保持原值：Key 为空必须剔除。
    dlg._backup_panel.load_glm_entry(
        model=s.provider.backup_glm_model,
        base_url=s.provider.backup_glm_base_url,
        api_key="",
        enabled=True,
    )
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_api_key == ""
    assert all(fb.model != GLM_MODEL for fb in loaded.provider.fallbacks)


def test_unchecking_glm_row_drops_fallback_immediately(qapp, monkeypatch, tmp_path):
    """取消勾选 = 停用：保存当次就从链里剔除，不等下一次分析。"""
    import pa_agent.gui.ai_model_settings_dialog as mod
    from pa_agent.ai.glm_backup import GLM_MODEL, ensure_glm_fallback
    from pa_agent.config.settings import load_settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_JSON_PATH", path)
    s = Settings()
    s.provider.base_url = SENSENOVA
    s.provider.api_key = "sk-primary"
    s.provider.backup_glm_api_key = "glm-key-abc"
    ensure_glm_fallback(s.provider)
    assert any(fb.model == GLM_MODEL for fb in s.provider.fallbacks)
    dlg = _dialog(s)
    dlg._backup_panel.load_glm_entry(
        model=s.provider.backup_glm_model,
        base_url=s.provider.backup_glm_base_url,
        api_key="glm-key-abc",
        enabled=False,
    )
    dlg._on_save()
    loaded = load_settings(path)
    assert loaded.provider.backup_glm_enabled is False
    assert loaded.provider.backup_glm_api_key == "glm-key-abc"
    assert all(fb.model != GLM_MODEL for fb in loaded.provider.fallbacks)
