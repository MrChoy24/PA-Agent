"""Centralised logging configuration for PA Agent.

Public API
----------
configure_logging(api_key: str = "") -> None
update_api_key(new_key: str) -> None
verify_logging_handlers() -> bool
"""
from __future__ import annotations

import copy
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from typing import List

from pa_agent.config.paths import LOG_FILE_PATH
from pa_agent.util.mask_secret import mask_secret

# ── Module-level state ────────────────────────────────────────────────────────

_active_formatters: List["MaskingFormatter"] = []
_configured: bool = False

# ── MaskingFormatter ──────────────────────────────────────────────────────────


class MaskingFormatter(logging.Formatter):
    """Logging formatter that replaces the plaintext API key with its masked form."""

    def __init__(self, fmt: str, api_key: str = "") -> None:
        super().__init__(fmt)
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        message = super().format(record)
        if self._api_key:
            message = message.replace(self._api_key, mask_secret(self._api_key))
        return message

    def set_api_key(self, new_key: str) -> None:
        self._api_key = new_key


class DropRateLimitExhaustedFilter(logging.Filter):
    """Drop console output for rate-limit give-up lines entirely.

    When a provider exhausts its retries the console used to print a
    neutral line ("本次分析暂未完成，请稍后重试。"). Even that is unwanted:
    the analysis just fails silently for the user, and the full 429 detail
    stays in the log file for diagnostics. Only the give-up line is
    dropped - transient retry warnings keep their neutral wording.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        lowered = message.lower()
        is_rate_limited = (
            "限流重试" in message
            or "触发限流" in message
            or "insufficient_quota" in lowered
            or "rate_limit_error" in lowered
            or "inference exceeds tpm/rpm" in lowered
        )
        if not is_rate_limited:
            return True
        # Only silence the terminal "gave up" line, not the retry progress.
        return "放弃本轮" not in message


#: Modules whose output is internal spare-channel diagnostics (which model
#: answered, prompt/completion counts, latency). The console window must show
#: only the single neutral progress line, so these records are dropped there
#: entirely; the log file keeps every detail for diagnosis.
_SPARE_DIAGNOSTIC_LOGGERS = (
    "pa_agent.ai.lite_fallback",
    "pa_agent.ai.glm_backup",
    "pa_agent.ai.glm_flash_lite",
    "pa_agent.orchestrator.free_chat",
)

#: Message fragments that identify spare-channel diagnostics regardless of the
#: logger name (e.g. after a module rename).
_SPARE_DIAGNOSTIC_MARKERS = (
    "lite spare channel",
    "lite fallback",
    "spare-channel",
    "free-chat primary provider failed",
    "free-chat spare-model",
    "free-chat spare channel",
    "free-chat follow-up answered",
    # 内部降级细节一律不进控制台。
    "free fallback triggered",
    "free fallback exhausted",
    "free fallback aborted",
    "已由备用链路接管",
)


class DropSpareChannelDiagnosticsFilter(logging.Filter):
    """Keep spare-channel internals out of the user-facing console."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if record.name in _SPARE_DIAGNOSTIC_LOGGERS:
            return False
        try:
            message = record.getMessage().lower()
        except Exception:  # noqa: BLE001
            return True
        return not any(marker in message for marker in _SPARE_DIAGNOSTIC_MARKERS)


#: 账号级故障（欠费/余额不足/凭据失效）在控制台上的统一文案。
#: 刻意不提渠道名与 Key，但要明确指出「要你去充值或换 Key」——
#: 否则用户只能看到「正在进行分析，请稍等」，永远排不出原因。
_ACCOUNT_PROBLEM_TEXT = (
    "AI 服务账户余额不足或凭据失效，已切换备用通道；请登录对应平台充值或更换 API Key。"
)

#: 与 ``pa_agent.ai.ark_gateway.is_account_level_error`` 保持一致的兜底判定。
#: 这里不 import 那个模块，避免 logging 在极早期被配置时引入重依赖。
_ACCOUNT_PROBLEM_MARKERS = (
    "余额不足",
    "无可用资源包",
    "请充值",
    "accountoverdue",
    "overdue balance",
    "account has been suspended",
    "no available resource package",
    "insufficient balance",
    "insufficient_quota",  # noqa: E501 - marker literal
    "配额不足",  # noqa: E501 - marker literal
    "令牌已过期或验证不正确",
)


def _looks_like_account_problem(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker.lower() in lowered for marker in _ACCOUNT_PROBLEM_MARKERS)


