"""OKX V5 下单客户端（现货 SPOT + U 本位永续 SWAP）。

签名方式与 :mod:`pa_agent.data.okx_source` 完全一致：
``base64(hmac_sha256(secret, timestamp + method + requestPath + body))``，
附带 ``OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP /
OK-ACCESS-PASSPHRASE``；``demo=True`` 时附加 ``x-simulated-trading: 1``。

方法名与签名刻意与 :class:`pa_agent.trading.binance_trader.BinanceTrader`
保持一致（参数用 OKX 口径：``inst_id`` / ``buy|sell`` / ``market|limit``），
方便 ``AutoTrader`` 在两个交易所之间切换。

现货与合约的差异点（**都在本文件内收敛，调用方不用关心**）：

============  ==========================  ==============================
维度          现货 SPOT                   合约 SWAP（U 本位永续）
============  ==========================  ==============================
instId        ``BTC-USDT``                ``BTC-USDT-SWAP``
instType      ``SPOT``                    ``SWAP``
tdMode        ``cash``（固定）            ``isolated`` / ``cross``
posSide       无                          ``long_short_mode`` 才传（否则 51010）
sz 单位       基础币数量                   **张数**（= 基础币 / ctVal·ctMult）
杠杆          无                          ``/api/v5/account/set-leverage``
============  ==========================  ==============================

``sz`` 单位是最容易出错的地方：OKX SWAP 的 ``sz`` 是**张数**，每张面值由
``GET /api/v5/public/instruments?instType=SWAP`` 的 ``ctVal`` 给出
（单位：基础币）。所以 ``0.05 BTC`` 在 ``ctVal=0.01`` 的合约上是 ``5`` 张。
本模块统一用 :meth:`OKXTrader._contracts_for` 换算，按 ``lotSz`` 步长向下取整
（``lotSz`` 常为 ``0.01``，所以张数可以是小数，例如 ``0.5`` 张）；
换算结果低于 ``minSz`` 时返回 0 由调用方拒单（**绝不向上兜底到 1 张**，
那会让名义金额成倍放大）。

Usage:
    trader = OKXTrader(api_key="...", api_secret="...", passphrase="...")
    ok, msg = trader.test_connection()
    res = trader.place_order("BTC-USDT", "buy", "market", notional_usdt=20)

    # 合约：10 USDT 保证金 × 20 倍 = 200 USDT 名义，逐仓，做空
    res = trader.place_order(
        "BTC-USDT-SWAP", "sell", "market",
        market_type="futures", margin_usdt=10, leverage=20,
        margin_mode="isolated", position_side="short",
    )
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any

from pa_agent.data.crypto_common import to_okx_inst_id
from pa_agent.trading.common import (
    MARKET_FUTURES,
    MARKET_SPOT,
    MARGIN_CROSS,
    MARGIN_ISOLATED,
    POSITION_LONG,
    POSITION_SHORT,
    PROTECT_STOP_LOSS,
    PROTECT_TAKE_PROFIT_1,
    PROTECT_TAKE_PROFIT_2,
    ProtectionOrder,
    TradeResult,
    clamp_leverage,
    floor_to_precision,
    floor_to_step,
    format_decimal,
    normalize_margin_mode,
    normalize_market_type,
    normalize_position_side,
    to_decimal,
    to_float,
    to_text,
)
from pa_agent.util import endpoints, throttle

logger = logging.getLogger(__name__)

_HOSTS: tuple[str, ...] = (
    "https://www.okx.com",
    "https://aws.okx.com",
)

# ``_HOSTS`` 是历史常量：``aws.okx.com`` 在很多网络下 DNS 解析不了，
# 直接当备用会导致主域名超时后必然再失败一轮。实际候选列表改由
# ``pa_agent.util.endpoints.okx_hosts()`` 提供（默认只有 www.okx.com，
# 可用环境变量 PA_AGENT_OKX_HOSTS 追加）。


def _host_candidates(known: str) -> list[str]:
    """当前可用的 OKX 主机，已确认过的 *known* 排在最前。"""
    hosts = list(endpoints.okx_hosts())
    if known:
        return [known] + [h for h in hosts if h != known]
    return hosts

_BALANCE_PATH = "/api/v5/account/balance"
_ORDER_PATH = "/api/v5/trade/order"
_ALGO_ORDER_PATH = "/api/v5/trade/order-algo"
_ALGO_PENDING_PATH = "/api/v5/trade/orders-algo-pending"
_ORDER_DETAIL_PATH = "/api/v5/trade/order"
_CANCEL_ALGO_PATH = "/api/v5/trade/cancel-algos"
_OPEN_ORDERS_PATH = "/api/v5/trade/orders-pending"
_CANCEL_PATH = "/api/v5/trade/cancel-order"
_CLOSE_POSITION_PATH = "/api/v5/trade/close-position"
_INSTRUMENTS_PATH = "/api/v5/public/instruments"
_TICKER_PATH = "/api/v5/market/ticker"
_POSITIONS_PATH = "/api/v5/account/positions"
_SET_LEVERAGE_PATH = "/api/v5/account/set-leverage"
_ACCOUNT_CONFIG_PATH = "/api/v5/account/config"

def _per_contract(rules: dict[str, Any]) -> float:
    """每张合约代表的基础币数量（``ctVal × ctMult``），无效时返回 0。"""
    ct_val = to_float(rules.get("ct_val"))
    ct_mult = to_float(rules.get("ct_mult"), 1.0) or 1.0
    return ct_val * ct_mult


# 交易所默认最小名义金额（instrument 缺失时的保底值，单位 USDT）
_DEFAULT_MIN_NOTIONAL = 5.0

_HOST_LEVEL_STATUS = (403, 418, 429, 451, 500, 502, 503, 504)

# 持仓模式：双向持仓必须传 posSide，净持仓（net）传了会报 51010
_POS_MODE_LONG_SHORT = "long_short_mode"
_POS_MODE_NET = "net_mode"


def to_okx_swap_inst_id(symbol: str) -> str:
    """``BTCUSDT`` / ``BTC-USDT`` / ``BTC-USDT-SWAP`` → ``BTC-USDT-SWAP``。

    与 :func:`pa_agent.data.crypto_common.to_okx_inst_id` 同源，只是追加
    ``-SWAP`` 后缀；已是 SWAP 的原样返回。空输入返回空串，永不抛异常。
    """
    text = to_text(symbol).upper()
    if not text:
        return ""
    if text.endswith("-SWAP"):
        return text
    # 已经带分隔符（BTC-USDT）时 to_okx_inst_id 会原样返回
    return f"{to_okx_inst_id(text)}-SWAP"


class OKXTrader:
    """OKX 交易客户端（现货 ``tdMode=cash``，合约 ``isolated``/``cross``）。"""

    venue: str = "okx"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo: bool = False,
        timeout_s: float = 10.0,
        market_type: str = MARKET_SPOT,
        leverage: int = 1,
        margin_mode: str = MARGIN_ISOLATED,
    ) -> None:
        self._api_key: str = (api_key or "").strip()
        self._api_secret: str = (api_secret or "").strip()
        self._passphrase: str = (passphrase or "").strip()
        self._demo: bool = bool(demo)
        self._timeout_s: float = timeout_s

        self._market_type: str = normalize_market_type(market_type)
        self._leverage: int = clamp_leverage(leverage)
        self._margin_mode: str = normalize_margin_mode(margin_mode)

        self._client_obj = None          # 延迟创建的 httpx.Client
        self._host: str = ""             # 已确认可用的主机（空 = 尚未解析）
        # (instType, instId) → 交易规则（lotSz/minSz/tickSz/ctVal/ctMult）
        self._filters: dict[tuple[str, str], dict[str, Any]] = {}
        # 缓存的持仓模式（"long_short_mode" / "net_mode" / "" 未知）
        self._pos_mode: str = ""

    # ── 实例默认参数 ──────────────────────────────────────────────────────────

    @property
    def market_type(self) -> str:
        """当前实例默认市场类型（``spot`` / ``futures``）。"""
        return self._market_type

    @property
    def leverage(self) -> int:
        """当前实例默认杠杆倍数（1~125）。"""
        return self._leverage

    @property
    def margin_mode(self) -> str:
        """当前实例默认保证金模式（``isolated`` / ``cross``）。"""
        return self._margin_mode

    def _inst_type(self, market: str | None = None) -> str:
        """``SPOT`` / ``SWAP``（按 *market* 或实例默认市场）。"""
        mkt = normalize_market_type(market if market is not None else self._market_type)
        return "SWAP" if mkt == MARKET_FUTURES else "SPOT"

    # ── HTTP plumbing ─────────────────────────────────────────────────────────

    def _client(self):
        """Lazily create the shared httpx client."""
        if self._client_obj is None:
            import httpx  # 延迟导入：httpx 缺失时只在调用处报错

            self._client_obj = httpx.Client(
                timeout=self._timeout_s,
                follow_redirects=True,
            )
        return self._client_obj

    def close(self) -> None:
        """Release the underlying HTTP client (safe to call twice)."""
        if self._client_obj is not None:
            try:
                self._client_obj.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("OKX trader client close error: %s", exc)
            finally:
                self._client_obj = None

    def _has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret and self._passphrase)

    @staticmethod
    def _error_text(payload: Any, status_code: int = 0) -> str:
        """Extract OKX's own ``code``/``msg``/``sCode`` text."""
        if isinstance(payload, dict):
            code = str(payload.get("code", "") or "")
            msg = str(payload.get("msg", "") or "")
            detail = ""
            for item in payload.get("data", ()) or ():
                if isinstance(item, dict):
                    sub_code = str(item.get("sCode", "") or "")
                    sub_msg = str(item.get("sMsg", "") or "")
                    if sub_msg:
                        detail = f"{sub_code}: {sub_msg}".strip(": ")
                        break
            parts = [p for p in (f"OKX 错误 {code}" if code else "", msg, detail) if p]
            if parts:
                return " ".join(parts)
        if status_code:
            return f"OKX HTTP {status_code}"
        return "OKX 未知错误"

    def _signed_headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        """Build OKX V5 auth headers for one request."""
        # ISO-8601 UTC 毫秒精度，例：2020-12-08T09:08:57.715Z
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        sign = base64.b64encode(
            hmac.new(
                self._api_secret.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        headers = {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }
        if self._demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        signed: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        """Issue one OKX request; returns ``(payload, error_text)``.

        成功（``code == "0"``）返回完整 payload，``error_text`` 为空。
        所有异常在这里收敛，调用方无需 try/except。
        """
        if signed and not self._has_credentials():
            return None, "未填写 OKX API Key/Secret/Passphrase"

        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001 — httpx 缺失
            return None, f"httpx 未安装 — 请执行: pip install httpx（{exc}）"

        method = method.upper()

        # 组装 requestPath（GET 的查询串也算在签名里）
        query = ""
        if params:
            items = []
            for key, value in params.items():
                if value is None or value == "":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    items.append(f"{key}={format_decimal(value)}")
                else:
                    items.append(f"{key}={value}")
            if items:
                query = "?" + "&".join(
                    f"{k}={urllib.parse.quote(str(v), safe='-_.~')}"
                    for k, v in (item.split("=", 1) for item in items)
                )
        request_path = f"{path}{query}"

        body_text = ""
        if body is not None:
            body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

        headers = (
            self._signed_headers(method, request_path, body_text)
            if signed
            else ({"x-simulated-trading": "1"} if self._demo else {})
        )

        # 下单/撤单只发一次：超时后状态未知，重试可能重复成交
        allow_failover = method == "GET"
        if allow_failover:
            candidates = _host_candidates(self._host)
        else:
            candidates = [self._host or endpoints.OKX_DEFAULT_HOST]

        tried: list[str] = []
        for host in candidates:
            if not host:
                continue
            # 本地节流：同一个进程里所有 OKX 请求共用一个令牌桶，
            # 把「一秒几十个请求」这类尖峰抹平，避免撞上交易所限频。
            throttle.acquire("okx")
            url = f"{host}{request_path}"
            try:
                resp = client.request(
                    method,
                    url,
                    headers=headers,
                    content=body_text.encode("utf-8") if body_text else None,
                )
            except Exception as exc:  # noqa: BLE001
                tried.append(f"{host}: {exc}")
                logger.warning("OKX %s %s failed on %s: %s", method, path, host, exc)
                continue

            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                if resp.status_code in _HOST_LEVEL_STATUS and allow_failover:
                    tried.append(f"{host}: HTTP {resp.status_code}")
                    continue
                return None, f"OKX 返回非 JSON 数据（HTTP {resp.status_code}）: {exc}"

            code = ""
            if isinstance(payload, dict):
                code = str(payload.get("code", "") or "")

            if code == "0":
                self._host = host
                return payload, ""

            if resp.status_code in _HOST_LEVEL_STATUS and allow_failover:
                tried.append(f"{host}: HTTP {resp.status_code}")
                logger.debug("OKX host %s returned %s; trying next", host, resp.status_code)
                continue

            self._host = host
            return None, self._error_text(payload, resp.status_code)

        detail = "; ".join(tried) or "unknown network error"
        logger.warning("OKX %s %s gave up: %s", method, path, detail)
        return None, endpoints.unreachable_hint(tried)

    # ── 交易规则 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_inst_id(inst_id: str) -> str:
        """接受 ``BTCUSDT`` 或 ``BTC-USDT``，统一成 ``BTC-USDT``。"""
        text = to_text(inst_id).upper()
        if not text:
            return ""
        if "-" in text:
            return text
        return to_okx_inst_id(text)

    @classmethod
    def _normalize_swap_inst_id(cls, inst_id: str) -> str:
        """接受任意写法，统一成 ``BTC-USDT-SWAP``（空输入返回空串）。"""
        text = to_text(inst_id).upper()
        if not text:
            return ""
        return to_okx_swap_inst_id(text)

    def _inst_id_for(self, inst_id: str, market: str | None = None) -> str:
        """按市场类型把用户输入归一成真实的 instId。"""
        mkt = normalize_market_type(market if market is not None else self._market_type)
        if mkt == MARKET_FUTURES:
            return self._normalize_swap_inst_id(inst_id)
        return self._normalize_inst_id(inst_id)

    def _pending_algo_orders(self, inst_id: str) -> list[dict[str, Any]]:
        """挡在当前交易对上的在挂条件单（``orders-algo-pending``）。

        查询失败一律返回空列表：宁可重复挂一次（后面的幂等判断兜底），
        也不能因为一次查询抖动就让保护单完全挂不上。
        """
        payload, err = self._request(
            "GET",
            f"{_ALGO_PENDING_PATH}?ordType=conditional&instId={inst_id}",
            signed=True,
        )
        if payload is None:
            logger.warning(
                "查询 %s 在挂条件单失败（%s），本次按「没有在挂单」处理", inst_id, err
            )
            return []
        rows = payload.get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    def _instrument_rules(
        self, inst_id: str, inst_type: str | None = None
    ) -> dict[str, Any]:
        """Load and cache trading rules for *inst_id*.

        SPOT 取 ``lotSz / minSz / tickSz / minNotionalUsd``；
        SWAP 额外取 ``ctVal``（每张面值，单位基础币）与 ``ctMult``，
        这是把「基础币数量」换算成「张数」的唯一依据。
        """
        kind = (to_text(inst_type) or self._inst_type()).upper()
        key = (kind, inst_id)
        cached = self._filters.get(key)
        if cached is not None:
            return cached

        rules: dict[str, Any] = {}
        payload, err = self._request(
            "GET",
            _INSTRUMENTS_PATH,
            {"instType": kind, "instId": inst_id},
            signed=False,
        )
        if payload is None:
            logger.debug("OKX instruments(%s, %s) unavailable: %s", kind, inst_id, err)
            return rules

        for item in payload.get("data", ()) or ():
            if not isinstance(item, dict):
                continue
            if str(item.get("instId", "")).upper() != inst_id:
                continue
            rules["step"] = to_float(item.get("lotSz"))
            rules["min_qty"] = to_float(item.get("minSz"))
            rules["tick"] = to_float(item.get("tickSz"))
            rules["min_notional"] = to_float(item.get("minNotionalUsd")) or to_float(
                item.get("minNotional")
            )
            # 合约面值：sz(=张数) × ctVal × ctMult = 基础币数量
            rules["ct_val"] = to_float(item.get("ctVal"))
            rules["ct_mult"] = to_float(item.get("ctMult"), 1.0) or 1.0
            rules["ct_type"] = item.get("ctType")
            rules["inst_type"] = kind
            break

        if rules:
            self._filters[key] = rules
        return rules

    def last_price(self, inst_id: str) -> float:
        """公开的交易所最新价（无需 API Key）；取不到时返回 ``0.0``。

        用于跨所价差保护：拿 Binance 行情做分析、到 OKX 下单时，
        两边价格偏离过大就不该下单。
        """
        return self._last_price(inst_id)

    def _last_price(self, inst_id: str) -> float:
        """Public ticker last price; ``0.0`` when unavailable."""
        payload, err = self._request(
            "GET", _TICKER_PATH, {"instId": inst_id}, signed=False
        )
        if payload is None:
            logger.debug("OKX ticker(%s) unavailable: %s", inst_id, err)
            return 0.0
        for item in payload.get("data", ()) or ():
            if isinstance(item, dict):
                price = to_float(item.get("last"))
                if price > 0:
                    return price
        return 0.0

    def _fill_price_for(self, inst_id: str, ord_id: str, *, retries: int = 3) -> float:
        """查一笔委托的真实成交均价 ``avgPx``；查不到返回 ``0.0``。

        为什么要单独查：``POST /trade/order`` 的响应里**没有成交价**，
        市价单尤其如此。早期版本直接把「限价」字段塞进 ``TradeResult.price``，
        市价单就变成 ``0.0``，下游用它当保护单的几何基准就会全错
        （止损按决策价校验，而不是真实成交价）。

        市价单刚提交时可能还没成交完，``avgPx`` 会是 ``""``，
        所以要短轮询几次；仍然拿不到就返回 0，由调用方决定怎么降级。
        """
        if not ord_id:
            return 0.0
        delay = 0.25
        for attempt in range(max(1, retries)):
            payload, err = self._request(
                "GET",
                _ORDER_DETAIL_PATH,
                {"instId": inst_id, "ordId": ord_id},
                signed=True,
            )
            if payload is None:
                logger.debug("OKX 查询成交价失败（%s/%s）: %s", inst_id, ord_id, err)
                return 0.0
            for item in payload.get("data", ()) or ():
                if not isinstance(item, dict):
                    continue
                avg = to_float(item.get("avgPx"))
                if avg > 0:
                    return avg
                # 完全没成交就再等一下；部分成交也把 avgPx 先记下来
                filled = to_float(item.get("accFillSz"))
                if filled > 0:
                    avg = to_float(item.get("avgPx"))
                    if avg > 0:
                        return avg
            if attempt + 1 < retries:
                time.sleep(delay)
                delay *= 2
        return 0.0

    def _contracts_for(self, base_qty: float, rules: dict[str, Any]) -> float:
        """把基础币数量换算成 SWAP **张数**，按 ``lotSz`` 步长向下取整。

        ``sz`` 的单位是张，每张代表 ``ctVal × ctMult`` 个基础币。
        例：``ctVal=0.1``、目标 ``0.05 ETH`` → ``0.5`` 张。

        **张数不一定是整数**：OKX 合约的 ``lotSz`` 常见为 ``0.01``，
        允许下 ``0.01`` 张（ETH-USDT-SWAP 即 0.001 ETH ≈ 1.9 USDT）。
        早期版本把张数当 ``int`` 并向下取整，等于强制 1 张起步，
        既会放大名义金额，也让 1 张仓位无法拆成两段止盈 —— 都是错的。

        ``ctVal`` 缺失（接口不可用）时无法安全换算，返回 ``0`` 让调用方拒单。
        也**不做 "至少一张" 的兜底**：那会把小额订单悄悄放大成 1 张。
        """
        per_contract = _per_contract(rules)
        if per_contract <= 0 or base_qty <= 0:
            return 0.0
        # 必须用 Decimal：0.3 / 0.1 在二进制浮点下是 2.9999999999999996，
        # 直接除完取整会少算一份 —— 止损就会漏保护一部分仓位。
        amount = to_decimal(base_qty)
        unit = to_decimal(per_contract)
        if amount is None or unit is None or unit <= 0:
            return 0.0
        try:
            raw = amount / unit
        except (InvalidOperation, ZeroDivisionError, ValueError):
            return 0.0
        if raw <= 0:
            return 0.0
        # lotSz 是下单张数的步长；缺失时退回 1（等价于只允许整张）。
        lot = to_decimal(rules.get("step"))
        if lot is None or lot <= 0:
            lot = Decimal(1)
        try:
            steps = (raw / lot).to_integral_value(rounding=ROUND_DOWN)
            return float((steps * lot).normalize())
        except (InvalidOperation, ValueError):
            return 0.0

    # ── 账户 ──────────────────────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """Check credentials against the signed balance endpoint."""
        if not self._has_credentials():
            return False, "未填写 OKX API Key/Secret/Passphrase"

        payload, err = self._request("GET", _BALANCE_PATH, {}, signed=True)
        if payload is None:
            return False, err
        return (
            True,
            f"OKX API 连接成功（账户余额接口可用, demo={self._demo}）",
        )

    def _balance_details(self, ccy: str = "") -> tuple[list[dict[str, Any]], str]:
        """Signed balance details; ``([], error)`` on failure."""
        params: dict[str, Any] = {"ccy": ccy} if ccy else {}
        payload, err = self._request("GET", _BALANCE_PATH, params, signed=True)
        if payload is None:
            return [], err

        out: list[dict[str, Any]] = []
        for account in payload.get("data", ()) or ():
            if not isinstance(account, dict):
                continue
            for item in account.get("details", ()) or ():
                if not isinstance(item, dict):
                    continue
                code = str(item.get("ccy", "")).upper()
                if ccy and code != ccy:
                    continue
                cash = to_float(item.get("cashBal"))
                avail = to_float(item.get("availBal"), cash)
                frozen = to_float(item.get("frozenBal"))
                out.append(
                    {
                        "ccy": code,
                        "cash": cash,
                        "avail": avail,
                        "frozen": frozen,
                    }
                )
        return out, ""

    def get_balance(self, ccy: str = "") -> tuple[bool, str, float]:
        """Return ``(ok, message, total)`` for *ccy* (``cashBal`` 口径)."""
        code = to_text(ccy).upper()
        if not code:
            return False, "未指定币种（ccy 为空）", 0.0
        if not self._has_credentials():
            return False, "未填写 OKX API Key/Secret/Passphrase", 0.0

        details, err = self._balance_details(code)
        if err:
            return False, err, 0.0
        for item in details:
            if item["ccy"] == code:
                return (
                    True,
                    f"{code} 余额 {format_decimal(item['cash'])}"
                    f"（可用 {format_decimal(item['avail'])}，"
                    f"冻结 {format_decimal(item['frozen'])}）",
                    item["cash"],
                )
        return False, f"OKX 账户中未找到 {code} 余额", 0.0

    def get_free_balance(self, ccy: str = "") -> float:
        """可用余额（失败时返回 ``0.0``，不抛异常）。"""
        code = to_text(ccy).upper()
        if not code or not self._has_credentials():
            return 0.0
        details, err = self._balance_details(code)
        if err:
            logger.warning("OKX get_free_balance(%s) failed: %s", code, err)
            return 0.0
        for item in details:
            if item["ccy"] == code:
                return item["avail"]
        return 0.0

    # ── 持仓模式 / 杠杆 / 持仓查询 ────────────────────────────────────────────

    def get_position_mode(self, refresh: bool = False) -> tuple[bool, str, str]:
        """读取账户持仓模式 ``(ok, "long_short_mode"|"net_mode"|"", message)``。

        对应 ``GET /api/v5/account/config`` 的 ``posMode`` 字段。
        ``long_short_mode``（双向持仓）下单才需要 ``posSide``；
        ``net_mode``（净持仓，OKX 默认）传 ``posSide`` 会报 **51010**。

        结果会缓存到 ``self._pos_mode``；``refresh=True`` 强制重读。
        """
        if not refresh and self._pos_mode:
            return True, self._pos_mode, f"OKX 持仓模式：{self._pos_mode}"
        if not self._has_credentials():
            return False, "", "未填写 OKX API Key/Secret/Passphrase"

        payload, err = self._request("GET", _ACCOUNT_CONFIG_PATH, {}, signed=True)
        if payload is None:
            return False, "", err or "OKX 查询持仓模式失败"
        rows = payload.get("data") or []
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        mode = str(first.get("posMode", "") or "").strip()
        if mode not in (_POS_MODE_LONG_SHORT, _POS_MODE_NET):
            return False, "", f"OKX 持仓模式未知：{mode or '空'}"
        self._pos_mode = mode
        return True, mode, f"OKX 持仓模式：{mode}"

    def _is_long_short_mode(self) -> bool:
        """True only when the account is confirmed in ``long_short_mode``."""
        ok, mode, msg = self.get_position_mode()
        if not ok:
            logger.warning(
                "OKX 持仓模式未知（%s），按 net 模式处理（不传 posSide）", msg
            )
            return False
        return mode == _POS_MODE_LONG_SHORT

    def set_leverage(self, inst_id: str, leverage: int | None = None) -> TradeResult:
        """设置合约杠杆（``POST /api/v5/account/set-leverage``）。

        现货不使用杠杆，直接返回 ``ok=True`` 说明（不发起任何请求）。
        """
        lev = clamp_leverage(self._leverage if leverage is None else leverage)
        is_futures = self._market_type == MARKET_FUTURES
        inst = self._inst_id_for(inst_id)

        if not is_futures:
            return TradeResult(
                ok=True,
                venue=self.venue,
                symbol=inst,
                message="现货不使用杠杆（market_type=spot），已忽略 set_leverage",
                market_type=self._market_type,
                leverage=1,
            )
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message="未填写 OKX API Key/Secret/Passphrase，无法设置杠杆",
                market_type=self._market_type,
                leverage=lev,
            )
        if not inst:
            return TradeResult(
                ok=False,
                venue=self.venue,
                message="设置杠杆需要 inst_id",
                market_type=self._market_type,
                leverage=lev,
            )

        payload, err = self._request(
            "POST",
            _SET_LEVERAGE_PATH,
            body={"instId": inst, "lever": str(lev), "mgnMode": self._margin_mode},
            signed=True,
        )
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message=err or "OKX 设置杠杆失败",
                market_type=self._market_type,
                leverage=lev,
                margin_mode=self._margin_mode,
            )

        self._leverage = lev
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=inst,
            message=f"OKX {inst} 杠杆已设为 {lev}x（{self._margin_mode}）",
            raw=dict(payload),
            market_type=self._market_type,
            leverage=lev,
            margin_mode=self._margin_mode,
        )

    def get_positions(self) -> tuple[bool, str, list[dict]]:
        """列出 SWAP 持仓 ``(ok, message, positions)``。

        对应 ``GET /api/v5/account/positions?instType=SWAP``，过滤掉
        ``pos == 0`` 的空仓。每项精简为：
        ``{"symbol","position_side","size","entry_price","mark_price",
        "unrealized_pnl","leverage","margin"}``（字段与 Binance 版对齐）。
        """
        if not self._has_credentials():
            return False, "未填写 OKX API Key/Secret/Passphrase", []

        payload, err = self._request(
            "GET", _POSITIONS_PATH, {"instType": "SWAP"}, signed=True
        )
        if payload is None:
            return False, err or "OKX 查询持仓失败", []

        out: list[dict[str, Any]] = []
        for item in payload.get("data", ()) or ():
            if not isinstance(item, dict):
                continue
            pos = to_float(item.get("pos"))
            if pos == 0:
                continue
            raw_side = str(item.get("posSide", "") or "").lower()
            if raw_side in (POSITION_LONG, POSITION_SHORT):
                side = raw_side
            else:  # net 模式返回 net，用数量正负判断
                side = POSITION_LONG if pos > 0 else POSITION_SHORT
            out.append(
                {
                    "symbol": str(item.get("instId", "")),
                    "position_side": side,
                    "size": abs(pos),
                    "entry_price": to_float(item.get("avgPx")),
                    "mark_price": to_float(item.get("markPx")),
                    "unrealized_pnl": to_float(item.get("upl")),
                    "leverage": int(to_float(item.get("lever"), 1.0)),
                    "margin": to_float(item.get("margin") or item.get("imr")),
                }
            )
        return True, f"OKX 持仓 {len(out)} 个", out

    # ── 与 BinanceTrader 对齐的别名 ──────────────────────────────────────────
    # OKX 与 Binance 的账户体系不同，这里补齐同名方法，避免上层代码为两个
    # 交易所写两套分支：持仓 = get_positions()，合约余额 = get_balance()。

    def list_positions(self) -> tuple[bool, str, list[dict]]:
        """``get_positions()`` 的别名（与 BinanceTrader 同名）。"""
        return self.get_positions()

    def get_futures_balance(self, ccy: str = "USDT") -> tuple[bool, str, float]:
        """查询合约账户某币种余额 ``(ok, message, free)``。"""
        return self.get_balance(ccy)

    def set_margin_mode(
        self, inst_id: str = "", margin_mode: str | None = None
    ) -> TradeResult:
        """OKX 的保证金模式随每笔订单的 ``tdMode`` 传递，无需单独设置。

        保留该方法只是为了和 :class:`~pa_agent.trading.binance_trader.BinanceTrader`
        的调用方式一致；传入 ``margin_mode`` 时会更新实例默认值。
        """
        mode = normalize_margin_mode(
            self._margin_mode if margin_mode is None else margin_mode
        )
        self._margin_mode = mode
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=self._inst_id_for(inst_id) if inst_id else "",
            message=(
                f"OKX 按订单设置保证金模式（tdMode={mode}），已在实例上生效，"
                "无需调用接口"
            ),
            market_type=self._market_type,
            leverage=self._leverage,
            margin_mode=mode,
        )

    # ── 下单 / 撤单 / 平仓 / 查询 ─────────────────────────────────────────────

    def place_order(
        self,
        inst_id: str,
        side: str,
        order_type: str = "market",
        notional_usdt: float | None = None,
        quantity: float | None = None,
        price: float | None = None,
        *,
        market_type: str | None = None,
        leverage: int | None = None,
        margin_mode: str | None = None,
        position_side: str = POSITION_LONG,
        reduce_only: bool = False,
        margin_usdt: float | None = None,
        trigger_price: float | None = None,
    ) -> TradeResult:
        """下现货或合约单，按 ``quantity`` / ``notional_usdt`` / ``margin_usdt`` 定量。

        ``trigger_price`` 只对 ``order_type="trigger"`` 生效：这是 OKX 的
        **计划委托**，价格触到 ``trigger_price`` 后才把单子送进撮合。
        突破单用它来避免「分析耗时几分钟、价格已经越过入场价」的滑点。

        ``quantity`` 一律是**基础币数量**（如 0.05 BTC）；合约内部会按
        ``ctVal`` 换算成张数再放进 ``sz``（参见 :meth:`_contracts_for`）。

        * ``market_type=None`` → 用实例默认市场（构造参数）。
        * ``margin_usdt`` 会先 ``× 杠杆`` 得到名义金额。
        * 现货市价单按金额下单时用 ``tgtCcy="quote_ccy"``（OKX 原生支持）。
        * 合约 ``tdMode`` 是 ``isolated`` / ``cross``；现货固定 ``cash``。
        * ``posSide`` 只在 ``long_short_mode`` 传（net 模式传了报 51010）。
        * 合约下单前先 ``set_leverage()``，失败只 warning 不阻断。
        """
        mkt = normalize_market_type(
            market_type if market_type is not None else self._market_type
        )
        is_futures = mkt == MARKET_FUTURES
        inst_type = "SWAP" if is_futures else "SPOT"
        lev = clamp_leverage(self._leverage if leverage is None else leverage)
        mode = normalize_margin_mode(
            self._margin_mode if margin_mode is None else margin_mode
        )
        pos_side = normalize_position_side(position_side)

        inst = self._inst_id_for(inst_id, mkt)
        side_l = to_text(side).lower()
        type_l = (to_text(order_type) or "market").lower()
        td_mode = mode if is_futures else "cash"

        def fail(message: str) -> TradeResult:
            logger.warning("OKX place_order %s %s %s: %s", inst, side_l, type_l, message)
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                side=side_l,
                order_type=type_l,
                message=message,
                raw={},
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
                position_side=pos_side if is_futures else POSITION_LONG,
            )

        if not self._has_credentials():
            return fail("未填写 OKX API Key/Secret/Passphrase，无法下单")
        if not inst:
            return fail("交易对（inst_id）为空")
        if side_l not in ("buy", "sell"):
            return fail(f"side 必须是 buy 或 sell，收到 {side!r}")
        if type_l not in ("market", "limit", "trigger"):
            return fail(
                f"order_type 必须是 market / limit / trigger，收到 {order_type!r}"
            )

        price_f = to_float(price, 0.0)
        if type_l == "trigger":
            # 计划委托的「价格」是触发价，不是委托价。
            price_f = to_float(trigger_price, 0.0)
            if price_f <= 0:
                return fail(
                    "计划委托（trigger）必须提供大于 0 的 trigger_price"
                )
        elif type_l == "limit" and price_f <= 0:
            return fail("限价单（limit）必须提供大于 0 的 price")

        rules = self._instrument_rules(inst, inst_type)
        step = rules.get("step", 0.0)
        tick = rules.get("tick", 0.0)
        min_qty = rules.get("min_qty", 0.0)
        min_notional = rules.get("min_notional", 0.0) or _DEFAULT_MIN_NOTIONAL

        ref_price = price_f if price_f > 0 else self._last_price(inst)

        target_notional = to_float(notional_usdt, 0.0)
        if target_notional <= 0:
            margin_f = to_float(margin_usdt, 0.0)
            if margin_f > 0:
                target_notional = margin_f * float(lev if is_futures else 1)
        qty_in = to_float(quantity, 0.0)

        params: dict[str, Any] = {
            "instId": inst,
            "tdMode": td_mode,
            "side": side_l,
            "ordType": type_l,
        }
        if reduce_only:
            params["reduceOnly"] = True

        intended_qty: float = 0.0     # 基础币数量（对外展示）
        sz_value: str = ""            # 实际写入 sz 的值（现货=基础币，合约=张数）
        order_price = 0.0

        if is_futures:
            # ── 合约：sz 是张数，必须先换算 ──
            if qty_in > 0:
                base_qty_raw = qty_in
            elif target_notional > 0:
                if ref_price <= 0:
                    return fail(f"无法获取 {inst} 参考价，不能按金额换算数量")
                base_qty_raw = target_notional / ref_price
            else:
                return fail("必须提供 quantity、notional_usdt 或 margin_usdt")

            if to_float(rules.get("ct_val")) <= 0:
                return fail(
                    f"无法获取 {inst} 的合约面值 ctVal，拒绝下单"
                    f"（SWAP 的 sz 单位是张数，缺少 ctVal 会下错数量）"
                )
            contracts = self._contracts_for(base_qty_raw, rules)
            if contracts <= 0:
                return fail(
                    f"目标数量 {format_decimal(base_qty_raw)} 换算成张数为 0，"
                    f"请提高数量/金额"
                )
            per_contract = to_float(rules.get("ct_val")) * (
                to_float(rules.get("ct_mult"), 1.0) or 1.0
            )
            if min_qty > 0 and contracts < min_qty:
                return fail(
                    f"张数 {contracts} 低于最小下单张数 minSz={format_decimal(min_qty)}"
                )
            sz_value = format_decimal(contracts)
            # 用 Decimal 再乘一次：0.07 * 0.1 在二进制浮点下是
            # 0.007000000000000001，显示出来很难看，也会写进日志与记录。
            _intended = (to_decimal(contracts) or Decimal(0)) * (
                to_decimal(per_contract) or Decimal(0)
            )
            intended_qty = float(_intended)
            order_price = floor_to_precision(price_f, tick) if price_f > 0 else 0.0
            if type_l in ("limit", "trigger"):
                if order_price <= 0:
                    return fail(f"价格 {format_decimal(price_f)} 取整后无效")
            if type_l == "limit":
                params["px"] = format_decimal(order_price)
            elif type_l == "trigger":
                # 计划委托：触到 triggerPx 后按市价（orderPx=-1）送进撮合。
                # 用市价而不是限价，是因为触发瞬间价格已经在动，
                # 挂限价极可能不成交（实测触发后等 52 秒才撮合上）。
                params["triggerPx"] = format_decimal(order_price)
                params["orderPx"] = "-1"
        elif type_l == "market" and qty_in <= 0 and target_notional > 0:
            # ── 现货市价单直接以计价币金额下单 ──
            if target_notional < min_notional:
                return fail(
                    f"下单金额 {format_decimal(target_notional)} USDT "
                    f"低于最小下单额 minNotional={format_decimal(min_notional)} USDT"
                )
            sz_value = format_decimal(target_notional)
            params["tgtCcy"] = "quote_ccy"
            intended_qty = (target_notional / ref_price) if ref_price > 0 else 0.0
        else:
            # ── 现货按数量下单 ──
            if qty_in > 0:
                qty_raw = qty_in
            elif target_notional > 0:
                if ref_price <= 0:
                    return fail(f"无法获取 {inst} 参考价，不能按 notional_usdt 换算数量")
                qty_raw = target_notional / ref_price
            else:
                return fail("必须提供 quantity、notional_usdt 或 margin_usdt")

            qty = floor_to_step(qty_raw, step)
            if qty <= 0:
                return fail(
                    f"数量 {format_decimal(qty_raw)} 向下取整到 lotSz="
                    f"{format_decimal(step)} 后为 0，请提高下单数量/金额"
                )
            if min_qty > 0 and qty < min_qty:
                return fail(
                    f"数量 {format_decimal(qty)} 低于最小下单量 minSz="
                    f"{format_decimal(min_qty)}"
                )
            sz_value = format_decimal(qty)
            intended_qty = qty

            if type_l == "trigger":
                return fail("OKX 现货不支持计划委托（trigger），请改用合约")
            order_price = floor_to_precision(price_f, tick) if price_f > 0 else 0.0
            check_price = order_price if order_price > 0 else ref_price
            if check_price > 0 and intended_qty * check_price < min_notional:
                return fail(
                    f"名义金额 {format_decimal(intended_qty * check_price)} USDT "
                    f"低于最小下单额 minNotional={format_decimal(min_notional)} USDT"
                )
            if type_l == "limit":
                if order_price <= 0:
                    return fail(f"价格 {format_decimal(price_f)} 取整后无效")
                params["px"] = format_decimal(order_price)

        params["sz"] = sz_value

        check_price = order_price if order_price > 0 else ref_price
        notional_actual = intended_qty * check_price if check_price > 0 else 0.0
        margin_actual = (
            notional_actual / float(lev) if is_futures and lev > 0 else 0.0
        )

        if is_futures:
            # 双向持仓模式才传 posSide；net 模式传了会报 51010
            if self._is_long_short_mode():
                params["posSide"] = pos_side
            elif reduce_only:
                params["reduceOnly"] = True

            lev_res = self.set_leverage(inst, lev)
            if not lev_res.ok:
                logger.warning(
                    "OKX set_leverage(%s, %sx) 未成功（继续下单）: %s",
                    inst,
                    lev,
                    lev_res.message,
                )

        # 计划委托走 order-algo 端点，返回的是 algoId 而不是 ordId。
        is_trigger = type_l == "trigger"
        endpoint = _ALGO_ORDER_PATH if is_trigger else _ORDER_PATH
        payload, err = self._request("POST", endpoint, body=params, signed=True)
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                side=side_l,
                order_type=type_l,
                qty=intended_qty,
                price=order_price,
                message=err or "OKX 下单失败（状态未知，请到交易所核对）",
                raw={"request": params},
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_usdt=margin_actual,
                notional_usdt=notional_actual,
                position_side=pos_side if is_futures else POSITION_LONG,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
            )

        data = payload.get("data") or []
        first = data[0] if data and isinstance(data[0], dict) else {}
        ord_id = str(first.get("algoId" if is_trigger else "ordId", "") or "")
        sub_code = str(first.get("sCode", "") or "")
        if sub_code not in ("", "0"):
            sub_msg = str(first.get("sMsg", "") or "").strip()
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                side=side_l,
                order_type=type_l,
                qty=intended_qty,
                price=order_price,
                message=f"OKX 下单被拒 {sub_code}: {sub_msg}".strip(),
                raw=dict(payload),
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_usdt=margin_actual,
                notional_usdt=notional_actual,
                position_side=pos_side if is_futures else POSITION_LONG,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
            )

        if is_trigger:
            # 计划委托只是「挂上去等触发」，此刻还没有成交价、也没有持仓。
            # 把它当成已成交会让上层立刻去挂保护单——挂到一个还不存在的仓位上。
            return TradeResult(
                ok=True,
                venue=self.venue,
                order_id=ord_id,
                symbol=inst,
                side=side_l,
                order_type=type_l,
                qty=intended_qty,
                price=order_price,
                message=(
                    f"OKX 合约计划委托已挂 {side_l} {inst}"
                    f"（触发价 {format_decimal(order_price)}，{lev}x {mode}，"
                    f"algoId={ord_id or '—'}）"
                ),
                raw={
                    "request": params,
                    "pending": True,
                    "algo_id": ord_id,
                    "trigger_price": order_price,
                },
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_usdt=margin_actual,
                notional_usdt=notional_actual,
                position_side=pos_side if is_futures else POSITION_LONG,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
            )

        # 真实成交均价：市价单的 order_price 是 0，必须回查 avgPx。
        # 拿不到（网络抖动/委托还没成交完）就退回限价或参考价，
        # 至少不会把 0 传出去让保护单的几何校验失去基准。
        fill_price = self._fill_price_for(inst, ord_id)
        if fill_price <= 0:
            fill_price = order_price if order_price > 0 else ref_price

        scope = "合约" if is_futures else "现货"
        return TradeResult(
            ok=True,
            venue=self.venue,
            order_id=ord_id,
            symbol=inst,
            side=side_l,
            order_type=type_l,
            qty=intended_qty,
            price=fill_price,
            message=(
                f"OKX {scope} {type_l} {side_l} {inst} 已提交"
                f"（数量 {format_decimal(intended_qty) or '—'}，"
                f"ordId={ord_id or '—'}"
                + (f"，{lev}x {mode}" if is_futures else "")
                + "）"
            ),
            raw=dict(payload),
            market_type=mkt,
            leverage=lev if is_futures else 1,
            margin_usdt=margin_actual,
            notional_usdt=notional_actual,
            position_side=pos_side if is_futures else POSITION_LONG,
            margin_mode=mode if is_futures else MARGIN_ISOLATED,
        )

    def place_protections(
        self,
        inst_id: str,
        *,
        position_side: str = POSITION_LONG,
        plans: list[tuple[str, float, float, str]],
        market_type: str | None = None,
        margin_mode: str | None = None,
    ) -> list[ProtectionOrder]:
        """开仓成功后立刻把止损 / TP1 / TP2 挂成交易所侧条件单。

        OKX 的条件单走 ``POST /api/v5/trade/order-algo``（``ordType=conditional``）：
        触发价 ``slTriggerPx`` / ``tpTriggerPx``，触发后按市价成交
        （``slOrdPx=-1`` / ``tpOrdPx=-1``），并用 ``reduceOnly=True`` 确保只减仓。

        **止损腿用 ``closeFraction="1"`` 而不是固定张数。** 原因是固定
        ``sz`` 在分批止盈后会「多」出来：开 0.4 张、TP1 成交 0.2 张后
        持仓只剩 0.2 张，而止损单仍写着 ``sz=0.4`` —— 触发时按当时持仓
        一次全平，本来打算留着的后半段敞口提前没了（2026-11-14 在模拟盘
        实测确认）。``closeFraction="1"`` 的语义是「平掉触发那一刻的全部
        剩余持仓」，天然不受分批止盈影响。OKX 对它有四条硬约束，实测：
        ``sz`` 与 ``closeFraction`` 不能同时给（50024）、必须配
        ``reduceOnly=True``（51328）、只接受 ``1``（传 0.5 报 51000）、
        仅限合约（51327）。

        每条腿独立成败：某条挂了不影响其它腿，也**绝不抛异常** ——
        调用方（:class:`~pa_agent.trading.auto_trader.AutoTrader`）拿到的是
        一组 :class:`ProtectionOrder`，失败原因已经是可以直接给人看的中文。

        ``plans`` 由 :func:`~pa_agent.trading.common.plan_protections` 生成。
        """
        mkt = normalize_market_type(
            market_type if market_type is not None else self._market_type
        )
        is_futures = mkt == MARKET_FUTURES
        mode = normalize_margin_mode(
            self._margin_mode if margin_mode is None else margin_mode
        )
        pos_side = normalize_position_side(position_side)
        inst = self._inst_id_for(inst_id, mkt)
        td_mode = mode if is_futures else "cash"

        # 现货没有交易所侧条件单：明确告错，不假装挂上
        if not is_futures:
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="OKX 现货不支持交易所侧止损/止盈条件单，需使用合约",
                )
                for kind, price, qty, _problem in plans
            ]

        if not self._has_credentials():
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="未填写 OKX API Key/Secret/Passphrase，无法挂保护单",
                )
                for kind, price, qty, _problem in plans
            ]
        if not inst:
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="交易对（inst_id）为空",
                )
                for kind, price, qty, _problem in plans
            ]

        rules = self._instrument_rules(inst, "SWAP")
        tick = to_float(rules.get("tick"), 0.0)
        ct_val = to_float(rules.get("ct_val"), 0.0)
        ct_mult = to_float(rules.get("ct_mult"), 1.0) or 1.0
        per_contract = ct_val * ct_mult
        # 平仓方向与开仓相反
        close_side = "sell" if pos_side == POSITION_LONG else "buy"

        # 止损腿用「全平」语义：closeFraction=1 平掉触发那一刻的全部剩余持仓。
        # 固定 sz 在分批止盈后会「多」出来 —— 开 0.4 张、TP1 成交 0.2 张后
        # 持仓只剩 0.2 张，止损单却仍写 sz=0.4（2026-11-14 模拟盘实测确认），
        # 于是触发时一次全平，本来打算留着的后半段敞口提前没了。
        # 约束（同样实测）：sz 与 closeFraction 互斥（50024）、只接受 1
        # （0.5 报 51000）、必须配 reduceOnly（51328）、仅限合约（51327）、
        # 双向持仓模式不可用（51329）→ 那种情况退回收敛到计划量的固定 sz。
        sl_use_close_fraction = not self._is_long_short_mode()

        # 幂等守卫：已经有条件单在场时，不要再叠一份。
        # 止损用 closeFraction=1 时 OKX 会以 51088 拒掉第二条（天然防重），
        # 但止盈腿是普通 sz 条件单，重复调用会**叠成双份**（实测：重启后
        # 补挂一次，场上从 3 条变 5 条，TP1/TP2 各多一条 → 多平一倍仓位）。
        # 所以这里先查一次在挂条件单：止损腿已有全平单就跳过，止盈腿
        # 按 (方向, 触发价) 判断是否已经存在。
        existing = self._pending_algo_orders(inst)
        # 净持仓模式下 OKX 回显的 posSide 是字面量 ``"net"``，不是 "long"/"short"，
        # 所以这里要把 ``""``、``"net"`` 和当前方向都认作同一条。
        existing_sl_all = any(
            str(a.get("closeFraction") or "") == "1"
            and str(a.get("posSide") or "") in ("", "net", pos_side)
            for a in existing
        )
        existing_tp_prices = {
            to_float(a.get("tpTriggerPx"), 0.0)
            for a in existing
            if str(a.get("tpTriggerPx") or "").strip()
        }

        def _tp_already_exists(price: float) -> bool:
            """触发价是否已有一条止盈单。

            用容差而不是 ``==``：交易所回显的是**按 tick 取整后**的价格
            （例如计划写 2010.005、tick=0.01 时回显 2010.0），直接比浮点数
            会漏判，于是又叠一条。容差取 tick 的一半，够覆盖取整误差。
            """
            tol = tick / 2 if tick > 0 else 1e-9
            return any(abs(price - p) <= tol for p in existing_tp_prices)

        # 张数可以是小数（lotSz 常为 0.01），所以 TP1/TP2 各挂半仓是合法的，
        # 不需要再做「只够 1 张就合并」的降级。只有当某一段换算后真的
        # 低于 minSz（极端小仓 / 整张步长的老合约）时才合并。
        min_sz = to_float(rules.get("min_qty"), 0.0)
        tp_leg_contracts: dict[str, float] = {}
        tp_total_qty = 0.0
        for _kind, _price, _qty, _problem in plans:
            if _kind in (PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2):
                leg = to_float(_qty, 0.0)
                tp_total_qty += leg
                tp_leg_contracts[_kind] = self._contracts_for(leg, rules)

        def _too_small(contracts: float) -> bool:
            return contracts <= 0 or (min_sz > 0 and contracts < min_sz)

        # 任何一段不达标 → 整个止盈合并成一条，挂在 TP1 的价位上（先落袋为安），
        # 数量为两段之和。合并后仍不达标的话，后面按 minSz 逐条报错。
        weak_legs = [k for k, c in tp_leg_contracts.items() if _too_small(c)]
        merge_tp = bool(weak_legs) and len(tp_leg_contracts) > 1
        merge_into = PROTECT_TAKE_PROFIT_1 if merge_tp else ""

        out: list[ProtectionOrder] = []
        for kind, price, qty, problem in plans:
            trigger = to_float(price, 0.0)
            leg_qty = to_float(qty, 0.0)

            if merge_tp and kind == PROTECT_TAKE_PROFIT_2:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=(
                            f"止盈两段里有张数低于最小下单张数 "
                            f"{format_decimal(min_sz)}，已合并到 TP1 一次平掉"
                        ),
                    )
                )
                continue
            if merge_tp and kind == merge_into:
                # 合并后由 TP1 一次平掉两段的量
                leg_qty = tp_total_qty

            if problem:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=problem,
                    )
                )
                continue
            if per_contract <= 0:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=f"无法获取 {inst} 合约面值 ctVal，拒绝挂单",
                    )
                )
                continue

            # 止损腿用「全平」语义：closeFraction=1 平掉触发那一刻的全部剩余
            # 持仓，因此不需要 sz、也不会因为换算张数为 0 而挂不上。
            use_close_fraction = kind == PROTECT_STOP_LOSS and sl_use_close_fraction

            # 幂等：止损已经有一条全平单、或止盈在这个价位已经挂过，就跳过，
            # 免得重启后重复补挂把止盈叠成双份（实测会多平一倍仓位）。
            if use_close_fraction and existing_sl_all:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        ok=True,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message="已有全平止损条件单在场，本次不重复挂",
                    )
                )
                continue
            if kind != PROTECT_STOP_LOSS and _tp_already_exists(trigger):
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        ok=True,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message="该价位已有止盈条件单在场，本次不重复挂",
                    )
                )
                continue

            contracts = self._contracts_for(leg_qty, rules)
            if contracts <= 0 and not use_close_fraction:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=(
                            f"数量 {format_decimal(leg_qty)} 换算成张数为 0，"
                            f"无法挂保护单"
                        ),
                    )
                )
                continue

            trigger_px = floor_to_precision(trigger, tick) if tick > 0 else trigger
            if trigger_px <= 0:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=f"触发价 {format_decimal(trigger)} 取整后无效",
                    )
                )
                continue

            params: dict[str, Any] = {
                "instId": inst,
                "tdMode": td_mode,
                "side": close_side,
                "ordType": "conditional",
                "reduceOnly": True,
            }
            # 止损腿走「全平」语义，TP 腿仍按计划量分批挂。
            # 约束：sz 与 closeFraction 互斥（50024）、closeFraction 只接受 1
            # （0.5 报 51000）、必须配 reduceOnly（51328）、仅限合约（51327）。
            if use_close_fraction:
                params["closeFraction"] = "1"
            else:
                params["sz"] = format_decimal(contracts)
            if self._is_long_short_mode():
                # 双向持仓模式必须指明是哪一边的仓位，否则报 51010
                params["posSide"] = pos_side
            if kind == PROTECT_STOP_LOSS:
                params["slTriggerPx"] = format_decimal(trigger_px)
                params["slOrdPx"] = "-1"
            else:
                params["tpTriggerPx"] = format_decimal(trigger_px)
                params["tpOrdPx"] = "-1"

            # 全平止损不带数量（contracts 可能是 0），回报时退回计划量
            reported_qty = round(leg_qty, 8) if use_close_fraction else round(
                contracts * per_contract, 8
            )

            payload, err = self._request(
                "POST", _ALGO_ORDER_PATH, body=params, signed=True
            )
            if payload is None:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger_px, 8),
                        qty=reported_qty,
                        message=err or "OKX 保护单提交失败（状态未知，请到交易所核对）",
                        raw={"request": params},
                    )
                )
                continue

            data = payload.get("data") or []
            first = data[0] if data and isinstance(data[0], dict) else {}
            algo_id = str(first.get("algoId", "") or "")
            sub_code = str(first.get("sCode", "") or "")
            if sub_code not in ("", "0"):
                sub_msg = str(first.get("sMsg", "") or "").strip()
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger_px, 8),
                        qty=reported_qty,
                        message=f"OKX 保护单被拒 {sub_code}: {sub_msg}".strip(),
                        raw=dict(payload),
                    )
                )
                continue

            out.append(
                ProtectionOrder(
                    kind=kind,
                    ok=True,
                    trigger_price=round(trigger_px, 8),
                    qty=reported_qty,
                    order_id=algo_id,
                    raw=dict(payload),
                )
            )
        return out

    def close_position(
        self, inst_id: str, position_side: str = POSITION_LONG
    ) -> TradeResult:
        """市价平掉指定方向的 SWAP 持仓（``/api/v5/trade/close-position``）。

        ``posSide`` 只在 ``long_short_mode`` 传；net 模式下 OKX 靠
        ``mgnMode`` + instId 自动识别方向（传 posSide 会报 51010）。
        """
        inst = self._normalize_swap_inst_id(inst_id)
        pos_side = normalize_position_side(position_side)

        if self._market_type != MARKET_FUTURES:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message="close_position 仅用于合约（market_type=futures）",
                market_type=self._market_type,
            )
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message="未填写 OKX API Key/Secret/Passphrase，无法平仓",
                market_type=self._market_type,
                position_side=pos_side,
            )
        if not inst:
            return TradeResult(
                ok=False,
                venue=self.venue,
                message="平仓需要 inst_id",
                market_type=self._market_type,
                position_side=pos_side,
            )

        body: dict[str, Any] = {"instId": inst, "mgnMode": self._margin_mode}
        if self._is_long_short_mode():
            body["posSide"] = pos_side

        payload, err = self._request("POST", _CLOSE_POSITION_PATH, body=body, signed=True)
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message=err or "OKX 平仓失败（状态未知，请到交易所核对）",
                raw={"request": body},
                market_type=self._market_type,
                margin_mode=self._margin_mode,
                position_side=pos_side,
            )

        for item in payload.get("data", ()) or ():
            if not isinstance(item, dict):
                continue
            sub_code = str(item.get("sCode", "") or "")
            if sub_code not in ("", "0"):
                return TradeResult(
                    ok=False,
                    venue=self.venue,
                    symbol=inst,
                    message=f"OKX 平仓被拒 {sub_code}: {str(item.get('sMsg', '')).strip()}",
                    raw=dict(payload),
                    market_type=self._market_type,
                    margin_mode=self._margin_mode,
                    position_side=pos_side,
                )

        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=inst,
            message=f"OKX 平仓已提交（{inst} {pos_side}，{self._margin_mode}）",
            raw=dict(payload),
            market_type=self._market_type,
            margin_mode=self._margin_mode,
            position_side=pos_side,
        )

    def cancel_order(self, inst_id: str, ord_id: str) -> TradeResult:
        """Cancel one pending order."""
        inst = self._inst_id_for(inst_id)
        oid = to_text(ord_id)
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                order_id=oid,
                message="未填写 OKX API Key/Secret/Passphrase，无法撤单",
                market_type=self._market_type,
            )
        if not inst or not oid:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                order_id=oid,
                message="撤单需要 inst_id 与 ord_id",
                market_type=self._market_type,
            )

        payload, err = self._request(
            "POST", _CANCEL_PATH, body={"instId": inst, "ordId": oid}, signed=True
        )
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                order_id=oid,
                message=err or "OKX 撤单失败",
                market_type=self._market_type,
            )

        for item in payload.get("data", ()) or ():
            if not isinstance(item, dict):
                continue
            sub_code = str(item.get("sCode", "") or "")
            if sub_code not in ("", "0"):
                return TradeResult(
                    ok=False,
                    venue=self.venue,
                    symbol=inst,
                    order_id=oid,
                    message=f"OKX 撤单被拒 {sub_code}: {str(item.get('sMsg', '')).strip()}",
                    raw=dict(payload),
                    market_type=self._market_type,
                )
        return TradeResult(
            ok=True,
            venue=self.venue,
            order_id=oid,
            symbol=inst,
            message=f"OKX 撤单成功（{inst} ordId={oid}）",
            raw=dict(payload),
            market_type=self._market_type,
        )

    def list_open_orders(self, inst_id: str = "") -> TradeResult:
        """List pending orders; ``inst_id`` empty means all instruments."""
        inst = self._inst_id_for(inst_id) if inst_id else ""
        inst_type = self._inst_type()
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message="未填写 OKX API Key/Secret/Passphrase，无法查询未成交订单",
                market_type=self._market_type,
            )

        payload, err = self._request(
            "GET", _OPEN_ORDERS_PATH, {"instType": inst_type, "instId": inst}, signed=True
        )
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=inst,
                message=err or "OKX 查询未成交订单失败",
                market_type=self._market_type,
            )
        orders = list(payload.get("data") or ())
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=inst,
            message=f"OKX 未成交订单 {len(orders)} 笔",
            raw={"orders": orders},
            market_type=self._market_type,
        )


__all__ = ["OKXTrader", "to_okx_swap_inst_id"]
