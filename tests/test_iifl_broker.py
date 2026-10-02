"""IIFL order-payload tests.

``build_payload`` is what actually reaches the exchange, so it is worth pinning
down without a live account. The SEBI-driven market-protection field in
particular: since 2026-04-01 an API market order with a zero or absent
market-protection value is rejected.
"""

from __future__ import annotations

import pytest

from atr.brokers.iifl.broker import IiflBroker
from atr.core.enums import OrderType, Side
from atr.core.models import Instrument, Order

EQ = Instrument(symbol="RELIANCE-EQ", exchange="NSEEQ", conid=2885)


def _broker(**kwargs) -> IiflBroker:
    return IiflBroker(client=None, master=None, **kwargs)


def _order(order_type=OrderType.MARKET, **kwargs) -> Order:
    return Order(instrument=EQ, side=Side.BUY, quantity=10, order_type=order_type, **kwargs)


def test_market_order_carries_a_non_zero_market_protection_value():
    payload = _broker().build_payload(_order())
    assert payload["marketProtectionPercent"] > 0


def test_market_protection_is_configurable():
    payload = _broker(market_protection_percent=2.5).build_payload(_order())
    assert payload["marketProtectionPercent"] == pytest.approx(2.5)


def test_explicit_broker_param_overrides_the_default():
    order = _order(broker_params={"marketProtectionPercent": 1.25})
    payload = _broker().build_payload(order)
    assert payload["marketProtectionPercent"] == pytest.approx(1.25)


def test_limit_order_does_not_get_market_protection():
    """Only market orders need the band."""
    payload = _broker().build_payload(_order(OrderType.LIMIT, limit_price=1250.0))
    assert "marketProtectionPercent" not in payload
    assert payload["price"] == pytest.approx(1250.0)


def test_core_fields_are_always_present():
    payload = _broker().build_payload(_order())
    for field in ("exchange", "instrumentId", "transactionType", "quantity", "product",
                  "orderComplexity", "orderType", "validity", "apiOrderSource"):
        assert field in payload, f"missing {field}"
    assert payload["exchange"] == "NSEEQ"
    assert payload["instrumentId"] == "2885"
    assert payload["transactionType"] == "BUY"
    assert payload["orderType"] == "MARKET"


def test_algo_id_only_sent_when_configured():
    assert "algoId" not in _broker().build_payload(_order())
    assert _broker(algo_id="ABC123").build_payload(_order())["algoId"] == "ABC123"


def test_stop_order_sends_trigger_price():
    payload = _broker().build_payload(_order(OrderType.STOP, stop_price=1200.0))
    assert payload["slTriggerPrice"] == pytest.approx(1200.0)
    assert payload["orderType"] == "SLM"


def test_stop_limit_order_sends_sl_type():
    payload = _broker().build_payload(
        _order(OrderType.STOP_LIMIT, stop_price=1200.0, limit_price=1205.0)
    )
    assert payload["slTriggerPrice"] == pytest.approx(1200.0)
    assert payload["price"] == pytest.approx(1205.0)
    assert payload["orderType"] == "SL"


def test_order_tag_is_truncated():
    payload = _broker().build_payload(_order(tag="x" * 100))
    assert len(payload["orderTag"]) == 40


def test_instrument_without_conid_is_rejected():
    from atr.brokers.base import BrokerError

    order = Order(instrument=Instrument(symbol="NOCONID"), side=Side.BUY, quantity=1)
    with pytest.raises(BrokerError, match="instrumentId"):
        _broker().build_payload(order)


def test_derivatives_default_to_intraday_product():
    fut = Instrument(symbol="NIFTY26OCTFUT", exchange="NSEFO", conid=48704, multiplier=75)
    order = Order(instrument=fut, side=Side.BUY, quantity=75, order_type=OrderType.MARKET)
    assert _broker().build_payload(order)["product"] == "INTRADAY"


def test_order_tif_flows_into_validity():
    from atr.core.enums import TimeInForce

    ioc = _order()
    ioc.tif = TimeInForce.IOC
    assert _broker().build_payload(ioc)["validity"] == "IOC"
    # An explicit broker param still wins (BO/CO legs need their own).
    day = _order(broker_params={"validity": "DAY"})
    day.tif = TimeInForce.IOC
    assert _broker().build_payload(day)["validity"] == "DAY"