#: 同一次故障常常被两条日志命中（限流层一条、客户端 stream error 一条），
#: 映射成同一句中文后在控制台上就是重复两遍。窗口内只提示一次。
_ACCOUNT_NOTICE_COOLDOWN_S = 30.0
_acct_lock = threading.Lock()
_acct_last: float = 0.0


def _account_notice_allowed(now: float | None = None) -> bool:
    """True 表示这条账号级提示还能打印；冷却窗口内重复的返回 False。"""
    global _acct_last
    cur = time.monotonic() if now is None else float(now)
    with _acct_lock:
        if _acct_last and cur - _acct_last < _ACCOUNT_NOTICE_COOLDOWN_S:
            return False
        _acct_last = cur
        return True


def reset_account_notice_state() -> None:
    """测试用：清掉去重窗口。"""
    global _acct_last
    with _acct_lock:
        _acct_last = 0.0


class DropDuplicateAccountNoticeFilter(logging.Filter):
    """Account-level faults are reported once per burst, not once per logger."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if not _looks_like_account_problem(message):
            return True
        return _account_notice_allowed()


class ConsoleFormatter(MaskingFormatter):
    """Neutral console progress; keep the original record for file diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # 账号级故障（欠费 / 余额不足 / 凭据失效）必须让用户看见：
        # 这是唯一能让他去充值或换 Key 的线索，压成「请稍等」等于让他永远查不出原因。
        # 判定放在限流之前——智谱余额不足的 HTTP 状态码也是 429。
        # 去重由 DropDuplicateAccountNoticeFilter 在 handler 层完成。
        if _looks_like_account_problem(message):
            public_record = copy.copy(record)
            public_record.name = "pa_agent.analysis"
            public_record.levelno = logging.WARNING
            public_record.levelname = "WARNING"
            public_record.msg = _ACCOUNT_PROBLEM_TEXT
            public_record.args = ()
            public_record.exc_info = None
            public_record.exc_text = None
            public_record.stack_info = None
            return super().format(public_record)
        is_backup = (
            record.name == "pa_agent.ai.sensenova_fallback"
            or "SenseNova free fallback" in message
            or "switching to fallback model=" in message
            or "provider fallback" in message
            or "model=glm-4-flash " in message
            or "model=glm-4-flash)" in message
        )
        # Rate-limit / quota messages must not reach the console: they are
        # noise for the end user and can expose account state (insufficient
        # quota). The file handler keeps the original text for diagnostics.
        _lowered = message.lower()
        is_rate_limited = (
            "限流重试" in message
            or "触发限流" in message
            or "insufficient_quota" in _lowered
            or "rate_limit_error" in _lowered
            or "inference exceeds tpm/rpm" in _lowered
            # Other gateways phrase the same condition differently:
            #   "tpm exhausted" / "quota_exceeded_error" / bare "429".
            or "quota_exceeded" in _lowered
            or "tpm exhausted" in _lowered
            or "rate limit" in _lowered
            or "429" in _lowered
        )
        if not is_backup and not is_rate_limited:
            return super().format(record)

        # Never mutate a shared LogRecord: the file handler must retain details.
        public_record = copy.copy(record)
        public_record.name = "pa_agent.analysis"
        # A transient provider hiccup that the fallback chain goes on to answer
        # is not an error for the user: show it as ordinary progress instead of
        # an ERROR line in the console window.
        if public_record.levelno >= logging.ERROR:
            public_record.levelno = logging.INFO
            public_record.levelname = "INFO"
        if is_rate_limited and not is_backup:
            # Give-up lines are dropped upstream by DropRateLimitExhaustedFilter;
            # anything reaching here is a transient retry notice.
            text = "正在进行分析，请稍等。"
        elif "empty content" in message:
            text = "本次请求未完成。"
        elif "update failed" in message:
            text = "正在重试。"
        elif "triggered" in message or "switching to fallback" in message:
            text = "正在进行分析，请稍等。"
        elif "已由备用链路接管" in message:
            text = "分析已完成，结果已通过校验。"
        elif "exhausted" in message:
            text = "本阶段分析未完成。"
        elif "aborted" in message:
            text = "本阶段分析未完成。"
        elif "listing failed" in message:
            text = "服务暂时不可用，请稍后重试。"
        elif "validation failed" in message:
            text = "本次返回未通过校验，正在重试。"
        elif "miss:" in message:
            text = "本次请求未成功，正在重试，请稍等。"
        else:
            text = "正在进行分析，请稍等。"
        public_record.msg = text
        public_record.args = ()
        public_record.exc_info = None
        public_record.exc_text = None
        public_record.stack_info = None
        return super().format(public_record)


