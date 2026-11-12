"""记录「本轮分析到底用了哪个模型」。

方舟网关每轮会从 13 个模型里轮换一个，主通道失败时还会沿 fallback 链切换，
所以工具栏上显示 ``settings.provider.model`` 是配置值而不是实际值。这里用一个
进程级的轻量记录器，把「真正发出去的模型」按发生顺序记下来，界面线程只读它。

设计约束：
* 不加锁地读（``snapshot()`` 复制一份再返回），写入用锁，避免 GUI 线程被阻塞；
* 不记录任何密钥，只留模型名与网关地址；
* 不参与业务逻辑，任何异常都不应影响分析流程。
"""
from __future__ import annotations

import threading
import time
from typing import Any

#: 只保留最近若干次尝试，够还原一轮分析的降级过程又不占内存。
_MAX_ATTEMPTS = 24

_LOCK = threading.Lock()
_ATTEMPTS: list[dict[str, Any]] = []


def reset() -> None:
    """清空记录（每轮分析开始时调用）。"""
    with _LOCK:
        _ATTEMPTS.clear()


def record(
    model: str,
    base_url: str = "",
    *,
    ok: bool | None = None,
    source: str = "primary",
) -> None:
    """记录一次模型尝试。``ok`` 为 None 表示请求刚发出、结果未知。"""
    name = str(model or "").strip()
    if not name:
        return
    entry = {
        "model": name,
        "base_url": str(base_url or "").strip(),
        "ok": ok,
        "source": str(source or "primary"),
        "ts": time.time(),
    }
    with _LOCK:
        _ATTEMPTS.append(entry)
        if len(_ATTEMPTS) > _MAX_ATTEMPTS:
            del _ATTEMPTS[: len(_ATTEMPTS) - _MAX_ATTEMPTS]


def mark(model: str | None = None, *, ok: bool) -> None:
    """把最近一条（或最近一条同名）尝试标记成功/失败。"""
    name = str(model or "").strip()
    with _LOCK:
        for entry in reversed(_ATTEMPTS):
            if name and entry.get("model") != name:
                continue
            entry["ok"] = bool(ok)
            return


def snapshot() -> dict[str, Any]:
    """返回当前记录的一份拷贝，供界面线程安全读取。"""
    with _LOCK:
        attempts = [dict(a) for a in _ATTEMPTS]
    latest = attempts[-1] if attempts else None
    return {
        "attempts": attempts,
        "latest": latest,
        "count": len(attempts),
        "models": [a["model"] for a in attempts],
    }


def summary() -> str:
    """一句话描述：``glm-4-flash``／``glm-4-flash（第 2 次尝试）``。"""
    snap = snapshot()
    latest = snap.get("latest")
    if not latest:
        return ""
    name = latest.get("model") or ""
    count = int(snap.get("count") or 0)
    if count > 1:
        return f"{name}（第 {count} 次尝试）"
    return name
