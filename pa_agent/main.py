"""Application entry point for PA Agent."""
from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    # Early diagnostics before Qt / heavy imports: crash dumps + file logging.
    from pa_agent.util.crash_diagnostics import enable_crash_diagnostics, log_startup_diagnostics
    from pa_agent.util.logging import configure_logging

    enable_crash_diagnostics()
    configure_logging()
    log_startup_diagnostics()

    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("PA Agent")

    from pa_agent.gui.theme import apply_theme
    apply_theme(app)

    logger.info("PA Agent starting up")

    # 单实例保护：与无头守护进程共用 settings.json，两个实例同时跑会导致
    # 重复下单与配置互相覆盖。锁只覆盖本机（daemon 在服务器上是另一把锁）。
    from pa_agent.util import instance_lock

    try:
        # acquire() 在构造时就把占用检查做完，异常在这里当场抛出。
        # （早期版本用 @contextmanager，异常只在 __enter__() 里抛，
        #  这个 try/except 完全接不住，用户只会看到一屏 traceback。）
        _lock_ctx = instance_lock.acquire("gui")
    except RuntimeError as exc:
        from PyQt6.QtWidgets import QMessageBox

        logger.error("启动被拒绝：%s", exc)
        QMessageBox.critical(
            None,
            "已有实例在运行",
            f"{exc}\n\n如果确认前任实例已经不在了，"
            f"可以手动删除锁文件：\n{instance_lock.default_lock_path()}",
        )
        return 1

    try:
        # Bootstrap all components (settings, data source, AI client, etc.)
        # GUI 侧是配置的写入方：role="gui" 允许落盘。
        from pa_agent.app_context import AppContext
        ctx = AppContext.bootstrap(role="gui")

        # Update logging with the real API key now that settings are loaded
        if ctx.settings is not None:
            from pa_agent.util.logging import configure_logging, update_api_key
            configure_logging(api_key=ctx.settings.provider.api_key)
            from pa_agent.util.crash_diagnostics import log_startup_diagnostics
            log_startup_diagnostics()

        # Build and show the main window
        from pa_agent.gui.main_window import MainWindow
        window = MainWindow(ctx)
        window.show()

        logger.info("Main window shown")
        return app.exec()
    finally:
        _lock_ctx.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