# ── Public functions ──────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_THIRD_PARTY_LOGGERS = ("urllib3", "openai", "httpx")

# 曾经 tvdatafeed 用 logging.getLogger("root") 打 websocket DEBUG，需要压静音。
# 但 logging.getLogger("root") 返回的就是**根 logger 本身**（root.name == "root"），
# 把它放进这个列表就等于在 configure_logging() 末尾把根日志级别从 DEBUG 改回 WARNING，
# 结果是整个程序的 INFO 日志（行情就绪、守护进程启动、分析完成…）全部不再落盘。
# tvdatafeed 已随 TradingView 数据源一起移除，这里不再需要它；如需压静音请改用
# 按 logger 前缀过滤的 Filter，绝不要动 "root"。
_QUIET_LOGGER_NAMES = (
    "urllib3",
    "openai",
    "httpx",
    "httpcore",
    "httpcore2",
    "websocket",
)


def verify_logging_handlers() -> bool:
    """Return True when the expected rotating file handler is attached to root."""
    expected = str(LOG_FILE_PATH.resolve())
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            base = getattr(handler, "baseFilename", "")
            if str(Path(base).resolve()) == expected:
                return True
    return False


def configure_logging(api_key: str = "") -> None:
    """Configure the root logger with rotating file handler and console handler.

    Both handlers use MaskingFormatter that replaces api_key with mask_secret(api_key).
    Third-party loggers (urllib3, openai, httpx) are also attached to the same handlers.

    If handlers were removed after a prior configure_logging call, re-attaches them.
    """
    global _configured  # noqa: PLW0603

    if _configured:
        if api_key:
            update_api_key(api_key)
        if verify_logging_handlers():
            return
        # Handlers missing (e.g. external code cleared root.handlers) — re-install.
        _configured = False

    # Build formatters
    file_formatter = MaskingFormatter(_LOG_FORMAT, api_key=api_key)
    console_formatter = ConsoleFormatter(_LOG_FORMAT, api_key=api_key)

    # Track all active formatters so update_api_key can reach them
    _active_formatters.clear()
    _active_formatters.append(file_formatter)
    _active_formatters.append(console_formatter)

    # Rotating file handler
    file_handler: logging.Handler
    try:
        LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
    except OSError as exc:
        # 容器里 logs/ 目录可能不可写（属主 root、进程是非特权用户）。
        # 日志不该成为启动失败的借口：退回只打 stdout，交给 docker logs 收集。
        print(
            f"[pa_agent] 无法写入日志文件 {LOG_FILE_PATH}（{exc}）；"
            "本次只输出到标准输出。若在 Docker 中，请执行 "
            "chown -R 10001:10001 logs",
            file=sys.stderr,
        )
        file_handler = logging.StreamHandler()
    file_handler.setFormatter(file_formatter)

    # Console (stream) handler — INFO+ only; file keeps DEBUG for troubleshooting
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    # Never print the rate-limit give-up line to the user-facing console.
    console_handler.addFilter(DropRateLimitExhaustedFilter())
    # Nor any spare-channel internals (model name, token counts, latency).
    console_handler.addFilter(DropSpareChannelDiagnosticsFilter())
    # 同一次欠费会从两个 logger 各打一条；把它们合并成控制台上的一句。
    console_handler.addFilter(DropDuplicateAccountNoticeFilter())

    handlers: list[logging.Handler] = [file_handler, console_handler]

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Remove any previously installed handlers to avoid duplicates on re-call
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    for h in handlers:
        root_logger.addHandler(h)

    # Attach the same handlers to third-party loggers
    for name in _THIRD_PARTY_LOGGERS:
        tp_logger = logging.getLogger(name)
        for h in list(tp_logger.handlers):
            tp_logger.removeHandler(h)
        for h in handlers:
            tp_logger.addHandler(h)
        # Prevent double-logging via root propagation
        tp_logger.propagate = False

    _silence_noisy_libraries()

    _configured = True
    logging.getLogger("pa_agent.diagnostics").info(
        "configure_logging: handlers attached (log_file=%s)", LOG_FILE_PATH
    )


def _silence_noisy_libraries() -> None:
    """Turn down chatty third-party DEBUG loggers (tvdatafeed websocket spam)."""
    for name in _QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


def update_api_key(new_key: str) -> None:
    """Update the masking key in all active MaskingFormatter instances."""
    for formatter in _active_formatters:
        formatter.set_api_key(new_key)
