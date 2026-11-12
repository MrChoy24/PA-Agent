"""备用链路的短期健康记录：失败过的候选排到最后，别再每轮白撞一次。

为什么需要它
------------
``_try_next_provider_fallback`` 只在**单次分析内**记 ``tried_keys``。分析结束
后这份记忆就丢了，于是每 15/30 分钟一轮的新分析都会从第一条备用重新撞起：
余额不足的通道 1.9 秒就失败（m13007 之后不会再白等 165 秒），但一条已经明确
不可用的通道**被反复重试**仍是纯浪费，而且会让真正的可用通道排到后面。

这里的规则很简单：

* 某个 ``(base_url, model)`` 失败后进入冷却（默认 10 分钟），冷却期内它被
  **排到链尾**而不是删除——万一所有候选都在冷却，我们仍然得试。
* 它一旦成功，冷却立即清除。
* 纯内存，进程级。重启容器即清空，不写任何文件（不污染 settings.json）。

不记录 Key、不记录错误正文，只按 ``(base_url, model)`` 记账。
"""
from __future__ import annotations

import threading
import time
from typing import Any

#: 失败后的冷却时长（秒）。压得过短会反复撞死通道，过长会让恢复的通道迟迟不回。
DEFAULT_COOLDOWN_S = 600.0

_LOCK = threading.Lock()
#: (base_url, model) -> 冷却结束时间戳
_COOLING: dict[tuple[str, str], float] = {}


def entry_key(entry: Any) -> tuple[str, str]:
    """跟 ``extra_providers.entry_key`` 同样的去重键，但容忍任意对象。"""
    return (
        str(getattr(entry, "base_url", "") or "").strip(),
        str(getattr(entry, "model", "") or "").strip(),
    )


def mark_failed(entry: Any, *, cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
    """记录一次失败，让该候选在 ``cooldown_s`` 秒内排到链尾。"""
    key = entry_key(entry)
    if not (key[0] and key[1]):
        return
    with _LOCK:
        _COOLING[key] = time.time() + max(0.0, float(cooldown_s))


def mark_ok(entry: Any) -> None:
    """记录一次成功：立刻解除冷却。"""
    key = entry_key(entry)
    if not (key[0] and key[1]):
        return
    with _LOCK:
        _COOLING.pop(key, None)


def is_cooling(entry: Any, *, now: float | None = None) -> bool:
    """该候选是否仍在冷却期。"""
    key = entry_key(entry)
    if not (key[0] and key[1]):
        return False
    moment = time.time() if now is None else float(now)
    with _LOCK:
        until = _COOLING.get(key)
        if until is None:
            return False
        if until <= moment:
            _COOLING.pop(key, None)
            return False
        return True


def reorder(chain: list[Any], *, now: float | None = None) -> list[Any]:
    """冷却中的候选挪到链尾，其余保持原有顺序。"""
    fresh: list[Any] = []
    cooling: list[Any] = []
    for item in chain or []:
        (cooling if is_cooling(item, now=now) else fresh).append(item)
    return fresh + cooling


def reset() -> None:
    """清空全部记录（测试与「我改了 Key，重新试一次」用）。"""
    with _LOCK:
        _COOLING.clear()


def snapshot() -> dict[tuple[str, str], float]:
    """当前冷却表的副本（只给诊断/测试用）。"""
    with _LOCK:
        return dict(_COOLING)
