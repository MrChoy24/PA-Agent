"""用户自定义的备用 AI 提供商（顺序即尝试顺序）。

与 ``provider.fallbacks`` 的区别：

* ``fallbacks`` 是**运行时派生链**——方舟模型池、GLM 备用会被
  ``ensure_ark_fallbacks`` / ``ensure_glm_fallback`` 反复重写，不能当用户配置用。
* 这里保存用户在「AI 模型设置」里手填的备用接口，按界面顺序依次尝试；
  它们被合并到派生链的**末尾**（免费池之前），并且不会被那两次 ``ensure_*``
  清掉，因为源头在 ``Settings.ai_backups``。

判定一个条目可用需要同时具备 base_url、model、api_key，缺一即跳过——
没有 Key 的条目上去只会撞 401，白等一轮。
"""
from __future__ import annotations

from typing import Any

#: 单条备用记录允许出现的字段（用于从界面/配置里清洗脏数据）。
_MAX_URL_LEN = 500
_MAX_MODEL_LEN = 200


def normalize_base_url(value: Any) -> str:
    """统一 Base URL：去空白、去尾部斜杠。"""
    text = str(value or "").strip()
    while text.endswith("/"):
        text = text[:-1]
    return text[:_MAX_URL_LEN]


def normalize_entry(raw: Any) -> Any:
    """就地清洗一条备用记录，让它具备可用的 base_url / model / api_key。"""
    if raw is None:
        return None
    base = normalize_base_url(getattr(raw, "base_url", ""))
    model = str(getattr(raw, "model", "") or "").strip()[:_MAX_MODEL_LEN]
    key = str(getattr(raw, "api_key", "") or "").strip()
    if base != getattr(raw, "base_url", ""):
        raw.base_url = base
    if model != getattr(raw, "model", ""):
        raw.model = model
    if not str(getattr(raw, "name", "") or "").strip():
        # 没起名字就用模型名当显示名，界面上不至于一片空白。
        raw.name = model
    return raw


def entry_key(entry: Any) -> tuple[str, str]:
    """去重键：同一接口上的同一模型只该出现一次。"""
    return (
        normalize_base_url(getattr(entry, "base_url", "")),
        str(getattr(entry, "model", "") or "").strip(),
    )


def is_entry_usable(entry: Any) -> bool:
    """base_url + model + api_key 三者齐全才算可用。"""
    return bool(
        normalize_base_url(getattr(entry, "base_url", ""))
        and str(getattr(entry, "model", "") or "").strip()
        and str(getattr(entry, "api_key", "") or "").strip()
    )


def extra_fallbacks(settings: Any) -> list[Any]:
    """``settings.ai_backups`` 里启用且可用的条目（保持界面顺序）。"""
    raw_items = list(getattr(settings, "ai_backups", None) or [])
    out: list[Any] = []
    for item in raw_items:
        if not bool(getattr(item, "enabled", True)):
            continue
        cleaned = normalize_entry(item.model_copy(deep=True))
        if cleaned is None or not is_entry_usable(cleaned):
            continue
        out.append(cleaned)
    return out


def combined_fallback_chain(provider: Any, settings: Any = None) -> list[Any]:
    """派生链 + 用户自定义备用，按顺序去重合并。

    ``provider.fallbacks`` 在前（方舟池、GLM），用户自定义项接在后面，
    这样「主通道 → 方舟池 → GLM → 我的备用 → 免费池」的顺序是稳定的。
    """
    chain: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for item in list(getattr(provider, "fallbacks", None) or []):
        cleaned = normalize_entry(item.model_copy(deep=True))
        if cleaned is None:
            continue
        key = entry_key(cleaned)
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        chain.append(cleaned)
    for item in extra_fallbacks(settings):
        key = entry_key(item)
        if key in seen:
            continue
        seen.add(key)
        chain.append(item)
    return chain