def test_cancel_order_reports_failure_honestly():
    class _Client:
        def cancel_order(self, broker_order_id):
            return {"status": "Not_Ok", "message": "already filled"}

    assert _broker_with_client(_Client()).cancel_order("X1") is False


def test_corrupt_order_book_rows_are_skipped_not_invented():
    from types import SimpleNamespace

    class _Client:
        def order_book(self):
            return [
                {"transactionType": "BUY", "quantity": 10, "orderStatus": "OPEN"},
                {"transactionType": "SELL", "orderStatus": "OPEN"},  # no quantity
            ]

    master = SimpleNamespace(find=lambda symbol, exchange: EQ)
    broker = IiflBroker(client=_Client(), master=master)
    orders = broker.open_orders()
    assert len(orders) == 1
    assert orders[0].quantity == pytest.approx(10.0)


def test_v4_positions_envelope_is_unwrapped():
    from types import SimpleNamespace

    class _Client:
        def positions(self):
            return {"status": "Ok", "result": [
                {"tradingSymbol": "RELIANCE-EQ", "exchange": "NSEEQ",
                 "netQuantity": 5, "averagePrice": 2500.0, "ltp": 2510.0},
            ]}

    master = SimpleNamespace(find=lambda symbol, exchange: EQ)
    positions = IiflBroker(client=_Client(), master=master).positions()
    assert len(positions) == 1
    assert positions[0].quantity == pytest.approx(5.0)


def _broker_with_client(client) -> IiflBroker:
    return IiflBroker(client=client, master=None)


def test_expired_session_is_rejected_before_any_http(tmp_path):
    """A JWT past midnight IST must never be sent; log in again instead."""
    from datetime import UTC, datetime

    from atr.brokers.iifl.auth import Session, SessionStore
    from atr.brokers.iifl.client import IiflClient, SessionExpiredError

    store = SessionStore(tmp_path / "sess.json")
    client = IiflClient(app_key="k", app_secret="s", session_store=store, force_ipv4=False)
    client.session = Session(
        user_session="x.y.z", client_id="C",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    calls: list = []
    client._http.request = lambda *a, **k: calls.append((a, k))
    with pytest.raises(SessionExpiredError):
        client.request("GET", "/orders")
    assert calls == []


def test_a_401_drops_the_session_and_raises_expiry(tmp_path):
    """Keep sending a rejected token and every call 401s; forget it once."""
    from datetime import datetime

    from atr.brokers.iifl.auth import Session, SessionStore
    from atr.brokers.iifl.client import IiflClient, SessionExpiredError

    class _Resp:
        status_code = 401
        text = "unauthorized"

    store = SessionStore(tmp_path / "sess.json")
    client = IiflClient(app_key="k", app_secret="s", session_store=store, force_ipv4=False)
    client.session = Session(
        user_session="x.y.z", client_id="C", created_at=datetime.now().astimezone(),
    )
    client._http.request = lambda *a, **k: _Resp()
    with pytest.raises(SessionExpiredError):
        client.request("GET", "/orders")
    assert client.session is None


def test_mutations_do_not_retry_server_errors(tmp_path):
    """A 5xx after a POST may mean the order was placed: retrying doubles it."""
    from datetime import datetime

    from atr.brokers.iifl.auth import Session, SessionStore
    from atr.brokers.iifl.client import IiflClient, _NoRetry

    class _Resp:
        status_code = 500
        text = "bad gateway"

    store = SessionStore(tmp_path / "sess.json")
    client = IiflClient(app_key="k", app_secret="s", session_store=store, force_ipv4=False)
    client.session = Session(
        user_session="x.y.z", client_id="C", created_at=datetime.now().astimezone(),
    )
    calls: list = []
    client._http.request = lambda *a, **k: (calls.append((a, k)), _Resp())[1]
    with pytest.raises(_NoRetry):
        client.request("POST", "/orders", json=[])
    assert len(calls) == 1
