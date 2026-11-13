"""钉钉自定义机器人通知模块.

在下单决策触发时，向钉钉群推送消息，内容包含：
  - 品种 / 周期 / 下单类型 / 方向 / 入场价 / 止损 / TP1 / TP2
  - 交易置信度 / 预估胜率
  - 决策理由 (decision.reasoning)
  - 下一个市场周期预期及理由 (next_cycle_prediction，若上游提供)

使用方式
--------
1. 在钉钉群里添加"自定义机器人"，复制 Webhook 地址。
2. （可选，推荐）安全设置选择"加签"，复制 Secret。
3. 编辑 config/settings.json 的 dingtalk 段，或设置环境变量
   DINGTALK_WEBHOOK_URL / DINGTALK_SECRET。

签名说明（加签）
----------------
    timestamp = str(round(time.time() * 1000))
    string_to_sign = timestamp + "\\n" + secret
    sign = base64(hmac_sha256(key=secret, msg=string_to_sign))
最终请求 URL 追加 ``&timestamp=...&sign=...``（sign 做 URL 编码）。

钉钉官方文档
------------
自定义机器人接入：
    https://open.dingtalk.com/document/orgapp/custom-robot-access
消息类型（markdown / text）：https://open.dingtalk.com/document/orgapp/message-types-and-data-format
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

try:  # httpx 已是项目依赖（pyproject: httpx[socks]>=0.27）
    import httpx  # type: ignore[import]
except ImportError:  # pragma: no cover - 仅在依赖缺失的环境触发
    httpx = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings

logger = logging.getLogger(__name__)

#: 与 PushPlus 保持一致的请求超时
_REQUEST_TIMEOUT_S = 15

_DEFAULT_MSG_TYPE = "markdown"

#: settings.json 中 dingtalk 段的字段默认值
_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "webhook_url": "",
    "secret": "",
    "at_mobiles": [],
    "at_all": False,
    "notify_on_order_only": True,
    "notify_on_trade_filled": True,
    "msg_type": _DEFAULT_MSG_TYPE,
}


# ── 配置加载 ──────────────────────────────────────────────────────────────────
def _settings_root(settings: "Settings | None") -> Any:
    """返回承载 dingtalk 段的配置对象（内存 Settings 或 settings.json 载入结果）."""
    if settings is not None:
        return settings
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings

    return load_settings(SETTINGS_JSON_PATH)


def _section_to_dict(section: Any) -> dict[str, Any]:
    """把 dingtalk 配置段（pydantic 模型 / dict / 普通对象）转成 dict."""
    if section is None:
        return {}
    if isinstance(section, dict):
        return dict(section)
    dump = getattr(section, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("钉钉配置 model_dump 失败: %s", exc)
    out: dict[str, Any] = {}
    for key in _CONFIG_DEFAULTS:
        if hasattr(section, key):
            out[key] = getattr(section, key)
    return out


def _as_mobile_list(value: Any) -> list[str]:
    """把 list[str] / 逗号或空白分隔的字符串统一成 list[str]."""
    if value is None:
        return []
    if isinstance(value, str):
        text = (
            value.replace("；", ",")
            .replace(";", ",")
            .replace("，", ",")
            .replace("\n", ",")
            .replace("\t", ",")
            .replace(" ", ",")
        )
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    msg_type = str(raw.get("msg_type") or _DEFAULT_MSG_TYPE).strip().lower()
    if msg_type not in ("markdown", "text"):
        msg_type = _DEFAULT_MSG_TYPE
    return {
        "enabled": bool(raw.get("enabled", True)),
        "webhook_url": str(raw.get("webhook_url") or "").strip(),
        "secret": str(raw.get("secret") or "").strip(),
        "at_mobiles": _as_mobile_list(raw.get("at_mobiles")),
        "at_all": bool(raw.get("at_all", False)),
        "notify_on_order_only": bool(raw.get("notify_on_order_only", True)),
        "notify_on_trade_filled": bool(raw.get("notify_on_trade_filled", True)),
        "msg_type": msg_type,
    }


def _dingtalk_config_dict(settings: "Settings | None" = None) -> dict[str, Any]:
    """读取钉钉配置：settings.dingtalk → settings.json → 环境变量.

    任何一步失败都不抛异常：配置段缺失（老版本 settings.json）时退回默认值 +
    环境变量，保证模块可被安全导入和调用。
    """
    raw: dict[str, Any] = dict(_CONFIG_DEFAULTS)

    section: Any = None
    try:
        root = _settings_root(settings)
        if isinstance(root, dict):
            section = root.get("dingtalk")
        else:
            section = getattr(root, "dingtalk", None)
    except Exception as exc:
        logger.debug("钉钉配置读取失败，回退默认值: %s", exc)
        section = None

    raw.update(_section_to_dict(section))

    # 环境变量兜底（仅在配置缺失时生效，不覆盖显式配置）
    cfg = _normalize_config(raw)
    if not cfg["webhook_url"]:
        env_url = (os.environ.get("DINGTALK_WEBHOOK_URL") or "").strip()
        if env_url:
            cfg["webhook_url"] = env_url
            # 环境变量只在配置段缺失时生效；既然整体由 env 提供，
            # 就等于「已配置」，不应因为 settings 里的 enabled=False 而失效。
            cfg["enabled"] = True
    if not cfg["secret"]:
        env_secret = (os.environ.get("DINGTALK_SECRET") or "").strip()
        if env_secret:
            cfg["secret"] = env_secret
    return cfg


# ── 签名 ──────────────────────────────────────────────────────────────────────
def _gen_sign(secret: str, timestamp: str) -> str:
    """按钉钉规范计算 HmacSHA256 + Base64 签名.

    签名字符串：timestamp + "\\n" + secret，密钥为 secret 本身。
    """
    string_to_sign = timestamp + "\n" + secret
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _signed_url(webhook_url: str, secret: str) -> str:
    """未配置 secret 时原样返回 webhook；配置时追加 timestamp 与 sign 查询参数."""
    url = (webhook_url or "").strip()
    key = (secret or "").strip()
    if not key:
        return url

    timestamp = str(round(time.time() * 1000))
    sign = _gen_sign(key, timestamp)
    separator = "&" if "?" in url else "?"
    return (
        f"{url}{separator}timestamp={quote(timestamp, safe='')}"
        f"&sign={quote(sign, safe='')}"
    )


# ── 辅助格式化 ────────────────────────────────────────────────────────────────
def _fmt(value: Any, default: str = "—") -> str:
    """安全转字符串，空值返回 default."""
    if value is None or value == "":
        return default
    return str(value)


def _truncate(text: str, max_len: int = 600) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _direction_label(order_dir: str) -> str:
    """根据方向返回带 emoji 的方向标签."""
    d = (order_dir or "").lower()
    if any(k in d for k in ("bull", "多", "long", "buy")):
        return "🟢 做多"
    if any(k in d for k in ("bear", "空", "short", "sell")):
        return "🔴 做空"
    return f"📊 {order_dir}"


def _to_plain_text(text: str) -> str:
    """把 markdown 粗转成纯文本（msg_type=text 时使用）."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped.startswith("- "):
            stripped = "· " + stripped[2:].strip()
        stripped = stripped.replace("**", "")
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


