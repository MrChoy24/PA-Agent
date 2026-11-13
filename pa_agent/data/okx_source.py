"""OKX spot/futures K-line data source (public REST V5 market data).

Public candles need no credentials; API key/secret/passphrase are only used by
the optional "test connection" button that hits the signed account endpoint.

Usage:
    source = OKXSource()
    source.connect()                       # non-fatal; retries lazily
    source.subscribe("BTCUSDT", "15m")
    bars = source.latest_snapshot(60)      # newest-first, bars[0] = forming bar
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.crypto_common import (
    COMMON_SYMBOLS,
    to_okx_inst_id,
)
from pa_agent.util import endpoints, throttle

logger = logging.getLogger(__name__)

# Map our timeframe strings → OKX bar strings
_TF_MAP: dict[str, str] = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1H",
    "2h":  "2H",
    "4h":  "4H",
    "6h":  "6H",
    "12h": "12H",
    "1d":  "1D",
    "1w":  "1W",
    "1M":  "1M",
}

# 首选主机常量（历史名 ``_HOSTS`` 保留，供模块内探测逻辑兜底引用）。
# 实际候选列表来自 ``pa_agent.util.endpoints.okx_hosts()``：默认只有
# www.okx.com，``aws.okx.com`` 在很多网络下 DNS 解析失败，不再默认当备用
# （可用环境变量 PA_AGENT_OKX_HOSTS 追加）。
_HOSTS: tuple[str, ...] = (endpoints.OKX_DEFAULT_HOST,)

# 同一 instId+bar 的响应缓存时长（秒），避免 1Hz 刷新重复请求
_CACHE_TTL_S = 0.7

# 主机探测用的短超时：两台主机串行探测时，12s 的默认超时会让一次失败拖到 24s。
_PROBE_TIMEOUT_S = 4.0

# 建连超时：不可达主机的 TCP 握手会挂到超时为止，单独压到 3s。
_CONNECT_TIMEOUT_S = 3.0

# 进程级主机记忆：切换数据源会新建实例，共享解析结果避免重复探测。
_RESOLVED_HOST: str = ""
# 全部主机探测失败后的冷却截止时间（monotonic），冷却期内不再重试整个列表。
_HOST_RETRY_AFTER: float = 0.0
_HOST_RETRY_COOLDOWN_S = 30.0

# /market/candles 单次上限
_CANDLES_MAX = 300
# 分页硬上限（每页最多 300 根 → 最远约 2400 根）
_MAX_PAGES = 8

_BALANCE_PATH = "/api/v5/account/balance"


class OKXSource(DataSource):
    """K-line data from OKX V5 REST API.

    Public market data is unauthenticated; ``api_key``/``api_secret``/
    ``passphrase`` are only required for :meth:`test_private_connection` /
    :meth:`account_overview`.  Set ``demo=True`` for simulated trading.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo: bool = False,
        timeout_s: float = 12.0,
    ) -> None:
        self._api_key: str = api_key or ""
        self._api_secret: str = api_secret or ""
        self._passphrase: str = passphrase or ""
        self._demo: bool = bool(demo)
        self._timeout_s: float = timeout_s

        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False

        self._client_obj = None          # 延迟创建的 httpx.Client
        self._host: str = ""             # 已确认可用的主机（空 = 尚未解析）

        # 简单响应缓存
        self._cache_bars: list[KlineBar] = []
        self._cache_ts: float = 0.0

    # ── HTTP plumbing ─────────────────────────────────────────────────────────

    def _client(self):
        """Lazily create the shared httpx client."""
        if self._client_obj is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover — httpx is a hard dep
                raise DataSourceTransientError(
                    "httpx not installed — run: pip install httpx"
                ) from exc
            # 连接超时单列：主机不可达时 TCP 握手会挂满整个超时，
            # 用 12s 的读超时去等连接会让首帧白等十几秒。
            self._client_obj = httpx.Client(
                timeout=httpx.Timeout(self._timeout_s, connect=_CONNECT_TIMEOUT_S),
                follow_redirects=True,
            )
        return self._client_obj

    def _reset_cache(self) -> None:
        self._cache_bars = []
        self._cache_ts = 0.0

    def _resolve_host(self) -> str:
        """Return the first reachable host, remembering it for the process."""
        global _RESOLVED_HOST, _HOST_RETRY_AFTER

        if self._host:
            return self._host
        if _RESOLVED_HOST:
            self._host = _RESOLVED_HOST
            return _RESOLVED_HOST
        now = time.monotonic()
        if now < _HOST_RETRY_AFTER:
            return endpoints.okx_hosts()[0]
        client = self._client()
        last_error = ""
        for host in endpoints.okx_hosts():
            try:
                resp = client.get(f"{host}/api/v5/public/time", timeout=_PROBE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — 逐个主机降级
                last_error = f"{host}: {exc}"
                logger.debug("OKX host %s unreachable: %s", host, exc)
                continue
            if resp.status_code == 200:
                self._host = host
                _RESOLVED_HOST = host
                logger.info("OKX host resolved: %s", host)
                return host
            last_error = f"{host}: HTTP {resp.status_code}"
        # 全部主机失败：不抛错，暂用首选主机，冷却一段时间后再重试
        _HOST_RETRY_AFTER = time.monotonic() + _HOST_RETRY_COOLDOWN_S
        logger.warning("OKX no host reachable (%s); will retry later", last_error)
        return endpoints.okx_hosts()[0]

    def _get(self, path: str, params: dict | None = None, *, headers: dict | None = None):
        """GET against the resolved host, walking the fallback list on failure."""
        hosts = list(endpoints.okx_hosts())
        candidates = (
            [self._host] + [h for h in hosts if h != self._host]
            if self._host
            else hosts
        )
        tried: list[str] = []
        for host in candidates:
            if not host:
                continue
            # 与交易侧共用一个令牌桶：行情 1Hz 刷新 + 后端轮询叠加时，
            # 不加节流很容易在短时间内打出几十个请求。
            throttle.acquire("okx")
            try:
                resp = self._client().get(
                    f"{host}{path}", params=params, headers=headers
                )
            except Exception as exc:  # noqa: BLE001
                tried.append(f"{host}: {exc}")
                logger.debug("OKX request to %s failed: %s", host, exc)
                continue
            if resp.status_code == 200:
                self._host = host
                return resp
            # 主机级错误 → 换主机；业务错误由调用方按 code 解析
            if resp.status_code in (403, 451, 418, 429, 500, 502, 503, 504):
                tried.append(f"{host}: HTTP {resp.status_code}")
                continue
            self._host = host
            return resp
        raise DataSourceTransientError(endpoints.unreachable_hint(tried))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Mark the source usable; host resolution is deferred to first request.

        之前这里会串行探测各主机的 ``/api/v5/public/time``（实测 0.7~0.8 s），
        而 ``connect()`` 跑在 GUI 线程的启动路径上，直接卡住窗口。
        本方法本来就"网络异常不抛错"，真正的可用性检查交给 ``_get()``。
        """
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "httpx not installed — run: pip install httpx"
            ) from exc

        self._connected = True
        logger.info("OKXSource connected (demo=%s, host resolution deferred)", self._demo)

    def disconnect(self) -> None:
        if self._client_obj is not None:
            try:
                self._client_obj.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("OKX client close error: %s", exc)
            finally:
                self._client_obj = None
        self._reset_cache()
        self._connected = False
        logger.info("OKXSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        """Curated neutral USDT pairs (offline-safe, deterministic)."""
        return list(COMMON_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. Use one of {list(_TF_MAP)}"
            )
        self._symbol = to_okx_inst_id(symbol)
        self._timeframe = timeframe
        self._reset_cache()
        logger.info("OKXSource subscribed: %s %s", self._symbol, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._reset_cache()
        logger.info("OKXSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar.

        ``/market/candles`` caps ``limit`` at 300, so requests above that page
        backwards through ``/market/history-candles`` using the ``after`` cursor.
        """
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        limit = max(2, min(1000, int(n)))

        # 0.7s 内同一 instId+bar 直接复用缓存
        now = time.monotonic()
        if self._cache_bars and (now - self._cache_ts) < _CACHE_TTL_S:
            return self._cache_bars[:n]

        bar = _TF_MAP[self._timeframe]
        rows = self._fetch_rows(self._symbol, bar, limit)

        # 归一化去重：OKX 已是 newest-first；分页拼接可能重复同一 ts
        seen: set[str] = set()
        unique: list[list] = []
        for row in rows:
            try:
                key = str(row[0])
            except (IndexError, TypeError):
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        unique.sort(key=lambda r: float(r[0]), reverse=True)

        bars: list[KlineBar] = []
        for i, row in enumerate(unique):
            try:
                ts_ms = float(row[0])                      # 已是毫秒
                confirm = str(row[8]) if len(row) > 8 else "1"
                closed = (confirm == "1") and (i != 0)
                bars.append(
                    normalize_kline_bar(
                        KlineBar(
                            seq=i + 1,
                            ts_open=ts_ms,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            amount=float(row[7] or 0.0),    # volCcyQuote
                            closed=closed,
                        )
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                raise DataSourceTransientError(f"OKX K线解析失败: {exc}") from exc
            if len(bars) >= n:
                break

        if not bars:
            raise DataSourceTransientError(f"OKX 无数据: {self._symbol} {bar}")

        self._cache_bars = bars
        self._cache_ts = time.monotonic()
        return bars

    def _fetch_rows(self, inst_id: str, bar: str, limit: int) -> list[list]:
        """Fetch up to *limit* raw candle rows, paging with the ``after`` cursor."""
        rows = self._request_candles(inst_id, bar, min(limit, _CANDLES_MAX))
        if limit <= _CANDLES_MAX or not rows:
            return rows

        pages = 1
        # 继续向更早的数据翻页
        while len(rows) < limit and pages < _MAX_PAGES:
            oldest_ts = rows[-1][0]
            try:
                older = self._request_history(inst_id, bar, _CANDLES_MAX, after=str(oldest_ts))
            except DataSourceTransientError as exc:
                # 已有数据时，分页失败只用告警降级，不破坏本次快照
                logger.warning("OKX pagination stopped early: %s", exc)
                break
            if not older:
                break
            # 以 ts 去重后再拼接，避免 after 边界重复
            known = {str(r[0]) for r in rows}
            fresh = [r for r in older if str(r[0]) not in known]
            if not fresh:
                break                      # 无进展，停止翻页
            rows.extend(fresh)
            pages += 1

        return rows

    def _request_candles(self, inst_id: str, bar: str, limit: int) -> list[list]:
        try:
            resp = self._get(
                "/api/v5/market/candles",
                {"instId": inst_id, "bar": bar, "limit": limit},
            )
        except DataSourceTransientError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataSourceTransientError(
                f"OKX 请求 {inst_id} {bar} 失败: {exc}"
            ) from exc
        return self._parse_payload(resp)

    def _request_history(
        self, inst_id: str, bar: str, limit: int, *, after: str
    ) -> list[list]:
        try:
            resp = self._get(
                "/api/v5/market/history-candles",
                {"instId": inst_id, "bar": bar, "limit": limit, "after": after},
            )
        except DataSourceTransientError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataSourceTransientError(
                f"OKX 历史K线请求 {inst_id} {bar} 失败: {exc}"
            ) from exc
        return self._parse_payload(resp)

    @staticmethod
    def _parse_payload(resp) -> list[list]:
        """Validate ``{"code": "0"}`` and return the ``data`` rows."""
        if resp.status_code != 200:
            raise DataSourceTransientError(f"OKX HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DataSourceTransientError(f"OKX 返回非 JSON 数据: {exc}") from exc
        code = str(payload.get("code", ""))
        if code != "0":
            msg = str(payload.get("msg", "") or "").strip()
            raise DataSourceTransientError(f"OKX 错误 {code}: {msg}")
        return list(payload.get("data") or ())

    # ── Private API (test connection button) ──────────────────────────────────

    def set_credentials(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str = "",
        demo: bool = False,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._api_secret = (api_secret or "").strip()
        self._passphrase = (passphrase or "").strip()
        self._demo = bool(demo)

    def _has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret and self._passphrase)

    def _signed_headers(self, method: str, request_path: str) -> dict[str, str]:
        """Build OKX V5 auth headers for one request."""
        # ISO-8601 UTC 毫秒精度，例：2020-12-08T09:08:57.715Z
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        prehash = f"{ts}{method.upper()}{request_path}"
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

    def test_private_connection(self) -> tuple[bool, str]:
        """Check credentials against the signed balance endpoint.

        Never raises — returns ``(ok, message)`` for the GUI to display.
        """
        if not self._has_credentials():
            return False, "未填写 OKX API Key/Secret/Passphrase"
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False, "httpx 未安装 — 请执行: pip install httpx"

        try:
            headers = self._signed_headers("GET", _BALANCE_PATH)
            resp = self._get(_BALANCE_PATH, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OKX private test failed: %s", exc)
            return False, f"OKX 连接失败: {exc}"

        if resp.status_code != 200:
            return False, f"OKX HTTP {resp.status_code}"
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"OKX 返回非 JSON 数据: {exc}"

        code = str(payload.get("code", ""))
        if code != "0":
            msg = str(payload.get("msg", "") or "").strip()
            return False, f"OKX 错误 {code}: {msg}"
        return True, "OKX API 连接成功 (账户余额接口可用)"

    def account_overview(self) -> dict:
        """Best-effort parsed account balances; ``{}`` on any error."""
        if not self._has_credentials():
            return {}
        try:
            headers = self._signed_headers("GET", _BALANCE_PATH)
            resp = self._get(_BALANCE_PATH, headers=headers)
            if resp.status_code != 200:
                logger.warning("OKX account_overview HTTP %s", resp.status_code)
                return {}
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("OKX account_overview failed: %s", exc)
            return {}

        if str(payload.get("code", "")) != "0":
            logger.warning("OKX account_overview error: %s", payload.get("msg"))
            return {}

        data = list(payload.get("data") or ())
        if not data:
            return {}

        account = data[0]
        balances: list[dict] = []
        for item in account.get("details", ()) or ():
            try:
                cash = float(item.get("cashBal", 0) or 0)
            except (TypeError, ValueError):
                cash = 0.0
            if cash == 0.0:
                continue
            balances.append(
                {
                    "ccy": str(item.get("ccy", "")),
                    "cashBal": cash,
                    "availBal": float(item.get("availBal", 0) or 0),
                    "frozenBal": float(item.get("frozenBal", 0) or 0),
                }
            )
        return {
            "totalEq": account.get("totalEq", ""),
            "uTime": account.get("uTime", ""),
            "balances": balances,
        }
