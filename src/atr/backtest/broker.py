"""Simulated broker: order book, fill logic, and realistic friction.

Two rules keep the backtest honest:
  * Market orders signalled on bar *i* fill at the open of bar *i+1*. Filling
    at the signal bar's close is look-ahead bias and inflates every result.
  * Limit orders fill at the *worse* of the limit price and the open, so a
    limit that gapped through still fills at the open, not at the limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from atr.backtest.costs import CommissionModel, SlippageModel
from atr.backtest.portfolio import Portfolio
from atr.core.enums import OrderStatus, OrderType, Side, TimeInForce
from atr.core.models import Bar, Fill, MarketSnapshot, Order


@dataclass
class SimulatedBroker:
    portfolio: Portfolio
    commission: CommissionModel = field(default_factory=CommissionModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)
    fill_on_next_open: bool = True
    #: Cap each fill at this fraction of bar volume (1.0 = no cap).
    participation_rate: float = 1.0

    submitted: list[Order] = field(default_factory=list, init=False)
    fills: list[Fill] = field(default_factory=list, init=False)
    _resting: list[Order] = field(default_factory=list, init=False)
    _queued_market: list[Order] = field(default_factory=list, init=False)
    _current_day: datetime.date | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    def submit(self, order: Order, now: datetime | None = None) -> Order:
        if order.status is OrderStatus.PENDING:
            order.status = OrderStatus.SUBMITTED
        order.created_at = order.created_at or now
        self.submitted.append(order)
        if order.order_type is OrderType.MARKET:
            self._queued_market.append(order)
        else:
            self._resting.append(order)
        return order

    def cancel(self, order: Order, reason: str = "cancelled") -> None:
        if order.is_active:
            order.mark(OrderStatus.CANCELLED, reason)
        for lst in (self._resting, self._queued_market):
            if order in lst:
                lst.remove(order)

    # ------------------------------------------------------------------
    def on_bar(self, snapshot: MarketSnapshot) -> list[Fill]:
        """Advance the simulation by one bar; return fills that occurred."""
        ts = snapshot.ts
        new_fills: list[Fill] = []

        # DAY orders expire at session rollover.
        if self._current_day is not None and ts.date() != self._current_day:
            for order in list(self._resting):
                if order.tif is TimeInForce.DAY:
                    self.cancel(order, "expired at session close")
        self._current_day = ts.date()

        # 1. Resting limit/stop orders first — they were in the book.
        for order in list(self._resting):
            bar = snapshot.bars.get(order.instrument.symbol)
            if bar is None:
                continue
            price = self._limit_fill_price(order, bar)
            if price is not None:
                new_fills += self._execute(order, price, ts, bar)
                if not order.is_active and order in self._resting:
                    self._resting.remove(order)

        # 2. Market orders queued from the previous bar fill at this open.
        for order in list(self._queued_market):
            bar = snapshot.bars.get(order.instrument.symbol)
            if bar is None:
                self.cancel(order, "no market data")
                continue
            price = bar.open if self.fill_on_next_open else bar.close
            new_fills += self._execute(order, price, ts, bar)
            if not order.is_active and order in self._queued_market:
                self._queued_market.remove(order)

        # 3. Time-in-force cleanup.
        for order in list(self._resting) + list(self._queued_market):
            if order.tif in (TimeInForce.IOC, TimeInForce.FOK) and order.is_active:
                self.cancel(order, f"{order.tif.value} expired")

        self.fills.extend(new_fills)
        return new_fills

    # ------------------------------------------------------------------
    def _limit_fill_price(self, order: Order, bar: Bar) -> float | None:
        """Return the fill price if the bar triggers this resting order."""
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                return None
            if order.side is Side.BUY and bar.low <= order.limit_price:
                return min(order.limit_price, bar.open)
            if order.side is Side.SELL and bar.high >= order.limit_price:
                return max(order.limit_price, bar.open)
            return None
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if order.stop_price is None:
                return None
            if order.side is Side.BUY and bar.high >= order.stop_price:
                return max(order.stop_price, bar.open)
            if order.side is Side.SELL and bar.low <= order.stop_price:
                return min(order.stop_price, bar.open)
            return None
        return None

    def _execute(self, order: Order, raw_price: float, ts: datetime, bar: Bar):
        if raw_price <= 0:
            self.cancel(order, "non-positive price")
            return []

        qty = order.remaining_quantity
        if self.participation_rate < 1.0 and bar.volume > 0:
            qty = min(qty, bar.volume * self.participation_rate)
        qty = self._round_quantity(order.instrument, qty)
        if qty <= 0:
            return []

        # Affordability: if the account can't fund the intended size we reject
        # outright rather than silently resizing — a partial fill here would
        # mask a sizing bug and quietly change the strategy's risk profile.
        if not self._can_fund(order, raw_price):
            order.mark(OrderStatus.REJECTED, "insufficient buying power")
            return []

        fill_price = self.slippage.apply(raw_price, order.side, order.instrument)
        # `side` matters: stamp duty is buy-only and DP charges are sell-only,
        # so a cost model that never sees the side cannot charge either.
        commission = self.commission.compute(
            qty, fill_price, order.instrument, side=order.side
        )

        order.apply_fill(qty, fill_price)
        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=qty,
            price=fill_price,
            ts=ts,
            commission=commission,
            slippage=abs(fill_price - raw_price),
            liquidity="TAKER" if order.order_type is OrderType.MARKET else "MAKER",
        )
        self.portfolio.apply_fill(fill)
        return [fill]

    def _round_quantity(self, instrument, qty: float) -> float:
        step = instrument.quantity_step or 1.0
        return float(int(qty / step) * step)

    def _can_fund(self, order: Order, price: float) -> bool:
        """Would the account still be within margin limits after this fill?"""
        inst = order.instrument
        if order.side is Side.SELL and not self.portfolio.allow_short:
            return order.remaining_quantity <= self.portfolio.position(inst.symbol).quantity + 1e-9

        existing = self.portfolio.position(inst.symbol).quantity
        new_qty = existing + order.side.sign * order.remaining_quantity
        required = self.portfolio.margin_for_order(inst, new_qty, price)
        return required <= self.portfolio.equity + 1e-6

    # ------------------------------------------------------------------
    def cancel_all(self) -> None:
        for order in list(self._resting) + list(self._queued_market):
            self.cancel(order, "square-off / shutdown")
