"""IIFL Capital execution adapter.

Translates our domain :class:`Order` into the IIFL order payload and their
response rows back into domain objects.

Order payload fields (per docs, Order Management):
exchange, instrumentId, transactionType, quantity, product, orderComplexity,
orderType, price, slTriggerPrice, slLegPrice, targetLegPrice, validity,
disclosedQuantity, marketProtectionPercent, apiOrderSource, algoId, orderTag
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from atr.brokers.base import Broker, BrokerError
from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.core.enums import AssetClass, OrderStatus, OrderType, Side
from atr.core.models import Fill, Instrument, Order, Position

# IIFL product codes
PRODUCT_INTRADAY = "INTRADAY"
PRODUCT_DELIVERY = "DELIVERY"
PRODUCT_NORMAL = "NORMAL"

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "SL",
    OrderType.STOP_LIMIT: "SL",
}

_STATUS_MAP = {
    "OPEN": OrderStatus.SUBMITTED,
    "PENDING": OrderStatus.PENDING,
    "PARTIALLY FILLED": OrderStatus.PARTIALLY_FILLED,
    "PARTIALLYFILLED": OrderStatus.PARTIALLY_FILLED,
    "COMPLETE": OrderStatus.FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


def _pick(row: dict, *keys: str, default=None):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


class IiflBroker(Broker):
    def __init__(
        self,
        client: IiflClient,
        master: InstrumentMaster,
        *,
        default_product: str = PRODUCT_INTRADAY,
        api_order_source: str = "atr",
        algo_id: str | None = None,
    ) -> None:
        self.client = client
        self.master = master
        self.default_product = default_product
        self.api_order_source = api_order_source
        self.algo_id = algo_id

    # ------------------------------------------------------------------
    def build_payload(self, order: Order) -> dict[str, Any]:
        inst = order.instrument
        if inst.conid is None:
            raise BrokerError(f"instrument {inst.symbol} has no IIFL instrumentId")
        params = order.broker_params

        product = params.get("product")
        if product is None:
            product = (
                PRODUCT_INTRADAY if inst.is_derivative else self.default_product
            )

        payload: dict[str, Any] = {
            "exchange": inst.exchange.upper(),
            "instrumentId": str(inst.conid),
            "transactionType": "BUY" if order.side is Side.BUY else "SELL",
            "quantity": int(order.quantity),
            "product": str(product).upper(),
            "orderComplexity": str(params.get("orderComplexity", "REGULAR")).upper(),
            "orderType": _ORDER_TYPE_MAP[order.order_type],
            "validity": str(params.get("validity", "DAY")).upper(),
        }
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limit_price:
            payload["price"] = round(order.limit_price, 2)
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stop_price:
            payload["slTriggerPrice"] = round(order.stop_price, 2)
        if params.get("slLegPrice") is not None:
            payload["slLegPrice"] = params["slLegPrice"]
        if params.get("targetLegPrice") is not None:
            payload["targetLegPrice"] = params["targetLegPrice"]
        if params.get("disclosedQuantity"):
            payload["disclosedQuantity"] = int(params["disclosedQuantity"])
        if params.get("marketProtectionPercent"):
            payload["marketProtectionPercent"] = float(params["marketProtectionPercent"])

        payload["apiOrderSource"] = params.get("apiOrderSource", self.api_order_source)
        if self.algo_id or params.get("algoId"):
            payload["algoId"] = params.get("algoId", self.algo_id)
        if order.tag:
            payload["orderTag"] = order.tag[:40]
        return payload

    # ------------------------------------------------------------------
    def place_order(self, order: Order) -> Order:
        payload = self.build_payload(order)
        logger.debug("placing IIFL order: {}", payload)
        result = self.client.place_orders([payload])
        if not result:
            raise BrokerError(f"empty response placing order for {order.instrument.symbol}")
        row = result[0] if isinstance(result, list) else result

        broker_id = str(_pick(row, "brokerOrderId", "BrokerOrderId", "orderId", default=""))
        if not broker_id:
            order.mark(OrderStatus.REJECTED, reason=str(_pick(row, "message", "Message")))
            raise BrokerError(f"order rejected: {row}")
        order.broker_order_id = broker_id
        order.status = OrderStatus.SUBMITTED
        order.tag = order.tag or str(_pick(row, "orderTag", default="") or "")
        return order

    def modify_order(self, broker_order_id: str, **fields: Any) -> Order:
        payload = {k: v for k, v in fields.items() if v is not None}
        result = self.client.modify_order(broker_order_id, **payload)
        return self._order_from_row(result, self._instrument_for(result))

    def cancel_order(self, broker_order_id: str) -> bool:
        result = self.client.cancel_order(broker_order_id)
        status = str(_pick(result, "status", "Status", default="")).upper()
        return status in ("OK", "SUCCESS", "0") or result is not None

    # ------------------------------------------------------------------
    def open_orders(self) -> list[Order]:
        return [
            self._order_from_row(row, self._instrument_for(row))
            for row in self.client.order_book()
        ]

    def positions(self) -> list[Position]:
        out = []
        for row in self.client.positions():
            instrument = self._instrument_for(row)
            qty = float(
                _pick(row, "netQuantity", "NetQuantity", "quantity", "Quantity", default=0) or 0
            )
            if qty == 0:
                continue
            avg = float(_pick(row, "averagePrice", "AveragePrice", "avgPrice", default=0) or 0)
            ltp = float(_pick(row, "ltp", "LTP", "lastTradedPrice", default=0) or 0)
            out.append(
                Position(
                    instrument=instrument,
                    quantity=qty,
                    avg_price=avg,
                    last_price=ltp,
                    realized_pnl=float(
                        _pick(row, "realizedPnl", "realisedPnl", "pnl", default=0) or 0
                    ),
                )
            )
        return out

    def last_price(self, instruments: list[Instrument]) -> dict[str, float]:
        legs = [(i.exchange.upper(), str(i.conid)) for i in instruments]
        quotes = self.client.market_quotes(legs)
        out: dict[str, float] = {}
        for instrument, row in zip(instruments, quotes, strict=False):
            ltp = _pick(row, "ltp", "LTP", "lastTradedPrice", "lastPrice")
            if ltp is not None:
                out[instrument.symbol] = float(ltp)
        return out

    # ------------------------------------------------------------------
    def margin_required(self, order: Order) -> dict[str, Any]:
        payload = self.build_payload(order)
        return self.client.preorder_margin(payload)

    # ------------------------------------------------------------------
    def _instrument_for(self, row: dict) -> Instrument:
        """Resolve the instrument for a broker row, falling back to a synthetic
        one built from the row itself when the contract isn't in the master."""
        symbol = str(_pick(row, "tradingSymbol", "TradingSymbol", "symbol", default="?"))
        exchange = str(_pick(row, "exchange", "Exchange", default="NSEEQ")).upper()
        instrument_id = _pick(row, "instrumentId", "InstrumentId")
        try:
            return self.master.find(symbol, exchange)
        except Exception:  # noqa: BLE001
            return Instrument(
                symbol=symbol.upper(),
                exchange=exchange,
                conid=int(instrument_id) if instrument_id else None,
                asset_class=AssetClass.FUTURE if exchange.endswith("FO") else AssetClass.EQUITY,
                currency="INR",
            )

    @staticmethod
    def _order_from_row(row: dict, instrument: Instrument) -> Order:
        side_text = str(_pick(row, "transactionType", "TransactionType", default="BUY")).upper()
        status_text = str(_pick(row, "orderStatus", "OrderStatus", default="OPEN")).upper()
        qty = float(_pick(row, "quantity", "Quantity", default=0) or 0)
        filled = float(_pick(row, "filledQuantity", "FilledQuantity", default=0) or 0)
        avg = float(_pick(row, "averageTradedPrice", "AverageTradedPrice", default=0) or 0)

        return Order(
            instrument=instrument,
            side=Side.BUY if side_text.startswith("B") else Side.SELL,
            quantity=qty or 1,
            status=_STATUS_MAP.get(status_text, OrderStatus.SUBMITTED),
            filled_quantity=filled,
            avg_fill_price=avg,
            broker_order_id=str(_pick(row, "brokerOrderId", "BrokerOrderId", default="") or ""),
            tag=str(_pick(row, "orderTag", "OrderTag", default="") or "") or None,
        )

    def fills_from_trades(self) -> list[Fill]:
        """Reconstruct :class:`Fill` objects from /trades (used to reconcile on
        startup or after a reconnect)."""
        fills: list[Fill] = []
        for row in self.client.trades():
            instrument = self._instrument_for(row)
            qty = float(_pick(row, "filledQuantity", "FilledQuantity", "quantity", default=0) or 0)
            price = float(_pick(row, "tradedPrice", "TradedPrice", "averageTradedPrice", default=0) or 0)
            if not qty or not price:
                continue
            side_text = str(_pick(row, "transactionType", default="BUY")).upper()
            fills.append(
                Fill(
                    order_id=str(_pick(row, "brokerOrderId", default="")),
                    instrument=instrument,
                    side=Side.BUY if side_text.startswith("B") else Side.SELL,
                    quantity=qty,
                    price=price,
                    ts=_parse_ts(_pick(row, "fillTimestamp", "exchangeTimestamp", "fillTime")),
                )
            )
        return fills


def _parse_ts(value) -> Any:
    from datetime import datetime

    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return datetime.now()
