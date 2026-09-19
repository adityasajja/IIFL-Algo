"""Paper trading: a venue that fills, and a ledger that folds the fills.

The brief's goal is ``BACKTEST → PAPER → LIVE`` on **one strategy definition**.
That is only true if paper is the same pipeline with a different venue, which is
what this module is:

```
ExecutionService(orders=OrderService(...), venue=PaperVenue(...))
ExecutionService(orders=OrderService(...), venue=iifl_venue(broker))
```

Same OMS lifecycle, same risk gate, same rule layer. Only the venue differs. There
is deliberately no second strategy implementation and no parallel order model.

Two pieces, and the split matters
---------------------------------
**``PaperVenue``** decides whether and at what price an order fills. It is a
*simulation of the exchange*, so it has to model the things that make a fill
uncertain — and refuse to model the things it cannot know.

**``PaperLedger``** is the account: cash, positions, realised and unrealised P&L.
It holds **no state of its own**. It replays ``order_events`` into the existing
:class:`~atr.backtest.portfolio.Portfolio`, so a paper position cannot drift from
the orders that produced it — the log is the state, and ``Portfolio`` already
asserts the identity ``equity == cash + market value`` on every fill.

Why the venue refuses to fill some orders
-----------------------------------------
Three refusals, and each one is the "unknown reported as a measurement" trap:

* **No price → reject, not fill at zero.** A symbol with no bar has no price. A
  fill at ``0.0`` would produce a position with a zero cost basis and an infinite
  return.
* **A limit order that has not been reached → resting, not filled.** Filling a buy
  limit above the limit is the classic paper-trading lie: it manufactures edge that
  the market never offered.
* **A stop that has not triggered → resting, not filled.** Same reasoning.

A resting order is a real state (``ACKNOWLEDGED``) and ``match()`` re-evaluates it
against a later price, so a paper order behaves like one that is live at the
exchange rather than one that fills instantly and always.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from atr.appdb.engine import AppDatabase, get_app_db, utcnow
from atr.appdb.repositories import DeploymentRepository, OrderEventRepository
from atr.backtest.costs import CommissionModel, IndianDeliveryCosts, SlippageModel
from atr.core.enums import OrderType, Side
from atr.core.models import Fill, Instrument
from atr.services.execution import (
    VENUE_ACCEPTED,
    VENUE_FILLED,
    VENUE_REJECTED,
    VenueOutcome,
)

logger = logging.getLogger("atr.services.paper")


class PriceSource(Protocol):
    """Where a paper fill gets its reference price.

    Can return a raw float price or a :class:`TickPrice` dataclass carrying timestamp
    and age metadata.
    """

    def __call__(self, symbol: str, exchange: str) -> float | TickPrice | None: ...


@dataclass(frozen=True)
class TickPrice:
    """A price measurement coupled with its observation timestamp.

    Guarantees that price age is observable and stale prices can be audited and rejected.
    """

    price: float
    timestamp: datetime | None = None
    age_seconds: float | None = None

    def get_age_seconds(self, now: datetime | None = None) -> float | None:
        if self.age_seconds is not None:
            return self.age_seconds
        if self.timestamp is not None:
            now_dt = now or utcnow()
            # Ensure timezone awareness matches
            ts = self.timestamp
            if ts.tzinfo is None and now_dt.tzinfo is not None:
                ts = ts.replace(tzinfo=now_dt.tzinfo)
            elif ts.tzinfo is not None and now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=ts.tzinfo)
            return max(0.0, (now_dt - ts).total_seconds())
        return None


# ===========================================================================
# Price sources
# ===========================================================================
def cached_close_source(cache_root: Path | None = None) -> Callable[[str, str], float | None]:
    """The last close in the local daily cache. The offline default.

    Returns a callable rather than the price so a long-running paper session reads
    the current cache on every fill instead of a snapshot taken when it started.
    """
    from atr.instruments.service import CACHE_ROOT, get_instrument_master

    root = Path(cache_root) if cache_root is not None else Path(CACHE_ROOT)

    def source(symbol: str, exchange: str) -> float | None:
        try:
            master = get_instrument_master()
            record = master.find(symbol, exchange)
        except Exception:  # noqa: BLE001 - an unknown symbol is "no price"
            return None
        cache_file = getattr(record, "cache_file", None)
        if not cache_file:
            return None
        path = root / cache_file
        if not path.exists():
            return None
        try:
            import pandas as pd

            frame = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 - an unreadable file is "no price"
            logger.warning("could not read %s for a paper price", path)
            return None
        if frame.empty or "close" not in frame.columns:
            return None
        value = frame["close"].iloc[-1]
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        # NaN is not a price. Returning it would put NaN into equity and from
        # there into every metric — the failure the feeds' `mark()` guards against.
        return price if price == price and price > 0 else None

    return source


def fixed_price_source(prices: dict[str, float]) -> Callable[[str, str], float | None]:
    """A fixed price per symbol. For tests and for replaying a known session."""
    table = {k.upper(): float(v) for k, v in prices.items()}
    return lambda symbol, exchange: table.get(symbol.upper())  # noqa: ARG005


def live_tick_source(
    broadcaster: Any | None = None,
    *,
    resolve: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[str, str], TickPrice | None]:
    """The live tick stream's last traded price and timestamp.

    Returns a :class:`TickPrice` containing the ltp, tick timestamp, and calculated age.
    """
    now_fn = clock or utcnow

    def source(symbol: str, exchange: str) -> TickPrice | None:  # noqa: ARG001
        try:
            b = broadcaster
            if b is None and resolve is not None:
                b = resolve()
            if b is None:
                return None

            # Prefer latest_price_and_time if available
            if hasattr(b, "latest_price_and_time"):
                price, ts = b.latest_price_and_time(symbol)
            else:
                price = b.latest_price(symbol)
                ts = None

            if price is None or not (price == price and price > 0):
                return None

            now = now_fn()
            age: float | None = None
            if ts is not None:
                ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=now.tzinfo)
                now_aware = now if now.tzinfo else now.replace(tzinfo=ts_aware.tzinfo)
                age = max(0.0, (now_aware - ts_aware).total_seconds())

            return TickPrice(price=float(price), timestamp=ts, age_seconds=age)
        except Exception:  # noqa: BLE001 - a missing stream is "no price"
            logger.debug("live tick lookup failed for %s", symbol)
            return None

    return source


def fallback_price_source(
    primary: Callable[[str, str], Any],
    secondary: Callable[[str, str], Any],
    *,
    only_outside_market: bool = True,
) -> Callable[[str, str], Any]:
    """Try ``primary``; use ``secondary`` only when it has no price.

    If ``only_outside_market`` is True, fallback to cached close is disallowed
    when the market is actively open, ensuring a live paper run never silently
    fills against old-session cached close prices during trading hours.
    """
    from atr.market_calendar import is_market_open

    def source(symbol: str, exchange: str) -> Any:
        price = primary(symbol, exchange)
        if price is not None:
            return price
        # When during session hours, do NOT silently fall back to daily cache!
        if only_outside_market and is_market_open():
            logger.debug("session is open but primary live tick is absent for %s; refusing cache fallback", symbol)
            return None
        return secondary(symbol, exchange)

    return source


# ===========================================================================
# Matching
# ===========================================================================
@dataclass(frozen=True)
class MatchDecision:
    """Whether an order fills now, and at what price."""

    fills: bool
    price: float | None = None
    reason: str | None = None


def match_order(
    *,
    order_type: str,
    side: str,
    reference: float | None,
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> MatchDecision:
    """Decide whether an order fills at ``reference``.

    Pure, so every rule below is testable without a venue, a cache or a clock.

    ``reference`` is the price the market is at. For a MARKET order that is the
    fill; for the others it is what the order is measured against.
    """
    if reference is None or reference <= 0:
        # Not "no fill" — unpriceable. The caller turns this into a rejection,
        # because a resting order with no price is one that can never fill and
        # would sit in the book forever looking live.
        return MatchDecision(False, reason="no price available")

    kind = OrderType.parse(order_type)
    buying = side.strip().upper() == "BUY"

    if kind is OrderType.MARKET:
        return MatchDecision(True, reference)

    if kind is OrderType.LIMIT:
        if limit_price is None or limit_price <= 0:
            return MatchDecision(False, reason="a limit order needs a limit price")
        # A buy fills at or below its limit; a sell at or above. Filling a buy
        # limit above the limit is the classic paper-trading lie — it manufactures
        # edge the market never offered.
        if buying and reference <= limit_price:
            return MatchDecision(True, reference)
        if not buying and reference >= limit_price:
            return MatchDecision(True, reference)
        return MatchDecision(
            False,
            reason=(
                f"resting: market {reference:,.2f} has not reached the limit "
                f"{limit_price:,.2f}"
            ),
        )

    # STOP and STOP_LIMIT share the trigger; STOP_LIMIT then applies its limit.
    if stop_price is None or stop_price <= 0:
        return MatchDecision(False, reason="a stop order needs a trigger price")
    triggered = reference <= stop_price if not buying else reference >= stop_price
    if not triggered:
        return MatchDecision(
            False,
            reason=(
                f"resting: market {reference:,.2f} has not reached the trigger "
                f"{stop_price:,.2f}"
            ),
        )
    if kind is OrderType.STOP:
        return MatchDecision(True, reference)
    # STOP_LIMIT: triggered, but the limit can still refuse the fill.
    if limit_price is None or limit_price <= 0:
        return MatchDecision(False, reason="a stop-limit order needs a limit price")
    if buying and reference <= limit_price:
        return MatchDecision(True, reference)
    if not buying and reference >= limit_price:
        return MatchDecision(True, reference)
    return MatchDecision(
        False, reason=f"triggered but the limit {limit_price:,.2f} was skipped past"
    )


# ===========================================================================
# The venue
# ===========================================================================
@dataclass
class PaperVenue:
    """Fills orders against a reference price, with slippage and Indian costs.

    The cost model defaults to :class:`~atr.backtest.costs.IndianDeliveryCosts`
    and **not** the IBKR-style default, because that default understates NSE
    delivery by roughly 28x. A paper account costed with it reports an edge that
    does not exist, which is the specific error the whole research layer in this
    repo exists to avoid.
    """

    prices: Callable[[str, str], float | TickPrice | None]
    instruments: Callable[[str, str], Instrument] | None = None
    slippage: SlippageModel = field(default_factory=SlippageModel)
    costs: CommissionModel = field(default_factory=IndianDeliveryCosts)
    #: Fraction of the order that fills immediately. 1.0 is the optimistic case;
    #: a value below it makes partial fills part of the simulation rather than a
    #: thing that only happens in live trading.
    fill_ratio: float = 1.0
    clock: Callable[[], datetime] = field(default=utcnow)
    #: Maximum allowable age for a reference tick in seconds. If exceeded, orders are rejected.
    max_tick_age_seconds: float | None = 60.0

    def submit(self, draft: Any) -> VenueOutcome:
        """Fill or rest. See :func:`match_order` for the rules."""
        instrument = self._instrument(draft)
        raw_reference = self._price_raw(draft.symbol, draft.exchange)
        reference, tick_ts, price_age = self._extract_price_and_age(raw_reference)

        # Freshness check: reject stale prices
        if self.max_tick_age_seconds is not None and price_age is not None:
            if price_age > self.max_tick_age_seconds:
                return VenueOutcome(
                    status=VENUE_REJECTED,
                    reject_reason=(
                        f"stale price for {draft.symbol}: tick age {price_age:.1f}s exceeds "
                        f"max acceptable {self.max_tick_age_seconds:.1f}s"
                    ),
                    raw={
                        "reference_price": reference,
                        "price_timestamp": tick_ts.isoformat() if tick_ts else None,
                        "price_age_seconds": price_age,
                        "stale": True,
                    },
                )

        decision = match_order(
            order_type=draft.order_type,
            side=draft.side,
            reference=reference,
            limit_price=draft.limit_price,
            stop_price=draft.stop_price,
        )
        if not decision.fills:
            if reference is None or reference <= 0:
                # Unpriceable is a *rejection*, not a resting order: an order that
                # can never fill would otherwise sit in the book looking live.
                return VenueOutcome(
                    status=VENUE_REJECTED,
                    reject_reason=(
                        f"no price available for {draft.symbol} — refusing to fill an "
                        "order that cannot be priced"
                    ),
                    raw={"reference_price": None},
                )
            return VenueOutcome(
                status=VENUE_ACCEPTED,
                broker_order_id=f"PAPER-{draft.symbol}-resting",
                raw={
                    "reference_price": reference,
                    "price_timestamp": tick_ts.isoformat() if tick_ts else None,
                    "price_age_seconds": price_age,
                    "resting_reason": decision.reason,
                },
            )

        return self._fill(
            draft, instrument, decision.price or reference or 0.0, reference, tick_ts, price_age
        )

    def match(self, order: dict[str, Any]) -> VenueOutcome:
        """Re-evaluate a resting order against the current price.

        This is what makes a paper LIMIT or STOP order behave like one that is
        actually live, rather than one that fills instantly and always.
        """
        raw_reference = self._price_raw(order["symbol"], order["exchange"])
        reference, tick_ts, price_age = self._extract_price_and_age(raw_reference)

        # Freshness check on resting match
        if self.max_tick_age_seconds is not None and price_age is not None:
            if price_age > self.max_tick_age_seconds:
                return VenueOutcome(
                    status=VENUE_ACCEPTED,
                    broker_order_id=order.get("broker_order_id"),
                    raw={
                        "reference_price": reference,
                        "price_timestamp": tick_ts.isoformat() if tick_ts else None,
                        "price_age_seconds": price_age,
                        "resting_reason": f"stale tick ({price_age:.1f}s)",
                    },
                )

        decision = match_order(
            order_type=order["order_type"],
            side=order["side"],
            reference=reference,
            limit_price=order.get("limit_price"),
            stop_price=order.get("stop_price"),
        )
        if not decision.fills:
            return VenueOutcome(
                status=VENUE_ACCEPTED,
                broker_order_id=order.get("broker_order_id"),
                raw={
                    "reference_price": reference,
                    "price_timestamp": tick_ts.isoformat() if tick_ts else None,
                    "price_age_seconds": price_age,
                    "resting_reason": decision.reason,
                },
            )
        instrument = self._instrument_from(order["symbol"], order["exchange"])
        # The remaining quantity, not the original: an order that partially filled
        # has less left to do.
        outstanding = float(order["quantity"]) - float(order.get("filled_quantity") or 0.0)
        if outstanding <= 0:
            return VenueOutcome(
                status=VENUE_ACCEPTED,
                broker_order_id=order.get("broker_order_id"),
                raw={"resting_reason": "nothing left to fill"},
            )
        return self._fill_from(
            order, instrument, decision.price or reference or 0.0, reference, outstanding, tick_ts, price_age
        )

    def _fill(
        self,
        draft: Any,
        instrument: Instrument,
        raw_price: float,
        reference: float,
        tick_ts: datetime | None = None,
        price_age: float | None = None,
    ) -> VenueOutcome:
        side = Side.BUY if draft.side.strip().upper() == "BUY" else Side.SELL
        price = self.slippage.apply(raw_price, side, instrument)
        quantity = abs(float(draft.quantity)) * max(0.0, min(1.0, self.fill_ratio))
        if quantity <= 0:
            return VenueOutcome(
                status=VENUE_ACCEPTED, raw={"resting_reason": "fill_ratio produced no size"}
            )
        commission = float(self.costs.compute(quantity, price, instrument, side))
        raw_payload: dict[str, Any] = {
            "reference_price": reference,
            "fill_price": price,
            "commission": commission,
            "slippage_bps": _bps(reference, price, side),
            "venue": "paper",
        }
        if tick_ts is not None:
            raw_payload["price_timestamp"] = tick_ts.isoformat()
        if price_age is not None:
            raw_payload["price_age_seconds"] = round(price_age, 3)

        return VenueOutcome(
            status=VENUE_FILLED,
            broker_order_id=f"PAPER-{draft.symbol}",
            filled_qty=quantity,
            filled_price=price,
            commission=commission,
            raw=raw_payload,
        )

    def _fill_from(
        self,
        order: dict[str, Any],
        instrument: Instrument,
        raw_price: float,
        reference: float,
        quantity: float,
        tick_ts: datetime | None = None,
        price_age: float | None = None,
    ) -> VenueOutcome:
        side = Side.BUY if order["side"].strip().upper() == "BUY" else Side.SELL
        price = self.slippage.apply(raw_price, side, instrument)
        commission = float(self.costs.compute(quantity, price, instrument, side))
        raw_payload: dict[str, Any] = {
            "reference_price": reference,
            "fill_price": price,
            "commission": commission,
            "slippage_bps": _bps(reference, price, side),
            "venue": "paper",
            "matched_resting_order": True,
        }
        if tick_ts is not None:
            raw_payload["price_timestamp"] = tick_ts.isoformat()
        if price_age is not None:
            raw_payload["price_age_seconds"] = round(price_age, 3)

        return VenueOutcome(
            status=VENUE_FILLED,
            broker_order_id=order.get("broker_order_id") or f"PAPER-{order['symbol']}",
            filled_qty=float(order.get("filled_quantity") or 0.0) + quantity,
            filled_price=price,
            commission=commission,
            raw=raw_payload,
        )

    def _extract_price_and_age(
        self, raw_val: float | TickPrice | None
    ) -> tuple[float | None, datetime | None, float | None]:
        if raw_val is None:
            return None, None, None
        if isinstance(raw_val, TickPrice):
            return raw_val.price, raw_val.timestamp, raw_val.get_age_seconds(now=self.clock())
        try:
            return float(raw_val), None, None
        except (ValueError, TypeError):
            return None, None, None

    def _price(self, draft: Any) -> float | None:
        p, _, _ = self._extract_price_and_age(self._price_raw(draft.symbol, draft.exchange))
        return p

    def _price_from(self, symbol: str, exchange: str) -> float | None:
        p, _, _ = self._extract_price_and_age(self._price_raw(symbol, exchange))
        return p

    def _price_raw(self, symbol: str, exchange: str) -> Any:
        try:
            return self.prices(symbol, exchange)
        except Exception:  # noqa: BLE001 - a broken source is "no price"
            logger.exception("paper price source failed for %s", symbol)
            return None

    def _instrument(self, draft: Any) -> Instrument:
        return self._instrument_from(draft.symbol, draft.exchange)

    def _instrument_from(self, symbol: str, exchange: str) -> Instrument:
        if self.instruments is not None:
            return self.instruments(symbol, exchange)
        return Instrument(symbol=symbol, exchange=exchange, multiplier=1.0)


def _bps(reference: float, filled: float, side: Side) -> float | None:
    """Adverse-positive slippage in bps, same convention as the OMS."""
    if not reference:
        return None
    raw = (filled - reference) / reference
    if side is Side.SELL:
        raw = -raw
    return raw * 10_000.0


# ===========================================================================
# The ledger
# ===========================================================================
@dataclass
class PaperLedger:
    """Positions, cash and P&L for a paper account — folded from ``order_events``.

    There is no paper state table and that is deliberate. The order log *is* the
    state: replaying it produces the account, so a paper position cannot disagree
    with the orders that produced it, and a restart changes nothing. The schema
    was written for this — "positions and P&L are folds over those rows".

    ``Portfolio`` is reused rather than reimplemented. It already carries the
    identity ``equity == cash + market value`` and asserts it after every fill, so
    the paper account inherits a check that has been running against the backtester
    for as long as the backtester has existed.
    """

    db: AppDatabase = field(default_factory=get_app_db)

    def portfolio(
        self,
        user_id: str,
        *,
        deployment_id: str | None = None,
        initial_cash: float | None = None,
        prices: dict[str, float] | None = None,
    ) -> Any:
        """Replay the user's fills into a :class:`Portfolio`.

        ``deployment_id`` scopes the replay to one deployment, which is what a
        per-strategy capital allocation means. Without it the account is the whole
        paper book.
        """
        from atr.backtest.portfolio import Portfolio

        cash = float(initial_cash if initial_cash is not None else self._deployment_cash(
            user_id, deployment_id
        ))
        portfolio = Portfolio(initial_cash=cash)

        for fill in self.fills(user_id, deployment_id=deployment_id):
            portfolio.apply_fill(fill)

        if prices:
            portfolio.mark(utcnow(), prices)
        return portfolio

    def _deployment_cash(self, user_id: str, deployment_id: str | None) -> float:
        """The deployment's capital, or 0.0 when there is no deployment.

        Zero rather than a guess. A paper account with no capital allocation has
        no starting cash, and inventing a default would make ``equity`` a number
        nobody chose — the same "unknown reported as a measurement" failure as
        everywhere else in this codebase.
        """
        if not deployment_id:
            return 0.0
        with self.db.session() as session:
            row = DeploymentRepository.get(session, deployment_id, user_id)
        return float(row["capital"]) if row else 0.0

    def fills(
        self, user_id: str, *, deployment_id: str | None = None
    ) -> list[Fill]:
        """The user's execution events, oldest first, as :class:`Fill` objects."""
        from atr.appdb.repositories import OrderRepository

        with self.db.session() as session:
            orders = {
                o["order_id"]: o
                for o in OrderRepository.list_for_user(session, user_id, limit=500)[0]
            }
            if deployment_id:
                orders = {
                    k: v for k, v in orders.items()
                    if v.get("deployment_id") == deployment_id
                }
            out: list[Fill] = []
            for order_id, order in orders.items():
                for event in OrderEventRepository.fills(session, order_id):
                    if event.get("filled_qty") is None:
                        continue
                    previous = _previous_cumulative(session, order_id, event["seq"])
                    incremental = float(event["filled_qty"]) - previous
                    if incremental <= 0:
                        # A repeated cumulative figure is not a new fill. Recording
                        # it would double-count the position.
                        continue
                    out.append(
                        _fill_from_event(order, event, incremental)
                    )
        out.sort(key=lambda f: f.ts)
        return out

    def snapshot(
        self,
        user_id: str,
        *,
        deployment_id: str | None = None,
        initial_cash: float | None = None,
        prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """The account as a JSON-able dict, for the dashboard.

        ``initial_cash`` overrides the deployment's capital, for an ad-hoc paper
        session with no deployment row. Without either, the account starts at zero
        rather than at an invented default.

        **Unpriced positions are excluded and named, not counted at zero.** A
        position whose symbol has no mark would otherwise be valued at
        ``last_price = 0``, and ``unrealized_pnl`` would report
        ``(0 - avg_price) * quantity`` — a fabricated loss of the entire cost basis,
        presented as a measurement. So the totals cover only the positions that
        could be priced, ``complete`` says whether that was all of them, and
        ``unpriced_symbols`` lists the rest. The dashboard shows the count; it does
        not show a total that quietly omits them.
        """
        portfolio = self.portfolio(
            user_id,
            deployment_id=deployment_id,
            initial_cash=initial_cash,
        )

        marks = prices if prices is not None else self._marks(portfolio)

        rows: list[dict[str, Any]] = []
        unpriced: list[str] = []
        market_value = 0.0
        unrealized = 0.0
        gross_exposure = 0.0
        for position in portfolio.positions.values():
            if position.is_flat:
                continue
            mark = marks.get(position.instrument.symbol)
            if mark is not None:
                position.mark(mark)
                market_value += position.market_value
                unrealized += position.unrealized_pnl
                gross_exposure += position.exposure
                last_price: float | None = position.last_price
                row_value: float | None = position.market_value
                row_pnl: float | None = position.unrealized_pnl
            else:
                # The position is real and is shown. Only its *valuation* is
                # unknown, so only the valuation fields are null. Omitting the row
                # would tell the user they hold nothing, which is a different lie.
                unpriced.append(position.instrument.symbol)
                last_price = None
                row_value = None
                row_pnl = None
            rows.append(
                {
                    "symbol": position.instrument.symbol,
                    "exchange": position.instrument.exchange,
                    "quantity": position.quantity,
                    "avg_price": position.avg_price,
                    "last_price": last_price,
                    "market_value": row_value,
                    "unrealized_pnl": row_pnl,
                    "realized_pnl": position.realized_pnl,
                    "direction": position.direction,
                    "priced": mark is not None,
                }
            )
        rows.sort(key=lambda r: r["symbol"])
        unpriced.sort()

        equity = portfolio.cash + market_value

        return {
            "initial_cash": portfolio.initial_cash,
            "cash": portfolio.cash,
            "equity": equity,
            "market_value": market_value,
            "realized_pnl": portfolio.realized_pnl,
            "unrealized_pnl": unrealized,
            "commission_paid": portfolio.commission_paid,
            # Net of frictions, which is the only figure worth showing next to a
            # gross one — and the one the research layer says decides whether a
            # strategy is worth considering at all.
            "net_realized_pnl": portfolio.realized_pnl - portfolio.commission_paid,
            "total_pnl": equity - portfolio.initial_cash,
            "gross_exposure": gross_exposure,
            "net_exposure": market_value,
            "positions": rows,
            # The honesty fields. A dashboard that ignores these is showing a
            # partial total as a whole one.
            "complete": not unpriced,
            "unpriced_symbols": unpriced,
            "deployment_id": deployment_id,
        }

    def _marks(self, portfolio: Any) -> dict[str, float]:
        """Live prices when available, falling back to cached close.

        Prioritizes live tick stream for real-time P&L. Only falls back to
        cached close when no live price is available.
        """
        # Try live tick source first
        live_source = default_live_source()
        source = cached_close_source()
        marks: dict[str, float] = {}
        for position in portfolio.positions.values():
            if position.is_flat:
                continue
            symbol = position.instrument.symbol
            # Prefer live price
            live_price = live_source(symbol, position.instrument.exchange)
            if live_price is not None:
                marks[symbol] = live_price.price
            else:
                # Fall back to cached close
                price = source(symbol, position.instrument.exchange)
                if price is not None:
                    marks[symbol] = price
        return marks


def _previous_cumulative(session: Any, order_id: str, seq: int) -> float:
    """The cumulative filled quantity before this event. Zero for the first fill."""
    from sqlalchemy import select

    from atr.appdb.schema import order_events

    stmt = (
        select(order_events.c.filled_qty)
        .where(order_events.c.order_id == order_id, order_events.c.seq < seq)
        .order_by(order_events.c.seq.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar()
    return float(value or 0.0)


def _fill_from_event(order: dict[str, Any], event: dict[str, Any], quantity: float) -> Fill:
    """Reconstruct one :class:`Fill` from an order row and its execution event.

    ``commission`` comes from the event, not from a cost model: it is what was
    actually charged, and recomputing it would restate history whenever a rate
    changed.
    """
    from atr.core.enums import AssetClass

    side = Side.BUY if order["side"].upper() == "BUY" else Side.SELL
    instrument = Instrument(
        symbol=order["symbol"],
        exchange=order["exchange"],
        asset_class=AssetClass(order.get("asset_class") or "EQUITY"),
    )
    price = float(event["filled_price"] or 0.0)
    return Fill(
        order_id=order["order_id"],
        instrument=instrument,
        side=side,
        quantity=quantity,
        price=price,
        ts=event["fill_ts"] or event["ts"],
        commission=float(event.get("commission") or 0.0),
        slippage=_per_unit_slippage(order, price, side),
    )


def _per_unit_slippage(order: dict[str, Any], price: float, side: Side) -> float:
    """Adverse-positive slippage in rupees **per unit**, which is what ``Portfolio`` wants.

    ``Portfolio.slippage_cost`` multiplies this by the quantity, so it must be a
    per-unit figure. Derived from the two prices directly rather than from the
    recorded basis points: the bps figure is rounded and re-deriving rupees from it
    introduces a drift for no gain, when both prices are already on the row.
    """
    reference = order.get("requested_price")
    if not reference or not price:
        # No reference price was recorded, so slippage was never measurable for this
        # order. Zero here means "not measured", not "no slippage".
        return 0.0
    raw = (price - float(reference)) / float(reference)
    if side is Side.SELL:
        raw = -raw
    return raw * float(reference)


def default_price_source() -> Callable[[str, str], float | None]:
    """The price source a running paper deployment fills against.

    **Live ticks first, the daily cache as the fallback.** The cache alone is not
    a price a paper trade can be judged on — it is yesterday's close, so a fill
    happens at a level the market was never at and a resting limit order is
    re-evaluated against a number that does not move. Live ticks alone would
    refuse every fill when the feed is down or the market is closed, which is
    when most paper strategies are actually started.

    A named function rather than an inline default, so a caller — a test, or an
    operator running paper against a different feed — can substitute a
    deterministic source. A paper fill whose price comes from whatever happens to
    be in the operator's cache is not a testable fill, and "the test passed because
    the cache had data today" is not a test.

    Which source priced a fill is never ambiguous: the price actually used is
    written to the order event, so the log carries the level rather than leaving
    it to be re-derived from a feed that has since moved.
    """
    return fallback_price_source(
        default_live_source(), cached_close_source()
    )


def default_live_source() -> Callable[[str, str], float | None]:
    """The live tick source, or a source that never prices if none is wired.

    The API layer calls :func:`atr.api.price_sources.install_live_source` at
    startup to point this at the process-wide broadcaster. Until then — in a CLI
    run, in a test, or if the stream failed to start — it returns nothing and
    :func:`default_price_source` falls through to the daily cache, which is the
    pre-existing behaviour rather than a crash or a fabricated price.
    """
    if _LIVE_SOURCE[0] is None:
        return live_tick_source()
    return _LIVE_SOURCE[0]


#: Set once by the API layer. A one-slot registry rather than a module import,
#: because the dependency runs the other way (api → services, never back).
_LIVE_SOURCE: list[Callable[[str, str], float | None] | None] = [None]


def install_live_source(source: Callable[[str, str], float | None] | None) -> None:
    """Register the live price source. Called by the API layer at startup."""
    _LIVE_SOURCE[0] = source


#: Set once by the API layer, alongside ``_LIVE_SOURCE``. A callable that asks
#: the tick stream to *carry* a set of symbols, which is a different job from
#: reading a price out of it — and the one that was missing.
_LIVE_SUBSCRIBER: list[Callable[..., Any] | None] = [None]


def install_live_subscriber(subscriber: Callable[..., Any] | None) -> None:
    """Register the function that puts symbols on the live feed.

    Injected for the same reason the price source is: ``services`` sits below
    ``api``, so this module cannot reach the broadcaster itself and the API layer
    owns the wiring.
    """
    _LIVE_SUBSCRIBER[0] = subscriber


def ensure_live_symbols(
    symbols: Any, exchange: str = "NSEEQ"
) -> dict[str, list[str]] | None:
    """Ask the live feed to carry ``symbols``, so their ticks exist to be read.

    **Reading the feed is not the same as being on it.** A symbol only reaches
    the broadcaster's tick store if somebody subscribed to it, and the only
    subscriber used to be a browser. So a paper deployment with no dashboard open
    read ``None`` from every price lookup, and ``default_price_source`` fell
    through to the daily cache — meaning the deployment was priced at yesterday's
    close, or not priced at all, while reporting itself as running. A paper
    account in that state produces either no forward observation or a wrong one,
    and both are worse than an honest refusal.

    Returns the subscriber's report, or ``None`` when no subscriber is installed.
    ``None`` is not an error: in a CLI run, in a test, or when the stream failed
    to start, the venue keeps pricing off the daily cache — the documented
    degradation rather than a crash or a fabricated price.
    """
    subscriber = _LIVE_SUBSCRIBER[0]
    if subscriber is None:
        return None
    try:
        return subscriber(symbols, exchange)
    except Exception:  # noqa: BLE001 - a missing feed must not stop the loop
        logger.exception("paper: could not subscribe the deployment universe")
        return None


def paper_venue(
    prices: Callable[[str, str], float | None] | None = None, **kwargs: Any
) -> PaperVenue:
    """A :class:`PaperVenue` over live ticks (cache as fallback) unless told otherwise."""
    return PaperVenue(prices=prices or default_price_source(), **kwargs)




# ===========================================================================
# Deployments
# ===========================================================================
class DeploymentError(Exception):
    """A deployment action was refused. Carries the code the API returns."""

    def __init__(self, message: str, *, code: str = "deployment_error", status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


@dataclass
class DeploymentService:
    """The paper deployment lifecycle: create, start, pause, stop, reset.

    A deployment is what gives a paper account a *capital allocation* and a
    *strategy version*, which is what makes per-strategy P&L and per-strategy risk
    limits meaningful. Without one, the paper ledger has no starting cash and
    reports zero rather than a number somebody chose.

    Every state change that stops or restarts trading requires a reason, and the
    reason is stored on the row — the same rule the kill switch follows, for the
    same reason.
    """

    db: AppDatabase = field(default_factory=get_app_db)
    ledger: PaperLedger = field(default_factory=PaperLedger)

    def create(
        self,
        user_id: str,
        *,
        strategy_id: str,
        strategy_version: int,
        capital: float,
        mode: str = "PAPER",
        broker_account: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Allocate capital to an immutable strategy version.

        **The pinned version is checked before the row is written**, and only when
        the strategy is one of the caller's saved strategies. Two refusals:

        * the version does not exist;
        * the version exists but resolves to no live entry/exit rules.

        Both would otherwise produce a deployment that reports itself ``RUNNING``,
        shows up on the monitor, and never places an order — the runner refuses to
        trade a version it cannot resolve, correctly, because the alternative is
        stamping a strategy's name onto rules it never ran. That refusal is honest
        and it is also indistinguishable from a quiet market on the screen, so it
        is better raised here where the operator is still looking.

        A ``strategy_id`` that is **not** one of the caller's saved strategies is
        left alone. That path is deliberate and tested (``tests/
        test_paper_deployment.py``): a deployment may name a bare registry key, and
        the runner reports the refusal on the status surface when it cannot be
        resolved. Widening the check to reject unknown ids would change behaviour
        those tests pin, and it would not catch a mistake it does not already
        catch — a typo in a *saved* strategy's id is caught by the version check.
        """
        from atr.appdb.repositories import StrategyRepository
        from atr.strategy.definition import parse_definition, resolve_rules

        with self.db.session() as session:
            strategy = StrategyRepository.get(session, strategy_id, user_id)
            if strategy is not None:
                row = StrategyRepository.version(session, strategy_id, int(strategy_version))
                if row is None:
                    raise DeploymentError(
                        f"strategy {strategy_id} has no version {strategy_version}, so "
                        "there is nothing to deploy",
                        code="version_not_found",
                        status=422,
                    )
                definition, _reason = parse_definition(row.get("definition"))
                resolution = resolve_rules(definition) if definition is not None else None
                if not (resolution and resolution.ok):
                    why = resolution.reason if resolution else "the definition is unreadable"
                    raise DeploymentError(
                        f"strategy version {strategy_id}#{strategy_version} cannot be "
                        f"deployed: {why}",
                        code="version_not_deployable",
                        status=422,
                    )

            from atr.services.portfolio import check_strategy_capital

            ok, reason = check_strategy_capital(
                user_id, strategy_id, float(capital), db=self.db
            )
            if not ok:
                raise DeploymentError(
                    reason or "capital allocation exceeds strategy cap",
                    code="strategy_capital_cap_exceeded",
                    status=422,
                )

            return DeploymentRepository.create(
                session,
                user_id=user_id,
                strategy_id=strategy_id,
                strategy_version=int(strategy_version),
                mode=mode,
                capital=capital,
                broker_account=broker_account,
                config=config,
            )

    def launch_challenger(
        self,
        user_id: str,
        *,
        strategy_id: str,
        champion_deployment_id: str,
        challenger_version: int,
    ) -> dict[str, Any]:
        """Launch a challenger version against a running champion, same conditions.

        The challenger inherits the champion's capital, universe and full
        deployment config — the only thing that differs is the strategy
        version, which is what makes the comparison a comparison rather than
        two unrelated books. Market data, venue, trading costs and the
        slippage model are shared structurally (one runner, one venue), so
        they cannot differ; capital and universe are copied here, so they
        cannot drift through a hand-typed form either.

        Refusals, each with its own code rather than a bare 422:

        * the champion deployment is not the caller's, not PAPER, or not
          RUNNING — a challenger needs live conditions to be compared against;
        * the challenger version equals the champion's — that is the same arm
          twice, and two loops on one version would even share idempotency
          keys, so the second loop's orders would collapse into the first's;
        * the challenger version does not exist or resolves to no rules
          (via :meth:`create`, which owns those checks);
        * a RUNNING PAPER deployment already exists for the challenger version
          — for the same idempotency reason: one version, one loop.

        Returns the challenger deployment row plus a ``parity`` statement
        naming what was copied from which champion, so the identical-conditions
        claim is auditable rather than asserted.
        """
        champion = self.get(user_id, champion_deployment_id)
        if str(champion.get("strategy_id") or "") != str(strategy_id):
            raise DeploymentError(
                "the champion deployment does not belong to "
                f"strategy {strategy_id}",
                code="champion_mismatch",
                status=422,
            )
        if str(champion.get("mode") or "").upper() != "PAPER":
            raise DeploymentError(
                "a challenger can only be launched against a PAPER champion",
                code="champion_not_paper",
                status=422,
            )
        if str(champion.get("status") or "").upper() != "RUNNING":
            raise DeploymentError(
                "a challenger can only be launched against a RUNNING champion; "
                "start it first so both arms observe live conditions",
                code="champion_not_running",
                status=409,
            )
        if int(challenger_version) == int(champion.get("strategy_version") or 0):
            raise DeploymentError(
                f"version {challenger_version} is already the champion; a "
                "challenger must be a different version",
                code="challenger_is_champion",
                status=422,
            )
        with self.db.session() as session:
            clashes = DeploymentRepository.list_for_user(
                session, user_id, status="RUNNING"
            )
        for row in clashes:
            if str(row.get("strategy_id") or "") == str(strategy_id) and int(
                row.get("strategy_version") or 0
            ) == int(challenger_version):
                raise DeploymentError(
                    f"version {challenger_version} already has a RUNNING PAPER "
                    f"deployment ({row.get('deployment_id')}); one version runs "
                    "one loop, because two loops on one version share "
                    "idempotency keys",
                    code="challenger_already_running",
                    status=409,
                )

        import copy
        import json as _json

        raw_config = champion.get("config")
        if isinstance(raw_config, str):
            try:
                config = _json.loads(raw_config)
            except ValueError:
                config = None
        elif isinstance(raw_config, dict):
            config = copy.deepcopy(raw_config)
        else:
            config = None

        created = self.create(
            user_id,
            strategy_id=strategy_id,
            strategy_version=int(challenger_version),
            capital=float(champion.get("capital") or 0.0),
            mode="PAPER",
            broker_account=champion.get("broker_account"),
            config=config,
        )
        created["parity"] = {
            "champion_deployment_id": champion.get("deployment_id"),
            "capital_copied": float(champion.get("capital") or 0.0),
            "config_copied": config,
            "venue_shared": True,
            "note": (
                "market data, trading costs and the slippage model are shared "
                "structurally: both loops fill against the runner's single "
                "venue. Capital and universe were copied from the champion row."
            ),
        }
        return created

    def list(self, user_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = DeploymentRepository.list_for_user(session, user_id, status=status)
        for row in rows:
            # Each row carries its own P&L so the list is useful without N calls.
            row["pnl"] = self.ledger.snapshot(
                user_id, deployment_id=row["deployment_id"], prices={}
            )
        return rows

    def get(self, user_id: str, deployment_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = DeploymentRepository.get(session, deployment_id, user_id)
        if row is None:
            raise DeploymentError("no such deployment", code="not_found", status=404)
        return row

    def start(self, user_id: str, deployment_id: str) -> dict[str, Any]:
        self.get(user_id, deployment_id)
        with self.db.session() as session:
            if DeploymentRepository.start(session, deployment_id, user_id) == 0:
                raise DeploymentError("no such deployment", code="not_found", status=404)
        return self.get(user_id, deployment_id)

    def pause(self, user_id: str, deployment_id: str, *, reason: str) -> dict[str, Any]:
        self.get(user_id, deployment_id)
        try:
            with self.db.session() as session:
                changed = DeploymentRepository.pause(
                    session, deployment_id, user_id, reason=reason
                )
        except ValueError as exc:
            # The repository refuses a blank reason. Translated here so the API can
            # answer 400 with a code rather than 500 — the same treatment `stop`
            # gives it, and the reason both go through the same path.
            raise DeploymentError(str(exc), code="reason_required") from exc
        if changed == 0:
            raise DeploymentError(
                "the deployment is not running, so it cannot be paused",
                code="not_running",
                status=409,
            )
        return self.get(user_id, deployment_id)

    def stop(self, user_id: str, deployment_id: str, *, reason: str) -> dict[str, Any]:
        self.get(user_id, deployment_id)
        try:
            with self.db.session() as session:
                changed = DeploymentRepository.stop(
                    session, deployment_id, user_id, reason=reason
                )
        except ValueError as exc:
            raise DeploymentError(str(exc), code="reason_required") from exc
        if changed == 0:
            raise DeploymentError(
                "the deployment is already stopped", code="already_stopped", status=409
            )
        return self.get(user_id, deployment_id)

    def reset(
        self, user_id: str, deployment_id: str, *, reason: str
    ) -> dict[str, Any]:
        """Stop this deployment and start a fresh one with the same configuration.

        **Reset does not delete anything, and it cannot.** ``order_events`` is
        append-only — that is what makes the order log trustworthy — so a paper
        account's history cannot be erased. Deleting the fills would leave orders
        whose fills had vanished, and reconciliation would report mismatches the
        platform had created itself.

        So a reset is a *new deployment*: the old one is stopped with the reason
        recorded, its history stays readable, and the returned deployment has a new
        id and the original capital. That is also what an operator actually wants —
        "start this strategy again from a clean account" — and it keeps the previous
        run's P&L available for comparison.
        """
        previous = self.get(user_id, deployment_id)
        reason = (reason or "").strip()
        if not reason:
            raise DeploymentError(
                "resetting a deployment requires a reason", code="reason_required"
            )
        try:
            with self.db.session() as session:
                DeploymentRepository.stop(
                    session, deployment_id, user_id,
                    reason=f"reset: {reason}",
                )
        except ValueError:  # already stopped
            pass

        import json as _json

        config = None
        if previous.get("config"):
            try:
                config = _json.loads(previous["config"])
            except (TypeError, ValueError):
                config = None
        fresh = self.create(
            user_id,
            strategy_id=previous["strategy_id"],
            strategy_version=previous["strategy_version"],
            capital=previous["capital"],
            mode=previous["mode"],
            broker_account=previous.get("broker_account"),
            config=config,
        )
        fresh["reset_from"] = deployment_id
        fresh["reset_reason"] = reason
        return fresh

    # ------------------------------------------------------------------ reads
    def positions(
        self, user_id: str, deployment_id: str, *, prices: dict[str, float] | None = None
    ) -> dict[str, Any]:
        self.get(user_id, deployment_id)
        snapshot = self.ledger.snapshot(
            user_id, deployment_id=deployment_id, prices=prices
        )
        return {
            "deployment_id": deployment_id,
            "positions": snapshot["positions"],
            "complete": snapshot["complete"],
            "unpriced_symbols": snapshot["unpriced_symbols"],
            "cash": snapshot["cash"],
            "equity": snapshot["equity"],
            "gross_exposure": snapshot["gross_exposure"],
        }

    def pnl(
        self, user_id: str, deployment_id: str, *, prices: dict[str, float] | None = None
    ) -> dict[str, Any]:
        self.get(user_id, deployment_id)
        return self.ledger.snapshot(user_id, deployment_id=deployment_id, prices=prices)

    def orders(self, user_id: str, deployment_id: str) -> list[dict[str, Any]]:
        from atr.appdb.repositories import OrderRepository

        self.get(user_id, deployment_id)
        with self.db.session() as session:
            rows, _ = OrderRepository.list_for_user(
                session, user_id, deployment_id=deployment_id, limit=500
            )
        return rows


__all__ = [
    "DeploymentError",
    "DeploymentService",
    "PaperLedger",
    "PaperVenue",
    "PriceSource",
    "TickPrice",
    "cached_close_source",
    "default_price_source",
    "fixed_price_source",
    "match_order",
    "paper_venue",
]
