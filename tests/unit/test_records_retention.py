"""records/pending 保留期回归（2026-11-17 用户指示）。

策略必须是「保守的」：只删超期的分析记录、永远保留最新若干个、
其它文件一律不碰、出任何问题都不抛异常。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pa_agent.records import retention


def _make(directory: Path, name: str, *, age_days: float, now: float, payload: str = "{}") -> Path:
    path = directory / name
    path.write_text(payload, encoding="utf-8")
    stamp = now - age_days * 86400.0
    os.utime(path, (stamp, stamp))
    return path


def test_expired_records_are_removed(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    for i in range(3):
        _make(tmp_path, f"old-{i}.json", age_days=20, now=now, payload="{\"a\": 1}")
    fresh = _make(tmp_path, "fresh.json", age_days=1, now=now)
    followup = _make(tmp_path, "old-0.followups.jsonl", age_days=20, now=now, payload="{}\n")

    stats = retention.prune_pending(tmp_path, keep_days=14, keep_min=0, now=now)

    assert stats["enabled"] is True
    assert stats["scanned"] == 5
    assert stats["removed"] == 4
    assert stats["kept"] == 1
    assert stats["freed_bytes"] > 0
    assert fresh.exists()
    assert not followup.exists()


def test_keep_min_always_protects_the_newest(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    paths = [
        _make(tmp_path, f"rec-{i}.json", age_days=30 + i, now=now)
        for i in range(5)
    ]

    stats = retention.prune_pending(tmp_path, keep_days=14, keep_min=3, now=now)

    assert stats["removed"] == 2
    assert stats["kept"] == 3
    # 按 mtime 降序：rec-0 最新，必须留下
    assert paths[0].exists()
    assert paths[1].exists()
    assert paths[2].exists()
    assert not paths[4].exists()


def test_unrelated_files_are_never_touched(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    keep = [
        _make(tmp_path, ".keep", age_days=99, now=now, payload=""),
        _make(tmp_path, "notes.txt", age_days=99, now=now, payload="hi"),
        _make(tmp_path, "rec.json.1234.abcd.tmp", age_days=99, now=now, payload="{}"),
    ]

    stats = retention.prune_pending(tmp_path, keep_days=1, keep_min=0, now=now)

    assert stats["scanned"] == 0
    assert stats["removed"] == 0
    assert all(path.exists() for path in keep)


def test_zero_days_disables_the_pruner(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    old = _make(tmp_path, "old.json", age_days=400, now=now)

    stats = retention.prune_pending(tmp_path, keep_days=0, keep_min=0, now=now)

    assert stats["enabled"] is False
    assert stats["removed"] == 0
    assert old.exists()


def test_environment_variables_override_the_defaults(tmp_path: Path, monkeypatch) -> None:
    now = 1_700_000_000.0
    _make(tmp_path, "two-days.json", age_days=2, now=now)
    monkeypatch.setenv(retention.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(retention.RETENTION_KEEP_MIN_ENV, "0")

    assert retention.retention_days() == 1
    assert retention.keep_min_files() == 0
    stats = retention.prune_pending(tmp_path, now=now)
    assert stats["removed"] == 1


def test_invalid_environment_falls_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv(retention.RETENTION_DAYS_ENV, "two weeks")
    assert retention.retention_days() == retention.DEFAULT_RETENTION_DAYS


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    stats = retention.prune_pending(tmp_path / "nope", keep_days=7, keep_min=0)
    assert stats["scanned"] == 0
    assert stats["removed"] == 0