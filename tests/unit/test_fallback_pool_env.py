"""备用池的环境变量通道 + 失效候选的冷却重排。

覆盖三条用户可见的约定：

1. ``.env`` 里的 ``PA_AGENT_FALLBACKS`` 能直接定义备用池（不用改 settings.json）。
2. 主通道不是方舟时，settings.json 里残留的方舟模型会在加载时被清掉。
3. 失败过的候选会被排到链尾，成功后立刻回到原位。
"""
from __future__ import annotations

import json

import pytest

from pa_agent.ai import fallback_health
from pa_agent.ai.extra_providers import (
    FALLBACK_SPEC_ENV,
    combined_fallback_chain,
    env_fallbacks,
    parse_fallback_spec,
)
from pa_agent.config.settings import AIProviderSettings, Settings, load_settings


@pytest.fixture(autouse=True)
def _clean():
    fallback_health.reset()
    yield
    fallback_health.reset()


# ── parse_fallback_spec ────────────────────────────────────────────────────


def test_spec_parses_four_fields():
    entries, problems = parse_fallback_spec(
        "GLM|GLM-5.3-Flash|https://api.scnet.cn/api/llm/v1|sk-abc"
    )
    assert problems == []
    assert len(entries) == 1
    assert entries[0].name == "GLM"
    assert entries[0].model == "GLM-5.3-Flash"
    assert entries[0].base_url == "https://api.scnet.cn/api/llm/v1"
    assert entries[0].api_key == "sk-abc"


def test_spec_reuses_primary_key_for_dash():
    entries, problems = parse_fallback_spec(
        "|m|https://x.example/v1|-", default_key="sk-primary"
    )
    assert problems == []
    assert entries[0].api_key == "sk-primary"
    # 名字留空时回落到模型名，界面上不至于一片空白。
    assert entries[0].name == "m"


def test_spec_allows_three_fields_via_default_key():
    entries, problems = parse_fallback_spec("|m|https://x.example/v1", default_key="k")
    assert problems == []
    assert entries[0].api_key == "k"


def test_spec_reports_missing_key_without_leaking_anything():
    entries, problems = parse_fallback_spec("|m|https://x.example/v1|")
    assert entries == []
    assert len(problems) == 1
    assert "api_key" in problems[0]


def test_spec_reports_short_entries_and_skips_them():
    entries, problems = parse_fallback_spec(
        "only|two;|ok|https://x.example/v1|-", default_key="k"
    )
    assert [e.model for e in entries] == ["ok"]
    assert len(problems) == 1
    assert "第 1 条" in problems[0]


def test_spec_handles_empty_input():
    assert parse_fallback_spec("") == ([], [])
    assert parse_fallback_spec(None) == ([], [])


def test_spec_keeps_order_and_tolerates_blank_segments():
    entries, problems = parse_fallback_spec(
        "a|m1|https://one|k ;; b|m2|https://two|k;", default_key=""
    )
    assert problems == []
    assert [e.model for e in entries] == ["m1", "m2"]
    assert entries[0].name == "a"


# ── env_fallbacks / daemon 覆盖 ────────────────────────────────────────────


def test_env_fallbacks_empty_without_variable(monkeypatch):
    monkeypatch.delenv(FALLBACK_SPEC_ENV, raising=False)
    assert env_fallbacks() == []


def test_env_fallbacks_reads_variable(monkeypatch):
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "n|m|https://x.example/v1|k")
    entries = env_fallbacks()
    assert [e.model for e in entries] == ["m"]


def test_env_fallbacks_does_not_raise_on_garbage(monkeypatch):
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "完全不是备用池的写法")
    assert env_fallbacks() == []


def test_daemon_env_overrides_replace_the_pool(monkeypatch):
    from pa_agent.daemon.config import apply_fallback_env_overrides

    settings = Settings()
    settings.provider.api_key = "sk-primary"
    settings.ai_backups = [
        AIProviderSettings(name="file", model="file-m", base_url="https://file", api_key="k")
    ]
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "env|env-m|https://env.example/v1|-")
    applied = apply_fallback_env_overrides(settings)
    assert applied == [FALLBACK_SPEC_ENV]
    assert [b.name for b in settings.ai_backups] == ["env"]
    assert settings.ai_backups[0].api_key == "sk-primary"