# ── 消息体构建 ────────────────────────────────────────────────────────────────
def _at_block(at_mobiles: list[str], at_all: bool) -> dict[str, Any]:
    """钉钉的 @ 配置必须放在消息体 JSON 里，不能放 URL."""
    return {"atMobiles": list(at_mobiles), "isAtAll": bool(at_all)}


def _build_payload(
    text: str,
    *,
    cfg: dict[str, Any],
    title: str = "",
    at_mobiles: list[str],
    at_all: bool,
) -> dict[str, Any]:
    """按 cfg['msg_type'] 组装 markdown / text 消息体."""
    msg_type = cfg.get("msg_type") or _DEFAULT_MSG_TYPE
    if msg_type == "text":
        body: dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": _to_plain_text(text)},
        }
    else:
        body = {
            "msgtype": "markdown",
            "markdown": {"title": title or "PA Agent 通知", "text": text},
        }
    body["at"] = _at_block(at_mobiles, at_all)
    return body


def _build_order_markdown(
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
) -> tuple[str, str]:
    """构建下单信号的 (title, markdown_text)."""
    dec = decision_inner or {}
    stage2 = stage2_full or {}
    ncp: dict = stage2.get("next_cycle_prediction") or {}

    order_type = _fmt(dec.get("order_type"))
    order_dir = _fmt(dec.get("order_direction"))
    entry = _fmt(dec.get("entry_price"))
    stop = _fmt(dec.get("stop_loss_price"))
    tp = _fmt(dec.get("take_profit_price"))
    tp2 = _fmt(dec.get("take_profit_price_2"))
    trade_conf = _fmt(dec.get("trade_confidence"))
    raw_win_rate = dec.get("win_rate")
    if raw_win_rate is None or raw_win_rate == "":
        raw_win_rate = dec.get("estimated_win_rate")
    win_rate = _fmt(raw_win_rate)
    reasoning = _truncate(str(dec.get("reasoning") or "").strip(), 600)

    title = f"🚨 PA Agent 下单信号 — {symbol} {timeframe}"

    lines = [
        f"#### {title}",
        f"- **品种**：{symbol}",
        f"- **周期**：{timeframe}",
        f"- **下单类型**：{order_type}",
        f"- **方向**：{_direction_label(order_dir)}",
        f"- **入场价**：{entry}",
        f"- **止损**：{stop}",
        f"- **TP1**：{tp}",
        f"- **TP2**：{tp2}",
        f"- **置信度**：{trade_conf}",
        f"- **预估胜率**：{win_rate}",
    ]

    if reasoning:
        lines.append("")
        lines.append(f"**📝 决策理由**：{reasoning}")

    probs: dict = ncp.get("probabilities") or {}
    if probs:
        best_key = max(probs, key=lambda k: probs[k])
        next_cycle = f"{best_key}（概率 {probs[best_key]}）"
    elif ncp.get("cycle"):
        next_cycle = _fmt(ncp.get("cycle"))
    else:
        next_cycle = ""
    if next_cycle:
        lines.append("")
        lines.append(f"**🔮 下一个市场周期预期**：{next_cycle}")
        ncp_reasoning = _truncate(str(ncp.get("reasoning") or "").strip(), 400)
        if ncp_reasoning:
            lines.append(ncp_reasoning)

    return title, "\n".join(lines)


