"""`pa_agent.ai.model_trace` 的行为测试。

背景：工具栏原先显示的是设置里配置的模型名，而方舟每轮会轮换、主通道失败后
还会沿备用链切换。model_trace 负责记录真正发出去的模型，这里锁定它的语义。
"""
from __future__ import annotations

import pytest

from pa_agent.ai import model_trace


@pytest.fixture(autouse=True)
def _clean_trace():
    model_trace.reset()
    yield
    model_trace.reset()


def test_empty_trace_reports_nothing():
    snap = model_trace.snapshot()
    assert snap["count"] == 0
    assert snap["latest"] is None
    assert snap["attempts"] == []
    assert model_trace.summary() == ""


def test_record_keeps_order_and_latest_is_last():
    model_trace.record("model-a", "https://a.example/v1")
    model_trace.record("model-b", "https://b.example/v1")

    snap = model_trace.snapshot()
    assert snap["models"] == ["model-a", "model-b"]
    assert snap["latest"]["model"] == "model-b"
    assert snap["count"] == 2


def test_blank_model_is_ignored():
    model_trace.record("", "https://a.example/v1")
    model_trace.record("   ", "https://a.example/v1")
    assert model_trace.snapshot()["count"] == 0


def test_mark_updates_latest_attempt_only():
    model_trace.record("model-a")
    model_trace.record("model-b")

    model_trace.mark(ok=False)
    attempts = model_trace.snapshot()["attempts"]
    assert attempts[0]["ok"] is None
    assert attempts[1]["ok"] is False


def test_mark_by_name_targets_that_attempt():
    model_trace.record("model-a")
    model_trace.record("model-b")

    model_trace.mark("model-a", ok=True)
    attempts = model_trace.snapshot()["attempts"]
    assert attempts[0]["ok"] is True
    assert attempts[1]["ok"] is None


def test_mark_on_empty_trace_is_a_noop():
    model_trace.mark(ok=True)  # 不能抛
    assert model_trace.snapshot()["count"] == 0


def test_trace_is_capped_and_drops_oldest():
    for i in range(model_trace._MAX_ATTEMPTS + 5):
        model_trace.record(f"m{i}")
    snap = model_trace.snapshot()
    assert snap["count"] == model_trace._MAX_ATTEMPTS
    assert snap["models"][0] == "m5"
    assert snap["latest"]["model"] == f"m{model_trace._MAX_ATTEMPTS + 4}"


def test_summary_mentions_retry_count():
    model_trace.record("first")
    assert model_trace.summary() == "first"

    model_trace.record("second")
    assert model_trace.summary() == "second（第 2 次尝试）"


def test_reset_clears_everything():
    model_trace.record("model-a", ok=True)
    model_trace.reset()
    assert model_trace.snapshot()["count"] == 0


def test_snapshot_is_a_copy():
    model_trace.record("model-a")
    snap = model_trace.snapshot()
    snap["attempts"][0]["model"] = "tampered"
    assert model_trace.snapshot()["attempts"][0]["model"] == "model-a"
