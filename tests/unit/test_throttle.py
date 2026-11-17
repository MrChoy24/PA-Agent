"""汇率令牌桶回归（2026-11-17 自检 O2）。

旧实现把 ``time.sleep`` 放在 ``with self._lock:`` 里：一个线程补觉时，
同一个交易所的所有请求（下单、对账、查持仓）全部排队。这里守住
「睡眠不进锁」和「等待期间额度被预扣」两条。
"""
from __future__ import annotations

import pytest

from pa_agent.util import throttle


def test_sleep_happens_outside_the_bucket_lock(monkeypatch) -> None:
    bucket = throttle._Bucket(rate=1.0)
    bucket.tokens = 0.0  # 一个令牌都没有 → 必须等满 1 秒
    observed: list[bool] = []

    def fake_sleep(_seconds: float) -> None:
        # 复现旧实现时这里会是 True（sleep 仍在 with 块里）
        observed.append(bucket._lock.locked())
        assert bucket._lock.acquire(timeout=0.5) is True
        bucket._lock.release()

    monkeypatch.setattr(throttle.time, "sleep", fake_sleep)
    waited = bucket.take(1.0)

    assert waited == pytest.approx(1.0, abs=0.05)  # 补令牌会吃掉几毫秒
    assert observed == [False]


def test_waiting_reserves_the_token_so_it_is_not_handed_out_twice(monkeypatch) -> None:
    bucket = throttle._Bucket(rate=1.0)
    bucket.tokens = 0.0
    monkeypatch.setattr(throttle.time, "sleep", lambda _s: None)
    before = throttle.time.monotonic()
    bucket.take(1.0)

    # 睡眠期间 tokens 必须是 0，updated 被推到等待结束之后
    assert bucket.tokens == 0.0
    assert bucket.updated >= before + 0.9


def test_absurd_wait_is_released_without_sleeping(monkeypatch) -> None:
    bucket = throttle._Bucket(rate=0.1)  # 取 1 个令牌要等 10 秒
    bucket.tokens = 0.0
    slept: list[float] = []
    monkeypatch.setattr(throttle.time, "sleep", lambda s: slept.append(s))

    assert bucket.take(1.0) == 0.0
    assert slept == []


def test_acquire_is_a_noop_when_throttling_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv(throttle._ENABLED_ENV, "0")
    throttle.reset()
    assert throttle.acquire("okx") == 0.0