# ── HTTP 发送 ────────────────────────────────────────────────────────────────
def _parse_reply(resp: Any) -> dict[str, Any] | None:
    """解析钉钉响应；兼容 fake 直接返回 dict 的情况."""
    if isinstance(resp, dict):
        return resp
    try:
        data = resp.json()
    except Exception:
        text = getattr(resp, "text", None)
        if not isinstance(text, str):
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def _post(
    payload: dict[str, Any],
    cfg: dict[str, Any],
    *,
    action: str = "钉钉通知",
) -> tuple[bool, str]:
    """发送 payload，返回 (ok, 人类可读消息)。绝不抛异常."""
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        logger.warning(
            "%s：未配置 dingtalk.webhook_url，跳过推送。"
            " 请编辑 config/settings.json 的 dingtalk 段，"
            "或设置环境变量 DINGTALK_WEBHOOK_URL。",
            action,
        )
        return False, "钉钉通知未配置：缺少 webhook_url（settings.json dingtalk 段或 DINGTALK_WEBHOOK_URL）"

    if httpx is None:  # pragma: no cover - 依赖缺失才会触发
        logger.warning("%s：httpx 库未安装，无法发送", action)
        return False, "httpx 库未安装，请运行 pip install httpx"

    target = _signed_url(url, cfg.get("secret") or "")

    try:
        resp = httpx.post(target, json=payload, timeout=_REQUEST_TIMEOUT_S)
    except Exception as exc:
        logger.warning("%s HTTP 请求失败: %s", action, exc)
        return False, f"{action} HTTP 请求失败：{exc}"

    data = _parse_reply(resp)
    if data is None:
        logger.warning("%s：无法解析钉钉响应", action)
        return False, f"{action}失败：钉钉返回内容无法解析为 JSON"

    errcode = data.get("errcode")
    errmsg = str(data.get("errmsg") or "").strip()
    if errcode == 0 or str(errcode) == "0":
        logger.info("%s发送成功（errmsg=%s）", action, errmsg or "ok")
        return True, f"{action}发送成功（errcode=0, errmsg={errmsg or 'ok'}）"

    logger.warning("%s返回错误：errcode=%s errmsg=%s", action, errcode, errmsg)
    return False, f"{action}失败（errcode={errcode}, errmsg={errmsg or 'unknown'}）"


