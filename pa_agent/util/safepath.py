"""容错的路径探测工具。

为什么需要：``pathlib.Path.exists()`` 只吞掉 ``ENOENT`` / ``ENOTDIR`` /
``EBADF`` / ``ELOOP`` / ``ENAMETOOLONG``，**不吞 ``EACCES``（PermissionError）**。
所以在容器里以非 root 用户运行时，探测 ``/root/.qoder-cn/...`` 这类路径会
直接抛异常 —— 而这些探测都写在模块顶层，一抛就让整个 ``import`` 失败，
表现为「程序启动失败」，极难定位。

本模块把这些探测统一成「拿不准就当不存在」，与 ``exists()`` 的语义一致，
只是额外容忍权限不足。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["safe_exists", "safe_is_dir", "safe_is_file", "safe_iterdir"]


def safe_exists(path: Path | str | None) -> bool:
    """``Path.exists()``，但权限不足时返回 False 而不是抛异常。"""
    if path is None:
        return False
    try:
        return Path(path).exists()
    except OSError:
        return False


def safe_is_dir(path: Path | str | None) -> bool:
    if path is None:
        return False
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def safe_is_file(path: Path | str | None) -> bool:
    if path is None:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def safe_iterdir(path: Path | str | None) -> list[Path]:
    """``Path.iterdir()`` 的列表版，读不了就返回空列表。"""
    if path is None:
        return []
    try:
        return list(Path(path).iterdir())
    except OSError:
        return []


def safe_read_text(path: Path | str | None, *, encoding: str = "utf-8") -> str | None:
    """读文本文件，任何 OSError 都返回 None。"""
    if path is None:
        return None
    try:
        return Path(path).read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError):
        return None


def home_dir() -> Path:
    """当前进程的家目录。

    ``Path.home()`` 读 ``HOME`` 环境变量；在容器里用 ``setpriv`` 降权时
    ``HOME`` 往往还停在 ``/root``，于是所有 ``~`` 探测都会指向无权限的目录。
    这里优先用 ``pwd`` 里记录的真实家目录。
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path.home()
