"""Binance 下单客户端（现货 SPOT ``/api/v3`` + U 本位永续 FUTURES ``/fapi``）。

与 :mod:`pa_agent.data.binance_source` 保持一致的签名方式：
``HMAC-SHA256(query)`` 十六进制摘要 + ``X-MBX-APIKEY`` 请求头，
并在参数里附加 ``timestamp`` 与 ``recvWindow=5000``。

设计约束：

* **绝不抛异常** —— 网络/HTTP/解析错误统一降级成 ``ok=False``，
  ``message`` 里保留交易所自己的报错文本。
* 数量一律向下取整到 ``LOT_SIZE.stepSize``，价格向下取整到
  ``PRICE_FILTER.tickSize``（宁可少买一点，也不让交易所拒单）。
* 下单（POST）不做跨主机重试：请求超时后订单状态未知，
  重试可能重复成交。
* **一套代码两种市场**：主机 / 路径 / 交易规则按 ``market_type``（或单次
  调用的 ``market_type=`` 覆盖）分派到 ``_SPOT_*`` 或 ``_FUTURES_*`` 表，
  不复制两份下单逻辑。

合约要点（U 本位永续）：

* ``positionSide`` 只在**双向持仓（hedge）模式**下传；单向（oneway）模式
  传了会报 ``-4061``，所以下单前先用 ``GET /fapi/v1/positionSide/dual`` 判断。
* hedge 模式下同样不能传 ``reduceOnly``（会报 ``-1106``）——平仓靠
  ``side`` 取反 + ``positionSide=被平方向`` 表达。
* ``POST /fapi/v1/marginType`` 在「已经是该模式」时返回
  ``-4046 No need to change margin type``，这属于成功。
* 合约的最小名义金额 filter 是 ``MIN_NOTIONAL``，键名是 ``notional``；
  现货是 ``NOTIONAL`` / ``minNotional``。两种都兼容。

Usage:
    trader = BinanceTrader(api_key="...", api_secret="...")
    ok, msg = trader.test_connection()
    res = trader.place_order("BTCUSDT", "BUY", "MARKET", notional_usdt=20)

    # 合约：10 USDT 保证金 × 20 倍 = 200 USDT 名义，逐仓，做空
    res = trader.place_order(
        "BTCUSDT", "SELL", "MARKET",
        market_type="futures", margin_usdt=10, leverage=20,
        margin_mode="isolated", position_side="short",
    )
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any

from pa_agent.data.crypto_common import to_binance_symbol
from pa_agent.trading.common import (
    MARKET_FUTURES,
    MARKET_SPOT,
    MARGIN_CROSS,
    MARGIN_ISOLATED,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
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
    normalize_order_type,
    normalize_position_side,
    to_float,
    to_text,
)
from pa_agent.util import throttle

logger = logging.getLogger(__name__)

# 现货 REST 主机：按顺序尝试，记住本次会话每个市场第一个可用的
_SPOT_HOSTS: tuple[str, ...] = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
)
_SPOT_TESTNET_HOSTS: tuple[str, ...] = ("https://testnet.binance.vision",)

# U 本位永续 REST 主机
_FUTURES_HOSTS: tuple[str, ...] = (
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
)
_FUTURES_TESTNET_HOSTS: tuple[str, ...] = ("https://testnet.binancefuture.com",)

_SPOT_PATHS: dict[str, str] = {
    "account": "/api/v3/account",
    "order": "/api/v3/order",
    "open_orders": "/api/v3/openOrders",
    "exchange_info": "/api/v3/exchangeInfo",
    "ticker": "/api/v3/ticker/price",
}

_FUTURES_PATHS: dict[str, str] = {
    "account": "/fapi/v2/balance",
    "order": "/fapi/v1/order",
    "open_orders": "/fapi/v1/openOrders",
    "exchange_info": "/fapi/v1/exchangeInfo",
    "ticker": "/fapi/v1/ticker/price",
    "leverage": "/fapi/v1/leverage",
    "margin_type": "/fapi/v1/marginType",
    "position_mode": "/fapi/v1/positionSide/dual",
    "positions": "/fapi/v2/position",
    # 条件单（止损/止盈）与普通下单同一个 endpoint，只是 type 不同
    "conditional_order": "/fapi/v1/order",
}

_RECV_WINDOW_MS = 5000

# 交易所默认最小名义金额（filter 缺失时的保底值，单位 USDT）
_DEFAULT_MIN_NOTIONAL = 5.0

# 这些状态码属于「主机不可用」，换下一台主机重试
_HOST_LEVEL_STATUS = (403, 418, 429, 451, 500, 502, 503, 504)

# Binance「不需要重复设置」类错误码：当成功处理
_CODE_NO_NEED_MARGIN_CHANGE = "-4046"
_CODE_NO_NEED_LEVERAGE_CHANGE = "-4059"  # 部分场景沿用（leverage not changed）


class BinanceTrader:
    """Binance 交易客户端（现货 + U 本位永续）。

    ``testnet=True`` 时走 ``testnet.binance.vision``（现货）/
    ``testnet.binancefuture.com``（合约）；否则在主网主机间自动降级。
    凭据为空时所有需要签名的接口直接返回 ``ok=False``，不会发起任何网络请求。

    实例级默认值 ``market_type`` / ``leverage`` / ``margin_mode`` 可以被
    ``place_order()`` 的关键字参数临时覆盖（单次调用生效）。
    """

    venue: str = "binance"

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        timeout_s: float = 10.0,
        market_type: str = MARKET_SPOT,
        leverage: int = 1,
        margin_mode: str = MARGIN_ISOLATED,
    ) -> None:
        self._api_key: str = (api_key or "").strip()
        self._api_secret: str = (api_secret or "").strip()
        self._testnet: bool = bool(testnet)
        self._timeout_s: float = timeout_s

        self._market_type: str = normalize_market_type(market_type)
        self._leverage: int = clamp_leverage(leverage)
        self._margin_mode: str = normalize_margin_mode(margin_mode)

        self._client_obj = None          # 延迟创建的 httpx.Client
        # market_type → 已确认可用的主机（缺失 = 尚未解析）
        self._host_map: dict[str, str] = {}
        # (market_type, symbol) → 交易规则（step/tick/minQty/minNotional）
        self._filters: dict[tuple[str, str], dict[str, Any]] = {}

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
                logger.warning("Binance trader client close error: %s", exc)
            finally:
                self._client_obj = None

    def _hosts_for(self, market: str) -> tuple[str, ...]:
        """REST hosts for *market* (mainnet or testnet)."""
        if market == MARKET_FUTURES:
            return _FUTURES_TESTNET_HOSTS if self._testnet else _FUTURES_HOSTS
        return _SPOT_TESTNET_HOSTS if self._testnet else _SPOT_HOSTS

    @staticmethod
    def _path(name: str, market: str) -> str:
        """Resolve an endpoint path by logical name + market type."""
        if market == MARKET_FUTURES:
            return _FUTURES_PATHS.get(name) or _FUTURES_PATHS["order"]
        return _SPOT_PATHS.get(name) or _SPOT_PATHS["order"]

    def _has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @staticmethod
    def _error_payload(resp: Any) -> dict[str, Any]:
        """Best-effort ``{"code": ..., "msg": ...}`` from an error response."""
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _error_text(cls, resp: Any) -> str:
        """Extract Binance's own human-readable error message."""
        payload = cls._error_payload(resp)
        code = str(payload.get("code", "") or "")
        msg = str(payload.get("msg", "") or "")
        if not msg:
            try:
                msg = (resp.text or "").strip()[:200]
            except Exception:  # noqa: BLE001
                msg = ""
        if code or msg:
            return f"Binance 错误 {code}: {msg}".strip()
        return f"Binance HTTP {resp.status_code}"

    def _signature(self, query: str) -> str:
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_query(self, params: dict[str, Any]) -> str:
        """Build the exact query string, then append ``signature``."""
        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = _RECV_WINDOW_MS
        items = [(k, format_decimal(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))
                 for k, v in payload.items()]
        query = urllib.parse.urlencode(items)
        sign = self._signature(query)
        return f"{query}&signature={sign}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
        market: str | None = None,
    ) -> tuple[Any, str]:
        """Issue one request; returns ``(payload, error_text)``.

        ``payload`` 可能是 dict 也可能是 list（如 ``/api/v3/openOrders``）；
        ``error_text`` 为空表示成功。任何异常都在这里被收敛，
        调用方无需再包 try/except。

        ``market`` 决定使用现货还是合约主机（``None`` = 实例默认市场）。
        """
        if signed and not self._has_credentials():
            return None, "未填写 Binance API Key/Secret"

        try:
            client = self._client()
        except Exception as exc:  # noqa: BLE001 — httpx 缺失
            return None, f"httpx 未安装 — 请执行: pip install httpx（{exc}）"

        mkt = normalize_market_type(market if market is not None else self._market_type)
        hosts = self._hosts_for(mkt)
        known_host = self._host_map.get(mkt, "")

        method = method.upper()
        # 下单类请求只发一次：超时后状态未知，换主机重试可能重复成交
        allow_failover = method in ("GET", "DELETE")
        if allow_failover:
            candidates = (
                [known_host] + [h for h in hosts if h != known_host]
                if known_host
                else list(hosts)
            )
        else:
            candidates = [known_host or hosts[0]]

        tried: list[str] = []
        for host in candidates:
            if not host:
                continue
            query = self._signed_query(params or {}) if signed else urllib.parse.urlencode(
                [(k, format_decimal(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))
                 for k, v in (params or {}).items()]
            )
            url = f"{host}{path}?{query}" if query else f"{host}{path}"
            headers = {"X-MBX-APIKEY": self._api_key} if signed else {}
            # 本地节流：同进程里所有 Binance 请求共用一个令牌桶。
            throttle.acquire("binance")
            try:
                resp = client.request(method, url, headers=headers)
            except Exception as exc:  # noqa: BLE001
                tried.append(f"{host}: {exc}")
                logger.warning("Binance %s %s failed on %s: %s", method, path, host, exc)
                continue

            if resp.status_code == 200:
                self._host_map[mkt] = host
                try:
                    payload = resp.json()
                except Exception as exc:  # noqa: BLE001
                    return None, f"Binance 返回非 JSON 数据: {exc}"
                return payload, ""

            if resp.status_code in _HOST_LEVEL_STATUS and allow_failover:
                tried.append(f"{host}: HTTP {resp.status_code}")
                logger.debug("Binance host %s returned %s; trying next", host, resp.status_code)
                continue

            self._host_map[mkt] = host
            return None, self._error_text(resp)

        detail = "; ".join(tried) or "unknown network error"
        logger.warning("Binance %s %s gave up: %s", method, path, detail)
        return None, f"Binance 请求失败: {detail}"

    # ── 交易规则 ──────────────────────────────────────────────────────────────

    def _symbol_filters(self, symbol: str, market: str | None = None) -> dict[str, Any]:
        """Load and cache LOT_SIZE / PRICE_FILTER / NOTIONAL rules for *symbol*.

        合约走 ``/fapi/v1/exchangeInfo``，现货走 ``/api/v3/exchangeInfo``；
        合约的最小名义金额 filter 键名是 ``notional``，现货是 ``minNotional``。
        """
        key = to_binance_symbol(to_text(symbol)) if symbol else ""
        mkt = normalize_market_type(market if market is not None else self._market_type)
        cache_key = (mkt, key)
        cached = self._filters.get(cache_key)
        if cached is not None:
            return cached

        rules: dict[str, Any] = {}
        payload, err = self._request(
            "GET",
            self._path("exchange_info", mkt),
            {"symbol": key},
            signed=False,
            market=mkt,
        )
        if payload is None or not isinstance(payload, dict):
            logger.debug("Binance exchangeInfo(%s) unavailable: %s", key, err)
            return rules

        for item in payload.get("symbols", ()) or ():
            if str(item.get("symbol", "")).upper() != key:
                continue
            for flt in item.get("filters", ()) or ():
                kind = str(flt.get("filterType", ""))
                if kind == "LOT_SIZE":
                    rules["step"] = to_float(flt.get("stepSize"))
                    rules["min_qty"] = to_float(flt.get("minQty"))
                    rules["max_qty"] = to_float(flt.get("maxQty"))
                elif kind == "PRICE_FILTER":
                    rules["tick"] = to_float(flt.get("tickSize"))
                elif kind in ("NOTIONAL", "MIN_NOTIONAL"):
                    # 现货 NOTIONAL/minNotional，合约 MIN_NOTIONAL/notional
                    rules["min_notional"] = max(
                        to_float(flt.get("minNotional", 0.0)),
                        to_float(flt.get("notional", 0.0)),
                    )
            break

        if rules:
            self._filters[cache_key] = rules
        return rules

    def last_price(self, symbol: str, market: str | None = None) -> float:
        """公开的交易所最新价（无需 API Key）；取不到时返回 ``0.0``。

        用于跨所价差保护：拿 Binance 行情做分析、到 OKX 下单时，
        两边价格偏离过大就不该下单。
        """
        return self._last_price(symbol, market)

    def _last_price(self, symbol: str, market: str | None = None) -> float:
        """Public ticker price; ``0.0`` when unavailable."""
        key = to_text(symbol)
        if not key:
            return 0.0
        mkt = normalize_market_type(market if market is not None else self._market_type)
        payload, err = self._request(
            "GET",
            self._path("ticker", mkt),
            {"symbol": key},
            signed=False,
            market=mkt,
        )
        if payload is None:
            logger.debug("Binance ticker price(%s) unavailable: %s", key, err)
            return 0.0
        if not isinstance(payload, dict):
            return 0.0
        return to_float(payload.get("price"))

    # ── 账户 ──────────────────────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """Check key/secret validity against the signed account endpoint."""
        if not self._has_credentials():
            return False, "未填写 Binance API Key/Secret"

        payload, err = self._request(
            "GET", self._path("account", MARKET_SPOT), {}, signed=True, market=MARKET_SPOT
        )
        if payload is None:
            return False, err
        if not isinstance(payload, dict):
            return False, "Binance 账户接口返回格式异常"
        account_type = str(payload.get("accountType", "") or "未知")
        return (
            True,
            f"Binance API 连接成功（accountType={account_type}, "
            f"canTrade={payload.get('canTrade')}, "
            f"testnet={self._testnet}）",
        )

    def _account_balances(self) -> tuple[list[dict[str, Any]], str]:
        """Signed spot account balances; ``([], error)`` on failure."""
        payload, err = self._request(
            "GET", _SPOT_PATHS["account"], {}, signed=True, market=MARKET_SPOT
        )
        if payload is None:
            return [], err
        if not isinstance(payload, dict):
            return [], "Binance 账户接口返回格式异常"

        out: list[dict[str, Any]] = []
        for item in payload.get("balances", ()) or ():
            if not isinstance(item, dict):
                continue
            free = to_float(item.get("free"))
            locked = to_float(item.get("locked"))
            out.append(
                {
                    "asset": str(item.get("asset", "")).upper(),
                    "free": free,
                    "locked": locked,
                    "total": free + locked,
                }
            )
        return out, ""

    def get_balance(self, asset: str) -> tuple[bool, str, float]:
        """Return ``(ok, message, total)`` for *asset* (free + locked, 现货账户)."""
        code = to_text(asset).upper()
        if not code:
            return False, "未指定资产（asset 为空）", 0.0
        if not self._has_credentials():
            return False, "未填写 Binance API Key/Secret", 0.0

        balances, err = self._account_balances()
        if not err:
            for item in balances:
                if item["asset"] == code:
                    return (
                        True,
                        f"{code} 余额 {format_decimal(item['total'])}"
                        f"（可用 {format_decimal(item['free'])}，"
                        f"冻结 {format_decimal(item['locked'])}）",
                        item["total"],
                    )
            return False, f"Binance 账户中未找到 {code} 余额", 0.0
        return False, err, 0.0

    def get_free_balance(self, asset: str) -> float:
        """可用余额（失败时返回 ``0.0``，不抛异常）。"""
        code = to_text(asset).upper()
        if not code or not self._has_credentials():
            return 0.0
        balances, err = self._account_balances()
        if err:
            logger.warning("Binance get_free_balance(%s) failed: %s", code, err)
            return 0.0
        for item in balances:
            if item["asset"] == code:
                return item["free"]
        return 0.0

    def get_futures_balance(self, asset: str = "USDT") -> tuple[bool, str, float]:
        """合约账户可用余额 ``(ok, message, availableBalance)``。

        对应 ``GET /fapi/v2/balance``：该接口返回**数组**，逐项含
        ``balance`` / ``availableBalance``；这里取 ``availableBalance``
        （可开新仓的金额），不是钱包总额。
        """
        code = to_text(asset).upper() or "USDT"
        if not self._has_credentials():
            return False, "未填写 Binance API Key/Secret", 0.0

        payload, err = self._request(
            "GET",
            self._path("account", MARKET_FUTURES),
            {},
            signed=True,
            market=MARKET_FUTURES,
        )
        if payload is None:
            return False, err, 0.0
        rows = payload if isinstance(payload, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset", "")).upper() != code:
                continue
            available = to_float(item.get("availableBalance"))
            wallet = to_float(item.get("balance"), available)
            return (
                True,
                f"Binance 合约 {code} 可用 {format_decimal(available)}"
                f"（钱包余额 {format_decimal(wallet)}）",
                available,
            )
        return False, f"Binance 合约账户中未找到 {code} 余额", 0.0

    # ── 合约杠杆 / 保证金模式 / 持仓模式 ──────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int | None = None) -> TradeResult:
        """设置合约杠杆（``POST /fapi/v1/leverage``）。

        现货不使用杠杆，直接返回 ``ok=True`` 并说明（不发起任何请求）。
        """
        sym = to_binance_symbol(to_text(symbol))
        lev = clamp_leverage(self._leverage if leverage is None else leverage)

        if self._market_type != MARKET_FUTURES:
            return TradeResult(
                ok=True,
                venue=self.venue,
                symbol=sym,
                message="现货不使用杠杆（market_type=spot），已忽略 set_leverage",
                market_type=self._market_type,
                leverage=1,
            )
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message="未填写 Binance API Key/Secret，无法设置杠杆",
                market_type=self._market_type,
                leverage=lev,
            )
        if not sym:
            return TradeResult(
                ok=False,
                venue=self.venue,
                message="设置杠杆需要 symbol",
                market_type=self._market_type,
                leverage=lev,
            )

        payload, err = self._request(
            "POST",
            self._path("leverage", MARKET_FUTURES),
            {"symbol": sym, "leverage": lev},
            signed=True,
            market=MARKET_FUTURES,
        )
        if payload is None or not isinstance(payload, dict):
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message=err or "Binance 设置杠杆失败",
                market_type=self._market_type,
                leverage=lev,
            )

        applied = int(to_float(payload.get("leverage"), float(lev)))
        self._leverage = applied
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=sym,
            message=f"Binance {sym} 杠杆已设为 {applied}x",
            raw=dict(payload),
            market_type=self._market_type,
            leverage=applied,
        )

    def set_margin_mode(
        self, symbol: str, margin_mode: str | None = None
    ) -> TradeResult:
        """设置合约保证金模式（``POST /fapi/v1/marginType``）。

        ``isolated`` → ``ISOLATED``；``cross`` → ``CROSSED``（Binance 的写法）。
        已经是该模式时 Binance 返回 ``-4046 No need to change margin type``，
        **当作成功**处理。
        """
        sym = to_binance_symbol(to_text(symbol))
        mode = normalize_margin_mode(
            self._margin_mode if margin_mode is None else margin_mode
        )
        binance_mode = "ISOLATED" if mode == MARGIN_ISOLATED else "CROSSED"

        if self._market_type != MARKET_FUTURES:
            return TradeResult(
                ok=True,
                venue=self.venue,
                symbol=sym,
                message="现货不存在保证金模式（market_type=spot），已忽略 set_margin_mode",
                market_type=self._market_type,
                margin_mode=mode,
            )
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message="未填写 Binance API Key/Secret，无法设置保证金模式",
                market_type=self._market_type,
                margin_mode=mode,
            )
        if not sym:
            return TradeResult(
                ok=False,
                venue=self.venue,
                message="设置保证金模式需要 symbol",
                market_type=self._market_type,
                margin_mode=mode,
            )

        payload, err = self._request(
            "POST",
            self._path("margin_type", MARKET_FUTURES),
            {"symbol": sym, "marginType": binance_mode},
            signed=True,
            market=MARKET_FUTURES,
        )
        if payload is None or not isinstance(payload, dict):
            # -4046 = 已经是该模式，属于成功
            if _CODE_NO_NEED_MARGIN_CHANGE in (err or ""):
                self._margin_mode = mode
                return TradeResult(
                    ok=True,
                    venue=self.venue,
                    symbol=sym,
                    message=(
                        f"Binance {sym} 保证金模式已经是 {binance_mode}"
                        f"（-4046 No need to change margin type）"
                    ),
                    market_type=self._market_type,
                    margin_mode=mode,
                )
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message=err or "Binance 设置保证金模式失败",
                market_type=self._market_type,
                margin_mode=mode,
            )

        self._margin_mode = mode
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=sym,
            message=f"Binance {sym} 保证金模式已设为 {binance_mode}",
            raw=dict(payload),
            market_type=self._market_type,
            margin_mode=mode,
        )

    def get_position_mode(self) -> tuple[bool, str, str]:
        """读取持仓模式 ``(ok, "hedge"|"oneway"|"", message)``。

        对应 ``GET /fapi/v1/positionSide/dual``：``dualSidePosition=true``
        表示双向持仓（hedge），``false`` 表示单向持仓（oneway）。
        """
        if not self._has_credentials():
            return False, "", "未填写 Binance API Key/Secret"

        payload, err = self._request(
            "GET",
            self._path("position_mode", MARKET_FUTURES),
            {},
            signed=True,
            market=MARKET_FUTURES,
        )
        if payload is None or not isinstance(payload, dict):
            return False, "", err or "Binance 查询持仓模式失败"

        dual = payload.get("dualSidePosition")
        is_dual = dual is True or str(dual).strip().lower() == "true"
        mode = "hedge" if is_dual else "oneway"
        return (
            True,
            mode,
            f"Binance 持仓模式：{'双向(hedge)' if is_dual else '单向(oneway)'}",
        )

    def _is_hedge_mode(self) -> bool:
        """True only when the account is confirmed in dual-side (hedge) mode."""
        ok, mode, msg = self.get_position_mode()
        if not ok:
            logger.warning(
                "Binance 持仓模式未知（%s），按单向持仓处理（不传 positionSide）", msg
            )
            return False
        return mode == "hedge"

    def list_positions(self) -> tuple[bool, str, list[dict]]:
        """列出合约持仓 ``(ok, message, positions)``。

        对应 ``GET /fapi/v2/position``，过滤掉 ``positionAmt == 0`` 的空仓。
        每项精简为：
        ``{"symbol","position_side","size","entry_price","mark_price",
        "unrealized_pnl","leverage","margin"}``，数值全部转 ``float``，
        字段缺失给 ``0``。
        """
        if not self._has_credentials():
            return False, "未填写 Binance API Key/Secret", []

        payload, err = self._request(
            "GET",
            self._path("positions", MARKET_FUTURES),
            {},
            signed=True,
            market=MARKET_FUTURES,
        )
        if payload is None:
            return False, err or "Binance 查询持仓失败", []
        if not isinstance(payload, list):
            return False, "Binance 持仓接口返回格式异常", []

        out: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            amt = to_float(item.get("positionAmt"))
            if amt == 0:
                continue
            raw_side = str(item.get("positionSide", "") or "").upper()
            if raw_side in ("LONG", "SHORT"):
                side = raw_side.lower()
            else:  # oneway 模式返回 BOTH，用数量正负判断
                side = POSITION_LONG if amt > 0 else POSITION_SHORT
            margin = to_float(
                item.get("isolatedMargin", item.get("isolatedWallet", 0.0))
            )
            out.append(
                {
                    "symbol": str(item.get("symbol", "")),
                    "position_side": side,
                    "size": abs(amt),
                    "entry_price": to_float(item.get("entryPrice")),
                    "mark_price": to_float(item.get("markPrice")),
                    "unrealized_pnl": to_float(item.get("unRealizedProfit")),
                    "leverage": int(to_float(item.get("leverage"), 1.0)),
                    "margin": margin,
                }
            )
        return True, f"Binance 持仓 {len(out)} 个", out

    # ── 下单 / 撤单 / 查询 ────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
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
    ) -> TradeResult:
        """下现货或合约单，按 ``quantity`` / ``notional_usdt`` / ``margin_usdt`` 定量。

        * ``market_type=None`` → 用实例默认市场（构造参数）。
        * ``notional_usdt`` 与 ``margin_usdt`` 给一个即可；``margin_usdt`` 会先
          ``× 杠杆`` 得到名义金额（``notional_from_margin``）。
        * ``LIMIT`` 必须带 ``price`` 并自动附加 ``timeInForce=GTC``。
        * 数量向下取整到 ``stepSize``；取整后名义金额低于 ``minNotional``
          时直接返回 ``ok=False``（不把必然被拒的单发给交易所）。
        * 合约下单前会先 ``set_leverage()`` / ``set_margin_mode()``，
          **失败只 warning 不阻断**（很多情况下是「无需变更」）。
        * ``positionSide`` 只在 hedge 模式传；oneway 模式不传（否则 -4061）。
          hedge 模式也不传 ``reduceOnly``（否则 -1106）。
        """
        mkt = normalize_market_type(
            market_type if market_type is not None else self._market_type
        )
        is_futures = mkt == MARKET_FUTURES
        lev = clamp_leverage(self._leverage if leverage is None else leverage)
        mode = normalize_margin_mode(
            self._margin_mode if margin_mode is None else margin_mode
        )
        pos_side = normalize_position_side(position_side)

        sym = to_binance_symbol(to_text(symbol))
        side_u = to_text(side).upper()
        type_u = (to_text(order_type) or "MARKET").upper()

        def fail(message: str) -> TradeResult:
            logger.warning(
                "Binance place_order %s %s %s (%s): %s", sym, side_u, type_u, mkt, message
            )
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                side=side_u,
                order_type=type_u,
                message=message,
                raw={},
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
                position_side=pos_side if is_futures else POSITION_LONG,
            )

        if not self._has_credentials():
            return fail("未填写 Binance API Key/Secret，无法下单")
        if not sym:
            return fail("交易对（symbol）为空")
        if side_u not in ("BUY", "SELL"):
            return fail(f"side 必须是 BUY 或 SELL，收到 {side!r}")
        if type_u not in ("MARKET", "LIMIT"):
            return fail(f"order_type 必须是 MARKET 或 LIMIT，收到 {order_type!r}")

        price_f = to_float(price, 0.0)
        if type_u == "LIMIT" and price_f <= 0:
            return fail("LIMIT 单必须提供大于 0 的 price")

        rules = self._symbol_filters(sym, mkt)
        step = rules.get("step", 0.0)
        tick = rules.get("tick", 0.0)
        min_qty = rules.get("min_qty", 0.0)
        max_qty = rules.get("max_qty", 0.0)
        min_notional = rules.get("min_notional", 0.0) or _DEFAULT_MIN_NOTIONAL

        # 参考价：限价单用委托价；市价单用最新成交价（仅用于数量换算与校验）
        ref_price = price_f if price_f > 0 else self._last_price(sym, mkt)

        target_notional = to_float(notional_usdt, 0.0)
        if target_notional <= 0:
            margin_f = to_float(margin_usdt, 0.0)
            if margin_f > 0:
                target_notional = margin_f * float(lev if is_futures else 1)

        qty_in = to_float(quantity, 0.0)
        if qty_in > 0:
            qty_raw = qty_in
        elif target_notional > 0:
            if ref_price <= 0:
                return fail(f"无法获取 {sym} 参考价，不能按金额换算数量")
            qty_raw = target_notional / ref_price
        else:
            return fail("必须提供 quantity、notional_usdt 或 margin_usdt")

        qty = floor_to_step(qty_raw, step)
        if qty <= 0:
            return fail(
                f"数量 {format_decimal(qty_raw)} 向下取整到 stepSize="
                f"{format_decimal(step)} 后为 0，请提高下单数量/金额"
            )
        if min_qty > 0 and qty < min_qty:
            return fail(
                f"数量 {format_decimal(qty)} 低于最小下单量 minQty={format_decimal(min_qty)}"
            )
        if max_qty > 0 and qty > max_qty:
            return fail(
                f"数量 {format_decimal(qty)} 超过最大下单量 maxQty={format_decimal(max_qty)}"
            )

        order_price = floor_to_precision(price_f, tick) if price_f > 0 else 0.0
        if type_u == "LIMIT" and order_price <= 0:
            return fail(f"价格 {format_decimal(price_f)} 向下取整到 tickSize=0 后无效")

        check_price = order_price if order_price > 0 else ref_price
        if check_price > 0 and qty * check_price < min_notional:
            return fail(
                f"名义金额 {format_decimal(qty * check_price)} USDT "
                f"低于最小下单额 minNotional={format_decimal(min_notional)} USDT"
                f"（数量 {format_decimal(qty)} × 价格 {format_decimal(check_price)}）"
            )

        notional_actual = qty * check_price if check_price > 0 else 0.0
        margin_actual = notional_actual / float(lev) if is_futures and lev > 0 else 0.0

        # 合约：先设杠杆 / 保证金模式，失败只 warning（可能是「无需变更」）
        if is_futures:
            lev_res = self.set_leverage(sym, lev)
            if not lev_res.ok:
                logger.warning(
                    "Binance set_leverage(%s, %sx) 未成功（继续下单）: %s",
                    sym,
                    lev,
                    lev_res.message,
                )
            mode_res = self.set_margin_mode(sym, mode)
            if not mode_res.ok:
                logger.warning(
                    "Binance set_margin_mode(%s, %s) 未成功（继续下单）: %s",
                    sym,
                    mode,
                    mode_res.message,
                )

        params: dict[str, Any] = {
            "symbol": sym,
            "side": side_u,
            "type": type_u,
            "quantity": format_decimal(qty),
        }
        if type_u == "LIMIT":
            params["price"] = format_decimal(order_price)
            params["timeInForce"] = "GTC"

        if is_futures:
            hedge = self._is_hedge_mode()
            if hedge:
                # 双向持仓模式：必须给 positionSide，且不能给 reduceOnly
                params["positionSide"] = (
                    POSITION_LONG.upper() if pos_side == POSITION_LONG else POSITION_SHORT.upper()
                )
            elif reduce_only:
                params["reduceOnly"] = "true"

        payload, err = self._request(
            "POST", self._path("order", mkt), params, signed=True, market=mkt
        )
        if payload is None or not isinstance(payload, dict):
            # 下单类请求失败：状态可能未知，提示人工核对
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                side=side_u,
                order_type=type_u,
                qty=qty,
                price=order_price,
                message=err or "Binance 下单失败（状态未知，请到交易所核对）",
                raw={"request": dict(params)},
                market_type=mkt,
                leverage=lev if is_futures else 1,
                margin_usdt=margin_actual,
                notional_usdt=notional_actual,
                position_side=pos_side if is_futures else POSITION_LONG,
                margin_mode=mode if is_futures else MARGIN_ISOLATED,
            )

        order_id = str(payload.get("orderId", "") or "")
        filled_qty = to_float(payload.get("executedQty"), qty)
        avg_price = 0.0
        try:
            fills = payload.get("fills") or ()
            if fills:
                spent = sum(
                    to_float(f.get("price")) * to_float(f.get("qty")) for f in fills
                )
                got = sum(to_float(f.get("qty")) for f in fills)
                if got > 0:
                    avg_price = spent / got
        except Exception as exc:  # noqa: BLE001 — 成交均价只是展示用
            logger.debug("Binance fills parse failed: %s", exc)
        if avg_price <= 0:
            # 合约市价单的 price 是 0，成交均价在 avgPrice 里
            avg_price = to_float(
                payload.get("avgPrice"), to_float(payload.get("price"), order_price)
            )

        scope = "合约" if is_futures else "现货"
        return TradeResult(
            ok=True,
            venue=self.venue,
            order_id=order_id,
            symbol=sym,
            side=side_u,
            order_type=type_u,
            qty=filled_qty or qty,
            price=avg_price or order_price,
            message=(
                f"Binance {scope} {type_u} {side_u} {sym} 已提交"
                f"（数量 {format_decimal(filled_qty or qty)}，"
                f"orderId={order_id or '—'}，status="
                f"{payload.get('status', '') or '—'}"
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
        symbol: str,
        *,
        position_side: str = POSITION_LONG,
        plans: list[tuple[str, float, float, str]],
        market_type: str | None = None,
    ) -> list[ProtectionOrder]:
        """开仓成功后立刻把止损 / TP1 / TP2 挂成交易所侧条件单。

        Binance U 本位永续的条件单与普通下单同一个 endpoint
        （``POST /fapi/v1/order``），区别在 ``type``：

        * 止损 → ``STOP_MARKET`` + ``stopPrice``
        * 止盈 → ``TAKE_PROFIT_MARKET`` + ``stopPrice``

        止损用 ``closePosition=true``（触发时平掉整个仓位，最稳，不依赖数量
        精度）；TP1/TP2 用 ``quantity`` + ``reduceOnly`` 分批减仓，这样两段
        止盈才能各自独立触发。

        现货没有交易所侧条件单，直接返回「不支持」的中文原因，**不假装挂上**。
        每条腿独立成败，绝不抛异常。
        """
        mkt = normalize_market_type(
            market_type if market_type is not None else self._market_type
        )
        is_futures = mkt == MARKET_FUTURES
        pos_side = normalize_position_side(position_side)
        sym = to_binance_symbol(to_text(symbol))

        if not is_futures:
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="Binance 现货不支持交易所侧止损/止盈条件单，需使用合约",
                )
                for kind, price, qty, _problem in plans
            ]

        if not self._has_credentials():
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="未填写 Binance API Key/Secret，无法挂保护单",
                )
                for kind, price, qty, _problem in plans
            ]
        if not sym:
            return [
                ProtectionOrder(
                    kind=kind,
                    trigger_price=to_float(price, 0.0),
                    qty=to_float(qty, 0.0),
                    message="交易对（symbol）为空",
                )
                for kind, price, qty, _problem in plans
            ]

        rules = self._symbol_filters(sym, mkt)
        tick = to_float(rules.get("tick"), 0.0)
        step = to_float(rules.get("step"), 0.0)
        min_qty = to_float(rules.get("min_qty"), 0.0)
        hedge = self._is_hedge_mode()
        close_side = "SELL" if pos_side == POSITION_LONG else "BUY"

        # 先看两段止盈各自能否成单：拆不开就合并成一条
        tp_total_qty = 0.0
        tp_legs_splittable = True
        for _kind, _price, _qty, _problem in plans:
            if _kind in (PROTECT_TAKE_PROFIT_1, PROTECT_TAKE_PROFIT_2):
                _leg = floor_to_step(to_float(_qty, 0.0), step)
                if _leg <= 0 or (min_qty > 0 and _leg < min_qty):
                    tp_legs_splittable = False
                tp_total_qty += to_float(_qty, 0.0)
        merge_tp = (not tp_legs_splittable) and tp_total_qty > 0

        out: list[ProtectionOrder] = []
        for kind, price, qty, problem in plans:
            trigger = to_float(price, 0.0)
            leg_qty = to_float(qty, 0.0)

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

            is_stop = kind == PROTECT_STOP_LOSS

            # 仓位太小、TP1/TP2 拆出来的两段都低于 minQty 时，
            # 把两段止盈合并成一条（用 TP1 的价、两段合起来的量）。
            # 宁可少赚一点，也不要出现「止盈整段挂不上」。
            if kind == PROTECT_TAKE_PROFIT_2 and merge_tp:
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger, 8),
                        qty=round(leg_qty, 8),
                        message=(
                            "仓位太小，TP1/TP2 无法拆分成两张单，"
                            "已合并：整仓在 TP1 挂止盈"
                        ),
                    )
                )
                continue
            if kind == PROTECT_TAKE_PROFIT_1 and merge_tp:
                leg_qty = tp_total_qty

            params: dict[str, Any] = {
                "symbol": sym,
                "side": close_side,
                "type": "STOP_MARKET" if is_stop else "TAKE_PROFIT_MARKET",
                "stopPrice": format_decimal(trigger_px),
                # 标记价触发：比成交价触发更不容易被一根插针扫掉
                "workingType": "MARK_PRICE",
            }

            if hedge:
                params["positionSide"] = (
                    POSITION_LONG.upper() if pos_side == POSITION_LONG else POSITION_SHORT.upper()
                )

            if is_stop:
                # 止损永远全量：closePosition 不受数量精度影响，触发即平掉整仓
                params["closePosition"] = "true"
                sent_qty = leg_qty
            else:
                qty_leg = floor_to_step(leg_qty, step)
                if qty_leg <= 0:
                    out.append(
                        ProtectionOrder(
                            kind=kind,
                            trigger_price=round(trigger_px, 8),
                            qty=round(leg_qty, 8),
                            message=(
                                f"数量 {format_decimal(leg_qty)} 向下取整到 "
                                f"stepSize={format_decimal(step)} 后为 0"
                            ),
                        )
                    )
                    continue
                if min_qty > 0 and qty_leg < min_qty:
                    out.append(
                        ProtectionOrder(
                            kind=kind,
                            trigger_price=round(trigger_px, 8),
                            qty=round(qty_leg, 8),
                            message=(
                                f"数量 {format_decimal(qty_leg)} 低于最小下单量 "
                                f"minQty={format_decimal(min_qty)}"
                            ),
                        )
                    )
                    continue
                params["quantity"] = format_decimal(qty_leg)
                if not hedge:
                    params["reduceOnly"] = "true"
                sent_qty = qty_leg

            payload, err = self._request(
                "POST",
                self._path("conditional_order", mkt),
                params,
                signed=True,
                market=mkt,
            )
            if payload is None or not isinstance(payload, dict):
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger_px, 8),
                        qty=round(sent_qty, 8),
                        message=err or "Binance 保护单提交失败（状态未知，请到交易所核对）",
                        raw={"request": params},
                    )
                )
                continue

            order_id = str(payload.get("orderId", "") or "")
            status = str(payload.get("status", "") or "")
            if payload.get("code") not in (None, 0):
                out.append(
                    ProtectionOrder(
                        kind=kind,
                        trigger_price=round(trigger_px, 8),
                        qty=round(sent_qty, 8),
                        message=(
                            f"Binance 保护单被拒 {payload.get('code')}: "
                            f"{payload.get('msg', '')}"
                        ).strip(),
                        raw=dict(payload),
                    )
                )
                continue

            out.append(
                ProtectionOrder(
                    kind=kind,
                    ok=True,
                    trigger_price=round(trigger_px, 8),
                    qty=round(sent_qty, 8),
                    order_id=order_id,
                    raw=dict(payload),
                )
            )
            logger.info(
                "Binance 保护单已挂 %s %s %s 触发价 %s status=%s orderId=%s",
                sym,
                params.get("type"),
                close_side,
                format_decimal(trigger_px),
                status or "—",
                order_id or "—",
            )
        return out

    def close_position(
        self, symbol: str, position_side: str = POSITION_LONG
    ) -> TradeResult:
        """市价平掉指定方向的合约持仓（oneway 用 ``reduceOnly``）。

        hedge 模式：``side`` 取反 + ``positionSide=被平方向``；
        oneway 模式：``side`` 取反 + ``reduceOnly=true``。
        """
        sym = to_binance_symbol(to_text(symbol))
        pos_side = normalize_position_side(position_side)
        side_u = "SELL" if pos_side == POSITION_LONG else "BUY"

        if self._market_type != MARKET_FUTURES:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message="close_position 仅用于合约（market_type=futures）",
                market_type=self._market_type,
            )
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message="未填写 Binance API Key/Secret，无法平仓",
                market_type=self._market_type,
                position_side=pos_side,
            )
        if not sym:
            return TradeResult(
                ok=False,
                venue=self.venue,
                message="平仓需要 symbol",
                market_type=self._market_type,
                position_side=pos_side,
            )

        return self.place_order(
            sym,
            side_u,
            ORDER_TYPE_MARKET,
            market_type=MARKET_FUTURES,
            position_side=pos_side,
            reduce_only=True,
            quantity=self._position_size(sym, pos_side),
        )

    def _position_size(self, symbol: str, position_side: str) -> float:
        """Current size of one position side (``0.0`` when unknown/empty)."""
        ok, _msg, rows = self.list_positions()
        if not ok:
            return 0.0
        for row in rows:
            if row.get("symbol") == symbol and row.get("position_side") == position_side:
                return to_float(row.get("size"))
        return 0.0

    def cancel_order(self, symbol: str, order_id: str | int) -> TradeResult:
        """Cancel one open order."""
        mkt = self._market_type
        sym = to_binance_symbol(to_text(symbol))
        oid = to_text(order_id)
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                order_id=oid,
                message="未填写 Binance API Key/Secret，无法撤单",
                market_type=mkt,
            )
        if not sym or not oid:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                order_id=oid,
                message="撤单需要 symbol 与 order_id",
                market_type=mkt,
            )

        payload, err = self._request(
            "DELETE",
            self._path("order", mkt),
            {"symbol": sym, "orderId": oid},
            signed=True,
            market=mkt,
        )
        if payload is None or not isinstance(payload, dict):
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                order_id=oid,
                message=err or "Binance 撤单失败",
                market_type=mkt,
            )
        return TradeResult(
            ok=True,
            venue=self.venue,
            order_id=str(payload.get("orderId", oid) or oid),
            symbol=sym,
            message=f"Binance 撤单成功（{sym} orderId={oid}）",
            raw=dict(payload),
            market_type=mkt,
        )

    def list_open_orders(self, symbol: str = "") -> TradeResult:
        """List open orders; ``symbol`` empty means all symbols."""
        mkt = self._market_type
        sym = to_binance_symbol(to_text(symbol)) if symbol else ""
        if not self._has_credentials():
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message="未填写 Binance API Key/Secret，无法查询未成交订单",
                market_type=mkt,
            )

        params: dict[str, Any] = {"symbol": sym} if sym else {}
        payload, err = self._request(
            "GET", self._path("open_orders", mkt), params, signed=True, market=mkt
        )
        if payload is None:
            return TradeResult(
                ok=False,
                venue=self.venue,
                symbol=sym,
                message=err or "Binance 查询未成交订单失败",
                market_type=mkt,
            )
        # openOrders 成功时返回 JSON 数组
        orders = payload if isinstance(payload, list) else []
        return TradeResult(
            ok=True,
            venue=self.venue,
            symbol=sym,
            message=f"Binance 未成交订单 {len(orders)} 笔",
            raw={"orders": orders},
            market_type=mkt,
        )


__all__ = ["BinanceTrader"]
