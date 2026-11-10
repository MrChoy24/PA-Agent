"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal[
    "binance",
    "okx",
    "ashare",
    "akshare",
    "eastmoney_futures",
    "tushare",
]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore")

    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_key_encrypted: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "low"
    context_window: int = 2_000_000
    #: 备用 provider 链。主通道任何普通异常（非用户取消）都按顺序尝试；
    #: 每个元素是完整的 provider 配置。嵌套的 fallbacks 会被 extra="ignore" 丢弃，
    #: 因此不存在递归。
    fallbacks: list["AIProviderSettings"] = Field(default_factory=list)
    #: 备用链路的 api key（可选）。
    #: 由 glm_backup.ensure_glm_fallback() 自动挂到 fallbacks 链上。
    backup_glm_api_key: str = ""
    #: 显示名（只有「备用 AI 提供商」列表里的条目会用到）。
    name: str = ""
    #: 是否参与尝试（只有「备用 AI 提供商」列表里的条目会用到）。
    enabled: bool = True


AIProviderSettings.model_rebuild()


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""
    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=0, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""
    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "lenient"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Allow Stage 1 truncated JSON tail repair before failing syntax validation.
    disable_truncation_repair: bool = False
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 99_999_999.0
    last_data_source: DataSourceKind = "binance"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    last_symbol: str = "BTCUSDT"
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    #: 0 = 关闭该门槛（默认），任何通过其它检查的方案都按原样展示
    decision_confidence_threshold: int = Field(default=0, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        # MT5 / TradingView 数据源已移除：历史配置一律迁移到 Binance。
        if v in ("mt5", "tradingview"):
            return "binance"
        if v in ("yfinance", "eastmoney", "adata", "a_share"):
            return "ashare"
        if v == "tushare":
            return "tushare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


class TushareSettings(BaseModel):
    """Tushare Pro data source settings (persisted in ignored settings.json)."""
    model_config = ConfigDict(extra="ignore")

    token: str = ""


class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""


class BinanceSettings(BaseModel):
    """Binance 行情 / 账户 API 凭据（settings.json；行情为公开接口，无需 Key）。"""
    model_config = ConfigDict(extra="ignore")

    api_key: str = ""
    api_secret: str = ""
    #: True = 使用合约测试网域名（仅影响私有接口；公开行情仍走主网）
    testnet: bool = False


class OKXSettings(BaseModel):
    """OKX 行情 / 账户 API 凭据（settings.json；行情为公开接口，无需 Key）。"""
    model_config = ConfigDict(extra="ignore")

    api_key: str = ""
    api_secret: str = ""
    #: OKX 私有接口必须提供 passphrase
    passphrase: str = ""
    #: True = 模拟盘（请求头 x-simulated-trading: 1）
    demo: bool = False


class AutoTradeSettings(BaseModel):
    """自动交易总开关与风控上限（真实下单前请务必先用 dry_run 观察）。"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    #: True = 只记录"本应下单"的意图，不真正发单；建议先观察几天
    dry_run: bool = True
    #: 下单所用的交易所：binance / okx
    venue: str = "binance"
    #: 只有 AI 给出的置信度 ≥ 该值才下单
    min_confidence: int = 60
    #: 单笔最大名义金额（USDT）
    max_notional_usdt: float = 50.0
    #: 每日最多下单次数
    max_daily_orders: int = 10
    #: 白名单交易对；留空 = 不限制
    allowed_symbols: list[str] = Field(default_factory=list)
    #: True = 没有止损价就拒绝下单
    require_stop_loss: bool = True

    # ── 下单参数（仿交易终端「下单参数」面板，改了立即生效）──────────────
    #: 市场类型：spot = 现货，futures = U 本位永续合约
    market_type: str = "spot"
    #: 每单保证金（USDT）；> 0 时优先用它 × 杠杆算名义金额
    margin_per_trade_usdt: float = 10.0
    #: 杠杆倍数（仅合约有意义；现货固定按 1 倍计）
    leverage: int = 1
    #: 订单类型：MARKET = 市价，LIMIT = 限价
    order_type: str = "MARKET"
    #: 保证金模式：isolated = 逐仓，cross = 全仓（仅合约）
    margin_mode: str = "isolated"
    #: 跨所价差保护：行情源与下单所不同时，最新价偏离超过该百分比就跳过本次下单。
    #: 0 = 关闭。默认 0.3%，够躲开真正跑偏的行情，又不影响正常的几美元价差。
    max_price_deviation_pct: float = 0.3


class DingTalkSettings(BaseModel):
    """钉钉群机器人通知设置（webhook + 加签 secret）。"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    webhook_url: str = ""
    #: 加签密钥（钉钉后台「加签」那一串 SEC 开头/纯字母数字）；留空则免签
    secret: str = ""
    #: @ 手机号列表（钉钉后台可见的号码）
    at_mobiles: list[str] = Field(default_factory=list)
    at_all: bool = False
    #: True = 仅在出现下单机会时推送，不推其它状态
    notify_on_order_only: bool = True
    #: markdown / text
    msg_type: str = "markdown"


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore")

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)
    binance: BinanceSettings = Field(default_factory=BinanceSettings)
    okx: OKXSettings = Field(default_factory=OKXSettings)
    dingtalk: DingTalkSettings = Field(default_factory=DingTalkSettings)
    auto_trade: AutoTradeSettings = Field(default_factory=AutoTradeSettings)
    #: 用户手填的备用 AI 提供商，按顺序在派生链之后依次尝试。
    #: 与 provider.fallbacks（运行时派生链）分开存，避免被 ensure_* 重写冲掉。
    ai_backups: list[AIProviderSettings] = Field(default_factory=list)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _drop_legacy_feishu_section(raw: dict) -> bool:
    """删除 settings.json 里遗留的 feishu 段（通知通道已改为钉钉）."""
    if "feishu" not in raw:
        return False
    raw.pop("feishu", None)
    logger.info("已移除 settings.json 中废弃的 feishu 段（通知改走钉钉）")
    return True


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        _try_save_defaults(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    # Migrate legacy encrypted key: drop it, api_key already in provider dict
    raw.setdefault("provider", {}).setdefault("api_key", "")

    dropped_feishu = _drop_legacy_feishu_section(raw)
    settings = Settings.model_validate(raw)
    dirty = dropped_feishu
    if settings.pushplus.enabled and not settings.pushplus.token.strip():
        if not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            settings.pushplus.enabled = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled "
                "(DingTalk notifications unaffected)"
            )
            dirty = True
    if dirty:
        try:
            save_settings(settings, path)
        except OSError as exc:
            logger.warning("配置迁移结果无法写回 %s（%s）；本次按内存中的值运行", path, exc)
    return settings


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _try_save_defaults(defaults: "Settings", path: Path) -> None:
    """Best-effort write of freshly-created defaults.

    In a container the mounted ``config/`` directory may be owned by root
    while the process runs as an unprivileged user, so this write can fail
    with ``PermissionError``. That is *not* fatal: the daemon only needs to
    read settings, and the in-memory defaults are perfectly usable. We log a
    pointer to the fix instead of crashing the whole service.
    """
    try:
        save_settings(defaults, path)
    except OSError as exc:
        logger.warning(
            "无法写入默认配置 %s（%s）；本次使用内存中的默认值。"
            " 若在 Docker 中，请确认挂载目录对容器用户可写："
            "chown -R 10001:10001 config",
            path,
            exc,
        )

