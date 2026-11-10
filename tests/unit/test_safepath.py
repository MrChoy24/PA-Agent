"""容器/无头环境下的路径探测容错（回归测试）。

背景：``Path.exists()`` 不吞 ``EACCES``。以非 root 用户运行时，探测
``/root/.qoder-cn/...`` 会抛 ``PermissionError``，而这些探测写在
``qoder_connector`` / ``trae_connector`` / ``workbuddy_connector`` /
``qclaw_connector`` 的**模块顶层**，一抛就让 import 失败，表现成
「程序启动失败：Permission denied: /root/.qoder-cn/...」。

真实事故日志：
    启动失败：[Errno 13] Permission denied:
    '/root/.qoder-cn/SharedClientCache/cache/machine_token.json'
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

from pa_agent.util.safepath import safe_exists, safe_is_dir, safe_is_file, safe_iterdir


# ── safepath 自身 ────────────────────────────────────────────────────────────


def test_safe_exists_returns_false_on_permission_error(monkeypatch):
    def boom(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(pathlib.Path, "exists", boom)
    assert safe_exists("/root/.qoder-cn/x.json") is False


def test_safe_exists_true_for_real_path(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}", encoding="utf-8")
    assert safe_exists(f) is True


def test_safe_exists_false_for_missing(tmp_path):
    assert safe_exists(tmp_path / "nope") is False


def test_safe_exists_none_is_false():
    assert safe_exists(None) is False


def test_safe_is_file_and_dir_tolerate_permission_error(monkeypatch):
    def boom(self):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(pathlib.Path, "is_file", boom)
    monkeypatch.setattr(pathlib.Path, "is_dir", boom)
    assert safe_is_file("/root/a") is False
    assert safe_is_dir("/root/a") is False


def test_safe_iterdir_returns_empty_on_permission_error(monkeypatch):
    def boom(self):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    assert safe_iterdir("/root/logs") == []


# ── connector 模块在「/root 不可读」时仍能 import ────────────────────────────

_CONNECTORS = [
    "pa_agent.ai.qoder_connector",
    "pa_agent.ai.trae_connector",
    "pa_agent.ai.workbuddy_connector",
    "pa_agent.ai.qclaw_connector",
    "pa_agent.ai.qclaw_relay_manager",
    "pa_agent.ai.client_factory",
]


@pytest.mark.parametrize("module_name", _CONNECTORS)
def test_connector_imports_when_root_unreadable(monkeypatch, module_name):
    """模拟容器：任何以 /root 开头的路径探测都抛 PermissionError。

    这正是 1Panel 里 uid=10001 跑、HOME 仍为 /root 时的真实情况。
    """
    real_exists = pathlib.Path.exists
    real_is_file = pathlib.Path.is_file
    real_is_dir = pathlib.Path.is_dir
    real_iterdir = pathlib.Path.iterdir

    def guard(fn):
        def wrapper(self, *args, **kwargs):
            text = str(self).replace("\\", "/")
            if text.startswith("/root") or ":/root" in text:
                raise PermissionError(13, "Permission denied", str(self))
            return fn(self, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(pathlib.Path, "exists", guard(real_exists))
    monkeypatch.setattr(pathlib.Path, "is_file", guard(real_is_file))
    monkeypatch.setattr(pathlib.Path, "is_dir", guard(real_is_dir))
    monkeypatch.setattr(pathlib.Path, "iterdir", guard(real_iterdir))

    # 强制重新执行模块顶层代码（探测发生在 import 时）。
    import sys

    sys.modules.pop(module_name, None)
    try:
        importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)

    # 探测函数本身也不该抛
    mod = importlib.import_module(module_name)
    for attr in ("detect_qoder_cn", "detect_trae_cn", "detect_workbuddy", "detect_qclaw"):
        fn = getattr(mod, attr, None)
        if callable(fn):
            assert fn() in (True, False)
