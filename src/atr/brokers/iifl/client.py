"""Thin, typed REST client for the IIFL Capital Open API (v1).

Endpoint reference: https://developers.iiflcapital.com/apidocs
Base URL: https://api.iiflcapital.com/v1

Every call is authenticated with ``Authorization: Bearer <userSession>``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from atr.brokers.iifl.auth import Session, SessionStore, build_checksum

BASE_URL = "https://api.iiflcapital.com/v1"

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
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.session: Session | None = None
        self._store = session_store or SessionStore()
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

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
    @retry(
        retry=retry_if_exception_type((httpx.TransportError, IiflApiError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        if auth and self.session is None:
            raise IiflApiError("no active session — call create_session() or restore_session()")

        response = self._http.request(method, path, json=json, params=params)
        if response.status_code >= 500:
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

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        return self.request("GET", "/profile")

    def limits(self) -> dict[str, Any]:
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
        return self.request("GET", "/positions")

    def holdings(self) -> list[dict[str, Any]]:
        return self.request("GET", "/holdings")

    # ------------------------------------------------------------------
    # Margin
    # ------------------------------------------------------------------
    def span_exposure(self, legs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.request("POST", "/spanexposure", json=list(legs))

    def preorder_margin(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/preordermargin", json=order)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def historical_data(
        self,
        exchange: str,
        instrument_id: str,
        interval: str,
        from_date: datetime | str,
        to_date: datetime | str,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles.

        ``interval`` accepts either our short codes ("1m", "5m", "1d") or the
        literal API values ("1 minute", "1 day").
        """
        interval = INTERVALS.get(interval, interval)
        payload = {
            "exchange": exchange,
            "instrumentId": str(instrument_id),
            "interval": interval,
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
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> IiflClient:  # pragma: no cover - convenience
        return self

    def __exit__(self, *exc: object) -> None:  # pragma: no cover
        self.close()


_DATE_FMT = "%d-%b-%Y"


def _fmt_date(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.strftime(_DATE_FMT)
    return value
