"""End-to-end check: does the follow-up ("追问") path survive a dead primary?

Runs the real FreeChatSession against the user's real config/settings.json and
the real network. Nothing is written back: settings.json, records and the
pending JSONL are all left untouched (MagicMock sinks for persistence).

Usage (from any directory):

    python scripts/verify_followup_spare_channel.py            # up to 6 candidates
    python scripts/verify_followup_spare_channel.py --fast     # up to 2 candidates

Exit code 0 means an answer came back, whichever channel produced it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.ai.deepseek_client import DeepSeekClient  # noqa: E402
from pa_agent.config.paths import SETTINGS_JSON_PATH  # noqa: E402
from pa_agent.config.settings import load_settings  # noqa: E402
from pa_agent.orchestrator.free_chat import FreeChatSession  # noqa: E402
from pa_agent.records.schema import AnalysisRecord, RecordMeta  # noqa: E402
from pa_agent.util.threading import CancelToken  # noqa: E402

PROMPT = "请只回答一句：这是一次追问连通性测试，如果你收到了，请回复“收到”。"


def _base_record() -> AnalysisRecord:
    meta = RecordMeta(
        timestamp_local_iso="2026-10-29T00:00:00.000",
        timestamp_local_ms=1_761_705_600_000,
        symbol="XAUUSD",
        timeframe="15m",
        bar_count=2,
        ai_provider={"model": "deepseek-flash"},
    )
    return AnalysisRecord(
        meta=meta,
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response={"content": "", "reasoning_content": ""},
        stage2_decision={"decision": {"order_type": "不下单"}},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="limit the free pool to 2 candidates")
    parser.add_argument("--prompt", default=PROMPT)
    args = parser.parse_args()

    settings = load_settings(SETTINGS_JSON_PATH)
    provider = settings.provider
    print(f"[i] 主通道 model={provider.model} base_url={provider.base_url}")
    print(f"[i] fallbacks={[f.model for f in provider.fallbacks]}")

    if args.fast:
        import pa_agent.ai.sensenova_fallback as sf

        real_run = sf.run_free_stage_fallback

        def limited(**kwargs):
            kwargs["max_candidates"] = 2
            return real_run(**kwargs)

        sf.run_free_stage_fallback = limited  # type: ignore[assignment]

    client = DeepSeekClient(settings.provider)
    session = FreeChatSession(
        base_record=_base_record(),
        client=client,
        assembler=MagicMock(),
        pending_writer=MagicMock(),
        ledger=MagicMock(),
        settings=settings,
        kline_snapshot_fn=None,
    )

    progress_lines: list[str] = []

    def on_progress(text: str) -> None:
        progress_lines.append(text)
        print(f"[进度] {text.strip()}")

    t0 = time.monotonic()
    try:
        reply = session.send(args.prompt, CancelToken(), on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        print(f"[x] 失败（{elapsed:.1f}s）：{type(exc).__name__}: {exc}")
        print(f"[x] 已走通道：{session.spare_channel_used or '无（主通道即失败，备用通道也未接住）'}")
        return 1

    elapsed = time.monotonic() - t0
    channel = session.spare_channel_used or "主通道"
    print(f"[ok] 用时 {elapsed:.1f}s，回答来自：{channel}")
    print(f"[ok] 内容：{(reply.content or '').strip()[:200]}")
    print(f"[ok] 思考字数：{len(reply.reasoning_content or '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
