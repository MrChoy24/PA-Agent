"""自动下单历史读取（trade_records/auto_orders.jsonl）.

``AutoTrader`` 每次评估都会向 ``trade_records/auto_orders.jsonl`` 追加一行
JSON（含被闸门拦下的记录），本模块把它读成结构化行，供 GUI「历史下单」
面板与命令行/守护进程复用。

设计约束：

* **绝不抛异常**：文件不存在、某行 JSON 坏了、字段缺失，全部跳过或降级，
  因为历史文件是用户/旧版本留下的，格式不可控。
* **不写文件**：只读。任何清理/导出由调用方决定。
* 纯 Python，不 import PyQt6（守护进程也可能用得上）。
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

#: 相对路径（与 ``AutoTrader._DEFAULT_LOG_PATH`` 保持一致）
DEFAULT_HISTORY_RELATIVE = Path("trade_records") / "auto_orders.jsonl"

#: 状态分组——面板筛选用的三个桶
STATUS_EXECUTED = "executed"
STATUS_DRY_RUN = "dry-run"
STATUS_SKIPPED = "skipped"


def _project_root() -> Path | None:
    try:
        from pa_agent.config.paths import PROJECT_ROOT

        return Path(PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - 路径模块不可用时降级
        return None


def resolve_history_path(path: str | Path | None = None) -> Path:
    """定位 ``auto_orders.jsonl``：显式路径 > 当前目录 > 项目根目录."""
    if path is not None:
        return Path(path)
    relative = DEFAULT_HISTORY_RELATIVE
    candidates: list[Path] = [relative]
    root = _project_root()
    if root is not None:
        candidates.append(root / relative)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    # 都不存在时返回第一个（保证调用方拿到一个稳定的「预期路径」，便于界面提示）
    return candidates[0]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return default
    return text or default


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class OrderHistoryRow:
    """一行下单历史（或一次「本应下单但被拦下」的记录）。"""

    ts: str = ""
    venue: str = ""
    symbol: str = ""
    timeframe: str = ""
    side: str = ""
    order_type: str = ""
    market_type: str = "spot"
    position_side: str = ""
    margin_mode: str = ""
    leverage: int = 1
    margin_usdt: float = 0.0
    notional_usdt: float = 0.0
    qty: float = 0.0
    price: float = 0.0
    executed: bool = False
    dry_run: bool = False
    order_id: str = ""
    order_status: str = ""
    reason: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    # ── 派生属性（界面直接用）──────────────────────────────────────────────

    @property
    def status_bucket(self) -> str:
        """归类到 :data:`STATUS_EXECUTED` / :data:`STATUS_DRY_RUN` / :data:`STATUS_SKIPPED`."""
        if self.dry_run:
            return STATUS_DRY_RUN
        if self.executed:
            return STATUS_EXECUTED
        return STATUS_SKIPPED

    @property
    def is_real_order(self) -> bool:
        """真正发到交易所并成功的单."""
        return self.executed and not self.dry_run

    @property
    def side_label(self) -> str:
        """``BUY``/``SELL``/``buy``/``sell`` → 中文."""
        text = self.side.strip().lower()
        if text in ("buy", "long", "做多"):
            return "做多"
        if text in ("sell", "short", "做空"):
            return "做空"
        return self.side or "—"

    @property
    def market_label(self) -> str:
        return "合约" if self.market_type == "futures" else "现货"

    @property
    def status_label(self) -> str:
        bucket = self.status_bucket
        if bucket == STATUS_DRY_RUN:
            return "试运行"
        if bucket == STATUS_EXECUTED:
            return "已提交"
        return "被拦截"

    @property
    def time_label(self) -> str:
        """把 ISO 时间戳裁到「YYYY-MM-DD HH:MM:SS」，解析失败就原样返回."""
        text = self.ts
        if not text:
            return "—"
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return moment.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            # 兜底：手写截断，形如 2026-11-07T01:11:00.123+08:00
            cleaned = text.replace("T", " ")
            return cleaned[:19] if len(cleaned) >= 19 else cleaned


def parse_row(payload: Any) -> OrderHistoryRow | None:
    """把一行 JSON 转成 :class:`OrderHistoryRow`；不合格返回 ``None``."""
    if not isinstance(payload, dict) or not payload:
        return None
    return OrderHistoryRow(
        ts=_text(payload.get("ts")),
        venue=_text(payload.get("venue")).lower(),
        symbol=_text(payload.get("symbol")),
        timeframe=_text(payload.get("timeframe")),
        side=_text(payload.get("side")),
        order_type=_text(payload.get("order_type")),
        market_type=_text(payload.get("market_type"), "spot").lower() or "spot",
        position_side=_text(payload.get("position_side")).lower(),
        margin_mode=_text(payload.get("margin_mode")).lower(),
        leverage=int(_number(payload.get("leverage"), 1.0)) or 1,
        margin_usdt=_number(payload.get("margin_usdt")),
        notional_usdt=_number(payload.get("notional_usdt")),
        qty=_number(payload.get("qty")),
        price=_number(payload.get("price")),
        executed=_flag(payload.get("executed")),
        dry_run=_flag(payload.get("dry_run")),
        order_id=_text(payload.get("order_id")),
        order_status=_text(payload.get("order_status")),
        reason=_text(payload.get("reason")),
        message=_text(payload.get("message")),
        raw=payload,
    )


def load_order_history(
    path: str | Path | None = None,
    *,
    limit: int | None = None,
) -> list[OrderHistoryRow]:
    """读取下单历史，**最新的在前**；文件不存在返回空列表."""
    target = resolve_history_path(path)
    rows: list[OrderHistoryRow] = []
    try:
        if not target.is_file():
            return []
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except (ValueError, TypeError):
                    continue
                row = parse_row(payload)
                if row is not None:
                    rows.append(row)
    except OSError as exc:
        logger.warning("读取下单历史失败（%s）: %s", target, exc)
        return []

    rows.reverse()  # 文件是追加写，倒序后最新在前
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    return rows


def summarize(rows: Iterable[OrderHistoryRow]) -> dict[str, Any]:
    """统计三类记录数量与总名义金额（真实单）."""
    total = 0
    executed = 0
    dry_run = 0
    skipped = 0
    real_notional = 0.0
    by_venue: dict[str, int] = {}
    for row in rows:
        total += 1
        bucket = row.status_bucket
        if bucket == STATUS_EXECUTED:
            executed += 1
            real_notional += row.notional_usdt
        elif bucket == STATUS_DRY_RUN:
            dry_run += 1
        else:
            skipped += 1
        if row.venue:
            by_venue[row.venue] = by_venue.get(row.venue, 0) + 1
    return {
        "total": total,
        "executed": executed,
        "dry_run": dry_run,
        "skipped": skipped,
        "real_notional_usdt": round(real_notional, 2),
        "by_venue": by_venue,
    }


def filter_rows(
    rows: Sequence[OrderHistoryRow],
    *,
    bucket: str = "",
    venue: str = "",
    keyword: str = "",
) -> list[OrderHistoryRow]:
    """按状态桶 / 交易所 / 关键字（品种、理由、订单号）过滤."""
    needle = keyword.strip().lower()
    out: list[OrderHistoryRow] = []
    for row in rows:
        if bucket and row.status_bucket != bucket:
            continue
        if venue and row.venue != venue:
            continue
        if needle:
            haystack = " ".join(
                (row.symbol, row.reason, row.message, row.order_id, row.timeframe)
            ).lower()
            if needle not in haystack:
                continue
        out.append(row)
    return out


#: 导出 CSV 的列（顺序即表格列顺序）
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("时间", "time_label"),
    ("交易所", "venue"),
    ("品种", "symbol"),
    ("周期", "timeframe"),
    ("方向", "side_label"),
    ("市场", "market_label"),
    ("杠杆", "leverage"),
    ("保证金(USDT)", "margin_usdt"),
    ("名义(USDT)", "notional_usdt"),
    ("数量", "qty"),
    ("价格", "price"),
    ("状态", "status_label"),
    ("订单号", "order_id"),
    ("原因", "reason"),
    ("说明", "message"),
)


def export_csv(rows: Sequence[OrderHistoryRow], path: str | Path) -> bool:
    """把行导出成 CSV（UTF-8-BOM，Excel 打开不乱码）；失败返回 False."""
    if not rows:
        # 没有记录时不要覆盖用户已有的同名文件
        return False
    try:
        target = Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow([label for label, _ in CSV_COLUMNS])
            for row in rows:
                writer.writerow([getattr(row, attr, "") for _, attr in CSV_COLUMNS])
        return True
    except OSError as exc:
        logger.warning("导出下单历史失败（%s）: %s", path, exc)
        return False


__all__ = [
    "CSV_COLUMNS",
    "DEFAULT_HISTORY_RELATIVE",
    "OrderHistoryRow",
    "STATUS_DRY_RUN",
    "STATUS_EXECUTED",
    "STATUS_SKIPPED",
    "export_csv",
    "filter_rows",
    "load_order_history",
    "parse_row",
    "resolve_history_path",
    "summarize",
]
