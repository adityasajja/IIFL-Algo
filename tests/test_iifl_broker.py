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
