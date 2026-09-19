"""The attribution object: the TRADE tree, its reason codes, and its classifier.

The tree
--------

Every closed trade is described by the same nine-branch structure, because a P&L
number on its own answers no question anybody has::

    TRADE
     ├── Signal            what fired, and whether it was a real signal
     ├── Market Context    the regime the signal fired into
     ├── Sector Context    the sector's strength and the stock's strength within it
     ├── Entry             what the decision asked for vs what the fill got
     ├── Position Sizing   how the size was chosen, and whether a cap bound it
     ├── Risk              planned risk, realised risk, and the R multiple
     ├── Execution         slippage, delay, cost — the venue's contribution
     ├── Exit              why it ended, and how much of the move it kept
     └── Outcome           the money

The nine names are stable identifiers, not display strings; the frontend renders
its own labels from them.

Determinism
-----------

:func:`attribute_trade` is a pure function of an :class:`AttributionInput` and a
threshold dictionary. It reads no clock, no database, no environment variable. Two
calls with the same input produce byte-identical output, which is what makes the
row idempotent: re-attributing a trade replaces the row with the same values
rather than accumulating a second, subtly different one.

Classification, never prediction
--------------------------------

Each code is emitted from a measurable comparison against a stated threshold, and
the comparison is recorded alongside the code in ``reason_detail``. *"``POOR_ENTRY``
because entry slippage was +38.2bps against a 15.0bps threshold"* is a sentence a
reader can check. ``POOR_ENTRY`` on its own is an opinion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from atr.analytics import reason_codes as rc
from atr.analytics.excursions import (
    ExecutionQuality,
    ExcursionSet,
    compute_excursions,
    execution_quality,
)
from atr.analytics.models import AttributionInput, AttributionLeg, TradeDetails

# ---------------------------------------------------------------------------
# Thresholds — declared once, injectable, and recorded with every row
# ---------------------------------------------------------------------------

#: The comparisons that turn a measurement into a classification. Kept as a
#: plain dict rather than constants scattered through the classifier so that a
#: stored row's codes can be re-derived exactly from its stored inputs. Callers
#: may override any of them; whatever was used is written into the row.
DEFAULT_THRESHOLDS: dict[str, float] = {
    #: Round-trip slippage above this is materially adverse. 15bps is roughly one
    #: tick on a mid-cap and a rounding error on a large cap, which is why the
    #: frontend also shows the per-symbol breakdown: a single threshold cannot be
    #: right for the whole universe, and pretending otherwise would be a
    #: classification that is precise about nothing.
    "slippage_high_bps": 15.0,
    #: Below this, slippage is noise.
    "slippage_low_bps": 5.0,
    #: MFE above this multiple of planned risk means the trade went somewhere.
    "favorable_mfe_r": 1.5,
    #: MAE above this multiple of planned risk means it went somewhere painful.
    "high_mae_r": 0.75,
    #: Realised risk wider than planned by this factor.
    "risk_wide_factor": 1.25,
    #: Realised risk tighter than planned by this factor.
    "risk_tight_factor": 0.75,
    #: Exit capturing less than this share of the available move, when a move was
    #: available, is an early exit.
    "capture_early_pct": 40.0,
    #: Giving back more than this share of the peak, when the peak was materially
    #: above the entry, is a late exit.
    "giveback_late_pct": 50.0,
    #: A peak below this multiple of risk is not "a move that was given back" —
    #: there was nothing to give back, and calling it a late exit would blame the
    #: exit for the thesis.
    "giveback_min_peak_r": 0.5,
    #: Position above this share of the sizing cap is oversized.
    "sizing_oversize_pct": 105.0,
    #: Position below this share of the intended size is undersized.
    "sizing_undersize_pct": 80.0,
    #: A context score at or above this is supportive.
    "context_positive_score": 40.0,
    #: A context score below this is unsupportive.
    "context_negative_score": 40.0,
}


# ---------------------------------------------------------------------------
# The nine branches
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalBranch:
    """What raised the trade."""

    signal_id: str | None
    strategy_id: str | None
    strategy_version: int | None
    reason: str | None
    setup: str | None
    present: bool
    positive: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextBranch:
    """The market and sector backdrop the signal fired into."""

    market_regime: str | None
    context_score: int | None
    context_class: str | None
    context_model_version: str | None
    sector: str | None
    sector_strength: float | None
    stock_relative_strength: float | None
    rvol: float | None
    atr_pct: float | None
    supportive: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntryBranch:
    """The decision's price against the fill's price."""

    expected_price: float | None
    actual_price: float | None
    slippage_bps: float | None
    slippage_amount: float | None
    signal_to_order_sec: float | None
    order_to_fill_sec: float | None
    partial: bool
    fill_ratio: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SizingBranch:
    """How the size was chosen, and whether a recorded cap bound it."""

    method: str | None
    cap_reason: str | None
    cap_value: float | None
    quantity: float
    position_value: float | None
    oversized: bool | None
    undersized: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskBranch:
    """Planned risk against the risk that was actually taken."""

    stop_price: float | None
    planned_risk_amount: float | None
    planned_risk_pct: float | None
    realized_risk_pct: float | None
    mfe_over_risk: float | None
    realized_over_risk: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionBranch:
    """The venue's contribution, in its own branch so it cannot be confused
    with the strategy's."""

    entry_slippage_bps: float | None
    exit_slippage_bps: float | None
    total_slippage_bps: float | None
    total_slippage_amount: float | None
    transaction_costs: float | None
    cost_pct: float | None
    measurable_legs: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExitBranch:
    """Why it ended, and how much of the available move it kept."""

    reason: str | None
    exit_price: float | None
    realized_move_pct: float | None
    theoretical_move_pct: float | None
    capture_efficiency_pct: float | None
    holding_sec: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutcomeBranch:
    """The money. Last, because every branch above is an explanation of it."""

    gross_pnl: float | None
    net_pnl: float | None
    gross_return_pct: float | None
    net_return_pct: float | None
    costs_as_pct_of_gross: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TradeAttribution:
    """One closed trade, attributed across the nine branches, with its codes."""

    trade_id: str
    symbol: str
    side: str
    source: str
    evidence_grade: str
    simulated: bool

    signal: SignalBranch
    context: ContextBranch
    entry: EntryBranch
    sizing: SizingBranch
    risk: RiskBranch
    execution: ExecutionBranch
    exit: ExitBranch
    outcome: OutcomeBranch

    excursions: ExcursionSet
    #: Every code this row earned, in a fixed canonical order so two runs produce
    #: an identical list and a diff of two rows is meaningful.
    reason_codes: list[str] = field(default_factory=list)
    #: Code → the measurement and threshold that produced it. A code with no
    #: stated basis is a label a reader has to take on trust.
    reason_detail: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Inputs that could not be resolved, with the reason.
    missing_fields: dict[str, str] = field(default_factory=dict)
    #: The thresholds used. Stored so the classification is reproducible from the
    #: row alone, even after the defaults change.
    thresholds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "source": self.source,
            "evidence_grade": self.evidence_grade,
            "simulated": self.simulated,
            "tree": {
                "Signal": self.signal.as_dict(),
                "Market Context": self.context.as_dict(),
                "Sector Context": {
                    "sector": self.context.sector,
                    "sector_strength": self.context.sector_strength,
                    "stock_relative_strength": self.context.stock_relative_strength,
                },
                "Entry": self.entry.as_dict(),
                "Position Sizing": self.sizing.as_dict(),
                "Risk": self.risk.as_dict(),
                "Execution": self.execution.as_dict(),
                "Exit": self.exit.as_dict(),
                "Outcome": self.outcome.as_dict(),
            },
            "signal": self.signal.as_dict(),
            "context": self.context.as_dict(),
            "entry": self.entry.as_dict(),
            "sizing": self.sizing.as_dict(),
            "risk": self.risk.as_dict(),
            "execution": self.execution.as_dict(),
            "exit": self.exit.as_dict(),
            "outcome": self.outcome.as_dict(),
            "excursions": self.excursions.as_dict(),
            "reason_codes": list(self.reason_codes),
            "reason_detail": dict(self.reason_detail),
            "missing_fields": dict(self.missing_fields),
            "thresholds": dict(self.thresholds),
        }

    def codes_of_family(self, family: str) -> list[str]:
        return [c for c in self.reason_codes if rc.family_of(c) == family]

    # ── flat accessors, for the dataset row ──────────────────────────────────
    def flat(self) -> dict[str, Any]:
        """The row fields the learning dataset stores.

        Deliberately a subset and deliberately flat. The learning dataset is a
        table, and a table's columns are what an axis can slice on; the nested
        tree stays available on the API for a reader, but only these fields get
        a column, because a column that no axis reads is a column that rots.
        """
        return {
            "attribution_entry_quality": _quality_label(self.entry.slippage_bps, self.thresholds),
            "attribution_execution_quality": _execution_label(self.execution.total_slippage_bps, self.thresholds),
            "attribution_exit_quality": self.exit.capture_efficiency_pct,
            "attribution_entry_slippage_bps": self.entry.slippage_bps,
            "attribution_exit_slippage_bps": self.execution.exit_slippage_bps,
            "attribution_total_slippage_bps": self.execution.total_slippage_bps,
            "attribution_transaction_costs": self.execution.transaction_costs,
            "attribution_cost_pct": self.execution.cost_pct,
            "attribution_signal_to_order_sec": self.entry.signal_to_order_sec,
            "attribution_order_to_fill_sec": self.entry.order_to_fill_sec,
            "attribution_holding_sec": self.exit.holding_sec,
            "attribution_partial_fill": self.entry.partial,
            "attribution_fill_ratio": self.entry.fill_ratio,
            "attribution_sizing_method": self.sizing.method,
            "attribution_sizing_cap_reason": self.sizing.cap_reason,
            "attribution_realized_risk_pct": self.risk.realized_risk_pct,
            "attribution_planned_risk_amount": self.risk.planned_risk_amount,
            "attribution_mfe_over_risk": self.excursions.mfe_over_risk,
            "attribution_realized_over_risk": self.excursions.realized_over_risk,
            "attribution_capture_efficiency_pct": self.exit.capture_efficiency_pct,
            "attribution_context_score": self.context.context_score,
            "attribution_context_class": self.context.context_class,
            "attribution_sector_strength": self.context.sector_strength,
            "attribution_stock_relative_strength": self.context.stock_relative_strength,
            "attribution_rvol": self.context.rvol,
            "attribution_atr_pct": self.context.atr_pct,
            "attribution_market_regime": self.context.market_regime,
            "attribution_reason_codes": ",".join(self.reason_codes),
        }


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


