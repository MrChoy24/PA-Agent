"""交易所 REST 主机列表（可配置）。

OKX 官方除了 ``www.okx.com`` 还有一个 AWS 落地域名 ``aws.okx.com``，
早期代码把它写死在 failover 列表里。实测本机（以及不少国内网络）对该域名
**直接 DNS 解析失败**（``getaddrinfo failed``），后果是：

1. 主域名超时以后，备用域名必然也失败，白白多等一轮超时；
2. 最终错误被聚合成
   ``https://www.okx.com: timed out; https://aws.okx.com: getaddrinfo failed``，
   对用户来说既长又没有可操作信息。

所以这里把主机列表挪出来，默认只留 ``www.okx.com``，
需要额外落地域名时用环境变量显式追加（逗号或分号分隔）：

* ``PA_AGENT_OKX_HOSTS`` —— 推荐名，例：``https://www.okx.com,https://aws.okx.com``
* ``OKX_HOSTS`` —— 简写别名

追加的条目会被规范成「带 scheme、无尾斜杠」的形式，重复与非法条目自动丢弃。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

OKX_DEFAULT_HOST = "https://www.okx.com"

HOSTS_ENV_VARS: tuple[str, ...] = ("PA_AGENT_OKX_HOSTS", "OKX_HOSTS")

#: 历史上默认启用的备用主机。保留常量是为了让文档/迁移代码能引用，
#: 但**不再参与默认列表** —— 见模块 docstring。
OKX_LEGACY_FALLBACK_HOST = "https://aws.okx.com"

_cache_lock = threading.Lock()
_cache_key: tuple[str, ...] = ()
_cache_value: tuple[str, ...] = (OKX_DEFAULT_HOST,)


def _split(text: str) -> list[str]:
    parts: list[str] = []
    for chunk in text.replace(";", ",").replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?::\d{1,5})?$")


def normalize_host(value: Any) -> str:
    """把一个主机配置项规范成 ``https://host`` 形式；非法返回空串。

    只接受「纯主机名（可带端口）」：带路径、带查询串、协议不是 http(s)
    或者主机名里出现非法字符的条目一律丢弃 —— 这类配置写错时静默当备用
    主机是没有意义的，只会在失败列表里多一条看不懂的记录。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    scheme, _, rest = text.partition("://")
    if scheme.lower() not in ("http", "https"):
        return ""
    host_part = rest.rstrip("/").strip()
    if "/" in host_part:
        # 带路径的 URL 一律拒绝：主机列表只描述「协议 + 主机[:端口]」
        return ""
    if not host_part or not _HOST_RE.match(host_part):
        return ""
    return f"{scheme.lower()}://{host_part}"


def _raw_hosts_text() -> str:
    for name in HOSTS_ENV_VARS:
        raw = str(os.environ.get(name, "") or "").strip()
        if raw:
            return raw
    return ""


def okx_hosts() -> tuple[str, ...]:
    """当前的 OKX 主机列表（第一个始终是 ``https://www.okx.com``）。

    环境变量为空时只返回默认主机；有配置时把追加项接在后面。
    结果按「原始配置文本」缓存，配置不变就不重复解析。
    """
    global _cache_key, _cache_value

    raw = _raw_hosts_text()
    key = (raw,)
    with _cache_lock:
        if key == _cache_key:
            return _cache_value

        hosts: list[str] = [OKX_DEFAULT_HOST]
        for item in _split(raw):
            normalized = normalize_host(item)
            if not normalized:
                logger.warning("OKX 主机配置项无效，已忽略：%r", item)
                continue
            if normalized not in hosts:
                hosts.append(normalized)
        if len(hosts) > 1:
            logger.info("OKX 额外主机：%s", ", ".join(hosts[1:]))

        _cache_key = key
        _cache_value = tuple(hosts)
        return _cache_value


def reset_cache() -> None:
    """清空主机列表缓存（测试用）。"""
    global _cache_key, _cache_value
    with _cache_lock:
        _cache_key = ()
        _cache_value = (OKX_DEFAULT_HOST,)


def unreachable_hint(tried: list[str]) -> str:
    """给「所有主机都失败」生成带排查建议的错误文本。"""
    detail = "; ".join(tried) or "unknown network error"
    return (
        f"OKX 请求失败: {detail} —— 请检查网络或代理设置"
        f"（如需额外落地域名，可设置环境变量 {HOSTS_ENV_VARS[0]}）"
    )


__all__ = [
    "HOSTS_ENV_VARS",
    "OKX_DEFAULT_HOST",
    "OKX_LEGACY_FALLBACK_HOST",
    "normalize_host",
    "okx_hosts",
    "reset_cache",
    "unreachable_hint",
]
