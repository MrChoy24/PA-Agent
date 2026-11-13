"""settings.json 损坏时的保护行为（P0-2）。

背景：早期 ``load_settings`` 碰到 JSON 解析失败只打一行 warning 就
``return Settings()`` —— 一次断电或半截写入，用户丢掉的不只是配置，
还有写在里面的 API Key 和风控参数；更糟的是下一次任意 ``save_settings()``
会把还能手工救回来的原文件直接覆盖掉。

现在要求：
1. 损坏文件一律**改名保留**成 ``settings.json.corrupt-<时间戳>``，绝不静默丢；
2. 有 ``.bak`` 就自动恢复并回写正本；
3. 没有 ``.bak`` 才退回默认配置，且必须留下 ERROR 日志；
4. ``save_settings`` 每次覆盖前把上一个**能解析**的版本留成 ``.bak``，
   但不许拿已损坏的内容顶掉更早的好备份。
"""
from __future__ import annotations

import json
import logging

import pytest

from pa_agent.config.settings import (
    Settings,
    _settings_backup_path,
    load_settings,
    save_settings,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def _corrupt_files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("settings.json.corrupt-*"))


def test_save_creates_a_backup_on_the_second_write(tmp_path):
    p = tmp_path / "settings.json"
    save_settings(Settings(), p)
    assert not _settings_backup_path(p).exists(), "第一次保存没有可备份的旧版本"

    s = Settings()
    s.provider.api_key = "second-write-key"
    save_settings(s, p)

    bak = _settings_backup_path(p)
    assert bak.exists()
    assert bak.name == "settings.json.bak"
    # 备份的是「上一个」版本，不是刚写的这个
    assert json.loads(bak.read_text(encoding="utf-8"))["provider"]["api_key"] != "second-write-key"


def test_broken_file_is_quarantined_and_restored_from_backup(tmp_path, caplog):
    p = tmp_path / "settings.json"

    good = Settings()
    good.provider.api_key = "restored-key-xyz"
    good.general.last_data_source = "okx"
    save_settings(good, p)
    # 再写一次，让 .bak 里落的是 good 这一版
    save_settings(good, p)

    _write(p, '{"provider": {"api_key": "trunc')  # 半截 JSON

    with caplog.at_level(logging.WARNING, logger="pa_agent.config.settings"):
        recovered = load_settings(p)

    assert recovered.provider.api_key == "restored-key-xyz"
    assert recovered.general.last_data_source == "okx"

    kept = _corrupt_files(tmp_path)
    assert len(kept) == 1, "损坏的原文件必须被改名保留，不能直接丢弃"
    assert "trunc" in (tmp_path / kept[0]).read_text(encoding="utf-8")

    # 正本已被恢复成合法 JSON
    assert json.loads(p.read_text(encoding="utf-8"))["provider"]["api_key"] == "restored-key-xyz"
    assert any("已用备份" in r.getMessage() for r in caplog.records)


def test_broken_file_without_backup_falls_back_to_defaults_and_keeps_the_file(
    tmp_path, caplog
):
    p = tmp_path / "settings.json"
    _write(p, "")

    with caplog.at_level(logging.ERROR, logger="pa_agent.config.settings"):
        loaded = load_settings(p)

    assert loaded.provider.api_key == "", "没有备份时只能退回默认配置"
    kept = _corrupt_files(tmp_path)
    assert len(kept) == 1
    assert not p.exists(), "损坏文件已改名，正本位置为空（下次保存会重建）"
    assert any("没有可用的 settings.json 备份" in r.getMessage() for r in caplog.records)


def test_saving_over_a_broken_file_does_not_clobber_a_good_backup(tmp_path):
    p = tmp_path / "settings.json"

    good = Settings()
    good.provider.api_key = "good-key"
    save_settings(good, p)
    save_settings(good, p)  # 让 .bak 里是 good-key 这一版
    bak_bytes = _settings_backup_path(p).read_bytes()

    _write(p, "{ not json at all")

    fresh = Settings()
    fresh.provider.api_key = "brand-new-key"
    save_settings(fresh, p)

    assert _settings_backup_path(p).read_bytes() == bak_bytes, (
        "已损坏的正本不许覆盖掉更早的可用备份"
    )
    assert json.loads(p.read_text(encoding="utf-8"))["provider"]["api_key"] == "brand-new-key"


def test_backup_copy_failure_only_warns(tmp_path, caplog, monkeypatch):
    """备份写不下去（磁盘满 / 只读挂载）不能让保存本身失败。"""
    import shutil as _shutil

    p = tmp_path / "settings.json"
    save_settings(Settings(), p)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(_shutil, "copy2", _boom)
    with caplog.at_level(logging.WARNING, logger="pa_agent.config.settings"):
        save_settings(Settings(), p)

    assert p.exists()
    assert any("备份" in r.getMessage() for r in caplog.records)


def test_restore_rejects_a_structural_mismatch(tmp_path, caplog):
    """``.bak`` 里是合法 JSON 但结构不对时，当作没有备份处理（不抛、不给半套配置）。"""
    p = tmp_path / "settings.json"
    _settings_backup_path(p).write_text(json.dumps({"provider": "not-a-dict"}), encoding="utf-8")
    _write(p, "{ broken")

    with caplog.at_level(logging.WARNING, logger="pa_agent.config.settings"):
        recovered = load_settings(p)

    assert isinstance(recovered, Settings)
    assert recovered.provider.api_key == "", "结构不符的备份不能被当成恢复成功"
    assert any("结构不兼容" in r.getMessage() for r in caplog.records)
    assert len(_corrupt_files(tmp_path)) == 1


def test_load_missing_file_still_creates_defaults(tmp_path):
    """正常路径不能被这次的改动带坏。"""
    p = tmp_path / "settings.json"
    assert not p.exists()
    loaded = load_settings(p)
    assert loaded.provider.model
    assert p.exists()
    assert _corrupt_files(tmp_path) == []
