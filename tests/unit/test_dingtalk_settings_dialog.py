"""钉钉机器人设置对话框的单元测试（纯 GUI，无网络）."""
from __future__ import annotations

import pytest

from pa_agent.config.settings import Settings


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def _dialog(settings: Settings):
    from pa_agent.gui.dingtalk_settings_dialog import DingTalkSettingsDialog

    dlg = DingTalkSettingsDialog(settings=settings)
    dlg.show()
    return dlg


def test_title_and_load(qapp):
    s = Settings()
    s.dingtalk.enabled = True
    s.dingtalk.webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    s.dingtalk.secret = "SECxyz"
    s.dingtalk.at_all = True
    s.dingtalk.at_mobiles = ["13800000000", "13900000000"]
    dlg = _dialog(s)
    assert dlg.windowTitle() == "钉钉机器人设置"
    assert dlg._enabled_check.isChecked() is True
    assert "access_token=abc" in dlg._webhook_edit.text()
    assert dlg._secret_edit.text() == "SECxyz"
    assert dlg._at_all_check.isChecked() is True
    assert dlg._at_mobiles_edit.text() == "13800000000, 13900000000"
    dlg.close()


def test_secret_masked_by_default_and_toggle(qapp):
    from PyQt6.QtWidgets import QLineEdit

    dlg = _dialog(Settings())
    assert dlg._secret_edit.echoMode() == QLineEdit.EchoMode.Password
    dlg._show_secret_btn.setChecked(True)
    assert dlg._secret_edit.echoMode() == QLineEdit.EchoMode.Normal
    assert dlg._show_secret_btn.text() == "隐藏"
    dlg._show_secret_btn.setChecked(False)
    assert dlg._secret_edit.echoMode() == QLineEdit.EchoMode.Password
    dlg.close()


def test_mobiles_parsing_handles_chinese_comma_and_spaces(qapp):
    dlg = _dialog(Settings())
    mobiles = dlg._split_mobiles("13800000000，13900000000; 13700000000 ")
    assert mobiles == ["13800000000", "13900000000", "13700000000"]
    dlg.close()


def test_apply_values_to_settings(qapp):
    s = Settings()
    dlg = _dialog(s)
    dlg._enabled_check.setChecked(True)
    dlg._webhook_edit.setText("  https://oapi.dingtalk.com/robot/send?access_token=xyz  ")
    dlg._secret_edit.setText("  SECabcd  ")
    dlg._at_all_check.setChecked(True)
    dlg._at_mobiles_edit.setText("13800000000")
    dlg._order_only_check.setChecked(False)
    dlg._msg_type_combo.setCurrentIndex(dlg._msg_type_combo.findData("text"))
    dlg._apply_values_to_settings()
    assert s.dingtalk.enabled is True
    assert s.dingtalk.webhook_url == "https://oapi.dingtalk.com/robot/send?access_token=xyz"
    assert s.dingtalk.secret == "SECabcd"
    assert s.dingtalk.at_all is True
    assert s.dingtalk.at_mobiles == ["13800000000"]
    assert s.dingtalk.notify_on_order_only is False
    assert s.dingtalk.msg_type == "text"
    dlg.close()


def test_save_without_webhook_warns(qapp, monkeypatch):
    """启用但没有 Webhook 时必须拦截，不能静默保存."""
    import pa_agent.gui.dingtalk_settings_dialog as mod

    warnings: list[tuple] = []
    monkeypatch.setattr(
        mod.QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    s = Settings()
    dlg = _dialog(s)
    dlg._enabled_check.setChecked(True)
    dlg._webhook_edit.setText("")
    dlg._on_save()
    assert warnings, "应当弹出警告"
    assert s.dingtalk.enabled is True  # 内存里已写入，但未落盘
    dlg.close()


def test_test_button_warns_without_webhook(qapp, monkeypatch):
    import pa_agent.gui.dingtalk_settings_dialog as mod

    warnings: list[tuple] = []
    monkeypatch.setattr(
        mod.QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    dlg = _dialog(Settings())
    dlg._webhook_edit.setText("")
    dlg._on_test()
    assert warnings
    assert dlg._status_label.text() == ""
    dlg.close()


def test_test_button_exists(qapp):
    dlg = _dialog(Settings())
    assert dlg._test_btn.text() == "发送测试消息"
    dlg.close()
