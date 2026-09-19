"""Path measures: MAE, MFE, and execution quality. Pure, and truncation-first.

The look-ahead rule, and why it is structural rather than documented
-------------------------------------------------------------------

A trade's **excursion** is a property of what happened between entry and exit, so
computing it obviously requires the bars in between. That is not look-ahead — it
is the definition. Look-ahead would be letting *some other* measure read those
bars, and the measure where that mistake is most tempting and most damaging is
entry quality: *"price rose 4% after this entry, so it was a good entry"* is a
statement that could only be made after the fact, and a screen full of those
teaches a trader nothing they can act on.

So the families are separated in code, not in prose:

* :func:`compute_excursions` reads bars from entry through ``bars_until`` and is
  the **only** function in this package permitted to. It refuses to run without a
  cutoff.
* :func:`execution_quality` reads **no bars at all**. It works entirely from the
  legs' own expected/actual prices and timestamps, so slippage and delay are
  computed from information that existed at the instant of the fill.

That second point is what makes the entry/exit quality figures honest: they
measure *how well the requested price was obtained*, never *whether the price
then moved favourably*.

Why ``bars_until`` is required and not defaulted
-----------------------------------------------

The tempting signature is ``compute_excursions(bars, entry_ts)`` and let the
function use every bar it has. That version is correct for a closed trade whose
series happens to end at the exit, and silently wrong for every other call — and
when it is wrong it overstates MFE, which overstates how good the trade looked,
which is precisely the direction a self-learning system must not drift. Requiring
the cutoff turns "I forgot to truncate" from a wrong number into a ``TypeError``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from atr.analytics.models import AttributionLeg, TradeDetails

#: Basis points in one unit of price ratio. Named so the arithmetic below reads
#: as the definition rather than as a magic number.
_BPS = 10_000.0


@dataclass(frozen=True)
class ExcursionSet:
    """MAE and MFE for one closed trade, in rupees, percent and risk multiples.

    Sign convention, stated because a sign convention nobody writes down is one
    that gets flipped: **MFE is favourable-positive and MAE is adverse-negative**,
    both in the direction of the position. A long trade that ran +2% before
    closing at +1% has ``mfe_pct = 2.0`` and, if its worst point was −1%,
    ``mae_pct = -1.0``. A short trade is mirrored: a price *fall* is favourable.

    ``rupees`` figures are the percent figure applied to the position's entry
    value, so they answer "how much money was on the table" rather than "how far
    did the price move". ``None`` whenever the input needed is absent — never 0.0.
    """

    mfe_pct: float | None
    mae_pct: float | None
    mfe_amount: float | None
    mae_amount: float | None
    #: MFE / initial planned risk. The number that answers "did this trade ever
    #: pay enough to justify the risk it took", which is a different question from
    #: "did it profit" — a 1R win that once reached 3R is a management problem,
    #: and a 1R win that never exceeded 1R is a thesis that barely worked.
    mfe_over_risk: float | None
    #: Realised P&L / initial planned risk. The R multiple.
    realized_over_risk: float | None
    #: How many bars the excursion scan actually saw. Zero means the series was
    #: unavailable, which is why every figure above is ``None`` — not because the
    #: trade did not move.
    bars_observed: int
    #: Late in the trade, ``close`` at the last bar at or before the cutoff.
    terminal_price: float | None = None
    #: True when the scan was cut off by ``bars_until`` rather than reaching the
    #: exit naturally. There is currently no way to trigger this (the cutoff *is*
    #: the exit) but the field exists so a future open-trade caller cannot quietly
    #: present a partial excursion as a complete one.
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction(side: str) -> float:
    """``+1`` for a long, ``-1`` for a short. Unknown sides are treated as long."""
    return -1.0 if str(side or "").strip().upper() in ("SELL", "S") else 1.0


def _truncate(bars: Any, *, start: Any, until: Any) -> list[dict[str, Any]]:
    """Bars in ``[start, until]``, as plain dicts, oldest first.

    Both bounds are inclusive. ``start`` is inclusive because the entry bar's own
    high/low are part of what the trade saw — a position opened at the open of a
    bar can trade its low in the same bar. ``until`` is inclusive for the mirror
    reason: the exit bar's extreme happened while the position was still open.

    The frames in this project are already sorted by the history loader, but this
    sorts anyway: an unsorted frame makes a "last bar" operation silently return
    the wrong row, and that failure is invisible in every summary statistic.
    """
    if bars is None:
        return []
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is a hard dependency
        return []

    frame = bars if isinstance(bars, pd.DataFrame) else pd.DataFrame(bars)
    if frame is None or getattr(frame, "empty", True):
        return []
    if "ts" not in frame.columns:
        return []

    work = frame.copy()
    work["ts"] = pd.to_datetime(work["ts"], errors="coerce")
    work = work.dropna(subset=["ts"]).sort_values("ts")

    # Timezone mismatches compare as False against everything, which yields an
    # empty window and reads as "no data" rather than "we compared wrong types".
    # Both sides are normalised to naive before the comparison.
    bound_start = pd.to_datetime(start, errors="coerce") if start is not None else None
    bound_until = pd.to_datetime(until, errors="coerce") if until is not None else None
    if bound_start is not None and bound_start.tzinfo is not None:
        bound_start = bound_start.tz_localize(None)
    if bound_until is not None and bound_until.tzinfo is not None:
        bound_until = bound_until.tz_localize(None)
    if getattr(work["ts"].dtype, "tz", None) is not None:
        work["ts"] = work["ts"].dt.tz_localize(None)

    if bound_start is not None:
        work = work[work["ts"] >= bound_start]
    if bound_until is not None:
        work = work[work["ts"] <= bound_until]
    return [dict(row) for _, row in work.iterrows()]


def compute_excursions(
    bars: Any,
    *,
    entry_price: float | None,
    entry_ts: Any,
    side: str,
    quantity: float | None = None,
    bars_until: Any,
    planned_risk_amount: float | None = None,
    net_pnl: float | None = None,
) -> ExcursionSet:
    """MAE and MFE over exactly ``[entry_ts, bars_until]``.

    ``bars_until`` is keyword-only with no default, deliberately: see the module
    docstring. It is the exit timestamp for a closed trade.

    The extremes are taken from each bar's **high and low**, not its close. A
    close-to-close excursion understates both figures — the whole value of MAE is
    the *worst* the position was marked, and a position whose low was below every
    close it printed was still stopped out at that low. Using closes would report
    a survivable drawdown for a trade that was actually stopped.

    Bars with a missing or non-positive ``high``/``low`` are skipped rather than
    coerced to zero: a zero low on a long trade manufactures a −100% MAE, which
    would put a fabricated disaster into the dataset.
    """
    out = ExcursionSet(
        mfe_pct=None,
        mae_pct=None,
        mfe_amount=None,
        mae_amount=None,
        mfe_over_risk=None,
        realized_over_risk=None,
        bars_observed=0,
    )
    window = _truncate(bars, start=entry_ts, until=bars_until)
    if not window or not entry_price:
        return out

    entry = float(entry_price)
    if entry <= 0:
        return out
    sign = _direction(side)

    best: float | None = None
    worst: float | None = None
    last_close: float | None = None
    # ``best`` and ``worst`` are the *favourable* and *adverse* extremes for this
    # position, not the highest high and lowest low. For a long the two coincide —
    # the high is favourable and the low is adverse. For a short they invert: a
    # short profits when the price falls, so the **low** is the favourable extreme
    # and the **high** is the adverse one, and the favourable extreme is the
    # *minimum* of the lows rather than the maximum.
    #
    # Getting this wrong is not a rounding error: tracking max-high/min-low
    # regardless of direction and then subtracting labels a short's best mark as
    # its worst, so a losing short reports a favourable excursion.
    for bar in window:
        high = _positive(bar.get("high"))
        low = _positive(bar.get("low"))
        if sign > 0:
            if high is not None and (best is None or high > best):
                best = high
            if low is not None and (worst is None or low < worst):
                worst = low
        else:
            if low is not None and (best is None or low < best):
                best = low
            if high is not None and (worst is None or high > worst):
                worst = high
        close = _positive(bar.get("close"))
        if close is not None:
            last_close = close

    if best is None or worst is None:
        # Every bar was unusable. Reported as unobserved rather than as a flat
        # trade: "we could not measure it" and "it did not move" are different
        # claims and only one of them is true here.
        return ExcursionSet(
            mfe_pct=None,
            mae_pct=None,
            mfe_amount=None,
            mae_amount=None,
            mfe_over_risk=None,
            realized_over_risk=None,
            bars_observed=0,
            terminal_price=last_close,
        )

    # ``best`` and ``worst`` are the *favourable* and *adverse* extremes in price
    # terms for this position, so a short's ``best`` is a low and its ``worst`` is
    # a high. The move each represents still has to be signed from the entry, and
    # the sign of a price fall depends on which side of the book you are on: it is
    # a gain for a short. Both moves therefore run through ``_direction``, which
    # leaves ``favourable_move`` at or above zero and ``adverse_move`` at or below
    # it for either direction — the convention the ``ExcursionSet`` documents.
    favourable_move = (best - entry) * sign
    adverse_move = (worst - entry) * sign

    mfe_pct = favourable_move / entry * 100.0
    mae_pct = adverse_move / entry * 100.0

    mfe_amount = favourable_move * float(quantity) if quantity else None
    mae_amount = adverse_move * float(quantity) if quantity else None

    mfe_over_risk = None
    if mfe_amount is not None and planned_risk_amount:
        mfe_over_risk = mfe_amount / float(planned_risk_amount)

    realized_over_risk = None
    if net_pnl is not None and planned_risk_amount:
        realized_over_risk = float(net_pnl) / float(planned_risk_amount)

    return ExcursionSet(
        mfe_pct=round(mfe_pct, 4),
        mae_pct=round(mae_pct, 4),
        mfe_amount=round(mfe_amount, 4) if mfe_amount is not None else None,
        mae_amount=round(mae_amount, 4) if mae_amount is not None else None,
        mfe_over_risk=round(mfe_over_risk, 4) if mfe_over_risk is not None else None,
        realized_over_risk=(
            round(realized_over_risk, 4) if realized_over_risk is not None else None
        ),
        bars_observed=len(window),
        terminal_price=last_close,
    )


def _positive(value: Any) -> float | None:
    """A usable price, or ``None``. Zero and negatives are not prices."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


