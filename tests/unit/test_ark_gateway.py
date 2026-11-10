"""火山方舟（Ark）网关：别名映射、模型轮换、额度降级。

这些用例锁定 upstram `pa_agent/ai/ark_gateway.py` 的行为，防止后续改动破坏
「每轮换模型」与「额度用完自动降级」两条硬要求。
"""
from __future__ import annotations

import json

import pytest

from pa_agent.ai import ark_gateway as ag
from pa_agent.config.settings import AIProviderSettings


# ── 别名与网关识别 ────────────────────────────────────────────────────────────

def test_base_url_constant() -> None:
    assert ag.ARK_BASE_URL == "https://ark.cn-beijing.volces.com/api/v3"


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("deepseek-flash", "deepseek-v4-1-flash-economy-261017"),
        ("deepseek", "deepseek-v4-1-flash-261017"),
        ("deepseek-chat", "deepseek-v4-1-flash-261017"),
        ("glm-flash", "glm-5-3-flash-260828"),
        ("doubao-flash", "doubao-seed-2-1-lite-260915"),
    ],
)
def test_resolve_ark_model_aliases(alias: str, expected: str) -> None:
    assert ag.resolve_ark_model(alias) == expected


def test_resolve_ark_model_is_case_insensitive() -> None:
    assert ag.resolve_ark_model("DEEPSEEK-FLASH") == ag.resolve_ark_model("deepseek-flash")


def test_resolve_ark_model_passthrough_for_real_ids() -> None:
    real = "kimi-k3-1-pro-261024"
    assert ag.resolve_ark_model(real) == real


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (ag.ARK_BASE_URL, True),
        ("https://ark.cn-beijing.volces.com/api/v3/", True),
        ("https://some.volcengine.example/v1", True),
        ("", False),
        (None, False),
        ("https://api.deepseek.com", False),
        ("https://token.sensenova.cn/v1", False),
    ],
)
def test_is_ark_base(url: str | None, expected: bool) -> None:
    assert ag.is_ark_base(url) is expected


# ── 账号级错误识别 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "AccountOverdueError: the account is overdue",
        "overdue balance, please recharge",
        "your account has been suspended",
        "arrears detected",
        # 凭据类：Key 失效时换模型同样没用，必须一起当账号级处理。
        "Error code: 401 - The API key doesn't exist.",
        "AuthenticationError: invalid api key",
        "令牌已过期或验证不正确",
    ],
)
def test_is_account_level_error_matches(text: str) -> None:
    assert ag.is_account_level_error(text) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("rate limit exceeded", False),
        ("model not found", False),
        ("has not activated the model", False),
        ("timeout", False),
    ],
)
def test_is_account_level_error_rejects(value: str | None, expected: bool) -> None:
    assert ag.is_account_level_error(value) is expected


def test_is_account_level_error_accepts_exception() -> None:
    assert ag.is_account_level_error(RuntimeError("AccountOverdue: 账户欠费")) is True


# ── 轮换 ──────────────────────────────────────────────────────────────────────

def test_rotation_pool_is_unique_and_nonempty() -> None:
    assert len(ag.ARK_ROTATION_POOL) >= 5
    assert len(set(ag.ARK_ROTATION_POOL)) == len(ag.ARK_ROTATION_POOL)


def test_next_rotation_model_cycles_and_persists(tmp_path) -> None:
    state = tmp_path / "ark_rotation_state.json"
    pool = ag.ARK_ROTATION_POOL
    seen = [ag.next_rotation_model(state) for _ in range(len(pool) + 2)]

    # 前 len(pool) 次恰好覆盖整个池，且顺序与常量一致
    assert seen[: len(pool)] == list(pool)
    # 越界后回绕
    assert seen[len(pool)] == pool[0]
    assert seen[len(pool) + 1] == pool[1]

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["cursor"] == (len(pool) + 2) % len(pool)
    assert saved["last_model"] == seen[-1]


def test_next_rotation_model_recovers_from_bad_state(tmp_path) -> None:
    state = tmp_path / "ark_rotation_state.json"
    state.write_text("{not json", encoding="utf-8")
    assert ag.next_rotation_model(state) == ag.ARK_ROTATION_POOL[0]


def test_next_rotation_model_recovers_from_negative_and_huge_cursor(tmp_path) -> None:
    state = tmp_path / "ark_rotation_state.json"
    for raw in ("-3", "999999"):
        state.write_text(json.dumps({"cursor": int(raw)}), encoding="utf-8")
        model = ag.next_rotation_model(state)
        assert model in ag.ARK_ROTATION_POOL


def test_rotation_state_survives_unwritable_path(tmp_path) -> None:
    """写状态失败时仍返回模型，不抛异常。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")  # 文件当目录用 -> mkdir 失败
    target = blocker / "ark_rotation_state.json"
    assert ag.next_rotation_model(target) == ag.ARK_ROTATION_POOL[0]


# ── 降级链 ────────────────────────────────────────────────────────────────────

def test_ark_fallback_models_excludes_primary() -> None:
    primary = ag.ARK_ROTATION_POOL[0]
    models = ag.ark_fallback_models(primary)
    assert primary not in models
    assert models[0] == ag.ARK_MODEL_POOL[0]


def test_ensure_ark_fallbacks_attaches_pool() -> None:
    provider = AIProviderSettings(
        model="deepseek-flash", base_url=ag.ARK_BASE_URL, api_key="k"
    )
    added = ag.ensure_ark_fallbacks(provider)

    assert added == len(ag.ark_fallback_models(provider.model))
    assert len(provider.fallbacks) == added
    assert all(fb.base_url == ag.ARK_BASE_URL for fb in provider.fallbacks)
    assert all(fb.reasoning_effort == ag.ARK_THINKING_EFFORT for fb in provider.fallbacks)
    # 别名不参与去重：备用里不该出现主模型的别名
    assert all(fb.model != provider.model for fb in provider.fallbacks)


def test_ensure_ark_fallbacks_is_idempotent() -> None:
    provider = AIProviderSettings(
        model="deepseek-flash", base_url=ag.ARK_BASE_URL, api_key="k"
    )
    first = ag.ensure_ark_fallbacks(provider)
    size_after_first = len(provider.fallbacks)
    second = ag.ensure_ark_fallbacks(provider)

    assert first > 0
    assert second == 0
    assert len(provider.fallbacks) == size_after_first


def test_ensure_ark_fallbacks_skips_non_ark_gateway() -> None:
    provider = AIProviderSettings(
        model="deepseek-flash", base_url="https://api.deepseek.com", api_key="k"
    )
    assert ag.ensure_ark_fallbacks(provider) == 0
    assert provider.fallbacks == []


def test_ensure_ark_fallbacks_skips_without_api_key() -> None:
    provider = AIProviderSettings(model="deepseek-flash", base_url=ag.ARK_BASE_URL, api_key="")
    assert ag.ensure_ark_fallbacks(provider) == 0


def test_ensure_ark_fallbacks_handles_none_provider() -> None:
    assert ag.ensure_ark_fallbacks(None) == 0
