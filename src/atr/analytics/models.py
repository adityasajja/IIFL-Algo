"""The attribution field vocabulary — one shape for backtest, paper and live.

Why one shape and not three
---------------------------

The requirement is *shared execution truth*: a backtest, a paper deployment and a
live deployment must produce the same attribution object, because the whole point
is to compare them. The comparison the learning engine actually needs to make is
*"does this strategy's execution quality in simulation match its execution
quality with real money?"* — and that question is unanswerable if the simulated
trade and the live trade are stored in different tables with different column
names.

So there is one set of frozen dataclasses here, and one serialiser. What differs
between venues is not the *shape* but the *provenance of the values*, and that is
carried explicitly:

* a backtest leg carries ``simulated=True`` and its fills come from the matching
  engine's model, not from a broker;
* a paper leg carries ``simulated=True`` and its fills come from ``PaperVenue``,
  which is also a model — but a model fed by the live price;
* a live leg carries ``simulated=False`` and its fills were reported by the
  broker.

A viewer that conflates the first two with the third is claiming real execution
for a simulated fill, so the flag travels on the row and the API surfaces it.

Immutability
------------

Every dataclass is frozen. An attribution row is a record of a measurement that
happened; the one thing that may ever change about it is whether it has been
written yet. ``frozen=True`` makes "we corrected the record in place" a
``FrozenInstanceError`` rather than a silent edit to evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# The trade being attributed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeDetails:
    """The facts about a closed trade, as the book recorded them.

    Every field is optional except the identity and the two prices, because a
    trade can exist in the book without every context feature having been
    resolvable — and a missing context feature is not a reason to lose the P&L.

    ``entry_price`` is the *actual average fill* on the opening side, not the
    price that was requested. The requested price is
    :attr:`AttributionLeg.expected_price` on the matching leg, and keeping the
    two apart is what makes slippage computable at all.
    """

    # ── identity & provenance ────────────────────────────────────────────────
    trade_id: str
    user_id: str
    symbol: str
    side: str  # BUY / SELL, the side that OPENED the position
    #: BACKTEST | PAPER | LIVE — where the record came from.
    source: str
    #: forward | in_sample — the direction a finding may depend on. Derived from
    #: the evidence class by the service, never restated here.
    evidence_grade: str
    evidence_class: str
    #: True when the fills were produced by a model rather than reported by a
    #: broker. A backtest and a paper deployment are both simulated; only live
    #: is not. Carried on the row so a comparison can filter on it.
    simulated: bool

    # ── quantities and prices ────────────────────────────────────────────────
    quantity: float
    entry_price: float | None
    exit_price: float | None
    position_value: float | None
    gross_pnl: float | None
    net_pnl: float | None
    gross_return_pct: float | None
    net_return_pct: float | None

    # ── timing ───────────────────────────────────────────────────────────────
    entry_ts: Any | None = None
    exit_ts: Any | None = None
    holding_duration_sec: int | None = None

    # ── risk and sizing ──────────────────────────────────────────────────────
    stop_price: float | None = None
    planned_risk_amount: float | None = None
    realized_risk_pct: float | None = None
    sizing_method: str | None = None
    #: When set, the sizing rule refused to reach its target size and said why.
    #: This is the field that lets ``OVERSIZED`` mean something precise: a
    #: position larger than a cap that was *recorded* is oversized as a matter of
    #: arithmetic, not of opinion.
    sizing_cap_reason: str | None = None
    sizing_cap_value: float | None = None

    # ── attribution ──────────────────────────────────────────────────────────
    signal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None
    context_model_version: str | None = None
    context_score: int | None = None
    context_class: str | None = None

    # ── market / sector context at entry ─────────────────────────────────────
    market_regime: str | None = None
    sector: str | None = None
    sector_strength: float | None = None
    stock_relative_strength: float | None = None
    rvol: float | None = None
    atr: float | None = None
    atr_pct: float | None = None

    # ── outcome ──────────────────────────────────────────────────────────────
    exit_reason: str | None = None
    transaction_costs: float | None = None

    #: Fields that could not be resolved, with the reason. Never empty for a
    #: real trade: a row claiming every field is present is a row worth checking.
    missing_fields: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# One leg of the round trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionLeg:
    """One side of the round trip: the entry fill and the exit fill.

    A leg records three prices, and their relationship is the entire substance of
    "execution quality":

    ``expected_price``  what the decision asked for — the reference price the
                        order carried (the last trade seen when the signal fired,
                        or the limit it was placed at).
    ``actual_price``    what the fill got, averaged over the fills that made it.
    ``signal_to_order_sec`` / ``order_to_fill_sec``  the two delays, kept apart
                        because they have different causes and different fixes:
                        one is the platform deciding, the other is the market
                        filling.

    ``requested_qty`` versus ``filled_qty`` is what makes partial fills visible.
    An entry that only got 40% of its intended size is a materially different
    trade from one that got all of it, and a system that nets them into "the
    entry price" alone cannot tell the two apart.
    """

    #: ``entry`` or ``exit``.
    kind: str
    side: str
    expected_price: float | None
    actual_price: float | None
    requested_qty: float | None
    filled_qty: float | None
    #: Wall-clock order creation, the fill, and the signal that caused it.
    signal_ts: Any | None = None
    order_ts: Any | None = None
    fill_ts: Any | None = None
    signal_to_order_sec: float | None = None
    order_to_fill_sec: float | None = None
    commission: float | None = None
    #: Adverse-positive basis points, this leg only.
    slippage_bps: float | None = None
    #: Adverse-positive rupees, this leg only (price difference × filled qty).
    slippage_amount: float | None = None
    #: True when the leg filled across more than one execution event. A
    #: multi-fill leg is recorded, not flattened: the average price is honest,
    #: but the fact that it took several attempts is itself execution quality.
    partial: bool = False
    fill_count: int = 0

    @property
    def fill_ratio(self) -> float | None:
        """Filled / requested. ``None`` when the request was never recorded."""
        if not self.requested_qty:
            return None
        if self.filled_qty is None:
            return None
        return float(self.filled_qty) / float(self.requested_qty)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["fill_ratio"] = self.fill_ratio
        return out


# ---------------------------------------------------------------------------
# The assembled input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionInput:
    """Everything the pure engine needs, already resolved by the service.

    This type is the seam between I/O and arithmetic. The service's job is to
    read the database, the order-event log and the price cache and produce one of
    these; the engine's job is to turn it into a :class:`TradeAttribution`. The
    engine therefore needs no database, no data root and no clock, which is what
    makes its tests deterministic — and a look-ahead bug in it is a unit-test
    failure rather than a research conclusion.

    ``bars_after_entry`` is the symbol's price series **from entry onwards**, and
    ``bars_until`` is the observation point. They are separate parameters because
    the caller must state where the view stops: for a closed trade that is the
    exit; for an open one it is now. Passing the whole series with no cutoff is
    how a future bar gets in, so there is no way to express that call.
    """

    trade: TradeDetails
    entry_leg: AttributionLeg
    exit_leg: AttributionLeg | None = None
    #: Price bars at or after the entry, oldest first. Rows must have ``ts`` and
    #: ``high``/``low``; ``close`` is used for the terminal mark.
    bars_after_entry: Any | None = None
    #: The instant the observation stops — the exit for a closed trade. Required
    #: whenever ``bars_after_entry`` is given, so "how far did it run" can never
    #: silently mean "how far did it run by today".
    bars_until: Any | None = None
    #: The strategy's own planned move, when it recorded one (a target price).
    #: Used for capture efficiency; absent for a time- or stop-driven exit.
    target_price: float | None = None
    #: Thresholds, injected so the classification is reproducible from the row.
    thresholds: dict[str, float] = field(default_factory=dict)


__all__ = [
    "AttributionInput",
    "AttributionLeg",
    "TradeDetails",
]
