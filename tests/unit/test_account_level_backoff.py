"""账号级故障不能走限流退避。

真实事故（2026-11-12 容器日志）：智谱 bigmodel 余额不足时返回 **HTTP 429**，
响应体才是真相 ``{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}``。
旧逻辑只认 429 → 按 ``call_with_rate_limit_backoff`` 退避 5/10/20/40/80 秒
（≈155 秒/阶段），再被备用链接管；docker stdout 上又全被 ``ConsoleFormatter``
抹成「正在进行分析，请稍等。」，于是表现为「分析永不结束」。

这里锁定两件事：
1. 账号级错误一次都不重试，立刻上抛让备用链接管。
2. 控制台必须给出一条可操作的中文提示，而不是中性进度文案。
"""
from __future__ import annotations

import io
import logging

import pytest

from pa_agent.ai.rate_limit import call_with_rate_limit_backoff
from pa_agent.ai.ark_gateway import is_account_level_error
from pa_agent.util.logging import (
    ConsoleFormatter,
    DropRateLimitExhaustedFilter,
    MaskingFormatter,
    _LOG_FORMAT,
)

_GLM_NO_BALANCE = "Error code: 429 - 余额不足或无可用资源包,请充值。"
_PLAIN_429 = "Error code: 429 - rate limit reached, please retry later"


# ── 退避：账号级错误零重试 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        _GLM_NO_BALANCE,
        '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}',
        "Error code: 403 - The request failed because your account has an overdue balance.",
        "Error code: 401 - The API key doesn't exist.",
        "insufficient balance",
    ],
)
def test_account_level_errors_are_not_retried(monkeypatch, message):
    calls: list[int] = []

    def _boom():
        calls.append(1)
        raise RuntimeError(message)

    # 退避时长不该被真正等待：如果实现走错分支，这里会立刻暴露（sleep 被打桩）。
    sleeps: list[float] = []

    def _no_sleep(seconds, cancel_token=None):
        sleeps.append(seconds)
        return False

    monkeypatch.setattr("pa_agent.ai.rate_limit._sleep_with_cancel", _no_sleep)

    with pytest.raises(RuntimeError):
        call_with_rate_limit_backoff(_boom, stage_label="DeepSeek")

    assert len(calls) == 1, "账号级错误只能请求一次"
    assert sleeps == [], "账号级错误不允许任何退避等待"


def test_plain_429_still_retries_with_backoff(monkeypatch):
    """普通限流必须保持原有重试行为——不能把 429 一律当账号级。"""
    calls: list[int] = []
    sleeps: list[float] = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(_PLAIN_429)
        return "ok"

    def _no_sleep(seconds, cancel_token=None):
        sleeps.append(seconds)
        return False

    monkeypatch.setattr("pa_agent.ai.rate_limit._sleep_with_cancel", _no_sleep)

    assert call_with_rate_limit_backoff(_flaky, stage_label="DeepSeek") == "ok"
    assert len(calls) == 3
    assert sleeps == [5.0, 10.0], "指数退避序列应保持 5/10/20/40/80"


def test_account_level_detection_does_not_swallow_ordinary_errors():
    assert is_account_level_error("model not found") is False
    assert is_account_level_error(_PLAIN_429) is False
    assert is_account_level_error("Connection failed: upstream timeout") is False


# ── 控制台：必须出现可操作提示，且不泄漏渠道细节 ──────────────────────────────

def _console_handler(stream):
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    handler.addFilter(DropRateLimitExhaustedFilter())
    return handler