def test_daemon_env_overrides_leave_file_alone_without_variable(monkeypatch):
    from pa_agent.daemon.config import apply_fallback_env_overrides

    monkeypatch.delenv(FALLBACK_SPEC_ENV, raising=False)
    settings = Settings()
    settings.ai_backups = [
        AIProviderSettings(name="file", model="m", base_url="https://file", api_key="k")
    ]
    assert apply_fallback_env_overrides(settings) == []
    assert [b.name for b in settings.ai_backups] == ["file"]


def test_daemon_env_overrides_keep_file_pool_when_unparsable(monkeypatch):
    from pa_agent.daemon.config import apply_fallback_env_overrides

    settings = Settings()
    settings.ai_backups = [
        AIProviderSettings(name="file", model="m", base_url="https://file", api_key="k")
    ]
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "nonsense")
    assert apply_fallback_env_overrides(settings) == []
    assert [b.name for b in settings.ai_backups] == ["file"]


# ── combined_fallback_chain 里 env 的位置与去重 ────────────────────────────


def _provider(**kw) -> AIProviderSettings:
    data = {
        "model": "glm-5",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "sk-primary",
    }
    data.update(kw)
    return AIProviderSettings(**data)


def test_env_entries_replace_the_file_pool(monkeypatch):
    settings = Settings()
    settings.ai_backups = [
        AIProviderSettings(name="file", model="fm", base_url="https://file", api_key="k")
    ]
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "env|em|https://env.example/v1|-")
    chain = combined_fallback_chain(_provider(), settings)
    # .env 里写了清单就以它为准，不再混进 settings.json 的旧条目。
    assert [c.name for c in chain] == ["env"]


def test_env_entries_replace_the_derived_chain_too(monkeypatch):
    provider = _provider(
        fallbacks=[
            AIProviderSettings(
                name="derived", model="GLM-5.3-Flash", base_url="https://api.scnet.cn/api/llm/v1", api_key="k"
            )
        ]
    )
    monkeypatch.setenv(
        FALLBACK_SPEC_ENV, "env|GLM-5.3-Flash|https://api.scnet.cn/api/llm/v1|-"
    )
    # 整段替换：派生链里的同名条目也一并被替换掉，不会重复出现。
    assert [c.name for c in combined_fallback_chain(provider, Settings())] == ["env"]


def test_env_duplicates_are_collapsed(monkeypatch):
    monkeypatch.setenv(
        FALLBACK_SPEC_ENV,
        "a|m|https://one|-;b|m|https://one|-",
    )
    assert len(combined_fallback_chain(_provider(), Settings())) == 1


# ── 加载时清掉失效方舟池 ──────────────────────────────────────────────────


def _write(tmp_path, provider: dict) -> "object":
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"provider": provider}, ensure_ascii=False), encoding="utf-8")
    return path


def test_stale_ark_fallbacks_dropped_when_primary_is_not_ark(tmp_path):
    ark_entry = {
        "model": "doubao-seed-2-1-lite-260915",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "ark-key",
    }
    mine = {"model": "GLM-5.3-Flash", "base_url": "https://api.scnet.cn/api/llm/v1", "api_key": "k"}
    path = _write(
        tmp_path,
        {
            "model": "glm-5",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "sk-primary",
            "fallbacks": [ark_entry, mine],
        },
    )
    settings = load_settings(path)
    assert [f.model for f in settings.provider.fallbacks] == ["GLM-5.3-Flash"]


def test_ark_fallbacks_kept_when_primary_is_ark(tmp_path):
    ark_entry = {
        "model": "doubao-seed-2-1-lite-260915",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "ark-key",
    }
    path = _write(
        tmp_path,
        {
            "model": "deepseek-flash",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": "ark-key",
            "fallbacks": [ark_entry],
        },
    )
    settings = load_settings(path)
    assert len(settings.provider.fallbacks) == 1


def test_no_fallbacks_leaves_file_untouched(tmp_path):
    path = _write(
        tmp_path,
        {
            "model": "glm-5",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "sk-primary",
            "fallbacks": [],
        },
    )
    before = path.read_text(encoding="utf-8")
    load_settings(path)
    assert path.read_text(encoding="utf-8") == before


# ── 失效候选的冷却重排 ────────────────────────────────────────────────────


def test_mark_failed_moves_entry_to_the_back():
    chain = [_provider(model=f"m{i}") for i in range(3)]
    fallback_health.mark_failed(chain[0])
    assert [c.model for c in fallback_health.reorder(list(chain))] == ["m1", "m2", "m0"]


