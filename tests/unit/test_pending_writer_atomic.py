"""PendingWriter 原子写回归（2026-11-17 自检 O1）。

旧实现用固定临时名 ``<记录名>.tmp``：GUI 与守护进程同一秒写同名记录时
共用同一个临时文件，后到者的 ``os.replace`` 会失败（或自己的内容被对方的
``finally: unlink`` 删掉），整条分析记录静默丢失。这里守住「临时名唯一」
与「replace 失败重试」。
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pa_agent.records import pending_writer
from pa_agent.records.pending_writer import PendingWriter


def test_concurrent_writes_to_the_same_record_leave_no_temp_files(tmp_path: Path) -> None:
    writer = PendingWriter(pending_dir=tmp_path)
    target = tmp_path / "2026-11-17_20-00-00_ETHUSDT_30m.json"
    payloads = [{"seq": i, "body": "x" * 200} for i in range(16)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda payload: writer._write_json(target, payload), payloads))

    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["body"] == "x" * 200
    assert 0 <= data["seq"] < 16


def test_two_writers_never_share_a_temp_name(tmp_path: Path) -> None:
    seen: list[str] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    writer = PendingWriter(pending_dir=tmp_path)
    target = tmp_path / "rec.json"
    original = pending_writer.os.replace
    pending_writer.os.replace = recording_replace
    try:
        writer._write_json(target, {"a": 1})
        writer._write_json(target, {"a": 2})
    finally:
        pending_writer.os.replace = original

    assert len(set(seen)) == 2
    assert all(name.startswith("rec.json.") for name in seen)


def test_replace_is_retried_before_giving_up(tmp_path: Path, monkeypatch) -> None:
    """Windows 上杀毒/索引器短暂占用目标文件时不能一次就放弃。"""
    writer = PendingWriter(pending_dir=tmp_path)
    target = tmp_path / "retry.json"
    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by indexer")
        return real_replace(src, dst)

    monkeypatch.setattr(pending_writer.os, "replace", flaky_replace)
    writer._write_json(target, {"ok": True})

    assert calls["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []