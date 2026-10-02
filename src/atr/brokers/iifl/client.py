"""Thin, typed REST client for the IIFL Capital Open API (v1).

Endpoint reference: https://developers.iiflcapital.com/apidocs
Base URL: https://api.iiflcapital.com/v1

Every call is authenticated with ``Authorization: Bearer <userSession>``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from atr.brokers.iifl.auth import Session, SessionStore, build_checksum
from atr.brokers.iifl.ratelimit import RateLimits, SharedWindow

BASE_URL = "https://api.iiflcapital.com/v1"
MARKETS_API_URL = "https://marketsapi.iiflcapital.com"

# Exchange segments accepted by the API.
EXCHANGES = (
    "NSEEQ",
    "BSEEQ",
    "NSEFO",
    "BSEFO",
    "NSECURR",
    "BSECURR",
    "NSECOMM",
    "BSECOMM",
    "MCXCOMM",
    "NCDEXCOMM",
    "INDICES",
)

# Candlestick intervals accepted by /marketdata/historicaldata.
INTERVALS = {
    "1m": "1 minute",
    "1min": "1 minute",
    "5m": "5 minutes",
    "10m": "10 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "60m": "60 minutes",
    "1d": "1 day",
    "1w": "weekly",
    "1mo": "monthly",
}


class IiflApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SessionExpiredError(IiflApiError):
    """The day's JWT is dead (midnight IST) or the broker rejected it (401).

    Raised instead of a bare 401 so callers stop cleanly and ask for a fresh
    login rather than hammering an endpoint that will never answer.
    """


class _NoRetry(IiflApiError):
    """A failure that must not be retried (mutation with unknown outcome)."""


def _is_retryable(exc: Exception) -> bool:
    """Only retry genuine transport failures, 5xx server errors and 429s.

    Other 4xx responses are client errors (bad request, unauthorized,
    validation failures) — retrying them just amplifies load on the broker
    API. Session expiry and explicit no-retry markers never retry.
    """
    if isinstance(exc, (SessionExpiredError, _NoRetry)):
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, IiflApiError):
        # 429 means we went over a rate limit despite waiting (the docs give no penalty and the
        # limits may be tighter than published): back off and try again rather than fail.
        return exc.status_code is not None and (exc.status_code >= 500 or exc.status_code == 429)
    return False


def _limits_for(store: SessionStore) -> RateLimits:
    """The rate limiter, sharing its counts through a file next to the session file."""
    try:
        return RateLimits(SharedWindow(Path(store.path).parent / "iifl_ratelimit.sqlite"))
    except Exception:  # noqa: BLE001 - no usable file: count in this process instead
        return RateLimits()


def _transport(force_ipv4: bool) -> httpx.HTTPTransport | None:
    """Pin egress to IPv4 when requested.

    IIFL whitelists a single IPv4 address per app. ``api.iiflcapital.com``
    publishes both A and AAAA records (Akamai), so on a dual-stack connection
    the request leaves over IPv6 — which is not whitelisted. Every gated
    endpoint then returns ``EC500 IP address not authorized for trading`` even
    though the registered IP is correct, because the registered address is
    never actually the one being used.

    Binding the local socket to the IPv4 wildcard forces the source address to
    be the one that was registered.
    """
    if not force_ipv4:
        return None
    return httpx.HTTPTransport(local_address="0.0.0.0")


class IiflClient:
    """HTTP transport + session lifecycle.

    Usage::

        client = IiflClient(app_key="...", app_secret="...")
        session = client.create_session(client_id="TEST101", auth_code="ABC...")
        client.set_session(session)          # or load a cached one
        client.place_order({...})
    """

    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        session_store: SessionStore | None = None,
        force_ipv4: bool = True,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.session: Session | None = None
        self._store = session_store or SessionStore()
        # IIFL's documented per-endpoint limits (see ratelimit.py), counted in a file beside the
        # session so every process and client using the session shares one allowance.
        self._limits = _limits_for(self._store)
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=50,
            keepalive_expiry=60.0,
        )
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=_transport(force_ipv4),
            limits=limits,
            http2=True,
        )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def create_session(self, client_id: str, auth_code: str) -> Session:
        if not self.app_secret:
            raise IiflApiError("app_secret is required to create a session")
        checksum = build_checksum(client_id, auth_code, self.app_secret)
        data = self.request("POST", "/getusersession", json={"checkSum": checksum}, auth=False)
        if data.get("status") != "Ok" or not data.get("userSession"):
            raise IiflApiError(f"getusersession failed: {data}", payload=data)
        session = Session(
            user_session=data["userSession"],
            client_id=client_id,
            created_at=datetime.now().astimezone(),
        )
        self.set_session(session)
        self._store.save(session)
        logger.info("IIFL session created for client {}", client_id)
        return session

    def set_session(self, session: Session) -> None:
        self.session = session
        self._http.headers.update({"Authorization": f"Bearer {session.user_session}"})

    def restore_session(self) -> Session | None:
        session = self._store.load()
        if session:
            self.set_session(session)
        return session

    def logout(self) -> None:
        try:
            self.request("POST", "/profile/logout")
        finally:
            self._store.clear()
            self._http.headers.pop("Authorization", None)
            self.session = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        if auth:
            if self.session is None:
                raise IiflApiError("no active session — call create_session() or restore_session()")
            if not self.session.is_valid():
                raise SessionExpiredError(
                    "session expired at midnight IST — log in again", status_code=401
                )
        # Mutations retry on 429 only. A 5xx or transport failure after a
        # POST/PUT/DELETE may mean the order was placed; a blind retry
        # places it twice with no dedup token to save us.
        retry_429_only = method.upper() in ("POST", "PUT", "DELETE", "PATCH")
        try:
            return self._send(method, path, json=json, params=params, retry_429_only=retry_429_only)
        except SessionExpiredError:
            self._drop_session()
            raise

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        retry_429_only: bool = False,
    ) -> Any:
        self._limits.wait(method, path, scope=self.session.client_id if self.session else "")
        try:
            response = self._http.request(method, path, json=json, params=params)
        except Exception as exc:
            if retry_429_only:
                raise _NoRetry(
                    f"{method} {path} transport failure — not retried (unknown outcome)"
                ) from exc
            raise
        if response.status_code == 401:
            raise SessionExpiredError(
                f"{method} {path} -> 401 (session rejected)",
                status_code=401,
                payload=response.text,
            )
        if response.status_code >= 500:
            if retry_429_only:
                raise _NoRetry(
                    f"{method} {path} -> {response.status_code} — not retried (unknown outcome)",
                    status_code=response.status_code,
                    payload=response.text,
                )
            raise IiflApiError(
                f"{method} {path} -> {response.status_code}",
                status_code=response.status_code,
                payload=response.text,
            )
        try:
            body = response.json()
        except ValueError:
            body = response.text

        # IIFL signals business failures with HTTP 200 + status field.
        if response.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "Not_Ok"):
            raise IiflApiError(
                f"{method} {path} -> {body}",
                status_code=response.status_code,
                payload=body,
            )
        return body

    def _drop_session(self) -> None:
        """Forget a dead session locally so nothing keeps sending its JWT."""
        try:
            self._store.clear()
        except Exception:  # noqa: BLE001 - cleanup must not raise
            pass
        try:
            self._http.headers.pop("Authorization", None)
        except Exception:  # noqa: BLE001
            pass
        self.session = None

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        return self.request("GET", "/profile")

    def limits(self) -> dict[str, Any]:
        """Trading limits, cash and collateral margins.

        Tries IIFL Markets API (v4) first for granular collateral and cash breakdown
        matching the web portal. Falls back to v1 /limits if unavailable.
        """
        if self.session and self.session.user_session and "iiflcapital.com" in str(self._http.base_url):
            try:
                headers = {
                    "Origin": "https://markets.iiflcapital.com",
                    "Referer": "https://markets.iiflcapital.com/",
                }
                res = self._http.post(
                    f"{MARKETS_API_URL}/v4/limits",
                    headers=headers,
                    json={},
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict) and data.get("status") == "Ok" and "result" in data:
                        # Normalize v4 result list into standard dictionary representation if needed
                        result = data["result"]
                        if isinstance(result, list) and len(result) > 0:
                            item = dict(result[0])
                            # Provide backwards-compatible keys so both v1 & v4 readers work seamlessly
                            item.setdefault("collateralMargin", item.get("collateralAvailable", 0.0))
                            item.setdefault("utilizedMargin", item.get("utilisedTotalMargin", 0.0))
                            item.setdefault("utilizedSpanMargin", item.get("utilisedSpanMargin", 0.0))
                            item.setdefault("utilizedExposureMargin", item.get("utilisedExposureMargin", 0.0))
                            data["result"] = item
                        return data
            except Exception as exc:  # noqa: BLE001
                logger.debug("v4 limits lookup failed, falling back to v1: {}", exc)

        return self.request("GET", "/limits")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def place_orders(self, orders: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = list(orders)
        return self.request("POST", "/orders", json=payload)

    def modify_order(self, broker_order_id: str, **fields: Any) -> dict[str, Any]:
        return self.request("PUT", f"/orders/{broker_order_id}", json=fields)

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/orders/{broker_order_id}")

    def order_book(self) -> list[dict[str, Any]]:
        return self.request("GET", "/orders")

    def order_status(self, broker_order_id: str) -> dict[str, Any]:
        return self.request("GET", f"/orders/{broker_order_id}")

    def trades(self) -> list[dict[str, Any]]:
        return self.request("GET", "/trades")

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    def positions(self) -> list[dict[str, Any]]:
        """Intraday open positions.

        Tries IIFL Markets API (v4) first. Falls back to v1 /positions.
        """
        if self.session and self.session.user_session and "iiflcapital.com" in str(self._http.base_url):
            try:
                headers = {
                    "Origin": "https://markets.iiflcapital.com",
                    "Referer": "https://markets.iiflcapital.com/",
                }
                res = self._http.post(
                    f"{MARKETS_API_URL}/v4/positions",
                    headers=headers,
                    json={},
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict) and data.get("status") == "Ok" and "result" in data:
                        result = data["result"]
                        return result if isinstance(result, list) else []
            except Exception as exc:  # noqa: BLE001
                logger.debug("v4 positions lookup failed, falling back to v1: {}", exc)

        return self.request("GET", "/positions")

    def holdings(self) -> dict[str, Any] | list[dict[str, Any]]:
        """Settled holdings across all asset classes (Equity, ETFs, Collateral).
        
        Tries IIFL's comprehensive Markets API (v4) first, which matches the broker portal
        and returns all 37+ depository instruments including ETFs and pledged shares.
        Falls back to the legacy v1 Open API if unavailable.
        """
        if self.session and self.session.user_session and "iiflcapital.com" in str(self._http.base_url):
            try:
                headers = {
                    "Origin": "https://markets.iiflcapital.com",
                    "Referer": "https://markets.iiflcapital.com/",
                }
                res = self._http.post(
                    f"{MARKETS_API_URL}/v4/holdings",
                    headers=headers,
                    json={},
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict) and data.get("status") == "Ok" and "result" in data:
                        result = data["result"]
                        # A list of rows, or a dict grouped by asset class —
                        # the broker flattens both. Anything else is unusable.
                        return result if isinstance(result, (list, dict)) else []
            except Exception as exc:  # noqa: BLE001
                logger.debug("v4 holdings lookup failed, falling back to v1: {}", exc)

        return self.request("GET", "/holdings")

    # ------------------------------------------------------------------
    # Margin
    # ------------------------------------------------------------------
    def span_exposure(self, legs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.request("POST", "/spanexposure", json=list(legs))

    def preorder_margin(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/preordermargin", json=order)

    def historical_data(
        self,
        exchange: str,
        instrument_id: str,
        interval: str,
        from_date: datetime | str,
        to_date: datetime | str,
        *,
        auto_chunk: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles.

        ``interval`` accepts either our short codes ("1m", "5m", "1d") or the
        literal API values ("1 minute", "1 day").
        If ``auto_chunk`` is True and the requested window exceeds IIFL's recommended
        single-request range for intraday bars (e.g. 60 days for 1m/5m), the request
        is automatically chunked across consecutive windows and concatenated.
        """
        api_interval = INTERVALS.get(interval, interval)
        if auto_chunk and api_interval in ("1 minute", "5 minutes", "10 minutes", "15 minutes"):
            try:
                start_dt = _parse_date(from_date)
                end_dt = _parse_date(to_date)
                # IIFL brokers usually truncate or reject requests > 60 days of 1m/5m data
                max_window_days = 45 if api_interval == "1 minute" else 90
                if (end_dt - start_dt).days > max_window_days:
                    combined_candles: list[Any] = []
                    curr_start = start_dt
                    while curr_start < end_dt:
                        curr_end = min(curr_start + timedelta(days=max_window_days), end_dt)
                        payload = {
                            "exchange": exchange,
                            "instrumentId": str(instrument_id),
                            "interval": api_interval,
                            "fromDate": _fmt_date(curr_start),
                            "toDate": _fmt_date(curr_end),
                        }
                        sub_res = self.request("POST", "/marketdata/historicaldata", json=payload)
                        candles = sub_res.get("result", {}).get("candles", []) if isinstance(sub_res, dict) else []
                        combined_candles.extend(candles)
                        curr_start = curr_end + timedelta(days=1)
                    return {"status": "Ok", "result": [{"candles": combined_candles}]}
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto_chunk historical_data failed, performing standard request: {}", exc)

        payload = {
            "exchange": exchange,
            "instrumentId": str(instrument_id),
            "interval": api_interval,
            "fromDate": _fmt_date(from_date),
            "toDate": _fmt_date(to_date),
        }
        return self.request("POST", "/marketdata/historicaldata", json=payload)

    def market_quotes(self, legs: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        payload = [{"exchange": ex, "instrumentId": str(iid)} for ex, iid in legs]
        return self.request("POST", "/marketdata/marketquotes", json=payload)

    def market_depth(self, exchange: str, instrument_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/marketdata/marketdepth",
            json={"exchange": exchange, "instrumentId": str(instrument_id)},
        )

    def open_interest(self, exchange: str, instrument_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/marketdata/openinterest",
            json={"exchange": exchange, "instrumentId": str(instrument_id)},
        )

    # ------------------------------------------------------------------
    # Instrument master
    # ------------------------------------------------------------------
    def contract_file(self, exchange: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/contractfiles/{exchange.upper()}.json")

    # ------------------------------------------------------------------
    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> IiflClient:  # pragma: no cover - convenience
        return self

    def __exit__(self, *exc: object) -> None:  # pragma: no cover
        self.close()


_DATE_FMT = "%d-%b-%Y"


def _parse_date(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(str(value).strip())


def _fmt_date(value: datetime | date | str) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime(_DATE_FMT)
    try:
        return _parse_date(value).strftime(_DATE_FMT)
    except Exception:
        return str(value)
