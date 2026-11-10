"""CLI entry point for the headless daemon.

    python -m pa_agent.daemon --health-check    # 自检，退出码 0 = 健康
    python -m pa_agent.daemon --once            # 跑一轮就退出（冒烟）
    python -m pa_agent.daemon                   # 常驻

Also usable from the installed console script ``pa-daemon``.
"""
from __future__ import annotations

import logging
import sys

from pa_agent.daemon.config import ConfigError, DaemonConfig, config_from_args

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        cfg, args = config_from_args(argv)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    # Logging must be configured after we know the level, but before build().
    from pa_agent.util.logging import configure_logging

    try:
        configure_logging(api_key=cfg.settings.provider.api_key)
    except Exception as exc:  # noqa: BLE001 - logging is best-effort on servers
        print(f"[警告] 日志初始化失败：{exc}", file=sys.stderr)
    _apply_log_level(cfg.log_level)

    from pa_agent.daemon.runner import DaemonRunner

    runner = DaemonRunner(cfg)
    runner.install_signal_handlers()

    if getattr(args, "health_check", False):
        ok, problems = runner.health_check()
        runner.print_health(ok, problems)
        return 0 if ok else 1

    try:
        runner.build()
    except Exception as exc:  # noqa: BLE001
        logger.error("启动失败：%s", exc)
        print(f"[启动失败] {exc}", file=sys.stderr)
        return 1

    if getattr(args, "dry_run_trade", False):
        cfg.settings.auto_trade.dry_run = True
        logger.warning("--dry-run-trade 生效：自动交易强制走试运行")

    try:
        if getattr(args, "once", False):
            outcome = runner.tick(force=True)
            print(f"[once] ran={outcome.ran} reason={outcome.reason}")
            if outcome.decision:
                d = outcome.decision
                print(
                    f"[once] order_type={d.get('order_type', '—')} "
                    f"confidence={d.get('trade_confidence', '—')} "
                    f"direction={d.get('direction', '—')}"
                )
                if outcome.notified:
                    print(f"[once] 已推送：{', '.join(outcome.notified)}")
                if outcome.trade_reason:
                    print(f"[once] 自动交易：{outcome.trade_reason}")
            return 0 if outcome.ran else 3
        runner.run_forever()
        return 0
    finally:
        runner.shutdown()


def _apply_log_level(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        # Keep the file handler verbose; only raise the console threshold.
        if isinstance(handler, logging.StreamHandler) and not hasattr(
            handler, "baseFilename"
        ):
            handler.setLevel(level)
    logging.getLogger("pa_agent").setLevel(level)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
