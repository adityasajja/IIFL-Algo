"""Broker access: one place that constructs a broker, and one place that gates live use.

Why this exists
---------------
The construction of an authenticated IIFL client lived in ``atr/api/main.py``, which
meant anything else that needed a broker — the reconciler, a deployment runner — had
to reach into the transport layer for it, and a route is not allowed to touch a
broker directly. So the construction moves here, where both the routes and the
services can use it, and the *paper/live* decision stays with it rather than being
re-made at each call site.

The gate is deliberately two separate things:

* :func:`authed_client` restores a session and works in any environment. Reading
  positions, holdings, funds and orders is read-only and needs no mode.
* :func:`live_broker` is the one that can *transmit*, so it refuses in ``dev`` and
  it is the only place a caller gets a broker that can place an order.

Conflating the two is how a read-only diagnostic ends up needing live mode, or worse,
how a write path ends up with a client nobody gated.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("atr.services.broker_access")


class BrokerUnavailable(RuntimeError):
    """No usable broker: no session, or live access is disabled in this environment.

    Carries a ``status`` so the API can answer with the right code without having
    to interpret the message.
    """

    def __init__(self, message: str, *, status: int = 503, code: str = "broker_unavailable"):
        self.status = status
        self.code = code
        super().__init__(message)


def authed_client() -> Any:
    """An IIFL client with a restored session. Works in any environment.

    Read-only endpoints use this: positions, holdings, funds and the order book do
    not change anything, so they do not need the paper/live gate.
    """
    from atr.brokers.iifl.client import IiflClient
    from atr.config.settings import get_settings

    settings = get_settings()
    client = IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
    )
    if client.restore_session() is None:
        raise BrokerUnavailable(
            "no active IIFL session — run `atr login`", status=401, code="no_broker_session"
        )
    return client


def live_broker() -> Any:
    """The broker that may place orders. Refuses in ``dev``.

    The single gate between a validated strategy and a real order at a real
    exchange, so it lives in exactly one place.
    """
    from atr.brokers.iifl.broker import IiflBroker
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.config.settings import get_settings

    settings = get_settings()
    if settings.env == "dev":
        raise BrokerUnavailable(
            "live endpoints disabled in dev — set ENV=paper|live",
            status=403,
            code="live_disabled_in_dev",
        )
    client = authed_client()
    return IiflBroker(client, InstrumentMaster(client))


def read_broker() -> Any:
    """A broker for reading positions, holdings and funds. Available in any env.

    Separate from :func:`live_broker` because reconciliation is a *diagnostic*: it
    must be runnable in dev, and gating it behind live mode would mean the control
    that detects problems is unavailable exactly when someone is investigating one.
    """
    from atr.brokers.iifl.broker import IiflBroker
    from atr.brokers.iifl.contracts import InstrumentMaster

    client = authed_client()
    return IiflBroker(client, InstrumentMaster(client))


__all__ = [
    "BrokerUnavailable",
    "authed_client",
    "live_broker",
    "read_broker",
]
