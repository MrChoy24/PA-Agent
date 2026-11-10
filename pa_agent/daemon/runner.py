"""Headless analysis runner: poll → snapshot → two-stage analysis → notify → trade.

The desktop GUI does the same work inside ``pa_agent/gui/main_window.py``, but it
is entangled with widgets.  This module re-implements only the *orchestration*
(never the analysis itself):

    main loop
      └─ for each poll tick
           ├─ data_source.latest_snapshot(bar_count + warmup)
           ├─ detect a newly closed bar (bar_close_wait helpers)
           ├─ build_analysis_frame(bars)          # closed bars only
           ├─ TwoStageOrchestrator.submit(frame)  # the real engine
           ├─ prepare_stage2_for_ui(...)          # same payload the GUI renders
           ├─ save_trade_record / DingTalk / PushPlus
           └─ AutoTrader.evaluate(...)            # honours dry_run + all gates

Nothing here imports PyQt6, so the whole thing runs on a server with no display.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pa_agent.config.paths import (
    EXPERIENCE_DIR,
    PROJECT_ROOT,
    PROMPT_DIR,
    RECORDS_PENDING_DIR,
)
from pa_agent.daemon.config import DaemonConfig
from pa_agent.daemon.event_bus import LoggingEventBus
from pa_agent.data.base import DataSourceError, DataSourceTransientError, KlineBar
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_analysis_frame

logger = logging.getLogger(__name__)

#: How long to wait before retrying after a transient data-source failure.
_ERROR_BACKOFF_S = 15.0
#: Cap for the exponential backoff after repeated failures.
_MAX_BACKOFF_S = 300.0


@dataclass(slots=True)
class AnalysisOutcome:
    """Result of one analysis round (for logging / health checks)."""

    ran: bool
    reason: str = ""
    bar_ts_open: float | None = None
    decision: dict | None = None
    stage1: dict | None = None
    had_order: bool = False
    notified: list[str] = field(default_factory=list)
    trade_reason: str = ""


class DaemonRunner:
    """Owns the poll loop and the analysis pipeline for one symbol/timeframe."""

    def __init__(self, config: DaemonConfig) -> None:
        self._cfg = config
        self._stop = threading.Event()
        self._source: Any = None
        self._orchestrator: Any = None
        self._pending_writer: Any = None
        self._exp_reader: Any = None
        self._last_analyzed_bar_ts: float | None = None
        self._last_analysis_monotonic: float = 0.0
        self._consecutive_errors = 0
        self._rounds = 0
        self._analyses = 0
        self._last_outcome: AnalysisOutcome | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def request_stop(self, *_args: Any) -> None:
        """Signal handler target: ask the loop to finish the current tick."""
        if not self._stop.is_set():
            logger.info("收到停止信号，等待当前轮询结束…")
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):  # not in main thread / unsupported
                logger.debug("无法安装 %s 处理器（忽略）", sig_name)

    # ── setup ────────────────────────────────────────────────────────────────

    def build(self) -> None:
        """Create the data source and the analysis stack. Raises ConfigError on bad setup."""
        from pa_agent.config.settings import provider_api_key_configured
        from pa_agent.data.factory import create_data_source

        cfg = self._cfg

        if not provider_api_key_configured(cfg.settings):
            raise RuntimeError(
                "AI API Key 未配置：请在 config/settings.json 的 provider.api_key 填写，"
                "或在服务器上设置环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY。"
            )

        logger.info("连接数据源 %s …", cfg.data_source)
        source = create_data_source(cfg.data_source)
        try:
            source.connect()
        except DataSourceError as exc:
            raise RuntimeError(f"数据源连接失败：{exc}") from exc
        source.subscribe(cfg.symbol, cfg.timeframe)
        self._source = source
        logger.info("数据源就绪：%s", type(source).__name__)

        # Warm the Binance symbol cache in the background: list_symbols() is
        # then free, and this keeps the very first snapshot from blocking on
        # the exchangeInfo round-trip.
        warm = getattr(source, "warm_symbols_cache", None)
        if callable(warm):
            def _warm() -> None:
                try:
                    warm()
                except Exception as exc:  # noqa: BLE001 - cosmetic only
                    logger.debug("预热交易对缓存失败：%s", exc)

            threading.Thread(target=_warm, name="symbol-warmup", daemon=True).start()

        self._build_orchestrator()

    def _build_orchestrator(self) -> None:
        """Assemble the exact same objects ``AppContext.bootstrap`` builds for the GUI."""
        from pa_agent.ai.client_factory import create_ai_client
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.ai.prompt_assembler import PromptAssembler
        from pa_agent.ai.router import route_strategy_files
        from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
        from pa_agent.records.experience_reader import ExperienceReader
        from pa_agent.records.pending_writer import PendingWriter

        settings = self._cfg.settings
        bus = LoggingEventBus()

        client = create_ai_client(settings.provider, logger_=logger)
        exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=logger)
        assembler = PromptAssembler(
            prompt_dir=PROMPT_DIR,
            experience_reader=exp_reader,
            prompt_settings=settings.prompt,
        )
        validator = JsonValidator(settings.validation)
        pending_writer = PendingWriter(
            pending_dir=RECORDS_PENDING_DIR,
            event_bus=bus,
            logger=logger,
            api_key=settings.provider.api_key,
        )
        self._pending_writer = pending_writer
        self._exp_reader = exp_reader
        self._orchestrator = TwoStageOrchestrator(
            client=client,
            assembler=assembler,
            router=route_strategy_files,
            validator=validator,
            pending_writer=pending_writer,
            exp_reader=exp_reader,
            settings=settings,
        )
        logger.info("分析引擎就绪（provider=%s）", settings.provider.model)

    def shutdown(self) -> None:
        if self._source is not None:
            try:
                self._source.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("断开数据源时出错（忽略）：%s", exc)
            self._source = None

    # ── main loop ────────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        cfg = self._cfg
        logger.info("守护进程启动：%s", cfg.describe())
        if cfg.auto_trade_enabled():
            auto = cfg.settings.auto_trade
            if bool(getattr(auto, "dry_run", True)):
                logger.warning("自动交易已启用，但处于【试运行】模式：只记录不下单。")
            else:
                logger.warning(
                    "自动交易已启用，且为【真实下单】模式（venue=%s，单笔上限 %s USDT，每日 %s 单）。",
                    getattr(auto, "venue", "binance"),
                    getattr(auto, "max_notional_usdt", 0),
                    getattr(auto, "max_daily_orders", 0),
                )

        while not self._stop.is_set():
            started = time.monotonic()
            try:
                outcome = self.tick(force=cfg.analyze_on_start and self._analyses == 0)
                if outcome.ran:
                    self._consecutive_errors = 0
                slept = self._sleep_until_next_poll(started)
            except DataSourceTransientError as exc:
                self._consecutive_errors += 1
                delay = min(_MAX_BACKOFF_S, _ERROR_BACKOFF_S * self._consecutive_errors)
                logger.warning("行情暂时不可用（第 %d 次）：%s；%.0fs 后重试", self._consecutive_errors, exc, delay)
                slept = self._stop.wait(delay)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self._consecutive_errors += 1
                delay = min(_MAX_BACKOFF_S, _ERROR_BACKOFF_S * self._consecutive_errors)
                logger.exception("轮询异常（第 %d 次）：%s；%.0fs 后重试", self._consecutive_errors, exc, delay)
                slept = self._stop.wait(delay)
            if slept and self._stop.is_set():
                break

        logger.info("守护进程已停止（共轮询 %d 次，分析 %d 次）", self._rounds, self._analyses)

    def _sleep_until_next_poll(self, started_monotonic: float) -> bool:
        """Sleep for the remainder of the poll interval. Returns True if interrupted."""
        elapsed = time.monotonic() - started_monotonic
        remaining = max(1.0, self._cfg.poll_interval_s - elapsed)
        return bool(self._stop.wait(remaining))

    # ── one tick ─────────────────────────────────────────────────────────────

    def tick(self, *, force: bool = False) -> AnalysisOutcome:
        """Poll once and analyse if a new closed bar is available."""
        if self._source is None or self._orchestrator is None:
            raise RuntimeError("runner.build() 尚未调用")

        cfg = self._cfg
        self._rounds += 1

        needed = cfg.bar_count + INDICATOR_WARMUP_BARS + 5
        bars: list[KlineBar] = list(self._source.latest_snapshot(needed))
        if not bars:
            return AnalysisOutcome(ran=False, reason="交易所返回空K线")

        forming_ts = self._forming_ts(bars)
        newest_closed_ts = self._newest_closed_ts(bars)
        if newest_closed_ts is None:
            return AnalysisOutcome(ran=False, reason="还没有已收盘的K线")

        # Gate 1 — the same closed bar was already analysed.  This covers the
        # "wait for the bar to close" case too: while the newest bar is still
        # forming, the newest *closed* bar does not change, so nothing new can
        # be analysed until it closes and becomes the newest closed bar.
        if cfg.skip_duplicate_bars and not force and newest_closed_ts == self._last_analyzed_bar_ts:
            if forming_ts is not None:
                return AnalysisOutcome(
                    ran=False,
                    reason="等待当前K线收盘（上一根已分析过）",
                    bar_ts_open=newest_closed_ts,
                )
            return AnalysisOutcome(
                ran=False,
                reason="该K线已分析过",
                bar_ts_open=newest_closed_ts,
            )

        # Gate 2 — sanity: does the newest "closed" bar really have time to be
        # closed?  Some exchanges mark a bar closed a few seconds early.
        if cfg.wait_bar_close and not force:
            close_ms = newest_closed_ts + cfg.bar_seconds * 1000
            now_ms = int(time.time() * 1000)
            if now_ms < close_ms - 1000:
                return AnalysisOutcome(
                    ran=False,
                    reason="该K线尚未真正收盘",
                    bar_ts_open=newest_closed_ts,
                )

        # Gate 3 — token-budget cooldown between two analyses.
        cooldown_left = cfg.analysis_cooldown_s - (
            time.monotonic() - self._last_analysis_monotonic
        )
        if not force and self._last_analysis_monotonic > 0 and cooldown_left > 0:
            return AnalysisOutcome(
                ran=False,
                reason=f"冷却中（{cooldown_left:.0f}s）",
                bar_ts_open=newest_closed_ts,
            )

        frame = build_analysis_frame(
            bars,
            cfg.bar_count,
            cfg.symbol,
            cfg.timeframe,
        )
        if frame is None:
            return AnalysisOutcome(
                ran=False,
                reason=f"已收盘K线不足 {cfg.bar_count} 根",
                bar_ts_open=newest_closed_ts,
            )

        return self._run_analysis(frame, bars, newest_closed_ts, force=force)

    def _forming_ts(self, bars: list[KlineBar]) -> float | None:
        from pa_agent.data.bar_close_wait import current_forming_ts

        return current_forming_ts(
            bars,
            self._cfg.timeframe,
            symbol=self._cfg.symbol,
        )

    def _newest_closed_ts(self, bars: list[KlineBar]) -> float | None:
        from pa_agent.data.bar_close_wait import has_forming_bar_at_head

        has_forming = has_forming_bar_at_head(
            bars,
            self._cfg.timeframe,
            symbol=self._cfg.symbol,
        )
        start = 1 if has_forming else 0
        for bar in bars[start:]:
            if bar.closed:
                return float(bar.ts_open)
        return None

    # ── analysis ─────────────────────────────────────────────────────────────

    def _run_analysis(
        self,
        frame: Any,
        bars: list[KlineBar],
        bar_ts_open: float,
        *,
        force: bool,
    ) -> AnalysisOutcome:
        from pa_agent.records.analysis_history import (
            compute_incremental_bar_delta,
            find_latest_successful_record,
        )
        from pa_agent.util.threading import CancelToken, OrchestratorEvent

        cfg = self._cfg
        cancel_token = CancelToken()

        previous_record = None
        incremental_new_bar_count: int | None = None
        if cfg.incremental:
            previous_record = find_latest_successful_record(
                symbol=cfg.symbol,
                timeframe=cfg.timeframe,
            )
            if previous_record is not None:
                delta = compute_incremental_bar_delta(frame, previous_record)
                if delta is not None:
                    if delta.new_count <= cfg.incremental_threshold:
                        incremental_new_bar_count = delta.new_count
                    else:
                        previous_record = None

        logger.info(
            "开始分析 %s %s（%d 根%s）…",
            cfg.symbol,
            cfg.timeframe,
            len(frame.bars),
            f"，增量 {incremental_new_bar_count} 根"
            if incremental_new_bar_count is not None
            else "",
        )

        def _on_event(event: OrchestratorEvent) -> None:
            logger.debug("orchestrator event: %s", event.name)

        record = self._orchestrator.submit(
            frame,
            cancel_token,
            _on_event,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
        )

        self._analyses += 1
        self._last_analysis_monotonic = time.monotonic()
        self._last_analyzed_bar_ts = bar_ts_open

        exc = getattr(record, "exception", None)
        if exc:
            logger.warning("分析未成功完成：%s", exc)
            outcome = AnalysisOutcome(
                ran=True,
                reason=f"分析异常：{exc}",
                bar_ts_open=bar_ts_open,
            )
            self._last_outcome = outcome
            return outcome

        stage1 = getattr(record, "stage1_diagnosis", None) or {}
        s2_full = getattr(record, "stage2_decision", None) or {}
        if not s2_full:
            outcome = AnalysisOutcome(
                ran=True,
                reason="模型未给出决策",
                bar_ts_open=bar_ts_open,
                stage1=stage1 if isinstance(stage1, dict) else None,
            )
            self._last_outcome = outcome
            return outcome

        inner = self._prepare_inner(s2_full, stage1)
        had_order = self._has_order(inner)
        logger.info(
            "分析完成：order_type=%s confidence=%s",
            inner.get("order_type", "—"),
            inner.get("trade_confidence", "—"),
        )

        notified: list[str] = []
        if had_order:
            notified = self._post_order_followup(inner, s2_full, stage1, frame)

        trade_reason = self._maybe_auto_trade(inner)

        outcome = AnalysisOutcome(
            ran=True,
            reason="ok",
            bar_ts_open=bar_ts_open,
            decision=inner,
            stage1=stage1 if isinstance(stage1, dict) else None,
            had_order=had_order,
            notified=notified,
            trade_reason=trade_reason,
        )
        self._last_outcome = outcome
        return outcome

    def _prepare_inner(self, s2_full: dict, stage1: Any) -> dict:
        from pa_agent.gui.stage2_payload import prepare_stage2_for_ui

        general = self._cfg.settings.general
        return prepare_stage2_for_ui(
            s2_full if isinstance(s2_full, dict) else {},
            stage1_json=stage1 if isinstance(stage1, dict) else None,
            skip_next_bar=not bool(getattr(general, "enable_next_bar_prediction", False)),
        )

    def _has_order(self, inner: dict) -> bool:
        from pa_agent.gui.order_opportunity import has_order_opportunity

        threshold = int(
            getattr(self._cfg.settings.general, "decision_confidence_threshold", 0) or 0
        )
        return has_order_opportunity(inner, confidence_threshold=threshold)

    # ── notify ───────────────────────────────────────────────────────────────

    def _post_order_followup(
        self,
        inner: dict,
        decision: dict,
        stage1: Any,
        frame: Any,
    ) -> list[str]:
        """Record the trade CSV/chart and push DingTalk + PushPlus. Never raises."""
        cfg = self._cfg
        settings = cfg.settings
        general = settings.general
        sent: list[str] = []

        try:
            from pa_agent.records.trade_logger import save_trade_record

            save_trade_record(
                decision_inner=inner,
                stage2_full=decision,
                stage1_diagnosis=stage1 if isinstance(stage1, dict) else None,
                frame=frame,
                meta_symbol=cfg.symbol,
                meta_timeframe=cfg.timeframe,
                decision_stance=str(getattr(general, "decision_stance", "") or ""),
                model_name=str(getattr(settings.provider, "model", "") or ""),
                structure_flip_cooldown_bars=int(
                    getattr(general, "structure_flip_cooldown_bars", 3) or 3
                ),
            )
            sent.append("trade_record")
        except Exception as exc:  # noqa: BLE001
            logger.warning("交易记录写入失败（不影响主流程）：%s", exc)

        try:
            from pa_agent.notify.dingtalk_notifier import (
                dingtalk_is_active,
                send_order_signal as send_dingtalk_order,
            )

            if dingtalk_is_active(settings):
                if send_dingtalk_order(
                    inner,
                    decision,
                    cfg.symbol,
                    cfg.timeframe,
                    chart_image_path=None,
                    settings=settings,
                ):
                    sent.append("dingtalk")
                else:
                    logger.warning("钉钉推送失败（不影响主流程）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("钉钉通知异常（不影响主流程）：%s", exc)

        try:
            from pa_agent.notify.pushplus_notifier import (
                pushplus_is_active,
                send_order_signal as send_pushplus_order,
            )

            if pushplus_is_active(settings):
                if send_pushplus_order(
                    decision_inner=inner,
                    stage2_full=decision,
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                    settings=settings,
                ):
                    sent.append("pushplus")
                else:
                    logger.warning("PushPlus 推送失败（不影响主流程）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("PushPlus 通知异常（不影响主流程）：%s", exc)

        return sent

    # ── auto trade ───────────────────────────────────────────────────────────

    def _auto_trade_options(self) -> dict:
        settings = self._cfg.settings
        cfg = getattr(settings, "auto_trade", None)
        opts: dict[str, Any] = {}
        if cfg is not None:
            opts = {
                "enabled": bool(getattr(cfg, "enabled", False)),
                "dry_run": bool(getattr(cfg, "dry_run", True)),
                "min_confidence": int(getattr(cfg, "min_confidence", 60)),
                "max_notional_usdt": float(getattr(cfg, "max_notional_usdt", 50.0)),
                "max_daily_orders": int(getattr(cfg, "max_daily_orders", 10)),
                "allowed_symbols": list(getattr(cfg, "allowed_symbols", None) or []),
                "require_stop_loss": bool(getattr(cfg, "require_stop_loss", True)),
                # 下单参数（与桌面版「下单参数条」共用同一份 settings）
                "market_type": str(getattr(cfg, "market_type", "spot") or "spot"),
                "margin_per_trade_usdt": float(
                    getattr(cfg, "margin_per_trade_usdt", 10.0) or 0.0
                ),
                "leverage": int(getattr(cfg, "leverage", 1) or 1),
                "order_type": str(getattr(cfg, "order_type", "MARKET") or "MARKET"),
                "margin_mode": str(getattr(cfg, "margin_mode", "isolated") or "isolated"),
                "max_price_deviation_pct": float(
                    getattr(cfg, "max_price_deviation_pct", 0.3) or 0.0
                ),
            }
        venue = str(getattr(cfg, "venue", "") or "binance").lower() or "binance"
        opts["venue"] = venue
        creds = getattr(settings, venue, None)
        if creds is not None:
            opts["api_key"] = str(getattr(creds, "api_key", "") or "")
            opts["api_secret"] = str(getattr(creds, "api_secret", "") or "")
            opts["passphrase"] = str(getattr(creds, "passphrase", "") or "")
            opts["demo"] = bool(getattr(creds, "demo", False))
            opts["testnet"] = bool(getattr(creds, "testnet", False))
        return opts

    def _maybe_auto_trade(self, inner: dict) -> str:
        opts = self._auto_trade_options()
        if not opts.get("enabled"):
            return ""
        try:
            from pa_agent.trading.auto_trader import AutoTrader

            venue = str(opts.pop("venue", "binance") or "binance")
            trader = AutoTrader(opts, venue=venue)
            last_price = None
            close = inner.get("entry_price")
            try:
                if close is not None:
                    last_price = float(close)
            except (TypeError, ValueError):
                last_price = None
            outcome = trader.evaluate(
                inner,
                symbol=self._cfg.symbol,
                timeframe=self._cfg.timeframe,
                last_price=last_price,
            )
            logger.info(
                "自动交易：executed=%s dry_run=%s reason=%s",
                outcome.executed,
                outcome.dry_run,
                outcome.reason,
            )
            return f"{outcome.reason}" + ("（试运行）" if outcome.dry_run and outcome.executed else "")
        except Exception as exc:  # noqa: BLE001 - trading must never kill the loop
            logger.warning("自动交易异常（不影响主流程）：%s", exc)
            return f"error: {exc}"

    # ── health ───────────────────────────────────────────────────────────────

    def health_check(self) -> tuple[bool, list[str]]:
        """Verify config + market data + notifier wiring without running an analysis."""
        problems: list[str] = []
        cfg = self._cfg

        if not cfg.ai_key_configured():
            problems.append("AI API Key 未配置（provider.api_key）")
        if cfg.auto_trade_enabled():
            auto = cfg.settings.auto_trade
            venue = str(getattr(auto, "venue", "binance") or "binance")
            creds = getattr(cfg.settings, venue, None)
            if not str(getattr(creds, "api_key", "") or ""):
                problems.append(f"自动交易已启用，但 {venue} 的 API Key 为空")

        try:
            from pa_agent.data.factory import create_data_source

            source = create_data_source(cfg.data_source)
            source.connect()
            source.subscribe(cfg.symbol, cfg.timeframe)
            bars = source.latest_snapshot(cfg.bar_count + INDICATOR_WARMUP_BARS + 5)
            if not bars:
                problems.append(f"{cfg.data_source} 返回空K线（{cfg.symbol} {cfg.timeframe}）")
            else:
                logger.info(
                    "行情自检通过：%s %s 共 %d 根，最新 %s",
                    cfg.symbol,
                    cfg.timeframe,
                    len(bars),
                    bars[0].ts_open,
                )
            source.disconnect()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"行情自检失败：{exc}")

        try:
            from pa_agent.notify.dingtalk_notifier import dingtalk_is_active

            if not dingtalk_is_active(cfg.settings):
                logger.info("钉钉通知未启用（可选）")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"钉钉模块不可用：{exc}")

        if (
            str(getattr(cfg.settings.dingtalk, "webhook_url", "") or "")
            and not str(getattr(cfg.settings.dingtalk, "secret", "") or "")
        ):
            logger.warning("钉钉 Webhook 已填但未填加签密钥：请确认机器人安全设置允许无加签访问。")

        return (not problems), problems

    def print_health(self, ok: bool, problems: list[str]) -> None:
        cfg = self._cfg
        print("=" * 66)
        print(f"PA Agent 守护进程自检  ·  {cfg.describe()}")
        print("-" * 66)
        if ok:
            print("结果：健康 ✅")
        else:
            print("结果：发现问题 ❌")
            for item in problems:
                print(f"  · {item}")
        if self._last_outcome is not None:
            print(f"最近一次分析：ran={self._last_outcome.ran} reason={self._last_outcome.reason}")
        print("=" * 66)

    # ── read-only introspection (used by tests) ──────────────────────────────

    @property
    def last_outcome(self) -> AnalysisOutcome | None:
        return self._last_outcome

    @property
    def project_root(self):
        return PROJECT_ROOT
