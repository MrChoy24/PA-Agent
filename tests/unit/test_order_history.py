"""历史下单记录的解析 / 汇总 / 导出测试（纯 Python，无 Qt）."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.records import order_history as oh


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _sample(dry_run: bool = False, executed: bool = True, **over: object) -> dict:
    payload = {
        "ts": "2026-11-07T01:22:33Z",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "dry_run": dry_run,
        "executed": executed,
        "reason": "ok",
        "order_id": "12345",
        "side": "BUY",
        "order_type": "MARKET",
        "qty": 0.001,
        "price": 75000.0,
        "message": "",
        "market_type": "futures",
        "leverage": 20,
        "margin_usdt": 10.0,
        "notional_usdt": 200.0,
        "position_side": "long",
        "margin_mode": "isolated",
        "order_status": "NEW",
    }
    payload.update(over)
    return payload


# ── parse_row ────────────────────────────────────────────────────────────────


def test_parse_row_full_payload() -> None:
    row = oh.parse_row(_sample())
    assert row is not None
    assert row.venue == "binance"
    assert row.symbol == "BTCUSDT"
    assert row.market_type == "futures"
    assert row.leverage == 20
    assert row.margin_usdt == pytest.approx(10.0)
    assert row.notional_usdt == pytest.approx(200.0)
    assert row.position_side == "long"


def test_parse_row_minimal_payload_uses_defaults() -> None:
    row = oh.parse_row({"ts": "2026-11-07T01:22:33Z"})
    assert row is not None
    assert row.market_type == "spot"
    assert row.leverage == 1
    assert row.margin_usdt == 0.0
    assert row.notional_usdt == 0.0


@pytest.mark.parametrize("payload", [None, {}, "text", 42, []])
def test_parse_row_rejects_non_dict(payload: object) -> None:
    assert oh.parse_row(payload) is None  # type: ignore[arg-type]


def test_parse_row_rejects_garbage_numbers() -> None:
    row = oh.parse_row(_sample(qty="not-a-number", price="???", leverage="x"))
    assert row is not None
    assert row.qty == 0.0
    assert row.price == 0.0
    assert row.leverage == 1


# ── status_bucket / 标签 ─────────────────────────────────────────────────────


def test_status_bucket_dry_run_wins_over_executed() -> None:
    row = oh.parse_row(_sample(dry_run=True, executed=True))
    assert row is not None
    assert row.status_bucket == oh.STATUS_DRY_RUN
    assert row.is_real_order is False
    assert row.status_label == "试运行"


def test_status_bucket_executed() -> None:
    row = oh.parse_row(_sample(dry_run=False, executed=True))
    assert row is not None
    assert row.status_bucket == oh.STATUS_EXECUTED
    assert row.is_real_order is True
    assert row.status_label == "已提交"


def test_status_bucket_skipped() -> None:
    row = oh.parse_row(_sample(dry_run=False, executed=False, reason="low-confidence"))
    assert row is not None
    assert row.status_bucket == oh.STATUS_SKIPPED
    assert row.status_label == "被拦截"


@pytest.mark.parametrize(
    ("side", "expected"),
    [("BUY", "做多"), ("buy", "做多"), ("long", "做多"), ("SELL", "做空"),
     ("sell", "做空"), ("short", "做空"), ("", "—")],
)
def test_side_label(side: str, expected: str) -> None:
    row = oh.parse_row(_sample(side=side))
    assert row is not None
    assert row.side_label == expected


def test_time_label_formats_iso() -> None:
    row = oh.parse_row(_sample(ts="2026-11-07T01:22:33Z"))
    assert row is not None
    assert row.time_label == "2026-11-07 09:22:33" or row.time_label.startswith("2026-11-07")


def test_time_label_falls_back_on_garbage() -> None:
    row = oh.parse_row(_sample(ts="not-a-date"))
    assert row is not None
    assert row.time_label == "not-a-date"


# ── load_order_history ───────────────────────────────────────────────────────


def test_load_returns_newest_first(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "auto_orders.jsonl",
        [
            _sample(ts="2026-11-07T01:00:00Z", order_id="1"),
            _sample(ts="2026-11-07T02:00:00Z", order_id="2"),
            _sample(ts="2026-11-07T03:00:00Z", order_id="3"),
        ],
    )
    rows = oh.load_order_history(path)
    assert [r.order_id for r in rows] == ["3", "2", "1"]


def test_load_skips_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "auto_orders.jsonl"
    path.write_text(
        json.dumps(_sample(order_id="good")) + "\n" + "{not json\n" + "\n",
        encoding="utf-8",
    )
    rows = oh.load_order_history(path)
    assert len(rows) == 1
    assert rows[0].order_id == "good"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert oh.load_order_history(tmp_path / "nope.jsonl") == []


def test_load_limits_rows(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "auto_orders.jsonl",
        [_sample(order_id=str(i)) for i in range(10)],
    )
    rows = oh.load_order_history(path, limit=3)
    assert len(rows) == 3
    assert [r.order_id for r in rows] == ["9", "8", "7"]


# ── summarize ────────────────────────────────────────────────────────────────


def test_summarize_counts_and_notional() -> None:
    rows = [
        oh.parse_row(_sample(executed=True, dry_run=False, notional_usdt=100.0)),
        oh.parse_row(_sample(executed=True, dry_run=True, notional_usdt=999.0)),
        oh.parse_row(_sample(executed=False, dry_run=False, notional_usdt=50.0)),
    ]
    rows = [r for r in rows if r is not None]
    stats = oh.summarize(rows)
    assert stats["total"] == 3
    assert stats["executed"] == 1
    assert stats["dry_run"] == 1
    assert stats["skipped"] == 1
    # 只有真实成交计入名义
    assert stats["real_notional_usdt"] == pytest.approx(100.0)
    assert stats["by_venue"]["binance"] == 3


def test_summarize_empty() -> None:
    stats = oh.summarize([])
    assert stats["total"] == 0
    assert stats["real_notional_usdt"] == pytest.approx(0.0)


# ── filter_rows ──────────────────────────────────────────────────────────────


def test_filter_by_bucket() -> None:
    rows = [r for r in (oh.parse_row(_sample(executed=False, reason="x")),) if r]
    assert oh.filter_rows(rows, bucket=oh.STATUS_SKIPPED) == rows
    assert oh.filter_rows(rows, bucket=oh.STATUS_EXECUTED) == []


def test_filter_by_venue() -> None:
    rows = [r for r in (oh.parse_row(_sample(venue="okx")),) if r]
    assert oh.filter_rows(rows, venue="okx") == rows
    assert oh.filter_rows(rows, venue="binance") == []


def test_filter_by_keyword_matches_symbol_and_reason() -> None:
    rows = [
        r
        for r in (
            oh.parse_row(_sample(symbol="ETHUSDT", reason="ok")),
            oh.parse_row(_sample(symbol="BTCUSDT", reason="low-confidence")),
        )
        if r
    ]
    assert len(oh.filter_rows(rows, keyword="eth")) == 1
    assert len(oh.filter_rows(rows, keyword="LOW-CONF")) == 1
    assert len(oh.filter_rows(rows, keyword="")) == 2


# ── export_csv ───────────────────────────────────────────────────────────────


def test_export_csv_writes_header_and_rows(tmp_path: Path) -> None:
    rows = [r for r in (oh.parse_row(_sample()), oh.parse_row(_sample(symbol="ETHUSDT"))) if r]
    out = tmp_path / "export.csv"
    assert oh.export_csv(rows, out) is True
    text = out.read_text(encoding="utf-8-sig")
    lines = text.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].split(",")[0] == oh.CSV_COLUMNS[0][0]
    assert "BTCUSDT" in lines[1]


def test_export_csv_empty_returns_false(tmp_path: Path) -> None:
    assert oh.export_csv([], tmp_path / "empty.csv") is False


def test_export_csv_creates_missing_parent_dir(tmp_path: Path) -> None:
    """用户手输的导出路径可能带不存在的子目录，自动建出来而不是报错."""
    rows = [r for r in (oh.parse_row(_sample()),) if r]
    target = tmp_path / "no" / "such" / "dir" / "x.csv"
    assert oh.export_csv(rows, target) is True
    assert target.is_file()


def test_export_csv_unwritable_target_returns_false(tmp_path: Path) -> None:
    rows = [r for r in (oh.parse_row(_sample()),) if r]
    # 把「目录」当文件写 → OSError，必须吞掉返回 False 而不是抛
    dir_target = tmp_path / "a_directory"
    dir_target.mkdir()
    assert oh.export_csv(rows, dir_target) is False