def test_mark_ok_clears_the_cooldown():
    entry = _provider(model="m")
    fallback_health.mark_failed(entry)
    assert fallback_health.is_cooling(entry)
    fallback_health.mark_ok(entry)
    assert not fallback_health.is_cooling(entry)


def test_cooldown_expires():
    entry = _provider(model="m")
    fallback_health.mark_failed(entry, cooldown_s=0.0)
    assert not fallback_health.is_cooling(entry)


def test_all_cooling_still_returns_the_whole_chain():
    # 全部都在冷却时也必须把候选交出去，否则会变成「没有备用可试」。
    chain = [_provider(model=f"m{i}") for i in range(2)]
    for item in chain:
        fallback_health.mark_failed(item)
    assert len(fallback_health.reorder(list(chain))) == 2


def test_entries_without_identity_are_ignored():
    fallback_health.mark_failed(_provider(model="", base_url=""))
    assert fallback_health.snapshot() == {}


def test_reorder_keeps_relative_order_of_healthy_entries():
    chain = [_provider(model=f"m{i}") for i in range(4)]
    fallback_health.mark_failed(chain[2])
    fallback_health.mark_failed(chain[0])
    # 健康项保持原序在前（m1, m3），冷却项也按链内原序排在后面（m0, m2）。
    assert [c.model for c in fallback_health.reorder(list(chain))] == ["m1", "m3", "m0", "m2"]


# ── 变量名容错：daemon 其它键都是 PA_DAEMON_*，只有这一项是 PA_AGENT_* ─────────


def test_daemon_prefixed_alias_is_accepted(monkeypatch):
    """写 ``PA_DAEMON_FALLBACKS`` 也该生效（其它 daemon 键都是这个前缀）。"""
    monkeypatch.delenv(FALLBACK_SPEC_ENV, raising=False)
    monkeypatch.setenv(
        "PA_DAEMON_FALLBACKS", "甲|m1|https://a.example/v1|k1;乙|m2|https://b.example/v1|k2"
    )
    entries = env_fallbacks()
    assert [e.name for e in entries] == ["甲", "乙"]


def test_canonical_name_wins_over_the_alias(monkeypatch, caplog):
    """两个都写时以主名为准，并提示另一个被忽略。"""
    monkeypatch.setenv(FALLBACK_SPEC_ENV, "主|m|https://a.example/v1|k")
    monkeypatch.setenv("PA_DAEMON_FALLBACKS", "备|m|https://b.example/v1|k")
    entries = env_fallbacks()
    assert [e.name for e in entries] == ["主"]
    assert any("同时设置" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "bad_name",
    ["PA_AGENT_FALLBACK", "PA_DAEMON_FALLBACK", "PA_AGENT_FALLBACK_POOL", "FALLBACKS"],
)
def test_misspelled_variable_is_called_out(monkeypatch, caplog, bad_name):
    """拼错名字既不报错也不生效，是最难自查的一类问题 —— 至少要点名。"""
    monkeypatch.delenv(FALLBACK_SPEC_ENV, raising=False)
    monkeypatch.delenv("PA_DAEMON_FALLBACKS", raising=False)
    monkeypatch.setenv(bad_name, "甲|m|https://a.example/v1|k")
    assert env_fallbacks() == []
    assert any(bad_name in r.getMessage() and "不是有效" in r.getMessage()
               for r in caplog.records)


def test_describe_shows_where_the_pool_came_from(monkeypatch):
    """启动行要能一眼看出这一轮读的是 env 还是 settings。"""
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings
    from pa_agent.daemon.config import DaemonConfig

    monkeypatch.delenv(FALLBACK_SPEC_ENV, raising=False)
    monkeypatch.delenv("PA_DAEMON_FALLBACKS", raising=False)
    cfg = DaemonConfig(settings=load_settings(SETTINGS_JSON_PATH), settings_path=SETTINGS_JSON_PATH)
    assert "(settings)" in cfg.describe()

    monkeypatch.setenv(FALLBACK_SPEC_ENV, "甲|m|https://a.example/v1|k;乙|m|https://b.example/v1|k")
    cfg2 = DaemonConfig(settings=load_settings(SETTINGS_JSON_PATH), settings_path=SETTINGS_JSON_PATH)
    assert "fallbacks=2(env)" in cfg2.describe()
