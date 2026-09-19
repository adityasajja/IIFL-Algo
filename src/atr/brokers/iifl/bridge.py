"""Realtime market data over the IIFL Bridge (MQTT over TLS, port 8883).

Host: bridge.iiflcapital.com
Auth: the ``userSession`` JWT; the MQTT client id is the token's
``preferred_username`` claim, which is also the topic for order/trade updates.
The broker is **not** anonymous: it expects username = ``preferred_username``
and password = ``"OPENID~~" + <raw token> + "~"``, over MQTT 3.1.1 with a
20-second keepalive. Verified against IIFL's official ``BridgePy`` connector.

Topic prefixes (from the official bridge implementation)::

    prod/marketfeed/mw/v1/            market feed (prices, volume, depth)
    prod/marketfeed/index/v1/         index feed
    prod/marketfeed/oi/v1/            open interest
    prod/marketfeed/marketstatus/v1/  exchange status
    prod/marketfeed/lpp/v1/           limit price protection band
    prod/marketfeed/uppercircuit/v1/  upper circuit
    prod/marketfeed/lowercircuit/v1/  lower circuit
    prod/marketfeed/high52week/v1/    52-week high
    prod/marketfeed/low52week/v1/     52-week low
    prod/updates/order/v1/            order updates (JSON)
    prod/updates/trade/v1/            trade updates (JSON)

Limits: 6000 subscriptions per client, max 1024 topics per request.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from loguru import logger

from atr.brokers.iifl.auth import Session
from atr.config.settings import get_settings
from atr.brokers.iifl.codec import (
    decode_circuit,
    decode_lpp,
    decode_market_feed,
    decode_market_status,
    decode_open_interest,
)

HOST = "bridge.iiflcapital.com"
PORT = 8883

TOPIC_MW = "prod/marketfeed/mw/v1/"
TOPIC_INDEX = "prod/marketfeed/index/v1/"
TOPIC_OI = "prod/marketfeed/oi/v1/"
TOPIC_MARKET_STATUS = "prod/marketfeed/marketstatus/v1/"
TOPIC_LPP = "prod/marketfeed/lpp/v1/"
TOPIC_UPPER_CIRCUIT = "prod/marketfeed/uppercircuit/v1/"
TOPIC_LOWER_CIRCUIT = "prod/marketfeed/lowercircuit/v1/"
TOPIC_HIGH_52W = "prod/marketfeed/high52week/v1/"
TOPIC_LOW_52W = "prod/marketfeed/low52week/v1/"
TOPIC_ORDER = "prod/updates/order/v1/"
TOPIC_TRADE = "prod/updates/trade/v1/"

_TOPIC_RE = re.compile(r"v1/")


class BridgeClient:
    """Thin wrapper around paho-mqtt with typed callbacks.

    Callbacks are optional; register the ones you care about::

        bridge.on_feed = lambda topic, feed: ...
        bridge.on_order_update = lambda payload: ...
    """

    def __init__(self, session: Session, client_id: str | None = None) -> None:
        self.session = session
        self.client_id = client_id or session.preferred_username()

        self.on_feed: Callable[[str, Any], None] | None = None
        self.on_index: Callable[[str, Any], None] | None = None
        self.on_open_interest: Callable[[str, dict], None] | None = None
        self.on_market_status: Callable[[str, dict], None] | None = None
        self.on_lpp: Callable[[str, dict], None] | None = None
        self.on_circuit: Callable[[str, dict], None] | None = None
        self.on_week52: Callable[[str, dict], None] | None = None
        self.on_order_update: Callable[[dict], None] | None = None
        self.on_trade_update: Callable[[dict], None] | None = None
        self.on_ack: Callable[[dict], None] | None = None
        self.on_error: Callable[[int, str], None] | None = None

        # MQTT 3.1.1, matching the official connector — it sets clean_session,
        # which only exists in v3, and the bridge rejects v5 clients.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        # Authenticate with the session JWT. Connecting anonymously (as this
        # previously did) never succeeds against the live bridge.
        self._client.username_pw_set(
            username=self.client_id,
            password=f"OPENID~~{session.user_session}~",
        )
        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message
        self._client.on_disconnect = self._handle_disconnect
        # The bridge presents a certificate the official SDK also bypasses.
        self._client.tls_set(tls_version=ssl.PROTOCOL_TLSv1_2)
        if not get_settings().bridge_tls_verify:
            self._client.tls_insecure_set(True)
        self._connected = threading.Event()
        self._subscribed: set[str] = set()

    # ------------------------------------------------------------------
    def connect(self, timeout: float = 15.0) -> None:
        self._client.connect(HOST, PORT, keepalive=20)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            raise TimeoutError("timed out waiting for bridge CONNACK")

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected()

    # ------------------------------------------------------------------
    def _subscribe(self, prefix: str, topics: list[str]) -> None:
        if not topics:
            return
        self._subscribe_raw([f"{prefix}{t}" for t in topics])

    def _subscribe_raw(self, full_topics: list[str]) -> None:
        if len(full_topics) > 1024:
            raise ValueError("max 1024 topics per subscription request")
        self._client.subscribe([(t, 0) for t in full_topics])
        self._subscribed.update(full_topics)

    def _unsubscribe_raw(self, full_topics: list[str]) -> None:
        self._client.unsubscribe(full_topics)
        self._subscribed.difference_update(full_topics)

    # Public subscription helpers; topics look like "nseeq/2885".
    def subscribe_feed(self, topics: list[str]) -> None:
        self._subscribe(TOPIC_MW, topics)

    def unsubscribe_feed(self, topics: list[str]) -> None:
        self._unsubscribe_raw([f"{TOPIC_MW}{t}" for t in topics])

    def subscribe_index(self, topics: list[str]) -> None:
        self._subscribe(TOPIC_INDEX, topics)

    def subscribe_open_interest(self, topics: list[str]) -> None:
        self._subscribe(TOPIC_OI, topics)

    def subscribe_market_status(self, exchanges: list[str]) -> None:
        self._subscribe(TOPIC_MARKET_STATUS, exchanges)

    def subscribe_lpp(self, topics: list[str]) -> None:
        self._subscribe(TOPIC_LPP, topics)

    def subscribe_circuits(self, exchanges: list[str]) -> None:
        self._subscribe(TOPIC_UPPER_CIRCUIT, exchanges)
        self._subscribe(TOPIC_LOWER_CIRCUIT, exchanges)

    def subscribe_52week(self, exchanges: list[str]) -> None:
        self._subscribe(TOPIC_HIGH_52W, exchanges)
        self._subscribe(TOPIC_LOW_52W, exchanges)

    def subscribe_order_updates(self) -> None:
        self._subscribe(TOPIC_ORDER, [self.client_id])

    def subscribe_trade_updates(self) -> None:
        self._subscribe(TOPIC_TRADE, [self.client_id])

    # ------------------------------------------------------------------
    def _handle_connect(self, client, userdata, flags, rc, properties=None) -> None:
        # paho 2.x passes a ReasonCode here, not an int. It compares equal to 0
        # but cannot be cast, and raising inside this callback kills the network
        # thread outright — the client then reports "connected" while receiving
        # nothing at all.
        status = int(getattr(rc, "value", rc))
        if status == 0:
            self._connected.set()
            # Ultra-low latency optimization: Disable Nagle's algorithm (TCP_NODELAY)
            # on the underlying network socket to avoid buffered packet dispatch delays.
            try:
                import socket
                sock = client.socket()
                if sock is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    logger.debug("Bridge TCP_NODELAY enabled on MQTT socket")
            except Exception as e:
                logger.debug("Could not set TCP_NODELAY on bridge socket: {}", e)

            logger.info("bridge connected as {}", self.client_id)
            # Re-subscribe all previously requested topics on reconnect
            if self._subscribed:
                try:
                    self._client.subscribe([(t, 0) for t in self._subscribed])
                    logger.info("Bridge re-subscribed {} topics after connect", len(self._subscribed))
                except Exception as e:
                    logger.warning("Could not re-subscribe topics on bridge connect: {}", e)
        else:
            logger.error("bridge connect failed: {} (code {})", rc, status)
        if self.on_ack:
            self.on_ack({"packetType": 2, "packetName": "CONNACK", "status": status})

    def _handle_disconnect(self, client, userdata, flags, rc=0, properties=None) -> None:
        self._connected.clear()
        logger.warning("bridge disconnected (rc={})", rc)

    def _handle_message(self, client, userdata, message) -> None:
        topic = message.topic
        payload = message.payload
        try:
            if topic.startswith(TOPIC_MW):
                if self.on_feed:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    self.on_feed(suffix, decode_market_feed(payload))
            elif topic.startswith(TOPIC_INDEX):
                if self.on_index:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    self.on_index(suffix, decode_market_feed(payload))
            elif topic.startswith(TOPIC_OI):
                if self.on_open_interest:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    self.on_open_interest(suffix, decode_open_interest(payload))
            elif topic.startswith(TOPIC_MARKET_STATUS):
                if self.on_market_status:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    self.on_market_status(suffix, decode_market_status(payload))
            elif topic.startswith(TOPIC_LPP):
                if self.on_lpp:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    self.on_lpp(suffix, decode_lpp(payload))
            elif topic.startswith((TOPIC_UPPER_CIRCUIT, TOPIC_LOWER_CIRCUIT)):
                if self.on_circuit:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    key = "upperCircuit" if topic.startswith(TOPIC_UPPER_CIRCUIT) else "lowerCircuit"
                    self.on_circuit(suffix, decode_circuit(payload, key))
            elif topic.startswith((TOPIC_HIGH_52W, TOPIC_LOW_52W)):
                if self.on_week52:
                    _, suffix = _TOPIC_RE.split(topic, 1)
                    key = "52WeekHigh" if topic.startswith(TOPIC_HIGH_52W) else "52WeekLow"
                    self.on_week52(suffix, decode_circuit(payload, key))
            elif topic.startswith(TOPIC_ORDER) and self.on_order_update:
                self.on_order_update(json.loads(payload.decode("utf-8")))
            elif topic.startswith(TOPIC_TRADE):
                if self.on_trade_update:
                    self.on_trade_update(json.loads(payload.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - never let a bad frame kill the loop
            logger.exception("failed to handle bridge message on {}: {}", topic, exc)
            if self.on_error:
                self.on_error(-1, f"{topic}: {exc}")
