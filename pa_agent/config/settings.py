"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["binance", "okx"]
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
    #: GLM 备用通道的模型名 / Base URL（空则回落到 glm_backup 的常量）。
    #: 与 api_key 一起构成「备用 AI 提供商」表格里固定的 GLM 行。
    backup_glm_model: str = "glm-4-flash"
    backup_glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    #: 是否把 GLM 备用挂进 fallback 链（面板上那一行的勾选状态）。
    #: 与 is_glm_backup_eligible(base_url) 是 AND 关系；老配置缺该键时默认 True。
    backup_glm_enabled: bool = True
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
        # MT5 / TradingView / A 股（东方财富 / AkShare / Tushare / YFinance）
        # 数据源已移除：历史配置一律迁移到 Binance。
        if v in (
            "mt5",
            "tradingview",
            "ashare",
            "akshare",
            "tushare",
            "yfinance",
            "eastmoney",
            "eastmoney_futures",
            "adata",
            "a_share",
        ):
            return "binance"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


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

    # ── 开仓后的保护单（止损 / TP1 / TP2）────────────────────────────────
    #: True = 开仓成交后立刻把止损与两个止盈价提交给交易所（条件单）。
    #: 这样本程序崩了、容器重启、服务器断连都不影响止盈止损在交易所侧生效。
    #: 现货没有交易所侧条件单，只有合约（U 本位永续）才真正挂得上去。
    protect_after_entry: bool = True
    #: TP1 平掉的比例（0~1），其余留给 TP2；止损永远保护全量仓位。
    tp1_fraction: float = 0.5
    #: 成交价与决策入场价相差超过这个 bps（万分之一）就告警。<=0 关闭。
    #: 只告警不拒单 —— 单已成交，拒单只会让仓位裸奔。
    max_slippage_bps: float = 30.0


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
    #: True = 实盘成交（dry_run=False 且交易所已受理）后推送一条成交回报，
    #: 内容含成交均价 / 数量 / 名义金额与保证金 / 下单时间 / 委托号 / 保护单挂单结果。
    #: 试运行（dry_run=True）永远不会推送，与本开关无关。
    notify_on_trade_filled: bool = True
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
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 每个配置文件最近一次 ``load_settings()`` 读到的内容指纹（sha256）。
#:
#: 用途：``settings.json`` 是 GUI 与无头守护进程共用的唯一配置文件，而
#: ``save_settings()`` 是**整份覆盖**（不是 diff）。若 A 进程读入快照后，
#: B 进程改了盘上的文件，A 再保存就会把 B 的改动整体抹掉 —— 这正是
#: 「GUI 里改的杠杆被服务端的旧值覆盖」的实际成因。
#:
#: 保存时用这个指纹做比对：磁盘现状与「我读进来的那一版」不一致就拒绝写，
#: 让调用方提示用户刷新后重试，而不是静默覆盖。
_LOADED_DIGESTS: dict[str, str] = {}


def _path_key(path: Path) -> str:
    """把路径归一成字典键（尽量 resolve，失败就用原样）。"""
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - 极端情况下 resolve 会抛
        return str(path)


def file_digest(path: Path | None = None) -> str:
    """返回 *path* 当前内容的 sha256；文件不存在或读不到时返回空串。"""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def loaded_digest(path: Path | None = None) -> str:
    """返回该文件最近一次 ``load_settings()`` 看到的指纹（没加载过就是空串）。"""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    return _LOADED_DIGESTS.get(_path_key(path), "")


def _record_digest(path: Path) -> str:
    """记录并返回磁盘当前指纹。"""
    digest = file_digest(path)
    _LOADED_DIGESTS[_path_key(path)] = digest
    return digest


def _drop_legacy_feishu_section(raw: dict) -> bool:
    """删除 settings.json 里遗留的 feishu 段（通知通道已改为钉钉）."""
    if "feishu" not in raw:
        return False
    raw.pop("feishu", None)
    logger.info("已移除 settings.json 中废弃的 feishu 段（通知改走钉钉）")
    return True


