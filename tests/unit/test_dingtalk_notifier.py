"""Unit tests for DingTalk order notifications (pure unit, no network)."""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from pa_agent.config.settings import Settings
from pa_agent.notify import dingtalk_notifier as dt
from pa_agent.notify.dingtalk_notifier import (
    dingtalk_is_active,
    send_markdown,
    send_order_signal,
    send_test_message,
    send_text,
)

_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=test-token"
_SECRET = "SECtestsecret"


class _DingTalkSection:
    """Stand-in for ``Settings.dingtalk`` (field does not exist yet upstream)."""

    def __init__(self, **kwargs: Any) -> None:
        self.enabled = kwargs.get("enabled", True)
        self.webhook_url = kwargs.get("webhook_url", _WEBHOOK)
        self.secret = kwargs.get("secret", "")
        self.at_mobiles = kwargs.get("at_mobiles", [])
        self.at_all = kwargs.get("at_all", False)
        self.notify_on_order_only = kwargs.get("notify_on_order_only", True)
        self.msg_type = kwargs.get("msg_type", "markdown")


class _SettingsStub:
    def __init__(self, dingtalk: _DingTalkSection) -> None:
        self.dingtalk = dingtalk


def _settings(**kwargs: Any) -> _SettingsStub:
    return _SettingsStub(_DingTalkSection(**kwargs))


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch module-level httpx.post; record each call as {url, json}."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict | None = None, timeout: float | None = None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    return calls


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)


# ── (a) 未配置 webhook 时 is_active 为 False ─────────────────────────────────
def test_is_active_false_when_webhook_empty() -> None:
    assert dingtalk_is_active(_settings(webhook_url="")) is False
    assert dingtalk_is_active(_settings(webhook_url="   ")) is False


def test_is_active_true_with_enabled_and_webhook() -> None:
    assert dingtalk_is_active(_settings()) is True


def test_is_active_false_when_disabled() -> None:
    assert dingtalk_is_active(_settings(enabled=False)) is False