def _resolve_at(
    cfg: dict[str, Any],
    at_mobiles: Any,
    at_all: Any,
) -> tuple[list[str], bool]:
    mobiles = cfg["at_mobiles"] if at_mobiles is None else _as_mobile_list(at_mobiles)
    mention_all = cfg["at_all"] if at_all is None else bool(at_all)
    return mobiles, mention_all


# ── 公共 API ─────────────────────────────────────────────────────────────────
def dingtalk_is_active(settings: "Settings | None" = None) -> bool:
    """True 仅当钉钉通知已启用且 webhook_url 非空."""
    try:
        cfg = _dingtalk_config_dict(settings)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("钉钉通知配置读取异常: %s", exc)
        return False
    return bool(cfg["enabled"] and cfg["webhook_url"])


def send_text(
    content: str,
    *,
    settings: "Settings | None" = None,
    at_mobiles: Any = None,
    at_all: Any = None,
) -> bool:
    """发送纯文本消息（msgtype=text）."""
    try:
        cfg = _dingtalk_config_dict(settings)
        mobiles, mention_all = _resolve_at(cfg, at_mobiles, at_all)
        payload = {
            "msgtype": "text",
            "text": {"content": str(content or "")},
            "at": _at_block(mobiles, mention_all),
        }
        ok, _ = _post(payload, cfg, action="钉钉文本通知")
        return ok
    except Exception as exc:
        logger.warning("钉钉文本通知发送异常: %s", exc)
        return False


def send_markdown(
    title: str,
    content: str,
    *,
    settings: "Settings | None" = None,
    at_mobiles: Any = None,
    at_all: Any = None,
) -> bool:
    """发送 markdown 消息（msgtype=markdown）.

    content 若是已有的 markdown 正文则原样发送；若不以 ``#`` 标题开头，
    会在正文顶部补一行 ``#### {title}``（钉钉正文不自动渲染 title 字段）。
    """
    try:
        cfg = _dingtalk_config_dict(settings)
        mobiles, mention_all = _resolve_at(cfg, at_mobiles, at_all)
        body = str(content or "")
        if title and not body.lstrip().startswith("#"):
            body = f"#### {title}\n\n{body}" if body else f"#### {title}"
        payload = _build_payload(
            body,
            cfg=cfg,
            title=str(title or ""),
            at_mobiles=mobiles,
            at_all=mention_all,
        )
        ok, _ = _post(payload, cfg, action="钉钉 markdown 通知")
        return ok
    except Exception as exc:
        logger.warning("钉钉 markdown 通知发送异常: %s", exc)
        return False


def send_order_signal(
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
    chart_image_path: str | None = None,
    settings: "Settings | None" = None,
) -> bool:
    """下单决策触发时向钉钉群推送消息.

    Parameters
    ----------
    decision_inner:
        stage2_decision["decision"] 内层字典。
    stage2_full:
        完整的 stage2_decision 字典（含 next_cycle_prediction）。
    symbol:
        交易品种，如 "XAUUSDm"。
    timeframe:
        K线周期，如 "15m"。
    chart_image_path:
        保留参数以对齐 PushPlus 的调用签名；钉钉 markdown 不支持本地图片直传，
        此处仅记录日志后忽略。
    settings:
        可选的内存 Settings；未传时从 config/settings.json 读取。

    Returns
    -------
    bool
        发送成功返回 True；未启用、未配置或推送失败返回 False。
    """
    try:
        cfg = _dingtalk_config_dict(settings)
        if not (cfg["enabled"] and cfg["webhook_url"]):
            logger.debug("钉钉通知未启用或未配置 webhook_url，跳过下单信号推送")
            return False

        if chart_image_path:
            logger.debug(
                "钉钉通知：markdown 不支持本地图片直传，忽略 chart_image_path=%s",
                chart_image_path,
            )

        title, markdown_text = _build_order_markdown(
            decision_inner, stage2_full, symbol, timeframe
        )
        payload = _build_payload(
            markdown_text,
            cfg=cfg,
            title=title,
            at_mobiles=cfg["at_mobiles"],
            at_all=cfg["at_all"],
        )
        ok, _ = _post(payload, cfg, action="钉钉下单信号")
        return ok
    except Exception as exc:
        logger.warning("钉钉下单信号发送异常: %s", exc)
        return False


