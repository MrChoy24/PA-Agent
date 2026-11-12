"""免费池 stage1 候选轮换（上游 37f12ead / 1.91 的修复）。

修复前：``_accept_any`` 在 stage1 上无条件调用 ``_accept_fallback_stage1()``，
后者「永不拒绝」——契约不满足就用本地规则拼一个 Ok 返回。
后果是**第一个候选永远"成功"**，后面的候选根本没机会被尝试，
用户拿到的是本地规则兜底，而不是免费的模型推理。

修复后：stage1 契约不满足时原样返回 ``ValidationError``，
``run_free_stage_fallback`` 的循环照常换下一个候选。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pa_agent.ai.deepseek_client import AIReply, AIUsage
from pa_agent.ai.json_validator import Ok, ValidationError
from pa_agent.ai.sensenova_fallback import (
    FreeAttemptRecord,
    FreeCandidate,
    FreeStageFailure,
    FreeStageSuccess,
    run_free_stage_fallback,
)
from pa_agent.config.settings import AIProviderSettings, Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken

_CAND_A = FreeCandidate(
    "kilo", "cohere/north-mini-code:free", "https://api.kilo.ai/api/gateway"
)
_CAND_B = FreeCandidate("kilo", "openrouter/free", "https://api.kilo.ai/api/gateway")
_GOOD_OBJ = {
    "cycle_position": "trading_range",
    "direction": "neutral",
    "diagnosis_confidence": 60,
    "market_phase": "stable",
    "detected_patterns": [],
    "key_signals": [],
    "htf_context": "test",
    "entry_setup": "test",
    "strategy_files_needed": [],
    "bar_by_bar_summary": [],
    "gate_trace": [],
    "gate_result": "proceed",
}


def _miss(tag: str = "contract") -> ValidationError:
    return ValidationError(category="a", stage="stage1", raw_text="bad", message=tag)


def _reply(content: str) -> AIReply:
    return AIReply(
        content=content,
        reasoning_content="",
        raw={},
        usage=AIUsage(),
        request_id="r",
        latency_ms=1.0,
    )


def _orch() -> TwoStageOrchestrator:
    settings = Settings()
    settings.provider = AIProviderSettings(
        model="deepseek-flash",
        base_url="https://token.sensenova.cn/v1",
        api_key="sk-primary",
        thinking=True,
        reasoning_effort="low",
    )
    return TwoStageOrchestrator(
        client=MagicMock(),
        assembler=MagicMock(),
        router=MagicMock(),
        validator=MagicMock(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )


def _capture_validate(monkeypatch: pytest.MonkeyPatch, validate_reply=None) -> dict:
    """截获 _try_sensenova_free_stage 传给 run_free_stage_fallback 的 validate。"""
    captured: dict = {}
    if validate_reply is None:
        validate_reply = lambda _r: _miss()  # noqa: E731
    import pa_agent.ai.sensenova_fallback as sf

    def _fake_run(**kwargs):  # noqa: ANN003
        captured["validate"] = kwargs["validate"]
        return FreeStageFailure(attempts=[], message="stub")

    monkeypatch.setattr(sf, "run_free_stage_fallback", _fake_run)
    orch = _orch()
    assert (
        orch._try_sensenova_free_stage(
            stage="stage1",
            messages=[{"role": "user", "content": "hi"}],
            validate_reply=validate_reply,
            on_reasoning_token=None,
            on_content_token=None,
            cancel_token=CancelToken(),
        )
        is None
    )
    return captured


def test_stage1_contract_miss_returns_validation_error(monkeypatch) -> None:
    """契约不满足时 validate 必须回 ValidationError，不能兜成本地规则。"""
    captured = _capture_validate(monkeypatch)
    out = captured["validate"](_reply('{"bad": 1}'))
    assert isinstance(out, ValidationError), (
        "stage1 契约不满足必须返回 ValidationError，否则第一个候选会被误判成功"
    )
    assert not isinstance(out, Ok)


def test_stage1_contract_hit_passes_through(monkeypatch) -> None:
    """契约满足时原样返回 Ok，不影响正常候选。"""
    good = Ok(obj=dict(_GOOD_OBJ))
    captured = _capture_validate(monkeypatch, validate_reply=lambda _r: good)
    out = captured["validate"](_reply('{"ok": 1}'))
    assert out is good


def test_stage2_still_uses_quality_gate(monkeypatch) -> None:
    """stage2 行为不变：仍走 _accept_fallback_stage2_quality。"""
    captured: dict = {}
    import pa_agent.ai.sensenova_fallback as sf

    def _fake_run(**kwargs):  # noqa: ANN003
        captured["validate"] = kwargs["validate"]
        return FreeStageFailure(attempts=[], message="stub")

    monkeypatch.setattr(sf, "run_free_stage_fallback", _fake_run)
    orch = _orch()
    marker = _miss("quality")
    marks: list[object] = []

    def _fake_quality(result, content):  # noqa: ANN001,ANN202
        marks.append(result)
        return result

    monkeypatch.setattr(orch, "_accept_fallback_stage2_quality", _fake_quality)
    orch._try_sensenova_free_stage(
        stage="stage2",
        messages=[{"role": "user", "content": "hi"}],
        validate_reply=lambda _r: marker,
        on_reasoning_token=None,
        on_content_token=None,
        cancel_token=CancelToken(),
    )
    captured["validate"](_reply('{"x": 1}'))
    assert marks == [marker], "stage2 必须仍然经过 quality gate"


def test_rotation_reaches_the_second_candidate() -> None:
    """端到端（假 http）：第一个候选契约不满足后，必须真的试到第二个候选。"""
    tried: list[str] = []
    good_json = (
        '{"cycle_position":"trading_range","direction":"neutral",'
        '"diagnosis_confidence":60,"market_phase":"stable","detected_patterns":[],'
        '"key_signals":[],"htf_context":"t","entry_setup":"t",'
        '"strategy_files_needed":[],"bar_by_bar_summary":[],"gate_trace":[],'
        '"gate_result":"proceed"}'
    )

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        model = body["model"]
        tried.append(model)
        if model == _CAND_A.model:
            return 200, {"choices": [{"message": {"content": '{"bad": 1}'}}]}
        return 200, {"choices": [{"message": {"content": good_json}}]}

    def _validate(reply):  # noqa: ANN001
        content = getattr(reply, "content", "") or ""
        if '"bad"' in content:
            return _miss()
        return Ok(obj=dict(_GOOD_OBJ))

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=_validate,
        primary_api_key="sk-PRIMARY",
        candidates=[_CAND_A, _CAND_B],
        http_post=_post,
        attempts_per_candidate=1,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert outcome.candidate.model == _CAND_B.model
    assert tried == [_CAND_A.model, _CAND_B.model]


def test_old_behaviour_would_have_stopped_at_first(monkeypatch) -> None:
    """反向对照：若 validate 永远返回 Ok（修复前的语义），只会试第一个候选。"""
    tried: list[str] = []

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        tried.append(body["model"])
        return 200, {"choices": [{"message": {"content": '{"bad": 1}'}}]}

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"local": True}),  # 旧 _accept_fallback_stage1 的语义
        primary_api_key="sk-PRIMARY",
        candidates=[_CAND_A, _CAND_B],
        http_post=_post,
        attempts_per_candidate=1,
    )

    assert isinstance(outcome, FreeStageSuccess)
    assert tried == [_CAND_A.model], "永远返回 Ok 时不会轮换——这正是修复前的缺陷"