def test_is_active_safe_with_settings_without_dingtalk_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings.dingtalk 缺失时不得崩溃，按未配置处理。

    注意：``dingtalk_is_active(None)`` 会回退读取真实的 config/settings.json，
    结果取决于本机配置（用户可能已经配好钉钉），所以这里必须把回落路径
    隔离掉，否则测试会随用户配置漂移。
    """
    import pa_agent.notify.dingtalk_notifier as notifier

    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)
    monkeypatch.setattr(notifier, "_settings_root", lambda settings: None)

    assert dingtalk_is_active(Settings()) is False
    assert dingtalk_is_active(None) is False


def test_is_active_reads_webhook_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", _WEBHOOK)
    assert dingtalk_is_active(Settings()) is True


# ── (b) 加签：有 secret 追加 timestamp/sign，无 secret 原样 ───────────────────
def test_sign_appended_when_secret_set(captured: list[dict[str, Any]]) -> None:
    assert send_text("hello", settings=_settings(secret=_SECRET)) is True

    url = captured[0]["url"]
    query = parse_qs(urlparse(url).query)
    assert query["access_token"] == ["test-token"]
    assert "timestamp" in query and "sign" in query

    timestamp = query["timestamp"][0]
    assert timestamp.isdigit() and len(timestamp) == 13

    expected = base64.b64encode(
        hmac.new(
            _SECRET.encode(),
            (timestamp + "\n" + _SECRET).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert unquote(query["sign"][0]) == expected


def test_no_sign_when_secret_absent(captured: list[dict[str, Any]]) -> None:
    assert send_text("hello", settings=_settings(secret="")) is True

    url = captured[0]["url"]
    query = parse_qs(urlparse(url).query)
    assert url == _WEBHOOK
    assert "timestamp" not in query and "sign" not in query


def test_generated_sign_matches_dingtalk_algorithm() -> None:
    timestamp = "1700000000000"
    assert dt._gen_sign(_SECRET, timestamp) == base64.b64encode(
        hmac.new(
            _SECRET.encode(),
            f"{timestamp}\n{_SECRET}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()


def test_custom_security_url_uses_question_mark_when_no_query() -> None:
    bare = "https://oapi.dingtalk.com/robot/send"
    assert dt._signed_url(bare, "").endswith("/robot/send")
    signed = dt._signed_url(bare, _SECRET)
    assert signed.startswith(bare + "?timestamp=")
    assert "&sign=" in signed


# ── (c) errcode 非 0 → False ─────────────────────────────────────────────────
def test_nonzero_errcode_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict | None = None, timeout: float | None = None):
        return _FakeResponse({"errcode": 310000, "errmsg": "sign not match"})

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    assert send_text("hello", settings=_settings(secret=_SECRET)) is False
    assert send_markdown("标题", "正文", settings=_settings()) is False


def test_http_exception_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, json: dict | None = None, timeout: float | None = None):
        raise RuntimeError("network down")

    monkeypatch.setattr(dt.httpx, "post", boom)
    assert send_text("hello", settings=_settings()) is False
    assert send_order_signal({}, {}, "XAUUSDm", "15m", settings=_settings()) is False
    ok, msg = send_test_message(_settings())
    assert ok is False and "network down" in msg


def test_unparseable_reply_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Bad:
        text = "not json"

        def json(self) -> Any:
            raise ValueError("no json")

    monkeypatch.setattr(
        dt.httpx, "post", lambda url, json=None, timeout=None: _Bad()
    )
    assert send_text("hello", settings=_settings()) is False


# ── (d) 下单信号内容包含品种与 TP1/TP2 ───────────────────────────────────────
def test_send_order_signal_builds_markdown_payload(captured: list[dict[str, Any]]) -> None:
    decision = {
        "order_type": "限价单",
        "order_direction": "bullish",
        "entry_price": 2401.5,
        "stop_loss_price": 2395.0,
        "take_profit_price": 2415.0,
        "take_profit_price_2": 2425.5,
        "trade_confidence": 78,
        "win_rate": 63,
        "reasoning": "突破前高后回踩确认，" + "理由" * 400,
    }
    ok = send_order_signal(
        decision,
        {},
        "XAUUSDm",
        "15m",
        settings=_settings(secret=_SECRET),
    )

    assert ok is True
    payload = captured[0]["json"]
    assert payload["msgtype"] == "markdown"
    text = payload["markdown"]["text"]
    assert "XAUUSDm" in text
    assert "2401.5" in text
    assert "2415.0" in text  # TP1
    assert "2425.5" in text  # TP2
    assert "####" in text
    assert text.count("…") == 1  # reasoning 截断到 600 字
    assert len(text.split("决策理由")[1]) <= 900


def test_send_order_signal_respects_at_and_msg_type(captured: list[dict[str, Any]]) -> None:
    ok = send_order_signal(
        {"order_type": "市价单", "order_direction": "bearish"},
        {},
        "EURUSDm",
        "1h",
        settings=_settings(at_mobiles="13800000000,13900000000", at_all=True),
    )

    assert ok is True
    payload = captured[0]["json"]
    assert payload["at"] == {
        "atMobiles": ["13800000000", "13900000000"],
        "isAtAll": True,
    }


def test_send_order_signal_inactive_skips_http(captured: list[dict[str, Any]]) -> None:
    assert (
        send_order_signal(
            {"order_type": "限价单"},
            {},
            "XAUUSDm",
            "15m",
            settings=_settings(webhook_url=""),
        )
        is False
    )
    assert captured == []


def test_send_markdown_prefixes_title_and_supports_text_mode(
    captured: list[dict[str, Any]],
) -> None:
    assert send_markdown("测试标题", "第一行", settings=_settings()) is True
    markdown_text = captured[0]["json"]["markdown"]["text"]
    assert markdown_text.startswith("#### 测试标题")
    assert captured[0]["json"]["markdown"]["title"] == "测试标题"

    assert send_markdown("测试标题", "第一行", settings=_settings(msg_type="text")) is True
    text_payload = captured[1]["json"]
    assert text_payload["msgtype"] == "text"
    assert "测试标题" in text_payload["text"]["content"]


# ── (e) send_test_message 成功 / 失败分支 ────────────────────────────────────
def test_send_test_message_success(captured: list[dict[str, Any]]) -> None:
    ok, msg = send_test_message(_settings(secret=_SECRET))

    assert ok is True
    assert "ok" in msg
    assert captured[0]["json"]["msgtype"] == "markdown"


def test_send_test_message_reports_errcode_310000(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict | None = None, timeout: float | None = None):
        return _FakeResponse({"errcode": 310000, "errmsg": "sign not match"})

    monkeypatch.setattr(dt.httpx, "post", fake_post)
    ok, msg = send_test_message(_settings(secret=_SECRET))

    assert ok is False
    assert "310000" in msg
    assert "sign not match" in msg


def test_send_test_message_when_not_configured() -> None:
    ok, msg = send_test_message(_settings(webhook_url=""))
    assert ok is False
    assert "webhook_url" in msg
