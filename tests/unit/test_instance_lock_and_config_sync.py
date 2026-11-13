"""单实例锁 / 配置只读模式 / 保存前并发校验 / 热重载 的回归测试。

覆盖 P0-1（GUI 与 daemon 同时运行）的四个组成：

* ``pa_agent/util/instance_lock.py`` —— 本机单实例互斥；
* ``TwoStageOrchestrator(config_writable=False)`` —— 守护进程降级时不回写；
* ``save_settings(..., base_digest=...)`` —— 保存前的乐观锁；
* ``DaemonRunner._reload_if_settings_changed()`` —— 改完文件不必重启。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pa_agent.config import settings as settings_mod
from pa_agent.config.settings import (
    ConfigReloadError,
    Settings,
    file_digest,
    loaded_digest,
    load_settings,
    reload_settings,
    save_settings,
)
from pa_agent.util import instance_lock


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    """一个独立的 settings.json 路径，避免碰到真实的 config/。"""
    return tmp_path / "config" / "settings.json"


@pytest.fixture(autouse=True)
def _reset_digests() -> None:
    """每个用例前后都清空模块级的路径指纹缓存。"""
    settings_mod._LOADED_DIGESTS.clear()
    yield
    settings_mod._LOADED_DIGESTS.clear()


# ── save_settings / load_settings 的指纹 ─────────────────────────────────────


def test_save_records_digest_so_the_next_save_is_allowed(cfg_path: Path) -> None:
    settings = Settings()
    assert save_settings(settings, cfg_path) is True
    assert loaded_digest(cfg_path) == file_digest(cfg_path)

    # 用「刚写出来的那一版」做基准，应该允许再写一次。
    assert (
        save_settings(settings, cfg_path, base_digest=loaded_digest(cfg_path)) is True
    )


def test_save_is_refused_when_the_file_changed_underneath(cfg_path: Path) -> None:
    settings = Settings()
    save_settings(settings, cfg_path)
    stale = loaded_digest(cfg_path)

    # 模拟另一个进程（服务器上的 daemon / 另一个 GUI 实例）改了盘上的文件。
    other = Settings()
    other.auto_trade.leverage = 33
    settings_mod._LOADED_DIGESTS.clear()
    save_settings(other, cfg_path)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["auto_trade"]["leverage"] == 33

    # 拿着旧指纹去写必须被拒绝，且磁盘内容一点没动。
    mine = Settings()
    mine.auto_trade.leverage = 7
    assert save_settings(mine, cfg_path, base_digest=stale) is False
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["auto_trade"]["leverage"] == 33


def test_save_without_base_digest_is_unconditionally_allowed(cfg_path: Path) -> None:
    """默认参数保持旧行为：迁移回写、首次初始化等场景不受并发校验影响。"""
    save_settings(Settings(), cfg_path)
    other = Settings()
    other.auto_trade.leverage = 21
    settings_mod._LOADED_DIGESTS.clear()
    save_settings(other, cfg_path)

    mine = Settings()
    mine.auto_trade.leverage = 9
    assert save_settings(mine, cfg_path) is True
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["auto_trade"]["leverage"] == 9


def test_force_overrides_the_digest_check(cfg_path: Path) -> None:
    save_settings(Settings(), cfg_path)
    stale = loaded_digest(cfg_path)
    path_text = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(path_text.replace('"leverage": 10', '"leverage": 30'), encoding="utf-8")

    mine = Settings()
    mine.auto_trade.leverage = 4
    assert save_settings(mine, cfg_path, base_digest=stale, force=True) is True
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["auto_trade"]["leverage"] == 4


def test_load_records_digest_and_missing_file_gets_one_too(cfg_path: Path) -> None:
    assert loaded_digest(cfg_path) == ""
    loaded = load_settings(cfg_path)  # 文件不存在 → 写默认值
    assert isinstance(loaded, Settings)
    assert cfg_path.exists()
    assert loaded_digest(cfg_path) == file_digest(cfg_path)


def test_reload_settings_raises_on_broken_json_instead_of_using_defaults(
    cfg_path: Path,
) -> None:
    """热重载绝不能像 load_settings 那样静默回落到默认值。"""
    save_settings(Settings(), cfg_path)
    cfg_path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ConfigReloadError):
        reload_settings(cfg_path)


def test_reload_settings_returns_fresh_values(cfg_path: Path) -> None:
    settings = Settings()
    settings.auto_trade.leverage = 12
    save_settings(settings, cfg_path)

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["auto_trade"]["leverage"] = 44
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    reloaded = reload_settings(cfg_path)
    assert reloaded.auto_trade.leverage == 44


# ── instance_lock ────────────────────────────────────────────────────────────


def test_lock_file_is_written_and_removed(tmp_path: Path) -> None:
    lock = tmp_path / "config" / ".runtime.lock"
    with instance_lock.acquire("gui", path=lock):
        holder = instance_lock.read_holder(lock)
        assert holder is not None
        assert holder.role == "gui"
        assert holder.pid == os.getpid()
        assert holder.host
        assert holder.started_at > 0
    assert not lock.exists()


def test_second_instance_is_rejected_while_the_first_holds_the_lock(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".runtime.lock"
    with instance_lock.acquire("gui", path=lock):
        # 同一进程里的「第二个实例」也要被挡住（模拟用户双击两次图标）。
        assert instance_lock.check(lock) is not None
        with pytest.raises(RuntimeError, match="已有 PA Agent 正在运行"):
            with instance_lock.acquire("daemon", path=lock):
                pass


def test_stale_lock_from_a_dead_pid_is_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / ".runtime.lock"
    # 999999 这种 pid 基本不可能存在，且 Windows 的 OpenProcess 会直接失败。
    lock.write_text(
        json.dumps(
            {"role": "daemon", "pid": 999999, "host": instance_lock._hostname(), "started_at": 1.0}
        ),
        encoding="utf-8",
    )
    assert instance_lock.check(lock) is None
    with instance_lock.acquire("gui", path=lock):
        assert instance_lock.read_holder(lock).role == "gui"


def test_stale_lock_is_not_taken_over_when_asked_not_to(tmp_path: Path) -> None:
    lock = tmp_path / ".runtime.lock"
    lock.write_text(
        json.dumps(
            {"role": "daemon", "pid": 999999, "host": instance_lock._hostname(), "started_at": 1.0}
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="疑似陈旧"):
        with instance_lock.acquire("gui", path=lock, take_over_stale=False):
            pass


def test_lock_from_another_host_is_never_stolen(tmp_path: Path) -> None:
    """配置目录被共享挂载时，别的机器的锁必须被当成活的。"""
    lock = tmp_path / ".runtime.lock"
    lock.write_text(
        json.dumps(
            {"role": "daemon", "pid": 999999, "host": "some-other-server", "started_at": 1.0}
        ),
        encoding="utf-8",
    )
    assert instance_lock.check(lock) is not None
    with pytest.raises(RuntimeError):
        with instance_lock.acquire("gui", path=lock):
            pass


def test_broken_lock_file_is_treated_as_absent(tmp_path: Path) -> None:
    lock = tmp_path / ".runtime.lock"
    lock.write_text("not json at all", encoding="utf-8")
    assert instance_lock.read_holder(lock) is None
    assert instance_lock.check(lock) is None
    with instance_lock.acquire("gui", path=lock):
        assert instance_lock.read_holder(lock).role == "gui"


def test_release_does_not_delete_a_lock_taken_over_by_someone_else(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".runtime.lock"
    with instance_lock.acquire("gui", path=lock):
        holder = instance_lock.read_holder(lock)
        # 模拟锁被别人接管（比如本进程崩溃后被新实例接管）。
        lock.write_text(
            json.dumps(
                {"role": "daemon", "pid": os.getpid() + 1, "host": holder.host, "started_at": 2.0}
            ),
            encoding="utf-8",
        )
        instance_lock.release(lock, holder)
        assert lock.exists()


# ── 守护进程配置只读模式 ─────────────────────────────────────────────────────


def _orchestrator(config_writable: bool):
    """构造一个不做网络请求的最小 TwoStageOrchestrator。"""
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

    return TwoStageOrchestrator(
        client=object(),  # type: ignore[arg-type]
        assembler=object(),  # type: ignore[arg-type]
        router=lambda *a, **k: [],
        validator=object(),  # type: ignore[arg-type]
        pending_writer=object(),  # type: ignore[arg-type]
        exp_reader=object(),  # type: ignore[arg-type]
        settings=Settings(),
        config_writable=config_writable,
    )


def test_readonly_orchestrator_never_calls_save_settings() -> None:
    calls: list[object] = []
    orch = _orchestrator(config_writable=False)
    assert (
        orch._persist_provider_switch("QClaw", lambda: calls.append("saved")) is False
    )
    assert calls == []


def test_writable_orchestrator_does_persist() -> None:
    calls: list[object] = []
    orch = _orchestrator(config_writable=True)
    assert orch._persist_provider_switch("QClaw", lambda: calls.append("saved")) is True
    assert calls == ["saved"]


def test_persist_swallows_save_errors() -> None:
    orch = _orchestrator(config_writable=True)

    def boom() -> None:
        raise OSError("disk full")

    assert orch._persist_provider_switch("QClaw", boom) is False


# ── 热重载 ───────────────────────────────────────────────────────────────────


class _FakeRunner:
    """只借用 DaemonRunner 的重载逻辑，不启动真实数据源。"""

    _reload_if_settings_changed = None  # 占位，下面动态挂上


def _make_runner(cfg_path: Path):
    from pa_agent.daemon.config import DaemonConfig
    from pa_agent.daemon.runner import DaemonRunner

    settings = load_settings(cfg_path)
    cfg = DaemonConfig(
        settings=settings,
        settings_path=cfg_path,
        symbol="BTCUSDT",
        timeframe="1h",
        data_source="binance",
        bar_count=100,
    )
    return DaemonRunner(cfg)


def test_reload_is_a_noop_when_mtime_did_not_change(cfg_path: Path) -> None:
    save_settings(Settings(), cfg_path)
    runner = _make_runner(cfg_path)
    assert runner._reload_if_settings_changed() is False


def test_reload_picks_up_new_values_when_the_file_changes(cfg_path: Path) -> None:
    settings = Settings()
    settings.auto_trade.leverage = 10
    save_settings(settings, cfg_path)
    runner = _make_runner(cfg_path)

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["auto_trade"]["leverage"] = 25
    cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.utime(cfg_path, (runner._settings_mtime + 5, runner._settings_mtime + 5))

    assert runner._reload_if_settings_changed() is True
    assert runner._cfg.settings.auto_trade.leverage == 25
    # 第二次调用不该再重载。
    assert runner._reload_if_settings_changed() is False


def test_reload_keeps_running_with_the_old_config_on_broken_json(
    cfg_path: Path, caplog
) -> None:
    settings = Settings()
    settings.auto_trade.leverage = 15
    save_settings(settings, cfg_path)
    runner = _make_runner(cfg_path)

    cfg_path.write_text("{ broken", encoding="utf-8")
    os.utime(cfg_path, (runner._settings_mtime + 5, runner._settings_mtime + 5))

    with caplog.at_level("WARNING"):
        assert runner._reload_if_settings_changed() is False
    # 内存里仍是旧的好配置，没有被默认值顶掉。
    assert runner._cfg.settings.auto_trade.leverage == 15
    assert "继续使用当前配置" in caplog.text


# ── 容器内：容器重建 / docker restart 必须能自愈 ─────────────────────────────
#
# 2026-11-14 的服务器事故：容器重建后 hostname 变成新容器 ID，旧逻辑把锁判成
# 「别的机器」永久视为活着，restart: unless-stopped 于是每 61 秒崩一次，
# 除了手动 rm 没有任何自愈路径。


def _fake_container(monkeypatch, *, name: str, machine: str = "m-1") -> None:
    """把当前进程伪装成容器内的进程。"""
    monkeypatch.setattr(instance_lock, "_HOSTNAME", name)
    monkeypatch.setattr(instance_lock, "_MACHINE", machine)
    monkeypatch.setattr(instance_lock, "_CONTAINER", name)
    monkeypatch.setattr(instance_lock, "_PROC_START", 100.0)


def _write_lock(path: Path, **fields) -> None:
    base = {
        "role": "daemon",
        "pid": 1,
        "host": "abc123",
        "machine": "m-1",
        "container": "abc123",
        "proc_start": 100.0,
        "started_at": 1.0,
    }
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")


def test_legacy_lock_without_machine_id_is_stale_inside_a_container(
    tmp_path: Path, monkeypatch
) -> None:
    """升级前的锁文件没有 machine/container 字段 —— 必须能自动接管。"""
    _fake_container(monkeypatch, name="newctr01")
    lock = tmp_path / ".runtime.lock"
    # 旧版本写出来的样子：只有 role/pid/host/started_at，host 是上个容器 ID。
    lock.write_text(
        json.dumps({"role": "daemon", "pid": 1, "host": "69c10f51fc00", "started_at": 1.0}),
        encoding="utf-8",
    )
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "stale"
    ctx = instance_lock.acquire("daemon", path=lock)
    try:
        assert instance_lock.read_holder(lock).container == "newctr01"
    finally:
        ctx.release()


def test_lock_from_a_rebuilt_container_is_stale(tmp_path: Path, monkeypatch) -> None:
    """容器 ID 换了 = 上个容器已经不在了，即使 machine-id 相同。"""
    _fake_container(monkeypatch, name="newctr01")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, host="oldctr99", container="oldctr99")
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "stale"
    ctx = instance_lock.acquire("daemon", path=lock)
    ctx.release()


def test_same_container_id_but_new_process_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    """``docker restart``：容器 ID 不变、PID 还是 1，只有 proc_start 能认出来。"""
    _fake_container(monkeypatch, name="ctr00001")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, host="ctr00001", container="ctr00001", proc_start=55.0)
    verdict = instance_lock.lock_verdict(instance_lock.read_holder(lock))
    if os.getpid() == 1:
        pytest.skip("当前进程就是 PID 1，无法构造「PID 相同但进程不同」")
    assert verdict == "stale"


def test_alive_lock_inside_the_same_container_blocks_startup(
    tmp_path: Path, monkeypatch
) -> None:
    """同一个容器、同一进程 —— 这才是真正的「已经有一个在跑」。"""
    _fake_container(monkeypatch, name="ctr00001")
    lock = tmp_path / ".runtime.lock"
    _write_lock(
        lock, pid=os.getpid(), host="ctr00001", container="ctr00001", proc_start=100.0
    )
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "alive"
    with pytest.raises(RuntimeError, match="已有 PA Agent 正在运行"):
        instance_lock.acquire("daemon", path=lock)


def test_machine_id_mismatch_alone_does_not_block_inside_a_container(
    tmp_path: Path, monkeypatch
) -> None:
    """镜像里的 /etc/machine-id 可能是空文件 —— 绝不能用它判 foreign。"""
    _fake_container(monkeypatch, name="ctr00001", machine="image-default-id")
    lock = tmp_path / ".runtime.lock"
    _write_lock(
        lock,
        host="ctr00001",
        container="ctr00001",
        machine="some-other-machine-id",
        proc_start=55.0,
    )
    # 只有机器 ID 不同、容器 ID 相同 → 不能判 foreign（会永久卡死）。
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) != "foreign"


def test_host_lock_is_foreign_inside_a_container(tmp_path: Path, monkeypatch) -> None:
    """容器里看到宿主机进程写的锁（共享挂载）—— 不抢，也不提议删除。"""
    _fake_container(monkeypatch, name="ctr00001")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, host="my-laptop", container="", pid=999999)
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "foreign"
    with pytest.raises(RuntimeError, match="属于另一台机器"):
        instance_lock.acquire("daemon", path=lock)


def test_container_lock_is_foreign_outside_a_container(
    tmp_path: Path, monkeypatch
) -> None:
    """本机 GUI 看到服务器容器写的锁 —— 同样不抢。"""
    monkeypatch.setattr(instance_lock, "_HOSTNAME", "my-laptop")
    monkeypatch.setattr(instance_lock, "_MACHINE", "host:my-laptop")
    monkeypatch.setattr(instance_lock, "_CONTAINER", "")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, host="ctr00001", container="ctr00001", pid=999999)
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "foreign"


def test_ignore_env_bypasses_the_check_without_writing_a_lock(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """紧急开关：不读也不写锁文件，但必须打 WARNING。"""
    lock = tmp_path / ".runtime.lock"
    lock.write_text(
        json.dumps(
            {"role": "daemon", "pid": os.getpid(), "host": instance_lock._hostname(), "started_at": 1.0}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(instance_lock.IGNORE_ENV, "1")
    with caplog.at_level("WARNING"):
        ctx = instance_lock.acquire("daemon", path=lock)
    try:
        assert instance_lock.IGNORE_ENV in caplog.text
        # 锁文件内容没被改写。
        assert instance_lock.read_holder(lock).role == "daemon"
    finally:
        ctx.release()
    # 没写过锁，就不该在退出时删掉别人的锁。
    assert lock.exists()


def test_acquire_raises_at_call_time_not_at_enter(tmp_path: Path) -> None:
    """回归：@contextmanager 时异常只在 __enter__ 抛，启动点的 try/except 是空的。"""
    lock = tmp_path / ".runtime.lock"
    with instance_lock.acquire("gui", path=lock):
        # 不进入 with —— 异常必须在这一行就抛出。
        with pytest.raises(RuntimeError, match="已有 PA Agent 正在运行"):
            instance_lock.acquire("daemon", path=lock)


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = tmp_path / ".runtime.lock"
    ctx = instance_lock.acquire("gui", path=lock)
    ctx.release()
    ctx.release()  # 不抛
    assert not lock.exists()


def test_manual_release_path_used_by_daemon_entrypoint(tmp_path: Path) -> None:
    """守护进程用的是 acquire() + finally release()，不是 with 语句。"""
    lock = tmp_path / ".runtime.lock"
    ctx = instance_lock.acquire("daemon", path=lock)
    try:
        assert lock.exists()
    finally:
        ctx.release()
    assert not lock.exists()
    # 释放后同一个路径可以再次获取。
    ctx2 = instance_lock.acquire("daemon", path=lock)
    ctx2.release()


def test_pid_reuse_is_not_mistaken_for_the_live_holder(
    tmp_path: Path, monkeypatch
) -> None:
    """PID 被复用：PID 活着但不是同一个进程 —— 必须判陈旧，否则锁永远解不开。"""
    monkeypatch.setattr(instance_lock, "_HOSTNAME", "ctr00001")
    monkeypatch.setattr(instance_lock, "_MACHINE", "m-1")
    monkeypatch.setattr(instance_lock, "_CONTAINER", "ctr00001")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, pid=4242, host="ctr00001", container="ctr00001", proc_start=100.0)

    monkeypatch.setattr(instance_lock, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(instance_lock, "_proc_start_of", lambda pid: 999.0)
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "stale"


def test_same_pid_and_same_starttime_is_alive(tmp_path: Path, monkeypatch) -> None:
    """对照组：PID 与 starttime 都对得上，就是真正在跑的那个实例。"""
    monkeypatch.setattr(instance_lock, "_HOSTNAME", "ctr00001")
    monkeypatch.setattr(instance_lock, "_MACHINE", "m-1")
    monkeypatch.setattr(instance_lock, "_CONTAINER", "ctr00001")
    lock = tmp_path / ".runtime.lock"
    _write_lock(lock, pid=4242, host="ctr00001", container="ctr00001", proc_start=100.0)

    monkeypatch.setattr(instance_lock, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(instance_lock, "_proc_start_of", lambda pid: 100.0)
    assert instance_lock.lock_verdict(instance_lock.read_holder(lock)) == "alive"


def test_parse_proc_start_skips_spaces_in_the_comm_field() -> None:
    """comm 里带空格/括号时，必须从最后一个 ')' 之后再切字段。"""
    # 切完 ')' 之后 fields[0] 是 state（第 3 字段），starttime 是第 22 字段
    # → 下标 19，所以 state 与 starttime 之间正好还有 18 个数值字段。
    middle = " ".join(str(i) for i in range(4, 22))
    raw = f"1234 (my (weird) proc) S {middle} 88888 0 0\n"
    assert instance_lock._parse_proc_start(raw) == 88888.0
