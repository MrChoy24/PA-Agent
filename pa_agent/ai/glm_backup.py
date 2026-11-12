"""glm-4-flash backup channel for the SenseNova / Ark gateways.

Only active when the primary base_url is the SenseNova token gateway or the
Volcengine Ark gateway. Model / Base URL / Key all come from the user's settings
(`provider.backup_glm_model` / `backup_glm_base_url` / `backup_glm_api_key`),
never from source code or the repository; the module constants below are only
the fallback defaults when a settings field is blank.
"""
from __future__ import annotations

import logging
from typing import Any

GLM_MODEL = "glm-4-flash"
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

logger = logging.getLogger(__name__)


def _is_sensenova(base_url: str | None) -> bool:
    from pa_agent.ai.sensenova_fallback import is_sensenova_fallback_eligible

    return is_sensenova_fallback_eligible(base_url)


def is_ark_base(base_url: str | None) -> bool:
    """额外网关也允许配置该备用链路。"""
    from pa_agent.ai.ark_gateway import is_ark_base as _ark

    return _ark(base_url)


def is_glm_backup_eligible(base_url: str | None) -> bool:
    """GLM 备用是否适用于该主通道：主通道为 SenseNova 或火山方舟。"""
    return _is_sensenova(base_url) or is_ark_base(base_url)


def glm_identity(provider: Any) -> tuple[str, str]:
    """当前配置的 (model, base_url)，空值回落到模块常量。"""
    model = str(getattr(provider, "backup_glm_model", "") or "").strip() or GLM_MODEL
    base_url = str(getattr(provider, "backup_glm_base_url", "") or "").strip() or GLM_BASE_URL
    return model, base_url


def is_glm_fallback_entry(
    entry: Any,
    provider: Any = None,
    *,
    extra_identities: "list[tuple[str, str]] | tuple[tuple[str, str], ...]" = (),
) -> bool:
    """判断一条 fallback 是否就是本模块维护的 GLM 条目。

    匹配「当前配置值」、「历史默认常量」以及调用方给出的「改配置前的旧值」
    三种身份：用户把 model / base_url 改掉之后，链里那条老条目仍是用旧值
    写成的，必须认得出来，否则会留下两条。
    """
    model = str(getattr(entry, "model", "") or "").strip()
    base_url = str(getattr(entry, "base_url", "") or "").strip()
    if not model or not base_url:
        return False
    candidates = {glm_identity(provider), (GLM_MODEL, GLM_BASE_URL)}
    for item in extra_identities or ():
        try:
            cand_model, cand_url = item
        except (TypeError, ValueError):
            continue
        if cand_model and cand_url:
            candidates.add((str(cand_model).strip(), str(cand_url).strip()))
    return (model, base_url) in candidates


def drop_glm_fallback(
    provider: Any,
    *,
    extra_identities: "list[tuple[str, str]] | tuple[tuple[str, str], ...]" = (),
) -> int:
    """把链里所有 GLM 备用条目移除，返回移除的条数（用户清空 Key / 停用）。"""
    if provider is None:
        return 0
    fallbacks = list(getattr(provider, "fallbacks", None) or [])
    kept = [
        fb for fb in fallbacks
        if not is_glm_fallback_entry(fb, provider, extra_identities=extra_identities)
    ]
    removed = len(fallbacks) - len(kept)
    if removed:
        provider.fallbacks = kept
    return removed


def ensure_glm_fallback(
    provider: Any,
    *,
    extra_identities: "list[tuple[str, str]] | tuple[tuple[str, str], ...]" = (),
) -> bool:
    """Attach the GLM fallback when SenseNova/Ark + selected + a Key are present.

    Returns True when a fallback entry was appended. Idempotent for the entry
    itself, but the stored entry always tracks the settings: ``settings.json``
    keeps a materialised copy of the chain, so a rotated key — or an edited
    model / base_url — in the form must overwrite the stale copy, otherwise
    every request keeps using the old endpoint/key and fails with 401.

    ``extra_identities`` lets the caller pass the *previous* (model, base_url)
    pair so an entry written by an older configuration is still recognised as
    the GLM entry and updated in place instead of being duplicated.

    ``backup_glm_enabled`` is ANDed with gateway eligibility; a missing key in
    an older settings.json defaults to True so existing behaviour is kept.
    """
    if provider is None or not is_glm_backup_eligible(getattr(provider, "base_url", "")):
        return False
    if not bool(getattr(provider, "backup_glm_enabled", True)):
        return False
    key = str(getattr(provider, "backup_glm_api_key", "") or "").strip()
    if not key:
        return False
    model, base_url = glm_identity(provider)
    fallbacks = list(getattr(provider, "fallbacks", None) or [])
    matched = [
        i for i, cand in enumerate(fallbacks)
        if is_glm_fallback_entry(cand, provider, extra_identities=extra_identities)
    ]
    if matched:
        cand = fallbacks[matched[0]]
        changed = False
        if str(getattr(cand, "api_key", "") or "").strip() != key:
            # 用户换了 Key：设置里保存的备用链是落盘快照，必须跟着刷新。
            cand.api_key = key
            changed = True
            from pa_agent.util.mask_secret import mask_secret

            logger.info("备用链路 Key 已更新为当前配置（%s）", mask_secret(key))
        if str(getattr(cand, "model", "") or "").strip() != model:
            cand.model = model
            changed = True
        if str(getattr(cand, "base_url", "") or "").strip() != base_url:
            cand.base_url = base_url
            changed = True
        if changed:
            logger.info("备用链路已就地更新为 %s @ %s", model, base_url)
        # 历史遗留若有多条 GLM 条目，合并成第一条，绝不留两条。
        if len(matched) > 1:
            for idx in reversed(matched[1:]):
                del fallbacks[idx]
            provider.fallbacks = fallbacks
            logger.info("已清理 %d 条重复的 GLM 备用条目", len(matched) - 1)
        return False
    from pa_agent.config.settings import AIProviderSettings

    fallbacks.append(AIProviderSettings(
        model=model, base_url=base_url, api_key=key,
        thinking=False, reasoning_effort="low",
    ))
    provider.fallbacks = fallbacks
    return True


def is_sensenova_base(base_url: str | None) -> bool:
    """Public helper for the settings UI (kept for backward compatibility)."""
    return _is_sensenova(base_url)


def glm_fallback_available(provider: Any) -> bool:
    """True when the GLM key is set and the primary gateway supports a backup."""
    return (is_glm_backup_eligible(getattr(provider, "base_url", ""))
            and bool(str(getattr(provider, "backup_glm_api_key", "") or "").strip()))
