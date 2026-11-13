"""轻量令牌桶：给交易所 REST 请求加一层本地节流。

交易所的限频是按「时间窗口内的次数/权重」算的（OKX 下单 60 次/2 秒、
账户 10 次/2 秒；Binance 现货 6000 权重/分钟、U 本位合约 2400 权重/分钟）。
超限会被直接拒绝（OKX 返回 50011 / Binance 返回 429 + 封 IP），
而现有的请求路径没有任何本地节流 —— 一次批量对账或密集轮询就可能撞上。

这里的实现刻意做得很小：按**名字**维护一个进程级令牌桶，
桶容量 = 每秒速率（等价于允许 1 秒的突发），取不到令牌就短暂等待。
真正的目标不是精确复现交易所的权重模型，而是把「一秒内几十个请求」
这种明显的尖峰抹平，给上层留出退避与重试的余地。

环境变量：

* ``PA_AGENT_EXCHANGE_THROTTLE=0`` —— 关闭节流（测试与离线场景）
* ``PA_AGENT_EXCHANGE_RATE_PER_S=20`` —— 覆盖默认速率（每秒请求数）

失败时**绝不抛异常**：节流是保护措施，不能因为它自身的问题让交易失败。
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

#: 默认每秒请求数。OKX 账户类接口限 10 次/2 秒，这里给到 20/s 已经偏宽松，
#: 但配合「容量 = 速率」的突发额度，足以把连发几十条请求的尖峰摊平。
DEFAULT_RATE_PER_S = 20.0

RATE_ENV = "PA_AGENT_EXCHANGE_RATE_PER_S"
_ENABLED_ENV = "PA_AGENT_EXCHANGE_THROTTLE"

#: 单次等待上限：超过这个时间就放行并告警，宁可冒着被限频的风险，
#: 也不要让一次下单在本地干等一分钟。
MAX_WAIT_S = 5.0


class _Bucket:
    """一个可重入的令牌桶（线程安全）。"""

    __slots__ = ("rate", "tokens", "updated", "_lock")

    def __init__(self, rate: float) -> None:
        self.rate = max(0.1, float(rate))
        self.tokens = self.rate
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0) -> float:
        """取走 *tokens* 个令牌，返回实际等待的秒数。"""
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
            self.updated = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0

            deficit = tokens - self.tokens
            wait = deficit / self.rate
            if wait > MAX_WAIT_S:
                logger.warning(
                    "交易所请求节流等待 %.1fs 超过上限，直接放行（rate=%.1f/s）",
                    wait,
                    self.rate,
                )
                wait = 0.0
                self.tokens = 0.0
                return 0.0

            time.sleep(wait)
            self.updated = time.monotonic()
            self.tokens = 0.0
            return wait


_BUCKETS: dict[str, _Bucket] = {}
_BUCKETS_LOCK = threading.Lock()


def _bucket_rate() -> float:
    raw = os.environ.get(RATE_ENV, "").strip()
    if not raw:
        return DEFAULT_RATE_PER_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RATE_PER_S
    return max(0.1, value)


def enabled() -> bool:
    """节流是否开启（默认开启）。"""
    raw = os.environ.get(_ENABLED_ENV, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def acquire(name: str, *, tokens: float = 1.0) -> float:
    """在 *name* 这个桶里取令牌，必要时阻塞；返回等待秒数。

    ``name`` 用 ``"okx"`` / ``"binance"`` 这类稳定标识即可 —— 同一个交易所的
    所有请求共用一个桶，才能真正限制住总速率。
    """
    if not enabled():
        return 0.0
    key = str(name or "default")
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            bucket = _Bucket(_bucket_rate())
            _BUCKETS[key] = bucket
    try:
        return bucket.take(tokens)
    except Exception as exc:  # noqa: BLE001 — 节流故障不能影响交易
        logger.debug("请求节流异常（已忽略）: %s", exc)
        return 0.0


def reset() -> None:
    """清空所有桶（测试用）。"""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


__all__ = [
    "DEFAULT_RATE_PER_S",
    "RATE_ENV",
    "MAX_WAIT_S",
    "acquire",
    "enabled",
    "reset",
]