# ---------------------------------------------------------------------------
# Execution quality — no bars are read here, and that is the point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionQuality:
    """How well the orders were filled, measured only from the legs themselves.

    Every figure below is derived from *expected versus actual* prices and from
    *timestamps of events that had already happened*. No function in this section
    receives a price series, so none of them can be contaminated by what the
    market did afterwards — the "good entry" judgement is "we paid at or below
    the price our own decision named", which is the only version of that judgement
    that is knowable at the time and therefore the only one worth recording.

    Slippage is **adverse-positive**, matching ``atr.execution.oms.slippage_bps``:
    a positive number always means the fill cost money. A favourable fill is
    negative, and is kept negative rather than clamped, because "we got a better
    price than we asked for" is real execution quality and clamping it to zero
    would hide it.
    """

    entry_slippage_bps: float | None
    exit_slippage_bps: float | None
    #: Mean of the measurable legs — the round-trip figure. One number, averaged
    #: over the legs that have one.
    total_slippage_bps: float | None
    #: Adverse-positive rupees, summed over the measurable legs.
    total_slippage_amount: float | None
    #: All-in transaction cost in rupees, as recorded by the venue.
    transaction_costs: float | None
    #: Cost as a percentage of the position's entry value.
    execution_cost_pct: float | None

    signal_to_order_sec: float | None
    order_to_fill_sec: float | None
    #: Order creation → fill. Not the same as ``order_to_fill_sec`` when several
    #: fills happened; that field covers the first.
    order_to_last_fill_sec: float | None
    entry_to_exit_sec: float | None

    #: Realised move as a percentage of the position's direction.
    realized_move_pct: float | None
    #: The move that was *available* — the largest favourable excursion. Equal to
    #: MFE by construction; carried here so the efficiency ratio is readable
    #: without cross-referencing.
    theoretical_move_pct: float | None
    #: ``realized / theoretical`` where the denominator is favourable. ``None``
    #: when the trade never went favourable, because dividing by a favourable move
    #: of zero — or of the wrong sign — produces a ratio that reads as "0%
    #: captured" for a trade that simply never had anything to capture.
    capture_efficiency_pct: float | None
    #: How many legs had a measurable reference price. Zero means every slippage
    #: figure above is ``None``, which a reader needs to distinguish from "no
    #: slippage occurred".
    measurable_legs: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def leg_slippage_bps(leg: AttributionLeg) -> float | None:
    """Adverse-positive basis points for one leg, or ``None`` if unmeasurable.

    ``None`` rather than ``0.0`` when the expected price was never recorded: an
    unmeasurable slippage is not a zero-slippage fill, and averaging a zero in
    would drag a round trip's figure toward a friction that was never observed.
    """
    expected = _positive(leg.expected_price)
    actual = _positive(leg.actual_price)
    if expected is None or actual is None:
        return None
    raw = (actual - expected) / expected
    if str(leg.side or "").strip().upper() in ("SELL", "S"):
        raw = -raw
    return raw * _BPS