class _Codes:
    """Accumulates codes and their stated basis, in canonical order."""

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.detail: dict[str, dict[str, Any]] = {}

    def add(self, code: str, **basis: Any) -> None:
        if code in self.detail:
            return
        self.codes.append(code)
        self.detail[code] = basis

    def ordered(self) -> list[str]:
        """Canonical order: the order the vocabulary declares, not the order the
        comparisons happened to run. Two runs of the same input must produce an
        identical list, or the row is not deterministic in any useful sense."""
        position = {code: index for index, code in enumerate(rc.ALL_REASON_CODES)}
        return sorted(self.codes, key=lambda c: position.get(c, len(position)))


def attribute_trade(
    payload: AttributionInput,
    *,
    thresholds: dict[str, float] | None = None,
) -> TradeAttribution:
    """Attribute one closed trade. Pure: same input, same output, always.

    ``thresholds`` merges over :data:`DEFAULT_THRESHOLDS`; the merged set is
    recorded on the result so a stored row can be re-derived later without
    knowing what the defaults were at the time.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update({k: float(v) for k, v in thresholds.items()})

    trade = payload.trade
    entry_leg = payload.entry_leg
    exit_leg = payload.exit_leg
    missing = dict(trade.missing_fields)

    excursions = compute_excursions(
        payload.bars_after_entry,
        entry_price=trade.entry_price,
        entry_ts=trade.entry_ts,
        side=trade.side,
        quantity=trade.quantity,
        bars_until=payload.bars_until,
        planned_risk_amount=trade.planned_risk_amount,
        net_pnl=trade.net_pnl,
    )
    if excursions.bars_observed == 0:
        missing.setdefault("mfe", "no usable bars between entry and exit")
        missing.setdefault("mae", "no usable bars between entry and exit")

    quality = execution_quality(
        trade=trade,
        entry_leg=entry_leg,
        exit_leg=exit_leg,
        excursions=excursions,
    )

    codes = _Codes()

    # ── Signal ───────────────────────────────────────────────────────────────
    signal_present = bool(trade.signal_id or trade.strategy_id)
    # A signal is "positive" when the platform can show one existed and carried a
    # reason. It is deliberately not a judgement about the signal's quality: that
    # is what the learning engine measures over many trades, and a per-trade
    # opinion about a rule's merit is exactly the emotional reading this system
    # exists to replace.
    signal_positive = True if signal_present else None
    if signal_present:
        codes.add(rc.SIGNAL_POSITIVE, basis="strategy and signal recorded on the opening order")
    else:
        missing.setdefault("signal_id", "the trade could not be linked to a signal")
        codes.add(rc.INSUFFICIENT_DATA, basis="no signal linkage for this trade")

    signal = SignalBranch(
        signal_id=trade.signal_id,
        strategy_id=trade.strategy_id,
        strategy_version=trade.strategy_version,
        reason=None,
        setup=None,
        present=signal_present,
        positive=signal_positive,
    )

    # ── Context ──────────────────────────────────────────────────────────────
    supportive: bool | None = None
    if trade.context_score is not None:
        score = float(trade.context_score)
        if score >= limits["context_positive_score"]:
            supportive = True
            codes.add(
                rc.CONTEXT_POSITIVE,
                context_score=score,
                threshold=limits["context_positive_score"],
            )
        elif score < limits["context_negative_score"]:
            supportive = False
            codes.add(
                rc.CONTEXT_NEGATIVE,
                context_score=score,
                threshold=limits["context_negative_score"],
            )
        else:
            # At exactly the threshold the context is neither named supportive nor
            # unsupportive. Both codes would be a claim the number does not make.
            supportive = None
    else:
        missing.setdefault("context_score", "no recorded signal context for this trade")

    context = ContextBranch(
        market_regime=trade.market_regime,
        context_score=trade.context_score,
        context_class=trade.context_class,
        context_model_version=trade.context_model_version,
        sector=trade.sector,
        sector_strength=trade.sector_strength,
        stock_relative_strength=trade.stock_relative_strength,
        rvol=trade.rvol,
        atr_pct=trade.atr_pct,
        supportive=supportive,
    )

    # ── Entry ────────────────────────────────────────────────────────────────
    entry_slippage = quality.entry_slippage_bps
    if entry_slippage is not None:
        if entry_slippage > 0:
            codes.add(rc.POOR_ENTRY, entry_slippage_bps=entry_slippage, basis="filled worse than requested")
        else:
            codes.add(rc.GOOD_ENTRY, entry_slippage_bps=entry_slippage, basis="filled at or better than requested")
    entry = EntryBranch(
        expected_price=entry_leg.expected_price,
        actual_price=entry_leg.actual_price,
        slippage_bps=quality.entry_slippage_bps,
        slippage_amount=entry_leg.slippage_amount
        if entry_leg.slippage_amount is not None
        else None,
        signal_to_order_sec=quality.signal_to_order_sec,
        order_to_fill_sec=quality.order_to_fill_sec,
        partial=entry_leg.partial,
        fill_ratio=entry_leg.fill_ratio,
    )

    # ── Sizing ───────────────────────────────────────────────────────────────
    oversized: bool | None = None
    undersized: bool | None = None
    cap_value = trade.sizing_cap_value
    if cap_value is not None and float(cap_value) > 0 and trade.position_value is not None:
        ratio_pct = float(trade.position_value) / float(cap_value) * 100.0
        if ratio_pct > limits["sizing_oversize_pct"]:
            oversized = True
            codes.add(
                rc.OVERSIZED,
                position_value=trade.position_value,
                cap_value=float(cap_value),
                ratio_pct=round(ratio_pct, 4),
                threshold=limits["sizing_oversize_pct"],
            )
        elif ratio_pct < limits["sizing_undersize_pct"]:
            undersized = True
            codes.add(
                rc.UNDERSIZED,
                position_value=trade.position_value,
                cap_value=float(cap_value),
                ratio_pct=round(ratio_pct, 4),
                threshold=limits["sizing_undersize_pct"],
            )
    elif trade.sizing_cap_reason:
        # A cap was recorded as having bound the size but no cap value survived.
        # The fact is named and the magnitude is not measured, which is the
        # honest split: "the cap bound this" is a recorded fact, "by how much" is
        # not available.
        oversized = True
        codes.add(
            rc.OVERSIZED,
            sizing_cap_reason=trade.sizing_cap_reason,
            basis="a sizing cap was recorded as binding, with no cap value to measure against",
        )

    sizing = SizingBranch(
        method=trade.sizing_method,
        cap_reason=trade.sizing_cap_reason,
        cap_value=cap_value,
        quantity=trade.quantity,
        position_value=trade.position_value,
        oversized=oversized,
        undersized=undersized,
    )

    # ── Risk ─────────────────────────────────────────────────────────────────
    planned_risk_pct = None
    if trade.planned_risk_amount and trade.position_value:
        planned_risk_pct = (
            float(trade.planned_risk_amount) / float(trade.position_value) * 100.0
        )
    if trade.realized_risk_pct is not None and planned_risk_pct is not None:
        if trade.realized_risk_pct > planned_risk_pct * limits["risk_wide_factor"]:
            codes.add(
                rc.HIGH_RISK,
                realized_risk_pct=trade.realized_risk_pct,
                planned_risk_pct=round(planned_risk_pct, 4),
                factor=limits["risk_wide_factor"],
            )
        elif trade.realized_risk_pct < planned_risk_pct * limits["risk_tight_factor"]:
            codes.add(
                rc.LOW_RISK,
                realized_risk_pct=trade.realized_risk_pct,
                planned_risk_pct=round(planned_risk_pct, 4),
                factor=limits["risk_tight_factor"],
            )
    elif trade.stop_price is None:
        missing.setdefault("stop_price", "no protective stop recorded on this trade")

    risk = RiskBranch(
        stop_price=trade.stop_price,
        planned_risk_amount=trade.planned_risk_amount,
        planned_risk_pct=round(planned_risk_pct, 4) if planned_risk_pct is not None else None,
        realized_risk_pct=trade.realized_risk_pct,
        mfe_over_risk=excursions.mfe_over_risk,
        realized_over_risk=excursions.realized_over_risk,
    )

    # ── Execution ────────────────────────────────────────────────────────────
    total_slippage = quality.total_slippage_bps
    if total_slippage is not None and quality.measurable_legs:
        if total_slippage >= limits["slippage_high_bps"]:
            codes.add(
                rc.HIGH_SLIPPAGE,
                total_slippage_bps=total_slippage,
                threshold=limits["slippage_high_bps"],
                measurable_legs=quality.measurable_legs,
            )
        elif total_slippage <= limits["slippage_low_bps"]:
            codes.add(
                rc.LOW_SLIPPAGE,
                total_slippage_bps=total_slippage,
                threshold=limits["slippage_low_bps"],
                measurable_legs=quality.measurable_legs,
            )
    else:
        missing.setdefault(
            "slippage_bps",
            "no leg carried a reference price, so slippage was not measurable",
        )

    mfe_r = excursions.mfe_over_risk
    if mfe_r is not None:
        if mfe_r >= limits["favorable_mfe_r"]:
            codes.add(
                rc.FAVORABLE_MFE,
                mfe_over_risk=mfe_r,
                threshold=limits["favorable_mfe_r"],
            )
        elif mfe_r < 0:
            # The position never traded above its entry at all. Worth naming,
            # because it is the clearest evidence that the entry timing — not the
            # exit — was the problem.
            codes.add(rc.HIGH_MAE, mfe_over_risk=mfe_r, basis="never traded favourable to entry")

    mae_r = None
    if excursions.mae_amount is not None and trade.planned_risk_amount:
        mae_r = excursions.mae_amount / float(trade.planned_risk_amount)
        if mae_r <= -limits["high_mae_r"]:
            codes.add(
                rc.HIGH_MAE,
                mae_over_risk=round(mae_r, 4),
                threshold=-limits["high_mae_r"],
            )

    execution = ExecutionBranch(
        entry_slippage_bps=quality.entry_slippage_bps,
        exit_slippage_bps=quality.exit_slippage_bps,
        total_slippage_bps=quality.total_slippage_bps,
        total_slippage_amount=quality.total_slippage_amount,
        transaction_costs=quality.transaction_costs,
        cost_pct=quality.execution_cost_pct,
        measurable_legs=quality.measurable_legs,
    )

    # ── Exit ─────────────────────────────────────────────────────────────────
    exit_cause = rc.EXIT_REASON_CODES.get(str(trade.exit_reason or "").strip().lower())
    if exit_cause:
        codes.add(exit_cause, exit_reason=trade.exit_reason)
    elif trade.exit_reason:
        # A recorded reason the vocabulary does not recognise. The raw string is
        # kept and no cause is invented for it.
        missing.setdefault("exit_cause", f"unrecognised exit reason {trade.exit_reason!r}")
    else:
        missing.setdefault("exit_reason", "no exit reason recorded on the closing order")

    capture = quality.capture_efficiency_pct
    theoretical = quality.theoretical_move_pct
    realized = quality.realized_move_pct

    if capture is not None:
        if capture >= 100.0:
            codes.add(rc.GOOD_EXIT, capture_efficiency_pct=capture, basis="captured the whole available move or more")
        elif capture < limits["capture_early_pct"]:
            codes.add(
                rc.EARLY_EXIT,
                capture_efficiency_pct=capture,
                threshold=limits["capture_early_pct"],
            )
        else:
            codes.add(rc.GOOD_EXIT, capture_efficiency_pct=capture, basis="captured most of the available move")
    elif theoretical is not None and theoretical <= 0:
        # The trade never went favourable, so there was no move to capture and no
        # efficiency to measure. Not an early exit — an early exit is a decision
        # that left value behind.
        missing.setdefault(
            "capture_efficiency_pct",
            "the trade never traded favourable to entry, so there was no move to capture",
        )

    # The late exit is a *separate* signal from the early exit and can co-occur:
    # a trade can end early relative to its target while also having given back
    # most of what it made on the way there.
    if (
        theoretical is not None
        and theoretical > 0
        and realized is not None
        and excursions.mfe_over_risk is not None
        and excursions.mfe_over_risk >= limits["giveback_min_peak_r"]
    ):
        giveback_pct = (1.0 - realized / theoretical) * 100.0
        if giveback_pct > limits["giveback_late_pct"]:
            codes.add(
                rc.LATE_EXIT,
                realized_move_pct=realized,
                theoretical_move_pct=theoretical,
                giveback_pct=round(giveback_pct, 4),
                threshold=limits["giveback_late_pct"],
            )

    exit_branch = ExitBranch(
        reason=trade.exit_reason,
        exit_price=trade.exit_price,
        realized_move_pct=realized,
        theoretical_move_pct=theoretical,
        capture_efficiency_pct=capture,
        holding_sec=trade.holding_duration_sec,
    )

    # ── Outcome ──────────────────────────────────────────────────────────────
    costs_pct_of_gross = None
    if quality.transaction_costs is not None and trade.gross_pnl:
        costs_pct_of_gross = (
            abs(float(quality.transaction_costs)) / abs(float(trade.gross_pnl)) * 100.0
        )
    outcome = OutcomeBranch(
        gross_pnl=trade.gross_pnl,
        net_pnl=trade.net_pnl,
        gross_return_pct=trade.gross_return_pct,
        net_return_pct=trade.net_return_pct,
        costs_as_pct_of_gross=round(costs_pct_of_gross, 4) if costs_pct_of_gross is not None else None,
    )

    return TradeAttribution(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        side=trade.side,
        source=trade.source,
        evidence_grade=trade.evidence_grade,
        simulated=trade.simulated,
        signal=signal,
        context=context,
        entry=entry,
        sizing=sizing,
        risk=risk,
        execution=execution,
        exit=exit_branch,
        outcome=outcome,
        excursions=excursions,
        reason_codes=codes.ordered(),
        reason_detail=codes.detail,
        missing_fields=dict(sorted(missing.items())),
        thresholds=limits,
    )


# ---------------------------------------------------------------------------
# Labels the dataset slices on
# ---------------------------------------------------------------------------


def _quality_label(slippage_bps: float | None, thresholds: dict[str, float]) -> str | None:
    if slippage_bps is None:
        return None
    if slippage_bps > 0:
        return "poor"
    return "good"


def _execution_label(slippage_bps: float | None, thresholds: dict[str, float]) -> str | None:
    if slippage_bps is None:
        return None
    high = thresholds.get("slippage_high_bps", DEFAULT_THRESHOLDS["slippage_high_bps"])
    low = thresholds.get("slippage_low_bps", DEFAULT_THRESHOLDS["slippage_low_bps"])
    if slippage_bps >= high:
        return "high_slippage"
    if slippage_bps <= low:
        return "low_slippage"
    return "normal"


def build_input(**kwargs: Any) -> AttributionInput:
    """Convenience constructor, kept for symmetry with the service's call site."""
    return AttributionInput(**kwargs)


__all__ = [
    "DEFAULT_THRESHOLDS",
    "ContextBranch",
    "EntryBranch",
    "ExecutionBranch",
    "ExitBranch",
    "OutcomeBranch",
    "RiskBranch",
    "SignalBranch",
    "SizingBranch",
    "TradeAttribution",
    "attribute_trade",
    "build_input",
]