# ── 实盘成交回报 ──────────────────────────────────────────────────────────────
# 与 send_order_signal 的区别：那条推的是「决策意图」（还没下单、可能失败），
# 这条推的是「交易所已经成交的事实」——真实成交价、真实数量、真实金额、
# 委托号、以及止损/TP 有没有真的挂到交易所侧。只有 dry_run=False 才会走这里。
def _safe_float(value: Any) -> float:
    """容错转 float，失败返回 0.0（本模块不与交易层共享依赖）."""
    try:
        if value is None or value == "":
            return 0.0
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _fmt_price(value: Any) -> str:
    """价格格式化：去掉尾零，并避免 1e-05 这种科学计数法."""
    num = _safe_float(value)
    if num <= 0:
        return "—"
    text = f"{num:.10f}".rstrip("0").rstrip(".")
    return text or "—"


def _fmt_money(value: Any) -> str:
    """金额（USDT）格式化：>=100 用千分位两位小数，小额保留 4 位去尾零."""
    num = _safe_float(value)
    if num <= 0:
        return "—"
    if abs(num) >= 100:
        return f"{num:,.2f}"
    return f"{num:.4f}".rstrip("0").rstrip(".")


def _side_label(side: str) -> str:
    """BUY/SELL → 带 emoji 的中文方向."""
    s = (side or "").strip().upper()
    if s == "BUY":
        return "🟢 做多（BUY）"
    if s == "SELL":
        return "🔴 做空（SELL）"
    return _fmt(side)


_MARKET_LABELS = {"spot": "现货", "futures": "合约"}
_MODE_LABELS = {"cross": "全仓", "isolated": "逐仓"}


def _build_trade_markdown(
    *,
    venue: str,
    symbol: str,
    order_id: str,
    side: str,
    order_type: str,
    fill_price: Any,
    qty: Any,
    notional_usdt: Any,
    margin_usdt: Any,
    leverage: Any,
    margin_mode: str,
    market_type: str,
    executed_at: str,
    protections: Any = None,
    pending_trigger: bool = False,
    trigger_price: Any = None,
) -> tuple[str, str]:
    """构建实盘成交回报的 (title, markdown_text)."""
    venue_label = "OKX" if (venue or "").lower() == "okx" else (
        "Binance" if (venue or "").lower() == "binance" else _fmt(venue)
    )
    market_label = _MARKET_LABELS.get((market_type or "").lower(), _fmt(market_type))
    mode_label = _MODE_LABELS.get((margin_mode or "").lower(), _fmt(margin_mode))
    lev = int(_safe_float(leverage) or 1)

    if pending_trigger:
        title = f"🕒 PA Agent 计划委托已挂 — {symbol}"
    else:
        title = f"✅ PA Agent 实盘成交 — {symbol}"

    lines = [f"#### {title}"]

    venue_line = f"{venue_label} · {market_label}"
    if market_label == "合约":
        venue_line += f" · {mode_label} {lev}x"
    lines.append(f"- **交易所**：{venue_line}")
    lines.append(f"- **交易对**：{_fmt(symbol)}")
    lines.append(f"- **方向**：{_side_label(side)}")

    if pending_trigger:
        lines.append(f"- **触发价**：{_fmt_price(trigger_price)}（尚未成交）")
    else:
        lines.append(f"- **开仓价（成交均价）**：{_fmt_price(fill_price)}")
        lines.append(f"- **数量**：{_fmt_price(qty)}")
        notional_text = _fmt_money(notional_usdt)
        margin_text = _fmt_money(margin_usdt)
        if market_label == "合约" and margin_text != "—":
            lines.append(f"- **下单金额**：名义 {notional_text} USDT（保证金 {margin_text} USDT）")
        else:
            lines.append(f"- **下单金额**：{notional_text} USDT")

    lines.append(f"- **下单时间**：{_fmt(executed_at)}")
    lines.append(f"- **订单类型**：{_fmt(order_type)}")
    lines.append(f"- **委托号**：{_fmt(order_id)}")

    legs = list(protections or [])
    if legs:
        lines.append("")
        lines.append("**🛡 保护单（交易所侧）**")
        for leg in legs:
            kind = str(getattr(leg, "kind", "") or "")
            ok = bool(getattr(leg, "ok", False))
            price = _fmt_price(getattr(leg, "trigger_price", 0))
            label = _PROTECT_KIND_LABELS.get(kind, kind or "保护单")
            if ok:
                oid = str(getattr(leg, "order_id", "") or "")
                suffix = f"（{oid}）" if oid else ""
                lines.append(f"- {label}：{price} 已挂{suffix}")
            else:
                reason = _truncate(str(getattr(leg, "message", "") or "").strip(), 120)
                lines.append(f"- {label}：{price} ⚠ 未挂：{reason or '未知原因'}")
    elif pending_trigger:
        lines.append("")
        lines.append("**🛡 保护单**：计划委托尚未触发，触发成交后自动补挂止损 / TP1 / TP2。")
    else:
        lines.append("")
        lines.append("**🛡 保护单**：本次未提交保护单（未启用或决策缺止盈止损价），请人工核对持仓。")

    return title, "\n".join(lines)