def leg_slippage_amount(leg: AttributionLeg) -> float | None:
    """Adverse-positive rupees for one leg.

    Per-unit adverse difference times **filled** quantity, not requested: slippage
    is charged on what actually traded, so an order that got a third of its size
    at a bad price incurred a third of the cost. Using the requested quantity
    would overstate the cost of every partially filled leg.
    """
    expected = _positive(leg.expected_price)
    actual = _positive(leg.actual_price)
    filled = leg.filled_qty
    if expected is None or actual is None or not filled:
        return None
    sign = -1.0 if str(leg.side or "").strip().upper() in ("SELL", "S") else 1.0
    per_unit = (actual - expected) * sign
    return per_unit * float(filled)


def _delta_sec(later: Any, earlier: Any) -> float | None:
    """``later - earlier`` in seconds, or ``None`` when either is missing.

    Floored at zero: a negative delay means the timestamps came from two different
    clocks, and reporting a negative delay as a measurement would be worse than
    reporting nothing.
    """
    if later is None or earlier is None:
        return None
    try:
        import pandas as pd

        a = pd.to_datetime(later, errors="coerce")
        b = pd.to_datetime(earlier, errors="coerce")
        if pd.isna(a) or pd.isna(b):
            return None
        # Comparing an aware stamp with a naive one raises; normalise to naive.
        if getattr(a, "tzinfo", None) is not None:
            a = a.tz_localize(None)
        if getattr(b, "tzinfo", None) is not None:
            b = b.tz_localize(None)
        return max(0.0, float((a - b).total_seconds()))
    except Exception:  # noqa: BLE001 - unparseable timestamps are "no measurement"
        return None