def test_console_tells_the_user_to_top_up():
    stream = io.StringIO()
    record = logging.LogRecord(
        "pa_agent.ai.client_factory",
        logging.ERROR,
        __file__,
        1,
        "%s",
        (f"DeepSeek 账户额度或凭据不可用，本轮不再重试，直接切换备用链路: {_GLM_NO_BALANCE}",),
        None,
    )
    _console_handler(stream).handle(record)
    out = stream.getvalue()
    assert "余额不足" in out or "充值" in out, f"用户拿不到可操作提示: {out!r}"
    assert "凭据" in out or "API Key" in out
    # 只检查正文：时间戳的毫秒位偶尔正好是 429/glm 之类的子串，误报过一次。
    body = out.split("WARNING", 1)[-1] if "WARNING" in out else out
    # 不能泄漏原始报错细节与渠道/密钥
    for leaked in ("Error code", "429", "bigmodel", "glm-5", "sk-"):
        assert leaked not in body, f"控制台泄漏了 {leaked!r}: {out!r}"


def test_console_keeps_the_account_line_when_retry_filters_are_active():
    """DropRateLimitExhaustedFilter 只吞「放弃本轮」，这条不能被连带吞掉。"""
    record = logging.LogRecord(
        "pa_agent.ai.client_factory",
        logging.ERROR,
        __file__,
        1,
        "%s",
        ("DeepSeek 账户额度或凭据不可用，本轮不再重试，直接切换备用链路: " + _GLM_NO_BALANCE,),
        None,
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    handler.addFilter(DropRateLimitExhaustedFilter())
    handler.handle(record)
    assert stream.getvalue().strip(), "账号级提示被限流过滤器误吞"


def test_file_keeps_the_raw_account_error():
    record = logging.LogRecord(
        "pa_agent.ai.client_factory",
        logging.ERROR,
        __file__,
        1,
        "%s",
        (_GLM_NO_BALANCE,),
        None,
    )
    out = MaskingFormatter(_LOG_FORMAT).format(record)
    assert "1113" in out or "余额不足" in out


# ── 控制台：同一次故障只提示一次 ──────────────────────────────────────────────

def test_burst_of_account_errors_prints_one_line_only():
    """同一次欠费会从两个 logger 各打一条（限流层 + stream error），控制台只能出现一行。

    真实日志（2026-11-12 容器 pa-agent-20261112215916.log 第 17-18 行）
    就是同一句中文连着出现两遍。
    """
    from pa_agent.util.logging import (
        DropDuplicateAccountNoticeFilter,
        reset_account_notice_state,
    )

    reset_account_notice_state()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    handler.addFilter(DropDuplicateAccountNoticeFilter())

    for logger_name in ("pa_agent.ai.rate_limit", "pa_agent.ai.deepseek_client"):
        handler.handle(
            logging.LogRecord(
                logger_name,
                logging.ERROR,
                __file__,
                1,
                "%s",
                (_GLM_NO_BALANCE,),
                None,
            )
        )

    out = stream.getvalue()
    assert out.count("余额不足") == 1, f"重复提示: {out!r}"


def test_dedup_window_expires_so_a_later_fault_is_visible():
    """冷却窗过去后，下一次真正的欠费必须重新提示，不能被永久吞掉。"""
    from pa_agent.util.logging import reset_account_notice_state
    import pa_agent.util.logging as logging_mod

    reset_account_notice_state()
    assert logging_mod._account_notice_allowed(now=100.0) is True
    assert logging_mod._account_notice_allowed(now=101.0) is False
    assert (
        logging_mod._account_notice_allowed(now=100.0 + logging_mod._ACCOUNT_NOTICE_COOLDOWN_S + 1)
        is True
    )
    reset_account_notice_state()


def test_dedup_does_not_swallow_ordinary_messages():
    """普通日志不受去重影响：连续的进度行照常逐条出现。"""
    from pa_agent.util.logging import (
        DropDuplicateAccountNoticeFilter,
        reset_account_notice_state,
    )

    reset_account_notice_state()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    handler.addFilter(DropDuplicateAccountNoticeFilter())

    for _ in range(3):
        handler.handle(
            logging.LogRecord(
                "pa_agent.analysis",
                logging.INFO,
                __file__,
                1,
                "%s",
                ("正在进行分析，请稍等。",),
                None,
            )
        )

    assert stream.getvalue().count("请稍等") == 3
