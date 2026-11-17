"""records/pending 自动保留期。

用户 2026-11-17 指示：``records/pending/`` 每天新增约 37MB，不能无限增长。
清理策略刻意保守：

* 只删 ``*.json`` 与 ``*.followups.jsonl``（分析记录与其追问流水）；
* 按 **mtime** 判断，早于 ``keep_days`` 的才删；
* 无论多旧，**永远保留最新 ``keep_min`` 个** —— 长时间停机后一次性清理
  不会把最近的分析全删掉；
* ``.keep`` / ``*.tmp`` / 其它文件一律不碰；
* 删除失败只记日志、绝不向上抛：清理是维护动作，不能影响交易主链路。

环境变量：

``PA_AGENT_RECORDS_RETENTION_DAYS``
    保留天数（默认 14）；``0`` 表示关闭自动清理。
``PA_AGENT_RECORDS_RETENTION_KEEP_MIN``
    至少保留多少个最新文件（默认 200）。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RETENTION_DAYS_ENV = "PA_AGENT_RECORDS_RETENTION_DAYS"
RETENTION_KEEP_MIN_ENV = "PA_AGENT_RECORDS_RETENTION_KEEP_MIN"

DEFAULT_RETENTION_DAYS = 14
DEFAULT_KEEP_MIN = 200

_RECORD_SUFFIXES = (".json", ".followups.jsonl")


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 不是整数，按默认值 %d 处理", name, raw, default)
        return default


def retention_days() -> int:
    """保留天数；``0`` 表示关闭自动清理。"""
    return max(0, _env_int(RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS))


def keep_min_files() -> int:
    """至少保留的最新记录条数。"""
    return max(0, _env_int(RETENTION_KEEP_MIN_ENV, DEFAULT_KEEP_MIN))


def _is_record_file(path: Path) -> bool:
    name = path.name
    if not name or name.startswith("."):
        return False
    if name.endswith(".tmp"):
        return False
    return name.endswith(_RECORD_SUFFIXES)


def prune_pending(
    directory: str | Path,
    *,
    keep_days: int | None = None,
    keep_min: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """删除 *directory* 里超期的分析记录，返回统计信息。

    参数全部可注入（``keep_days`` / ``keep_min`` / ``now``）以便单测。
    永不抛异常：目录不存在、权限不足、单个文件删不掉都只记日志。
    """
    stats: dict[str, Any] = {
        "directory": str(directory),
        "enabled": True,
        "scanned": 0,
        "removed": 0,
        "kept": 0,
        "freed_bytes": 0,
        "cutoff": "",
    }
    days = retention_days() if keep_days is None else max(0, int(keep_days))
    if days <= 0:
        stats["enabled"] = False
        return stats
    minimum = keep_min_files() if keep_min is None else max(0, int(keep_min))
    current = time.time() if now is None else float(now)
    cutoff = current - days * 86400.0
    stats["cutoff"] = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(
        timespec="seconds"
    )

    root = Path(directory)
    if not root.is_dir():
        return stats

    rows: list[tuple[Path, float]] = []
    try:
        for path in root.iterdir():
            if not path.is_file() or not _is_record_file(path):
                continue
            try:
                rows.append((path, path.stat().st_mtime))
            except OSError as exc:
                logger.warning("records 清理：读取 %s 的 mtime 失败（%s）", path.name, exc)
    except OSError as exc:
        logger.warning("records 清理：无法列出 %s（%s）", root, exc)
        return stats

    stats["scanned"] = len(rows)
    rows.sort(key=lambda item: item[1], reverse=True)
    kept = 0
    for index, (path, mtime) in enumerate(rows):
        if index < minimum or mtime >= cutoff:
            kept += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("records 清理：删除 %s 失败（%s）", path.name, exc)
            kept += 1
            continue
        stats["removed"] += 1
        stats["freed_bytes"] += size
    stats["kept"] = kept
    if stats["removed"]:
        logger.info(
            "records 清理：%s 删除 %d 个超过 %d 天的记录，释放 %.1f MB（保留 %d 个）",
            root,
            stats["removed"],
            days,
            stats["freed_bytes"] / 1048576.0,
            kept,
        )
    return stats