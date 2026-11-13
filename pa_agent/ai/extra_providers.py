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


#: 环境变量名：在 ``.env`` 里直接写备用池，免去改 settings.json / 重开容器配置。
FALLBACK_SPEC_ENV = "PA_AGENT_FALLBACKS"

#: 可接受的等义变量名。daemon 的其它键几乎都是 ``PA_DAEMON_*`` 前缀，只有这一项
#: 是 ``PA_AGENT_*``，非常容易写错；写错后既不报错也不生效，是最难自查的一类问题。
#: 所以两种前缀都认，并在检测到「像但不对」的名字时给出提示。
FALLBACK_SPEC_ENV_ALIASES = ("PA_DAEMON_FALLBACKS",)

#: 近似拼写（缺复数、大小写变体等）：命中就提示，避免静默失效。
_FALLBACK_SPEC_LOOKALIKES = (
    "PA_DAEMON_FALLBACK",
    "PA_AGENT_FALLBACK",
    "PA_AGENT_FALLBACK_POOL",
    "PA_DAEMON_FALLBACK_POOL",
    "FALLBACKS",
)


def read_fallback_spec() -> tuple[str, str]:
    """读备用池环境变量，返回 ``(原始值, 命中的变量名)``；都没设返回 ``("", "")``。

    别名与主名同时存在时以主名为准，并在日志里提示另一个被忽略。
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    for name in (FALLBACK_SPEC_ENV, *FALLBACK_SPEC_ENV_ALIASES):
        raw = str(os.environ.get(name, "") or "").strip()
        if raw:
            others = [
                other
                for other in (FALLBACK_SPEC_ENV, *FALLBACK_SPEC_ENV_ALIASES)
                if other != name and str(os.environ.get(other, "") or "").strip()
            ]
            if others:
                log.warning(
                    "%s 与 %s 同时设置，按 %s 里的内容为准（另一个被忽略）",
                    name,
                    "、".join(others),
                    name,
                )
            return raw, name
    # 一个都没设：看看是不是拼错了名字
    for other in _FALLBACK_SPEC_LOOKALIKES:
        if str(os.environ.get(other, "") or "").strip():
            log.warning(
                "环境变量 %s 不是有效的备用池变量名（应为 %s 或 %s），已忽略；"
                "备用池仍按 settings.json 里的内容生效",
                other,
                FALLBACK_SPEC_ENV,
                FALLBACK_SPEC_ENV_ALIASES[0],
            )
            break
    return "", ""

#: 单条备用的字段分隔符与条目分隔符。用 ``|`` 与 ``;`` 是因为它们不会出现在
#: base_url / model 里，也不像逗号那样被 shell 或 compose 当成列表语法。
_FIELD_SEP = "|"
_ENTRY_SEP = ";"

#: api_key 字段写这个值 = 复用主通道的 Key（同网关下多个模型共用一个 Key 的常见情况）。
_KEY_REUSE_MARKERS = frozenset({"", "-", "*", "same", "reuse"})


def parse_fallback_spec(text: Any, *, default_key: str = "") -> tuple[list[Any], list[str]]:
    """把 ``.env`` 里的备用池字符串解析成完整的 provider 配置列表。

    格式（条目之间用 ``;``，字段之间用 ``|``）::

        name|model|base_url|api_key; name|model|base_url|api_key

    * ``name`` 可省略（留空则用 model 当显示名）。
    * ``api_key`` 写 ``-`` / 留空 / 省略，表示复用主通道的 Key。
    * 无法解析或字段不足的条目会被跳过并返回在第二个返回值里。

    返回 ``(entries, problems)``。``problems`` 只含可直接打印给人看的短句，
    不含 Key。
    """
    from pa_agent.config.settings import AIProviderSettings

    raw = str(text or "").strip()
    if not raw:
        return [], []

    entries: list[Any] = []
    problems: list[str] = []
    for index, chunk in enumerate(raw.split(_ENTRY_SEP), start=1):
        item = chunk.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(_FIELD_SEP)]
        # 允许 3 字段（无 Key）与 4 字段两种写法。
        if len(parts) < 3:
            problems.append(f"第 {index} 条字段不足（至少要 name|model|base_url）")
            continue
        name, model, base_url = parts[0], parts[1], parts[2]
        key_field = parts[3] if len(parts) > 3 else ""
        if not model or not base_url:
            problems.append(f"第 {index} 条缺少 model 或 base_url")
            continue
        api_key = default_key if key_field.lower() in _KEY_REUSE_MARKERS else key_field
        if not api_key:
            problems.append(f"第 {index} 条没有可用 api_key（也没有主通道 Key 可复用）")
            continue
        entry = AIProviderSettings(
            name=name or model,
            model=model,
            base_url=base_url,
            api_key=api_key,
            thinking=True,
            reasoning_effort="low",
            enabled=True,
        )
        entries.append(normalize_entry(entry))
    return entries, problems


#: 已经说明过的 PA_AGENT_FALLBACKS 内容；同一份配置只播报一次。
_ANNOUNCED_SPECS: set[str] = set()


def env_fallbacks(*, default_key: str = "") -> list[Any]:
    """``PA_AGENT_FALLBACKS`` 里声明的备用池（服务器上改一行即可生效）。

    没有该变量时返回空列表，因此不设它的部署行为完全不变。
    解析失败只写 WARNING，绝不让启动失败 —— 一个拼错的备用池不该
    把整个守护进程卡死。
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    raw, source = read_fallback_spec()
    if not raw:
        return []
    entries, problems = parse_fallback_spec(raw, default_key=default_key)
    for problem in problems:
        log.warning("%s 配置有误，已跳过：%s", FALLBACK_SPEC_ENV, problem)
    # 这是「配置长什么样」的一次性说明，不是每次请求的进度。
    # combined_fallback_chain 每轮分析都会重新解析，不去重就会每轮刷一条。
    if entries and raw not in _ANNOUNCED_SPECS:
        _ANNOUNCED_SPECS.add(raw)
        log.info(
            "%s 提供了 %d 条备用接口：%s",
            source,
            len(entries),
            ", ".join(str(getattr(e, "name", "") or e.model) for e in entries),
        )
    return entries


def combined_fallback_chain(provider: Any, settings: Any = None) -> list[Any]:
    """派生链 + 用户自定义备用，按顺序去重合并。

    ``provider.fallbacks`` 在前（方舟池、GLM），用户自定义项接在后面，
    这样「主通道 → 方舟池 → GLM → 我的备用 → 免费池」的顺序是稳定的。

    用户自定义部分有两个来源，``.env`` 的 ``PA_AGENT_FALLBACKS`` 优先于
    settings.json 的 ``ai_backups``：服务器上改一行环境变量比重挂配置文件省事。
    """
    # 环境变量里写了备用池就以它为准，整段替换（派生链与 ai_backups 都不再参与）。
    # 理由：用户既然在 .env 里列了清单，链上再混进 settings.json 的旧条目，
    # 现场就没人说得清最终顺序了。
    env_entries = env_fallbacks(default_key=str(getattr(provider, "api_key", "") or "").strip())
    if env_entries:
        chain: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for item in env_entries:
            key = entry_key(item)
            if key in seen or not key[0] or not key[1]:
                continue
            seen.add(key)
            chain.append(item)
        return chain

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
