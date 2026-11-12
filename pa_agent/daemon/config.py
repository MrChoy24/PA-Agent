"""Runtime configuration for the headless daemon.

Two layers, in increasing priority:

1. ``config/settings.json`` — the very same file the desktop GUI writes, so a
   user can configure the AI provider / exchange keys / DingTalk webhook /
   auto-trade gates once, in either UI or server.
2. Environment variables / CLI flags — for the handful of knobs that only make
   sense on a server (which symbol, how often, log level, data directory).

Nothing here imports PyQt6.
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import (
    Settings,
    load_settings,
    provider_api_key_configured,
)

logger = logging.getLogger(__name__)

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_TIMEFRAME = "15m"

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(RuntimeError):
    """Raised when the daemon cannot start with the given configuration."""


def _env_str(name: str, default: str = "") -> str:
    return str(os.environ.get(name, "") or "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，回退默认值 %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字，回退默认值 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    logger.warning("环境变量 %s=%r 不是布尔值，回退默认值 %s", name, raw, default)
    return default


#: Env vars checked (in order) for the LLM API key.  On a server it is much
#: safer to inject the key as a Docker secret / env var than to bake it into
#: config/settings.json and copy that file onto the host.
_API_KEY_ENV_VARS = ("PA_AGENT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
_API_BASE_ENV_VARS = ("PA_AGENT_API_BASE", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL")
_API_MODEL_ENV_VARS = ("PA_AGENT_MODEL",)

#: 智谱 glm-4-flash 备用通道的 Key（``provider.backup_glm_api_key``）。
#: 它只在主通道是 SenseNova 网关或火山方舟时才会被挂到 fallbacks 链上，
#: 见 ``pa_agent.ai.glm_backup.ensure_glm_fallback``。
_GLM_KEY_ENV_VARS = ("PA_AGENT_GLM_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY")

#: Exchange credentials.  These live in their own settings.json sections
#: (``binance`` / ``okx``) and previously had no environment channel at all,
#: which meant a containerised deployment *had* to mount a real settings.json
#: containing plaintext keys.  Env vars remove that requirement.
_BINANCE_KEY_ENV_VARS = ("BINANCE_API_KEY",)
_BINANCE_SECRET_ENV_VARS = ("BINANCE_API_SECRET", "BINANCE_SECRET_KEY")
_BINANCE_TESTNET_ENV_VARS = ("BINANCE_TESTNET",)
_OKX_KEY_ENV_VARS = ("OKX_API_KEY",)
_OKX_SECRET_ENV_VARS = ("OKX_API_SECRET", "OKX_SECRET_KEY")
_OKX_PASSPHRASE_ENV_VARS = ("OKX_PASSPHRASE", "OKX_API_PASSPHRASE")
_OKX_DEMO_ENV_VARS = ("OKX_DEMO", "OKX_SIMULATED")

#: One env var per auto-trade field, so a server can flip the gates without
#: editing settings.json.  PA_DAEMON_AUTO_TRADE is deliberately separate from
#: the dry-run flag: you must turn it on explicitly AND leave dry-run on to
#: rehearse, and turning off dry-run additionally requires a real key.
_AUTO_TRADE_ENV_VARS = ("PA_DAEMON_AUTO_TRADE",)
_AUTO_DRY_RUN_ENV_VARS = ("PA_DAEMON_DRY_RUN",)
_AUTO_VENUE_ENV_VARS = ("PA_DAEMON_VENUE",)


def _first_env(names: tuple[str, ...]) -> tuple[str, str]:
    for name in names:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return name, value
    return "", ""


def apply_provider_env_overrides(settings: Settings) -> list[str]:
    """Overlay LLM credentials from environment variables onto *settings*.

    Returns the list of applied variable names so the caller can log which
    source won (never the values).
    """
    applied: list[str] = []
    provider = settings.provider

    name, value = _first_env(_API_KEY_ENV_VARS)
    if name:
        provider.api_key = value
        provider.api_key_encrypted = ""
        applied.append(name)

    name, value = _first_env(_API_BASE_ENV_VARS)
    if name:
        provider.base_url = value
        applied.append(name)

    name, value = _first_env(_API_MODEL_ENV_VARS)
    if name:
        provider.model = value
        applied.append(name)

    name, value = _first_env(_GLM_KEY_ENV_VARS)
    if name:
        provider.backup_glm_api_key = value
        applied.append(name)

    return applied


def _notify_active(channel: str, settings: Settings) -> bool:
    """通知通道是否真的会发出去（含环境变量兜底的 webhook/token）。

    ``settings.<channel>.enabled`` 只反映配置文件。两个通道都允许用环境变量
    提供 webhook/token（见 ``pa_agent/notify/*_notifier.py``），此时
    ``*_is_active()`` 会把它视为已启用。日志若只看 settings，就会出现
    「dingtalk=off」却在同一轮推送成功的自相矛盾记录。
    """
    try:
        if channel == "dingtalk":
            from pa_agent.notify.dingtalk_notifier import dingtalk_is_active

            return bool(dingtalk_is_active(settings))
        from pa_agent.notify.pushplus_notifier import pushplus_is_active

        return bool(pushplus_is_active(settings))
    except Exception:  # noqa: BLE001 - 通知模块不可用时退回配置值
        section = getattr(settings, channel, None)
        return bool(getattr(section, "enabled", False))


def apply_fallback_env_overrides(settings: Settings) -> list[str]:
    """把 ``.env`` 里的 ``PA_AGENT_FALLBACKS`` 解析成备用池。

    环境变量里写了备用池就**整段替换** ``settings.ai_backups``（而不是追加）：
    .env 是服务器上唯一该被手改的地方，若它和文件里的条目混在一起，用户改完
    根本说不清最终链是什么。不设该变量则完全不动 settings.json 的内容。

    返回被应用的变量名（供启动日志打印来源），绝不返回 Key。
    """
    from pa_agent.ai.extra_providers import FALLBACK_SPEC_ENV, parse_fallback_spec

    raw = str(os.environ.get(FALLBACK_SPEC_ENV, "") or "").strip()
    if not raw:
        return []
    entries, problems = parse_fallback_spec(
        raw, default_key=str(settings.provider.api_key or "").strip()
    )
    for problem in problems:
        logger.warning("%s %s，已跳过", FALLBACK_SPEC_ENV, problem)
    if not entries:
        logger.warning(
            "%s 已设置但没有任何可解析的条目，继续使用 settings.json 里的备用池",
            FALLBACK_SPEC_ENV,
        )
        return []
    settings.ai_backups = entries
    logger.info(
        "%s 覆盖了备用池：%d 条（%s）",
        FALLBACK_SPEC_ENV,
        len(entries),
        ", ".join(str(getattr(e, "name", "") or e.model) for e in entries),
    )
    return [FALLBACK_SPEC_ENV]


def apply_exchange_env_overrides(settings: Settings) -> list[str]:
    """Overlay Binance / OKX credentials from environment variables.

    Only *non-empty* variables override the file, so a deployment that keeps
    using ``config/settings.json`` is unaffected.  Returns the names of the
    variables that won (never the values).
    """
    applied: list[str] = []

    binance = settings.binance
    name, value = _first_env(_BINANCE_KEY_ENV_VARS)
    if name:
        binance.api_key = value
        applied.append(name)
    name, value = _first_env(_BINANCE_SECRET_ENV_VARS)
    if name:
        binance.api_secret = value
        applied.append(name)
    if str(os.environ.get("BINANCE_TESTNET", "") or "").strip():
        binance.testnet = _env_bool("BINANCE_TESTNET", binance.testnet)
        applied.append("BINANCE_TESTNET")

    okx = settings.okx
    name, value = _first_env(_OKX_KEY_ENV_VARS)
    if name:
        okx.api_key = value
        applied.append(name)
    name, value = _first_env(_OKX_SECRET_ENV_VARS)
    if name:
        okx.api_secret = value
        applied.append(name)
    name, value = _first_env(_OKX_PASSPHRASE_ENV_VARS)
    if name:
        okx.passphrase = value
        applied.append(name)
    if str(os.environ.get("OKX_DEMO", "") or "").strip() or str(
        os.environ.get("OKX_SIMULATED", "") or ""
    ).strip():
        for var in _OKX_DEMO_ENV_VARS:
            if str(os.environ.get(var, "") or "").strip():
                okx.demo = _env_bool(var, okx.demo)
                applied.append(var)
                break

    return applied


def apply_auto_trade_env_overrides(settings: Settings) -> list[str]:
    """Overlay the auto-trade switches and order sizing from the environment.

    Switches (``enabled`` / ``dry_run`` / ``venue``) plus the *order sizing*
    fields the user configures on the panel — margin per trade, leverage,
    order type, market type, margin mode, notional cap. Every value is
    validated: an out-of-range or unrecognised entry is logged and ignored
    instead of silently loosening risk controls.

    Hard safety ceilings are **not** overridable from the environment
    (``min_confidence``, ``max_daily_orders``, ``require_stop_loss``,
    ``allowed_symbols``, ``max_price_deviation_pct``): those are the last
    line of defence, and a typo in ``.env`` must not be able to disable them.
    """
    applied: list[str] = []
    auto = settings.auto_trade

    if str(os.environ.get("PA_DAEMON_AUTO_TRADE", "") or "").strip():
        auto.enabled = _env_bool("PA_DAEMON_AUTO_TRADE", auto.enabled)
        applied.append("PA_DAEMON_AUTO_TRADE")
    if str(os.environ.get("PA_DAEMON_DRY_RUN", "") or "").strip():
        auto.dry_run = _env_bool("PA_DAEMON_DRY_RUN", auto.dry_run)
        applied.append("PA_DAEMON_DRY_RUN")
    venue = str(os.environ.get("PA_DAEMON_VENUE", "") or "").strip().lower()
    if venue:
        if venue in ("binance", "okx"):
            auto.venue = venue
            applied.append("PA_DAEMON_VENUE")
        else:
            logger.warning(
                "环境变量 PA_DAEMON_VENUE=%r 无效（只能是 binance / okx），已忽略", venue
            )

    # 每单保证金（合约逐仓模式下的下单金额，单位 USDT）
    margin = str(os.environ.get("PA_DAEMON_MARGIN_PER_TRADE", "") or "").strip()
    if margin:
        try:
            value = float(margin)
        except ValueError:
            logger.warning("环境变量 PA_DAEMON_MARGIN_PER_TRADE=%r 不是数字，已忽略", margin)
        else:
            if value > 0:
                auto.margin_per_trade_usdt = value
                applied.append("PA_DAEMON_MARGIN_PER_TRADE")
            else:
                logger.warning(
                    "环境变量 PA_DAEMON_MARGIN_PER_TRADE=%r 必须大于 0，已忽略", margin
                )

    # 名义金额上限（现货 / 全仓模式下的下单金额，单位 USDT）
    notional = str(os.environ.get("PA_DAEMON_MAX_NOTIONAL", "") or "").strip()
    if notional:
        try:
            value = float(notional)
        except ValueError:
            logger.warning("环境变量 PA_DAEMON_MAX_NOTIONAL=%r 不是数字，已忽略", notional)
        else:
            if value > 0:
                auto.max_notional_usdt = value
                applied.append("PA_DAEMON_MAX_NOTIONAL")
            else:
                logger.warning(
                    "环境变量 PA_DAEMON_MAX_NOTIONAL=%r 必须大于 0，已忽略", notional
                )

    # 杠杆倍数（1~125，与面板 OrderParamsBar 一致）
    leverage = str(os.environ.get("PA_DAEMON_LEVERAGE", "") or "").strip()
    if leverage:
        try:
            value = int(float(leverage))
        except ValueError:
            logger.warning("环境变量 PA_DAEMON_LEVERAGE=%r 不是整数，已忽略", leverage)
        else:
            if 1 <= value <= 125:
                auto.leverage = value
                applied.append("PA_DAEMON_LEVERAGE")
            else:
                logger.warning(
                    "环境变量 PA_DAEMON_LEVERAGE=%r 超出 1~125，已忽略", leverage
                )

    market = str(os.environ.get("PA_DAEMON_MARKET_TYPE", "") or "").strip().lower()
    if market:
        if market in ("spot", "futures"):
            auto.market_type = market
            applied.append("PA_DAEMON_MARKET_TYPE")
        else:
            logger.warning(
                "环境变量 PA_DAEMON_MARKET_TYPE=%r 无效（只能是 spot / futures），已忽略",
                market,
            )

    order_type = str(os.environ.get("PA_DAEMON_ORDER_TYPE", "") or "").strip().upper()
    if order_type:
        if order_type in ("MARKET", "LIMIT"):
            auto.order_type = order_type
            applied.append("PA_DAEMON_ORDER_TYPE")
        else:
            logger.warning(
                "环境变量 PA_DAEMON_ORDER_TYPE=%r 无效（只能是 MARKET / LIMIT），已忽略",
                order_type,
            )

    margin_mode = str(os.environ.get("PA_DAEMON_MARGIN_MODE", "") or "").strip().lower()
    if margin_mode:
        if margin_mode in ("isolated", "cross"):
            auto.margin_mode = margin_mode
            applied.append("PA_DAEMON_MARGIN_MODE")
        else:
            logger.warning(
                "环境变量 PA_DAEMON_MARGIN_MODE=%r 无效（只能是 isolated / cross），已忽略",
                margin_mode,
            )

    return applied


@dataclass(slots=True)
class DaemonConfig:
    """Effective daemon configuration (already merged: settings.json + env)."""

    settings: Settings
    settings_path: Path

    symbol: str = DEFAULT_SYMBOL
    timeframe: str = DEFAULT_TIMEFRAME
    data_source: str = ""
    bar_count: int = 100

    #: How often to poll the exchange for new bars, in seconds.
    poll_interval_s: float = 20.0
    #: Minimum spacing between two analyses (protects the AI token budget).
    analysis_cooldown_s: float = 120.0
    #: Skip analysis when an identical closed-bar snapshot was already analysed.
    skip_duplicate_bars: bool = True
    #: Only analyse after the newest bar has actually closed (recommended).
    wait_bar_close: bool = True
    #: Analyse once right after startup instead of waiting for the next close.
    analyze_on_start: bool = False
    #: Enable incremental analysis (carry the previous decision forward).
    incremental: bool = True
    incremental_threshold: int = 10

    log_level: str = "INFO"

    #: 每个配置项最终取自哪里：``env`` / ``cli`` / ``settings`` / ``default``。
    #: 用户在服务器上改完 .env 却「没生效」时，这张表就是唯一能看到真相的地方，
    #: 所以它必须跟着配置一起打印出来，而不是让人去猜。
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def bar_seconds(self) -> int:
        return _timeframe_seconds(self.timeframe)

    # ── factories ────────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        *,
        settings_path: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> "DaemonConfig":
        """Merge settings.json with environment variables and explicit overrides."""
        path = Path(settings_path) if settings_path is not None else SETTINGS_JSON_PATH
        settings = load_settings(path)
        applied_env = apply_provider_env_overrides(settings)
        applied_env += apply_fallback_env_overrides(settings)
        applied_env += apply_exchange_env_overrides(settings)
        applied_env += apply_auto_trade_env_overrides(settings)
        if applied_env:
            logger.info("已从环境变量覆盖配置：%s", ", ".join(applied_env))
        general = settings.general

        # 每一项都记下来源：改 .env 却没生效时，与其让用户猜，不如直接打印。
        sources: dict[str, str] = {}

        def _pick(field_name: str, env_name: str, settings_value: Any, default: Any) -> Any:
            """env > settings.json > 内置默认值，并记录最终来源。"""
            raw = str(os.environ.get(env_name, "") or "").strip()
            if raw:
                sources[field_name] = "env"
                return raw
            if settings_value:
                sources[field_name] = "settings"
                return settings_value
            sources[field_name] = "default"
            return default

        data_source = str(
            _pick(
                "data_source",
                "PA_DAEMON_DATA_SOURCE",
                str(general.last_data_source or ""),
                "",
            )
        )
        symbol = str(
            _pick(
                "symbol",
                "PA_DAEMON_SYMBOL",
                str(general.last_symbol or ""),
                DEFAULT_SYMBOL,
            )
        )
        timeframe = str(
            _pick(
                "timeframe",
                "PA_DAEMON_TIMEFRAME",
                str(general.last_timeframe or ""),
                DEFAULT_TIMEFRAME,
            )
        )

        cfg = cls(
            settings=settings,
            settings_path=path,
            symbol=symbol,
            timeframe=timeframe,
            data_source=data_source,
            bar_count=_env_int("PA_DAEMON_BAR_COUNT", int(general.analysis_bar_count or 100)),
            poll_interval_s=_env_float("PA_DAEMON_POLL_INTERVAL", 20.0),
            analysis_cooldown_s=_env_float("PA_DAEMON_ANALYSIS_COOLDOWN", 120.0),
            skip_duplicate_bars=_env_bool("PA_DAEMON_SKIP_DUPLICATE", True),
            wait_bar_close=_env_bool("PA_DAEMON_WAIT_BAR_CLOSE", True),
            analyze_on_start=_env_bool("PA_DAEMON_ANALYZE_ON_START", False),
            incremental=_env_bool("PA_DAEMON_INCREMENTAL", True),
            incremental_threshold=_env_int(
                "PA_DAEMON_INCREMENTAL_THRESHOLD",
                int(general.incremental_max_new_bars or 10),
            ),
            log_level=_env_str("PA_DAEMON_LOG_LEVEL", "INFO").upper(),
            sources=sources,
        )

        # 剩下这些只有 env / 默认值两层，来源同样记下来。
        for field_name, env_name in (
            ("bar_count", "PA_DAEMON_BAR_COUNT"),
            ("poll_interval_s", "PA_DAEMON_POLL_INTERVAL"),
            ("analysis_cooldown_s", "PA_DAEMON_ANALYSIS_COOLDOWN"),
            ("skip_duplicate_bars", "PA_DAEMON_SKIP_DUPLICATE"),
            ("wait_bar_close", "PA_DAEMON_WAIT_BAR_CLOSE"),
            ("analyze_on_start", "PA_DAEMON_ANALYZE_ON_START"),
            ("incremental", "PA_DAEMON_INCREMENTAL"),
            ("incremental_threshold", "PA_DAEMON_INCREMENTAL_THRESHOLD"),
            ("log_level", "PA_DAEMON_LOG_LEVEL"),
        ):
            has_env = bool(str(os.environ.get(env_name, "") or "").strip())
            sources.setdefault(field_name, "env" if has_env else "default")

        if overrides:
            for key, value in overrides.items():
                if value is None:
                    continue
                if not hasattr(cfg, key):
                    raise ConfigError(f"未知配置项：{key}")
                setattr(cfg, key, value)
                if key in sources or key in ("symbol", "timeframe", "data_source"):
                    sources[key] = "cli"
        cfg.validate()
        return cfg

    # ── validation ───────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Fail fast on obviously broken configuration, before touching网络."""
        from pa_agent.data.factory import normalize_data_source_kind

        normalized = normalize_data_source_kind(self.data_source)
        if normalized not in ("binance", "okx"):
            raise ConfigError(
                f"无头守护进程只支持 binance / okx 数据源，当前为 {self.data_source!r}。"
                " 请设置 PA_DAEMON_DATA_SOURCE=binance 或 okx。"
            )
        self.data_source = normalized

        if not str(self.symbol).strip():
            raise ConfigError("品种不能为空（PA_DAEMON_SYMBOL）")
        if not str(self.timeframe).strip():
            raise ConfigError("周期不能为空（PA_DAEMON_TIMEFRAME）")

        if self.bar_count < 10:
            raise ConfigError(
                f"分析K线数量过小（{self.bar_count}），至少 10 根才能算出指标。"
            )
        if self.poll_interval_s < 1.0:
            raise ConfigError(
                f"轮询间隔过小（{self.poll_interval_s}s），至少 1 秒，否则会触发交易所限频。"
            )
        if self.analysis_cooldown_s < 0:
            raise ConfigError("分析冷却时间不能为负")
        if self.log_level not in _VALID_LOG_LEVELS:
            logger.warning("未知日志级别 %r，回退 INFO", self.log_level)
            self.log_level = "INFO"

    # ── derived checks ───────────────────────────────────────────────────────

    def ai_key_configured(self) -> bool:
        return provider_api_key_configured(self.settings)

    def auto_trade_enabled(self) -> bool:
        return bool(getattr(self.settings.auto_trade, "enabled", False))

    def describe(self) -> str:
        """One-line summary for the startup log (never contains secrets).

        ``dingtalk`` / ``pushplus`` 刻意走通知模块自己的判定（``*_is_active``），
        而不是只看 ``settings.*.enabled``：这两个通道都支持用环境变量提供
        webhook/token（见 ``pa_agent/notify/*_notifier.py``），此时 enabled
        会由 env 兜底置真。只看 settings 会打出「dingtalk=off」却在同一轮
        里成功推送，日志自相矛盾。
        """
        auto = self.settings.auto_trade
        venue = str(getattr(auto, "venue", "binance") or "binance")
        dry = bool(getattr(auto, "dry_run", True))
        ding = "on" if _notify_active("dingtalk", self.settings) else "off"
        push = "on" if _notify_active("pushplus", self.settings) else "off"
        return (
            f"source={self.data_source} symbol={self.symbol} tf={self.timeframe} "
            f"bars={self.bar_count} poll={self.poll_interval_s:g}s "
            f"cooldown={self.analysis_cooldown_s:g}s wait_close={self.wait_bar_close} "
            f"auto_trade={'on(' + venue + (', dry-run)' if dry else ', LIVE)') if auto.enabled else 'off'} "
            f"dingtalk={ding} pushplus={push}"
        )

    def describe_sources(self) -> str:
        """Which layer each key setting came from (env / cli / settings / default).

        用户改了 .env 却看不到效果时，这行日志能直接指出两件事：
        容器是不是根本没读到新变量（显示 settings），
        或者读到了但被别处覆盖了。
        """
        if not self.sources:
            return ""
        wanted = (
            "data_source",
            "symbol",
            "timeframe",
            "bar_count",
            "poll_interval_s",
            "analysis_cooldown_s",
            "wait_bar_close",
        )
        parts = [
            f"{name}={self.sources.get(name, '?')}"
            for name in wanted
            if name in self.sources
        ]
        cli = [name for name, src in self.sources.items() if src == "cli"]
        if cli:
            parts.append("cli_overrides=" + ",".join(sorted(cli)))
        return "配置来源：" + " ".join(parts)


def _timeframe_seconds(timeframe: str) -> int:
    from pa_agent.data.bar_close_wait import timeframe_to_seconds

    seconds = timeframe_to_seconds(timeframe)
    if not seconds:
        # Unknown timeframe: fall back to a conservative one hour so the loop
        # still works instead of refusing to start.
        return 3600
    return int(seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pa-daemon",
        description="PA Agent 无头守护进程：Binance/OKX 行情 → 两阶段 AI 分析 → 钉钉/PushPlus 推送 → 自动交易",
    )
    parser.add_argument("--symbol", help="交易对，如 BTCUSDT（默认读 settings.json 的 last_symbol）")
    parser.add_argument("--timeframe", help="周期，如 15m / 1h / 4h（默认读 settings.json）")
    parser.add_argument(
        "--data-source",
        dest="data_source",
        choices=("binance", "okx"),
        help="行情来源（默认读 settings.json 的 last_data_source）",
    )
    parser.add_argument("--bar-count", type=int, dest="bar_count", help="分析使用的K线根数")
    parser.add_argument(
        "--poll-interval",
        type=float,
        dest="poll_interval_s",
        help="轮询间隔秒数（默认 20）",
    )
    parser.add_argument(
        "--analysis-cooldown",
        type=float,
        dest="analysis_cooldown_s",
        help="两次分析的最小间隔秒数（默认 120）",
    )
    parser.add_argument(
        "--analyze-on-start",
        action="store_true",
        default=None,
        help="启动后立刻分析一次，而不是等下一根K线收盘",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮（拉行情 + 分析一次）后退出，用于冒烟测试",
    )
    parser.add_argument(
        "--dry-run-trade",
        action="store_true",
        help="强制自动交易走试运行（即使配置里是真实下单）",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="只做自检（配置/行情/通知），不通分析，退出码 0 表示健康",
    )
    parser.add_argument(
        "--settings",
        dest="settings_path",
        help="settings.json 路径（默认项目内 config/settings.json）",
    )
    parser.add_argument(
        "--log-level",
        choices=_VALID_LOG_LEVELS,
        help="日志级别（默认 INFO，也可用 PA_DAEMON_LOG_LEVEL）",
    )
    return parser


def config_from_args(argv: list[str] | None = None) -> tuple[DaemonConfig, argparse.Namespace]:
    """Parse CLI args and return the merged config plus the raw namespace."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {
        "symbol": (args.symbol or "").strip() or None,
        "timeframe": (args.timeframe or "").strip() or None,
        "data_source": (args.data_source or "").strip() or None,
        "bar_count": args.bar_count,
        "poll_interval_s": args.poll_interval_s,
        "analysis_cooldown_s": args.analysis_cooldown_s,
        "analyze_on_start": args.analyze_on_start,
        "log_level": (args.log_level or "").strip().upper() or None,
    }
    settings_path = Path(args.settings_path) if args.settings_path else None
    cfg = DaemonConfig.from_env(settings_path=settings_path, overrides=overrides)
    return cfg, args