def _drop_stale_ark_fallbacks(raw: dict) -> bool:
    """主通道已经不是方舟时，删掉 fallbacks 里残留的方舟池。

    背景：早期版本会在方舟网关下把 13 条方舟模型写进 ``provider.fallbacks``
    并落盘。后来主通道换成别的网关，这 13 条仍在文件里，于是每次分析都要
    从一条整体欠费的池子里逐条撞 403（m13007 修的是「不再白等」，这里是
    「干脆别放进去」）。只删方舟网关的条目，用户自己的备用接口一条不碰。
    """
    provider = raw.get("provider")
    if not isinstance(provider, dict):
        return False
    primary = str(provider.get("base_url") or "").strip().lower()
    if "volces.com" in primary or "volcengine" in primary or "ark.cn-beijing" in primary:
        # 主通道就是方舟：池子是有意义的，保留。
        return False
    fallbacks = provider.get("fallbacks")
    if not isinstance(fallbacks, list) or not fallbacks:
        return False
    kept = []
    dropped = 0
    for item in fallbacks:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        url = str(item.get("base_url") or "").strip().lower()
        if "volces.com" in url or "volcengine" in url or "ark.cn-beijing" in url:
            dropped += 1
            continue
        kept.append(item)
    if not dropped:
        return False
    provider["fallbacks"] = kept
    logger.info(
        "已从 settings.json 的 fallbacks 移除 %d 条失效的方舟模型（主通道已不是方舟网关）",
        dropped,
    )
    return True


def _settings_backup_path(path: Path) -> Path:
    """``settings.json`` 的上一版副本（固定名字，只留一份）。"""
    return path.with_name(path.name + ".bak")