#: 保护单类型 → 中文标签（与 trading.common 保持一致，但这里不 import 交易层，
#: 以免 notify 模块被牵连进交易依赖）
_PROTECT_KIND_LABELS: dict[str, str] = {
    "stop_loss": "止损",
    "take_profit_1": "TP1",
    "take_profit_2": "TP2",
}


def send_trade_report(
    *,
    venue: str,
    symbol: str,
    order_id: str,
    side: str,
    order_type: str,
    fill_price: Any,
    qty: Any,
    notional_usdt: Any,
    margin_usdt: Any,
    leverage: Any,
    margin_mode: str,
    market_type: str,
    executed_at: str,
    protections: Any = None,
    pending_trigger: bool = False,
    trigger_price: Any = None,
    settings: "Settings | None" = None,
) -> bool:
    """推送一条「实盘已成交」回报到钉钉群。

    只在真实下单（``dry_run=False``）且交易所已受理时调用；试运行绝不推送。
    ``notify_on_trade_filled=False`` 时直接跳过（返回 False）。

    Returns
    -------
    bool
        发送成功 True；未启用 / 未配置 / 被开关关闭 / 推送失败均返回 False。
    """
    try:
        cfg = _dingtalk_config_dict(settings)
        if not (cfg["enabled"] and cfg["webhook_url"]):
            logger.debug("钉钉通知未启用或未配置 webhook_url，跳过成交回报")
            return False
        if not cfg.get("notify_on_trade_filled", True):
            logger.debug("钉钉成交回报已关闭（notify_on_trade_filled=false），跳过")
            return False

        title, markdown_text = _build_trade_markdown(
            venue=venue,
            symbol=symbol,
            order_id=order_id,
            side=side,
            order_type=order_type,
            fill_price=fill_price,
            qty=qty,
            notional_usdt=notional_usdt,
            margin_usdt=margin_usdt,
            leverage=leverage,
            margin_mode=margin_mode,
            market_type=market_type,
            executed_at=executed_at,
            protections=protections,
            pending_trigger=pending_trigger,
            trigger_price=trigger_price,
        )
        payload = _build_payload(
            markdown_text,
            cfg=cfg,
            title=title,
            at_mobiles=cfg["at_mobiles"],
            at_all=cfg["at_all"],
        )
        ok, _ = _post(payload, cfg, action="钉钉成交回报")
        return ok
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不能影响交易
        logger.warning("钉钉成交回报发送异常: %s", exc)
        return False


def send_test_message(settings: "Settings | None" = None) -> tuple[bool, str]:
    """发送一条测试消息，返回 (ok, 人类可读消息，含钉钉 errmsg)."""
    try:
        cfg = _dingtalk_config_dict(settings)
        if not cfg["enabled"]:
            return False, "钉钉通知已禁用（settings.json dingtalk.enabled=false）"

        title = "PA Agent 钉钉测试消息"
        text = (
            f"#### ✅ {title}\n"
            f"- **发送时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "- **说明**：收到此消息说明钉钉 Webhook 与加签配置正确。"
        )
        payload = _build_payload(
            text,
            cfg=cfg,
            title=title,
            at_mobiles=cfg["at_mobiles"],
            at_all=cfg["at_all"],
        )
        return _post(payload, cfg, action="钉钉测试消息")
    except Exception as exc:
        logger.warning("钉钉测试消息发送异常: %s", exc)
        return False, f"钉钉测试消息发送异常：{exc}"
