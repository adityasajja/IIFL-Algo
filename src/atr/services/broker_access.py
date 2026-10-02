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
import threading
from typing import Any

logger = logging.getLogger("atr.services.broker_access")

# One IiflClient (and its httpx.Client/connection pool) *per worker thread*,
# reused across that thread's requests. Read endpoints are polled every few
# seconds during market hours; building a fresh IiflClient per request meant a
# fresh TCP+TLS+HTTP2 handshake to IIFL on every single poll instead of reusing
# a keep-alive socket.
#
# A single client shared across *all* threads was tried first and reverted: an
# HTTP/2 stream reset (or any hard transport error) on one thread's request can
# make httpx close the whole connection pool out from under every other thread
# mid-request, which surfaced as concurrent requests failing with "the client
# has been closed" under real load. One client per thread keeps the handshake
# reuse — each thread in FastAPI's request threadpool settles on its own
# long-lived client — without any thread ever touching another's transport.
_local = threading.local()


def _build_client() -> Any:
    from atr.brokers.iifl.auth import SessionStore
    from atr.brokers.iifl.client import IiflClient
    from atr.config.settings import get_settings

    settings = get_settings()
    return IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
        session_store=SessionStore(settings.iifl_session_cache),
    )


def _get_shared_client() -> Any:
    """This thread's client, (re)built if it's missing or was closed out from under it."""
    client = getattr(_local, "client", None)
    if client is None or client.is_closed:
        client = _build_client()
        _local.client = client
    return client


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
    """This thread's IIFL client, with its session refreshed from disk. Works in
    any environment.

    Read-only endpoints use this: positions, holdings, funds and the order book do
    not change anything, so they do not need the paper/live gate. The client (and
    its underlying connection pool) is kept per-thread — see
    :func:`_get_shared_client` — so repeat calls from the same worker thread only
    re-read the small cached session file, not rebuild the HTTP transport.
    """
    client = _get_shared_client()
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