def _quarantine_broken_settings(
    path: Path, exc: BaseException, *, text: str = ""
) -> "Settings | None":
    """把读不动的 ``settings.json`` 改名留证，再放默认值。

    原先这里只是 ``logger.warning(...); return Settings()``：一次断电把文件写成
    半截 JSON，程序就静默跑在默认配置上 —— 凭据没了、``min_confidence`` 回到
    默认值、``dry_run`` 回到 True，而磁盘上的原文件仍在那里，下一次任意
    ``save_settings()`` 会把它**覆盖**掉，再也没法人工抢救。

    现在的做法：
      1. 原文件改名成 ``settings.json.corrupt-<时间戳>``（不删除、不覆盖）；
      2. 若已有 ``.bak`` 且它是合法 JSON，就**用它恢复**并立即回写正本 ——
         这是最理想的结果，用户几乎无感；
      3. 恢复不了才退回默认值，但要打一条 error 级日志说清楚「凭据需要重填」。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    broken = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(broken)
        logger.error(
            "settings.json 无法解析（%s），已改名保留为 %s —— "
            "不会覆盖、不会删除，可人工修复后改回 settings.json。",
            exc,
            broken.name,
        )
    except OSError as move_exc:  # 文件被占用 / 目录只读：至少别把内容弄丢
        logger.error(
            "settings.json 无法解析（%s），且无法改名保留（%s）；"
            "本次按默认配置运行，请尽快人工检查 %s。",
            exc,
            move_exc,
            path,
        )
        return None

    restored = _restore_from_backup(path)
    if restored is not None:
        logger.warning(
            "已用备份 %s 恢复配置（含 API Key 与风控参数）。"
            "损坏的原文件保留为 %s，可对照检查。",
            _settings_backup_path(path).name,
            broken.name,
        )
        return restored

    logger.error(
        "没有可用的 settings.json 备份，本次按【默认配置】运行："
        "API Key / 交易所凭据 / 自动交易风控参数都回到默认值。"
        "请把 %s 修好改回 settings.json，或重新在界面里填一遍。",
        broken.name,
    )
    return None


def _restore_from_backup(path: Path) -> "Settings | None":
    """尝试用 ``settings.json.bak`` 恢复；成功则回写正本并返回结果。"""
    bak = _settings_backup_path(path)
    if not bak.exists():
        return None
    try:
        raw = json.loads(bak.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("备份 %s 也不可用（%s），无法自动恢复。", bak.name, exc)
        return None
    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 —— 备份结构不符就当没有
        logger.warning("备份 %s 的内容与当前配置结构不兼容（%s）。", bak.name, exc)
        return None
    try:
        save_settings(settings, path)
    except OSError as exc:
        logger.warning(
            "已从备份恢复内存配置，但回写 %s 失败（%s）；本次照常运行。", path, exc
        )
    return settings


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        _try_save_defaults(defaults, path)
        _record_digest(path)
        return defaults

    text = ""
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        # 记住「我读到的是哪一版」：保存时用它判断盘上内容有没有被别人改过。
        # 用文件的原始字节算（不是 text），否则 Windows 上 \r\n↔\n 的换行
        # 转换会让指纹永远对不上。
        _record_digest(path)
    except (json.JSONDecodeError, OSError) as exc:
        recovered = _quarantine_broken_settings(path, exc, text=text)
        return recovered if recovered is not None else Settings()

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
    dropped_ark = _drop_stale_ark_fallbacks(raw)
    settings = Settings.model_validate(raw)
    dirty = dropped_feishu or dropped_ark
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


def save_settings(
    settings: "Settings",
    path: Path | None = None,
    *,
    base_digest: str | None = None,
    force: bool = False,
) -> bool:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH).

    Parameters
    ----------
    base_digest:
        「我这次保存是建立在哪一版文件之上的」的 sha256。传 ``None``（默认）
        表示不做并发校验 —— 用于首次初始化、迁移回写等「盘上内容本就是我
        刚读到的」场景，以及所有现有调用点（保持向后兼容）。
    force:
        跳过所有并发校验，直接覆盖。仅供显式「以我为准」的场景使用。

    Returns
    -------
    bool
        ``True`` 表示已写入；``False`` 表示因并发冲突被**拒绝**（磁盘上的
        文件已被别的进程改过），此时不改动磁盘内容，调用方应提示用户刷新。

    Notes
    -----
    ``settings.json`` 由 GUI 与无头守护进程共用，而本函数是整份覆盖。
    两进程同时运行时，谁后保存谁就抹掉对方的改动。``base_digest`` 提供
    乐观锁语义：只在「磁盘内容仍是我读到的这一版」时才允许覆盖。
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if not force and base_digest is not None:
        current = file_digest(path)
        # 文件不存在时 current 是空串：此时写入不会抹掉任何东西，放行。
        if current and current != base_digest:
            logger.warning(
                "拒绝写入 %s：磁盘上的配置已被其他进程修改"
                "（读入时 %s…，当前 %s…）；请刷新后重试本次修改。",
                path,
                base_digest[:8],
                current[:8],
            )
            return False

    data = settings.model_dump()

    # 覆盖前先把上一版留成 .bak：settings.json 是凭据与风控参数的唯一存放处，
    # 一次写坏（断电 / 磁盘满 / 容器被 SIGKILL）就等于全部丢失。
    # 只在原文件本身是合法 JSON 时才备份，避免把损坏内容当成「上一版」。
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # 原文件已经坏了，别把坏内容顶掉更早的好备份
        else:
            bak = _settings_backup_path(path)
            try:
                shutil.copy2(path, bak)
            except OSError as exc:
                logger.warning("无法写入配置备份 %s（%s）；继续保存正本。", bak, exc)

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 写完之后我就是最新的那一版：刷新指纹，后续保存才算「基于当前内容」。
    _record_digest(path)
    return True


class ConfigReloadError(RuntimeError):
    """热重载 settings.json 失败（文件写坏 / 读不到 / 结构不符）。"""


def reload_settings(path: Path | None = None) -> "Settings":
    """重新从磁盘读取 *path*，用于运行中的守护进程热重载。

    与 ``load_settings`` 的区别只有一点：**任何失败都抛异常**，而不是静默
    回落默认值。运行中的进程用默认值覆盖掉用户的真实配置是灾难性的，
    调用方（``DaemonRunner``）应该捕获异常并继续用内存里那份好的配置。
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    try:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigReloadError(f"{path} 无法读取或不是合法 JSON：{exc}") from exc
    try:
        return load_settings(path)
    except Exception as exc:  # noqa: BLE001 - 结构不符也算重载失败
        raise ConfigReloadError(f"{path} 内容无法解析为配置：{exc}") from exc


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

