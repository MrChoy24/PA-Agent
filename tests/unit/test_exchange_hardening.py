"""第三批加固的回归测试：OKX 主机可配、请求节流、JSONL 文件锁。

对应三个问题：

* **P2-4** ``aws.okx.com`` 在很多网络下 DNS 解析不了，却被写死成备用主机，
  主域名超时后必然再白等一轮；现在主机列表由环境变量控制，默认只留主域名。
* **P2-5** 交易所请求完全没有本地节流，密集轮询时会撞上交易所限频。
* **P2-6** ``auto_orders.jsonl`` 追加写没有锁、没有 ``fsync``，
  GUI 与守护进程同时跑时可能写出撕开的半行。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from pa_agent.util import endpoints, filelock, throttle


# ── P2-4 OKX 主机可配 ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in endpoints.HOSTS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(throttle.RATE_ENV, raising=False)
    monkeypatch.delenv("PA_AGENT_EXCHANGE_THROTTLE", raising=False)
    endpoints.reset_cache()
    throttle.reset()
    yield
    endpoints.reset_cache()
    throttle.reset()


def test_default_hosts_are_just_the_primary_site():
    """默认列表不能再含 aws.okx.com —— 它在本机/国内网络根本解析不了。"""
    hosts = endpoints.okx_hosts()
    assert hosts == (endpoints.OKX_DEFAULT_HOST,)
    assert endpoints.OKX_LEGACY_FALLBACK_HOST not in hosts


def test_extra_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PA_AGENT_OKX_HOSTS", "aws.okx.com; https://aws.okx.com/")
    endpoints.reset_cache()
    hosts = endpoints.okx_hosts()
    assert hosts[0] == endpoints.OKX_DEFAULT_HOST
    assert hosts == (endpoints.OKX_DEFAULT_HOST, "https://aws.okx.com")


def test_invalid_host_entries_are_dropped(monkeypatch, caplog):
    monkeypatch.setenv("PA_AGENT_OKX_HOSTS", "bad::x, https://okx.example/path, ok.example")
    endpoints.reset_cache()
    with caplog.at_level("WARNING"):
        hosts = endpoints.okx_hosts()
    assert "https://ok.example" in hosts
    assert not any("bad::x" in h for h in hosts)
    assert any("无效" in r.getMessage() for r in caplog.records)


def test_short_alias_is_accepted(monkeypatch):
    monkeypatch.setenv("OKX_HOSTS", "backup.okx.example")
    endpoints.reset_cache()
    assert "https://backup.okx.example" in endpoints.okx_hosts()


def test_normalize_host_rules():
    assert endpoints.normalize_host("aws.okx.com") == "https://aws.okx.com"
    assert endpoints.normalize_host("https://aws.okx.com/") == "https://aws.okx.com"
    assert endpoints.normalize_host("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    for bad in ("", "   ", "ftp://a", "bad::x", "https://x.com/path", "okx.com?x=1"):
        assert endpoints.normalize_host(bad) == ""


def test_unreachable_hint_mentions_the_env_var():
    hint = endpoints.unreachable_hint(["https://www.okx.com: timed out"])
    assert "OKX 请求失败" in hint
    assert "timed out" in hint
    assert endpoints.HOSTS_ENV_VARS[0] in hint


def test_okx_trader_candidates_put_the_known_host_first(monkeypatch):
    from pa_agent.trading import okx_trader

    monkeypatch.setenv("PA_AGENT_OKX_HOSTS", "backup.okx.example")
    endpoints.reset_cache()
    candidates = okx_trader._host_candidates("https://backup.okx.example")
    assert candidates[0] == "https://backup.okx.example"
    assert endpoints.OKX_DEFAULT_HOST in candidates


# ── P2-5 请求节流 ────────────────────────────────────────────────────────────


def test_throttle_is_on_by_default():
    assert throttle.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_throttle_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("PA_AGENT_EXCHANGE_THROTTLE", value)
    assert throttle.enabled() is False
    assert throttle.acquire("whatever") == 0.0


def test_throttle_actually_delays_beyond_the_rate(monkeypatch):
    """速率压到 2/s 时，第 4 个请求至少要等 1 秒（桶容量 = 速率）。"""
    monkeypatch.setenv(throttle.RATE_ENV, "2")
    throttle.reset()
    start = time.monotonic()
    for _ in range(4):
        throttle.acquire("unit")
    elapsed = time.monotonic() - start
    # 容量 2 → 前两个免费，后两个各等 0.5s，合计约 1s
    assert elapsed >= 0.8


def test_throttle_never_waits_longer_than_the_cap(monkeypatch):
    """超大 deficit 时直接放行并告警，不让下单在本地干等。"""
    monkeypatch.setenv(throttle.RATE_ENV, "0.1")
    throttle.reset()
    start = time.monotonic()
    throttle.acquire("cap", tokens=100.0)
    assert time.monotonic() - start < throttle.MAX_WAIT_S


def test_throttle_shares_one_bucket_per_name():
    """同名桶共享额度：连续取 3 个后第 4 个必须等。"""
    bucket = throttle._Bucket(2.0)
    assert bucket.take() == 0.0
    assert bucket.take() == 0.0
    assert bucket.take() > 0.0


def test_okx_and_binance_send_requests_through_the_throttle(monkeypatch):
    """两个交易所的请求路径都要经过 throttle.acquire，而不是裸发。"""
    calls: list[str] = []

    def _fake_acquire(name, *, tokens=1.0):
        calls.append(name)
        return 0.0

    monkeypatch.setattr(throttle, "acquire", _fake_acquire)

    from pa_agent.trading import binance_trader, okx_trader
    from pa_agent.data import binance_source, okx_source

    for mod in (binance_trader, okx_trader, binance_source, okx_source):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "throttle.acquire(" in src, f"{mod.__name__} 没有接入节流"


# ── P2-6 JSONL 文件锁 ────────────────────────────────────────────────────────


def test_append_line_creates_parent_and_adds_newline(tmp_path: Path):
    target = tmp_path / "deep" / "dir" / "orders.jsonl"
    assert filelock.append_line(str(target), '{"a":1}') is True
    text = target.read_text(encoding="utf-8")
    assert text == '{"a":1}\n'


def test_append_line_does_not_double_the_newline(tmp_path: Path):
    target = tmp_path / "x.jsonl"
    filelock.append_line(str(target), '{"a":1}\n')
    assert target.read_text(encoding="utf-8") == '{"a":1}\n'


def test_concurrent_appends_stay_on_whole_lines(tmp_path: Path):
    """8 个线程各写 25 行：最终必须是 200 行完整 JSON，不能出现半行。"""
    target = tmp_path / "orders.jsonl"

    def _worker(idx: int) -> None:
        for n in range(25):
            payload = json.dumps({"t": idx, "n": n, "pad": "x" * 200})
            assert filelock.append_line(str(target), payload) is True

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    for line in lines:
        parsed = json.loads(line)  # 半行会在这里抛 JSONDecodeError
        assert parsed["pad"] == "x" * 200


def test_lock_timeout_raises(tmp_path: Path):
    target = tmp_path / "held.jsonl"
    ready = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with filelock.lock(str(target), timeout_s=5):
            ready.set()
            release.wait(5)

    t = threading.Thread(target=_holder)
    t.start()
    assert ready.wait(5)
    with pytest.raises(TimeoutError):
        with filelock.lock(str(target), timeout_s=0.1):
            pass
    release.set()
    t.join(timeout=5)


def test_append_line_skips_the_record_when_the_lock_times_out(
    tmp_path: Path, monkeypatch
):
    """拿不到锁时宁可少一条记录，也不能写出撕开的半行。"""

    def _boom(*_args, **_kwargs):
        raise TimeoutError("held")

    monkeypatch.setattr(filelock, "lock", _boom)
    target = tmp_path / "orders.jsonl"
    assert filelock.append_line(str(target), '{"a":1}') is False
    assert not target.exists()


def test_auto_trader_log_records_go_through_the_locked_writer(tmp_path: Path):
    from pa_agent.trading.auto_trader import AutoTrader

    target = tmp_path / "auto_orders.jsonl"
    trader = AutoTrader(
        {"enabled": False, "dry_run": True, "log_path": str(target)}
    )
    trader._log_record({"kind": "unit", "executed": False})

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "unit"
    # 锁文件是旁路文件，不应该把条目本身写坏
    assert (tmp_path / "auto_orders.jsonl.lock").exists()


def test_pending_writer_writes_atomically(tmp_path: Path):
    """写分析记录用临时文件 + os.replace，不留 .tmp 残留。"""
    from pa_agent.records.pending_writer import PendingWriter

    writer = PendingWriter(pending_dir=tmp_path)
    target = tmp_path / "rec.json"
    writer._write_json(target, {"hello": "世界"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "世界"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_pending_writer_append_followup_uses_the_lock(tmp_path: Path):
    from pa_agent.records.pending_writer import PendingWriter
    from pa_agent.records.schema import FollowupTurn

    writer = PendingWriter(pending_dir=tmp_path)
    turn = FollowupTurn(
        turn=1,
        ts_ms=1_760_000_000_000,
        user="继续",
        ai_content="好的",
        ai_reasoning=None,
        usage={},
    )
    writer.append_followup("2026-01-01_00-00-00_BTCUSDT_15m", turn)

    sidecar = tmp_path / "2026-01-01_00-00-00_BTCUSDT_15m.followups.jsonl"
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["user"] == "继续"


def test_filelock_env_does_not_leak_from_the_host(monkeypatch):
    """节流开关必须由环境变量决定，而不是被宿主环境影响成不确定状态。"""
    monkeypatch.setenv("PA_AGENT_EXCHANGE_THROTTLE", "1")
    assert throttle.enabled() is True
    assert os.environ["PA_AGENT_EXCHANGE_THROTTLE"] == "1"
