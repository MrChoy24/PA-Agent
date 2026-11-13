"""Binance spot K-line data source (public REST market data).

Public endpoints need no credentials; API key/secret are only used by the
optional "test connection" button that hits the signed account endpoint.

Usage:
    source = BinanceSource()
    source.connect()                      # non-fatal; retries lazily
    source.subscribe("BTCUSDT", "15m")
    bars = source.latest_snapshot(60)     # newest-first, bars[0] = forming bar
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.crypto_common import (
    COMMON_SYMBOLS,
    to_binance_symbol,
)
from pa_agent.util import throttle

logger = logging.getLogger(__name__)

# Map our timeframe strings → Binance interval strings
_TF_MAP: dict[str, str] = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "6h":  "6h",
    "8h":  "8h",
    "12h": "12h",
    "1d":  "1d",
    "3d":  "3d",
    "1w":  "1w",
    "1M":  "1M",
}

# 备用主机：按顺序尝试，记住本次会话中第一个可用的。
# data-api.binance.vision 排第一：它是 Binance 官方的公开行情镜像，
# 在很多网络环境下比 api.binance.com 更快也更稳；而 api.binance.com
# 一旦被墙，单次 TCP 连接会耗满整个超时（实测 12s），首帧就要白等。
_HOSTS: tuple[str, ...] = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
)

# 私有接口（账户/下单）不能用公开行情镜像，只走真正的 API 主机。
_ACCOUNT_HOSTS: tuple[str, ...] = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
)

# 同一 symbol+timeframe 的响应缓存时长（秒），避免 1Hz 刷新重复请求
_CACHE_TTL_S = 0.7

# exchangeInfo 是准静态数据（一天难得变），单独用长 TTL 缓存，
# 否则每次打开品种下拉都要打一次网络（实测 0.5~0.9s，直接卡住 UI）。
_SYMBOLS_CACHE_TTL_S = 300.0

# 进程级主机记忆：多个 BinanceSource 实例（切换数据源会新建）共享解析结果，
# 避免每次切换都重新探测 4 台主机。
_RESOLVED_HOST: str = ""
# 全部主机探测失败后的冷却截止时间（monotonic），冷却期内不再重试整个列表。
_HOST_RETRY_AFTER: float = 0.0
_HOST_RETRY_COOLDOWN_S = 30.0

# 进程级交易所交易对缓存：(frozenset[live symbols], monotonic ts)
_SYMBOLS_CACHE: tuple[frozenset[str], float] | None = None


def _cached_live_symbols() -> frozenset[str] | None:
    """Return the cached TRADING symbol set, or None when stale/absent."""
    if _SYMBOLS_CACHE is None:
        return None
    live, ts = _SYMBOLS_CACHE
    if (time.monotonic() - ts) >= _SYMBOLS_CACHE_TTL_S:
        return None
    return live or None

_RECV_WINDOW_MS = 5000

# 主机探测用的短超时：4 台主机串行探测时，12s 的默认超时会让一次失败拖到 48s。
_PROBE_TIMEOUT_S = 4.0

# 建连超时：被墙的主机 TCP 握手会挂到超时为止，单独压到 3s，
# 这样 4 台主机全不可达时最多拖 12s 而不是 48s。
_CONNECT_TIMEOUT_S = 3.0


class BinanceSource(DataSource):
    """K-line data from Binance spot REST API.

    Public market data is unauthenticated; ``api_key``/``api_secret`` are only
    required for :meth:`test_private_connection` / :meth:`account_overview`.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        timeout_s: float = 12.0,
    ) -> None:
        self._api_key: str = api_key or ""
        self._api_secret: str = api_secret or ""
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
            # 连接超时单列：主机被墙时 TCP 握手会一直挂到超时为止，用 12s
            # 的读超时去等连接会让首帧白等十几秒。读超时仍保持 12s。
            self._client_obj = httpx.Client(
                timeout=httpx.Timeout(self._timeout_s, connect=_CONNECT_TIMEOUT_S),
                follow_redirects=True,
            )
        return self._client_obj

    def _reset_cache(self) -> None:
        self._cache_bars = []
        self._cache_ts = 0.0

    def _resolve_host(self) -> str:
        """Return the first reachable host, remembering it for the process.

        The result is cached at module level so switching data sources (which
        builds a fresh ``BinanceSource``) does not pay the probe cost again.
        When every host is unreachable we back off for
        ``_HOST_RETRY_COOLDOWN_S`` instead of re-probing on every call.
        """
        global _RESOLVED_HOST, _HOST_RETRY_AFTER

        if self._host:
            return self._host
        if _RESOLVED_HOST:
            self._host = _RESOLVED_HOST
            return _RESOLVED_HOST
        now = time.monotonic()
        if now < _HOST_RETRY_AFTER:
            # 冷却期内：不再探测，直接用首选主机，让真实请求自己去试
            return _HOSTS[0]
        client = self._client()
        last_error = ""
        for host in _HOSTS:
            try:
                resp = client.get(f"{host}/api/v3/time", timeout=_PROBE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — 逐个主机降级
                last_error = f"{host}: {exc}"
                logger.debug("Binance host %s unreachable: %s", host, exc)
                continue
            if resp.status_code == 200:
                self._host = host
                _RESOLVED_HOST = host
                logger.info("Binance host resolved: %s", host)
                return host
            last_error = f"{host}: HTTP {resp.status_code}"
        # 全部主机失败：不抛错，暂用首选主机，冷却一段时间后再重试
        _HOST_RETRY_AFTER = time.monotonic() + _HOST_RETRY_COOLDOWN_S
        logger.warning("Binance no host reachable (%s); will retry later", last_error)
        return _HOSTS[0]

    def _get(self, path: str, params: dict | None = None, *, signed_headers: dict | None = None):
        """GET against the resolved host, walking the fallback list on failure."""
        tried: list[str] = []
        candidates = [self._host] + [h for h in _HOSTS if h != self._host] if self._host \
            else list(_HOSTS)
        last_exc: Exception | None = None
        for host in candidates:
            if not host:
                continue
            # 本地节流：行情轮询 + 账户查询共用同一个 Binance 令牌桶。
            throttle.acquire("binance")
            try:
                resp = self._client().get(
                    f"{host}{path}", params=params, headers=signed_headers
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                tried.append(f"{host}: {exc}")
                logger.debug("Binance request to %s failed: %s", host, exc)
                continue
            if resp.status_code == 200:
                self._host = host
                return resp
            # 451/418 等主机级错误 → 换主机；4xx 业务错误交给调用方解析
            if resp.status_code in (403, 451, 418, 429, 500, 502, 503, 504):
                tried.append(f"{host}: HTTP {resp.status_code}")
                continue
            self._host = host
            return resp
        detail = "; ".join(tried) or "unknown network error"
        if last_exc is not None and not tried:  # pragma: no cover — defensive
            raise DataSourceTransientError(f"Binance 请求失败: {last_exc}") from last_exc
        raise DataSourceTransientError(f"Binance 请求失败: {detail}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Mark the source usable; host resolution is deferred to first request.

        之前这里会串行探测 4 台主机的 ``/api/v3/time``（实测 0.8~1.1 s），而
        ``connect()`` 是在 GUI 线程的启动路径上被调用的，直接把窗口卡住。
        由于本方法本来就"网络异常不抛错"，探测失败也只是换用首选主机，
        真正的可用性检查由 ``_get()`` 在后台刷新线程里完成即可。
        """
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "httpx not installed — run: pip install httpx"
            ) from exc

        self._connected = True
        logger.info("BinanceSource connected (host resolution deferred)")

    def disconnect(self) -> None:
        if self._client_obj is not None:
            try:
                self._client_obj.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Binance client close error: %s", exc)
            finally:
                self._client_obj = None
        self._reset_cache()
        self._connected = False
        logger.info("BinanceSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        """Curated USDT pairs, enriched from ``exchangeInfo`` when already cached.

        This method is called on the GUI thread while rebuilding the symbol
        combo box, so it must never block on the network.  The enrichment is
        therefore purely cache-driven: a background warm-up
        (:meth:`warm_symbols_cache`) fills ``_SYMBOLS_CACHE`` and later calls
        pick it up for free.
        """
        curated = list(COMMON_SYMBOLS)
        live = _cached_live_symbols()
        if live:
            merged = [sym for sym in curated if sym in live]
            if merged:
                return merged
        return curated

    def warm_symbols_cache(self, *, force: bool = False) -> bool:
        """Fetch ``exchangeInfo`` in the background and cache the TRADING set.

        Safe to call from a worker thread; returns True when the cache was
        (re)filled.  Never raises — failures are logged and simply leave the
        curated list in place.
        """
        global _SYMBOLS_CACHE

        if not self._connected:
            return False
        if not force and _cached_live_symbols():
            return False
        now = time.monotonic()
        try:
            resp = self._get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
            if resp.status_code != 200:
                return False
            payload = resp.json()
            live = frozenset(
                str(item.get("symbol", ""))
                for item in payload.get("symbols", ())
                if str(item.get("status", "")) == "TRADING"
            )
        except Exception as exc:  # noqa: BLE001 — 预热失败不影响主流程
            logger.debug("Binance exchangeInfo warm-up skipped: %s", exc)
            return False
        if not live:
            return False
        _SYMBOLS_CACHE = (live, now)
        logger.debug("Binance symbols cache warmed (%d trading pairs)", len(live))
        return True

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. Use one of {list(_TF_MAP)}"
            )
        self._symbol = to_binance_symbol(symbol)
        self._timeframe = timeframe
        self._reset_cache()
        logger.info("BinanceSource subscribed: %s %s", self._symbol, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._reset_cache()
        logger.info("BinanceSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar."""
        if not self._connected:
            raise DataSourceTransientError("Not connected — call connect() first")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Not subscribed — call subscribe() first")

        limit = max(2, min(1000, int(n)))

        # 0.7s 内同一 symbol+timeframe 直接复用缓存
        now = time.monotonic()
        if self._cache_bars and (now - self._cache_ts) < _CACHE_TTL_S:
            return self._cache_bars[:n]

        interval = _TF_MAP[self._timeframe]
        # 不在此处预校验交易对：交给交易所返回权威错误（如 -1121 Invalid symbol）
        try:
            resp = self._get(
                "/api/v3/klines",
                {"symbol": self._symbol, "interval": interval, "limit": limit},
            )
        except DataSourceTransientError:
            raise
        except Exception as exc:  # noqa: BLE001 — httpx 各类异常统一降级
            raise DataSourceTransientError(
                f"Binance 请求 {self._symbol} {interval} 失败: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise DataSourceTransientError(self._error_text(resp))

        try:
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DataSourceTransientError(
                f"Binance 返回非 JSON 数据: {exc}"
            ) from exc

        if isinstance(rows, dict):
            # {"code": -1121, "msg": "Invalid symbol."}
            raise DataSourceTransientError(self._error_text(resp))
        if not rows:
            raise DataSourceTransientError(
                f"Binance 无数据: {self._symbol} {interval}"
            )

        bars: list[KlineBar] = []
        # Binance 返回 oldest-first → 反转成 newest-first
        for i, row in enumerate(reversed(rows)):
            try:
                bars.append(
                    normalize_kline_bar(
                        KlineBar(
                            seq=i + 1,
                            ts_open=float(row[0]),          # 已是毫秒
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            amount=float(row[7] or 0.0),    # quoteVolume
                            closed=(i != 0),
                        )
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                raise DataSourceTransientError(
                    f"Binance K线解析失败: {exc}"
                ) from exc
            if len(bars) >= n:
                break

        self._cache_bars = bars
        self._cache_ts = time.monotonic()
        return bars

    @staticmethod
    def _error_text(resp) -> str:
        """Extract Binance's human-readable error message."""
        code = ""
        msg = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                code = str(payload.get("code", ""))
                msg = str(payload.get("msg", ""))
        except Exception:  # noqa: BLE001
            msg = (resp.text or "").strip()[:200]
        if code or msg:
            return f"Binance 错误 {code}: {msg}".strip()
        return f"Binance HTTP {resp.status_code}"

    # ── Private API (test connection button) ──────────────────────────────────

    def set_credentials(self, api_key: str, api_secret: str) -> None:
        self._api_key = (api_key or "").strip()
        self._api_secret = (api_secret or "").strip()

    def _has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _signed_account_get(self):
        """Signed ``GET /api/v3/account``; returns the httpx response.

        私有接口不能走 ``data-api.binance.vision``（它只镜像公开行情），
        因此这里用 ``_ACCOUNT_HOSTS`` 单独解析，并轮换重试。
        """
        params = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": _RECV_WINDOW_MS,
        }
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url_params = dict(params, signature=signature)
        headers = {"X-MBX-APIKEY": self._api_key}
        last_exc: Exception | None = None
        for host in _ACCOUNT_HOSTS:
            throttle.acquire("binance")
            try:
                return self._client().get(
                    f"{host}/api/v3/account", params=url_params, headers=headers
                )
            except Exception as exc:  # noqa: BLE001 — 换主机重试
                last_exc = exc
                logger.debug("Binance account request to %s failed: %s", host, exc)
                continue
        assert last_exc is not None
        raise last_exc

    def test_private_connection(self) -> tuple[bool, str]:
        """Check key/secret validity against the signed account endpoint.

        Never raises — returns ``(ok, message)`` for the GUI to display.
        """
        if not self._has_credentials():
            return False, "未填写 Binance API Key/Secret"
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False, "httpx 未安装 — 请执行: pip install httpx"

        try:
            resp = self._signed_account_get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance private test failed: %s", exc)
            return False, f"Binance 连接失败: {exc}"

        if resp.status_code != 200:
            return False, self._error_text(resp)

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"Binance 返回非 JSON 数据: {exc}"

        account_type = str(payload.get("accountType", "") or "未知")
        can_trade = payload.get("canTrade")
        can_withdraw = payload.get("canWithdraw")
        return (
            True,
            f"Binance API 连接成功（accountType={account_type}, "
            f"canTrade={can_trade}, canWithdraw={can_withdraw}）",
        )

    def account_overview(self) -> dict:
        """Best-effort parsed account balances; ``{}`` on any error."""
        if not self._has_credentials():
            return {}
        try:
            resp = self._signed_account_get()
            if resp.status_code != 200:
                logger.warning("Binance account_overview HTTP %s", resp.status_code)
                return {}
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance account_overview failed: %s", exc)
            return {}

        balances: list[dict] = []
        for item in payload.get("balances", ()) or ():
            try:
                free = float(item.get("free", 0) or 0)
                locked = float(item.get("locked", 0) or 0)
            except (TypeError, ValueError):
                continue
            if free == 0.0 and locked == 0.0:
                continue
            balances.append(
                {
                    "asset": str(item.get("asset", "")),
                    "free": free,
                    "locked": locked,
                    "total": free + locked,
                }
            )
        return {
            "accountType": payload.get("accountType", ""),
            "canTrade": payload.get("canTrade"),
            "canWithdraw": payload.get("canWithdraw"),
            "canDeposit": payload.get("canDeposit"),
            "balances": balances,
        }