def execution_quality(
    *,
    trade: TradeDetails,
    entry_leg: AttributionLeg,
    exit_leg: AttributionLeg | None,
    excursions: ExcursionSet | None = None,
) -> ExecutionQuality:
    """Slippage, delay, cost and capture for one closed trade.

    ``excursions`` is optional and used for one field only — the theoretical move
    available, which is MFE by definition. Everything else is computed from the
    legs. Passing it is what allows ``capture_efficiency_pct``, and not passing it
    costs only that field; nothing about entry quality depends on it.
    """
    legs = [entry_leg] + ([exit_leg] if exit_leg is not None else [])
    per_leg_bps = [leg_slippage_bps(leg) for leg in legs]
    measured = [value for value in per_leg_bps if value is not None]

    per_leg_amounts = [leg_slippage_amount(leg) for leg in legs]
    amount_legs = [value for value in per_leg_amounts if value is not None]

    total_slippage_bps = sum(measured) / len(measured) if measured else None
    total_slippage_amount = sum(amount_legs) if amount_legs else None

    costs = trade.transaction_costs
    cost_pct = None
    if costs is not None and trade.position_value:
        cost_pct = abs(float(costs)) / float(trade.position_value) * 100.0

    signal_to_order = entry_leg.signal_to_order_sec
    if signal_to_order is None:
        signal_to_order = _delta_sec(entry_leg.order_ts, entry_leg.signal_ts)
    order_to_fill = entry_leg.order_to_fill_sec
    if order_to_fill is None:
        order_to_fill = _delta_sec(entry_leg.fill_ts, entry_leg.order_ts)

    entry_to_exit = None
    if trade.entry_ts is not None and trade.exit_ts is not None:
        entry_to_exit = _delta_sec(trade.exit_ts, trade.entry_ts)

    realized_move_pct = None
    entry_price = _positive(trade.entry_price)
    exit_price = _positive(trade.exit_price)
    if entry_price is not None and exit_price is not None:
        sign = _direction(trade.side)
        realized_move_pct = (exit_price / entry_price - 1.0) * 100.0 * sign

    theoretical = excursions.mfe_pct if excursions is not None else None

    capture = None
    if (
        realized_move_pct is not None
        and theoretical is not None
        and theoretical > 0
    ):
        # Capped at neither end. A capture above 100% is possible and honest — it
        # means the exit beat the best mark the scan observed, which happens when
        # a fill lands beyond the bar high the scan saw (a gap on the exit). A
        # negative capture is equally honest: a favourable trade given back to a
        # loss. Clamping either would delete the two most informative cases.
        capture = realized_move_pct / theoretical * 100.0

    return ExecutionQuality(
        entry_slippage_bps=_round(per_leg_bps[0]),
        exit_slippage_bps=_round(per_leg_bps[1]) if exit_leg is not None else None,
        total_slippage_bps=_round(total_slippage_bps),
        total_slippage_amount=_round(total_slippage_amount),
        transaction_costs=_round(costs),
        execution_cost_pct=_round(cost_pct),
        signal_to_order_sec=_round(signal_to_order),
        order_to_fill_sec=_round(order_to_fill),
        order_to_last_fill_sec=(
            _round(_delta_sec(exit_leg.fill_ts, exit_leg.order_ts))
            if exit_leg is not None
            else _round(_delta_sec(entry_leg.fill_ts, entry_leg.order_ts))
        ),
        entry_to_exit_sec=_round(entry_to_exit),
        realized_move_pct=_round(realized_move_pct),
        theoretical_move_pct=_round(theoretical),
        capture_efficiency_pct=_round(capture),
        measurable_legs=sum(1 for value in per_leg_bps if value is not None),
    )


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


__all__ = [
    "ExecutionQuality",
    "ExcursionSet",
    "compute_excursions",
    "execution_quality",
    "leg_slippage_amount",
    "leg_slippage_bps",
]
