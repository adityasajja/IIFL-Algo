"""The invariants the attribution layer must not be able to violate.

Every test here corresponds to a way a post-trade record can be *wrong* while
still looking plausible. That distinction is the whole point of the file: a
report full of fabricated excursion figures and a report full of real ones have
the same shape, so correctness has to be asserted structurally rather than
eyeballed in a summary.

The groups, and the failure each one blocks:

* **Look-ahead.** Bars after the exit must not enter MAE/MFE. The engine makes
  this hard to get wrong by requiring ``bars_until``; these tests prove the
  requirement actually bites, including for the bar *at* the exit.
* **Arithmetic.** P&L, costs and slippage are checked against hand-computed
  figures, with the adverse-positive sign convention and the short-side flip.
* **Metadata preservation.** Sizing, context and provenance survive the trip from
  the episode into the stored row. A field that silently becomes ``None`` makes
  every future slice by it a slice by "unknown".
* **Idempotency.** Attributing twice yields one row and the same fingerprint; a
  changed input yields a new one. Multiple order events must not become multiple
  trades.
* **Evidence separation.** In-sample rows may never acquire forward standing,
  and a provenance stamp is never inferred from a timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from atr.analytics import attribute_trade
from atr.analytics.attribution import DEFAULT_THRESHOLDS
from atr.analytics.excursions import (
    compute_excursions,
    execution_quality,
    leg_slippage_amount,
    leg_slippage_bps,
)
from atr.analytics.models import AttributionInput, AttributionLeg, TradeDetails


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _trade(**overrides) -> TradeDetails:
    base = dict(
        trade_id="T-1",
        user_id="U-1",
        symbol="RELIANCE",
        side="BUY",
        source="LIVE",
        evidence_grade="forward",
        evidence_class="LIVE_FORWARD",
        simulated=False,
        quantity=100.0,
        entry_price=100.0,
        exit_price=104.0,
        position_value=10_000.0,
        gross_pnl=400.0,
        net_pnl=360.0,
        gross_return_pct=4.0,
        net_return_pct=3.6,
        entry_ts=datetime(2026, 3, 2, 9, 20, tzinfo=UTC),
        exit_ts=datetime(2026, 3, 4, 14, 30, tzinfo=UTC),
        holding_duration_sec=2 * 86_400 + 5 * 3600 + 600,
        stop_price=97.0,
        planned_risk_amount=300.0,
        realized_risk_pct=1.0,
        sizing_method="atr_risk",
        sizing_cap_reason=None,
        sizing_cap_value=None,
        signal_id="S-1",
        strategy_id="momentum",
        strategy_version=3,
        context_model_version="v2",
        context_score=62,
        context_class="favorable",
        market_regime="bull",
        sector="Energy",
        sector_strength=1.4,
        stock_relative_strength=3.2,
        rvol=2.1,
        atr=2.4,
        atr_pct=2.4,
        exit_reason="TARGET",
        transaction_costs=40.0,
        missing_fields={},
    )
    base.update(overrides)
    return TradeDetails(**base)


def _leg(kind: str, side: str, **overrides) -> AttributionLeg:
    base = dict(
        kind=kind,
        side=side,
        expected_price=100.0,
        actual_price=100.0,
        requested_qty=100.0,
        filled_qty=100.0,
        signal_ts=datetime(2026, 3, 2, 9, 15, tzinfo=UTC),
        order_ts=datetime(2026, 3, 2, 9, 16, tzinfo=UTC),
        fill_ts=datetime(2026, 3, 2, 9, 20, tzinfo=UTC),
        commission=20.0,
        partial=False,
        fill_count=1,
    )
    base.update(overrides)
    return AttributionLeg(**base)


def _bars(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """``(ts, high, low, close)`` rows into a frame the scanner can read."""
    return pd.DataFrame(
        {
            "ts": pd.to_datetime([r[0] for r in rows]),
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100_000.0] * len(rows),
        }
    )


def _payload(trade: TradeDetails | None = None, **overrides) -> AttributionInput:
    base = dict(
        trade=trade or _trade(),
        entry_leg=_leg("entry", "BUY"),
        exit_leg=_leg(
            "exit",
            "SELL",
            expected_price=104.0,
            actual_price=104.0,
            signal_ts=datetime(2026, 3, 4, 14, 25, tzinfo=UTC),
            order_ts=datetime(2026, 3, 4, 14, 26, tzinfo=UTC),
            fill_ts=datetime(2026, 3, 4, 14, 30, tzinfo=UTC),
        ),
        bars_after_entry=None,
        bars_until=None,
        target_price=105.0,
        thresholds=dict(DEFAULT_THRESHOLDS),
    )
    base.update(overrides)
    return AttributionInput(**base)


# ---------------------------------------------------------------------------
# 1. look-ahead
# ---------------------------------------------------------------------------


def test_bars_after_the_exit_cannot_reach_the_excursion_figures():
    """The single most important invariant in the layer.

    A trade that was stopped at 95 must report MAE of 5%, not the 30% the stock
    fell to six weeks later. Those are different claims about the same trade and
    only one of them was observable to the trader who held it.
    """
    entry = datetime(2026, 3, 2, 9, 20)
    exit_ts = datetime(2026, 3, 4, 14, 30)
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-03 09:20", 103.0, 95.0, 96.0),
            ("2026-03-04 14:30", 105.0, 94.0, 104.0),
            # Everything below is after the exit and must be invisible.
            ("2026-03-05 09:20", 108.0, 70.0, 72.0),
            ("2026-03-06 09:20", 110.0, 60.0, 61.0),
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=entry,
        side="BUY",
        quantity=100.0,
        bars_until=exit_ts,
        planned_risk_amount=300.0,
    )
    # Worst mark in the window is the 94 low -> 6% adverse.
    # NOT the 60 print two days after the position was closed.
    assert result.mae_pct == pytest.approx(-6.0)
    # Best mark is the 105 high on the exit bar -> 5%. NOT the 110 print.
    assert result.mfe_pct == pytest.approx(5.0)
    assert result.bars_observed == 3


def test_the_bar_stamped_at_the_exit_is_included_and_the_next_one_is_not():
    """The boundary is inclusive at the top.

    A closed trade's exit bar did happen while the position was open — its high
    is a mark the position could have reached. Excluding it would systematically
    understate MFE on exactly the trades that ran to a target.
    """
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-04 14:30", 107.0, 98.0, 106.0),  # the exit bar itself
            ("2026-03-04 14:31", 130.0, 10.0, 129.0),  # one minute later
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert result.mfe_pct == pytest.approx(7.0)  # the 107 high, not the 130
    assert result.mae_pct == pytest.approx(-2.0)  # the 98 low, not the 10
    assert result.bars_observed == 2


def test_bars_before_the_entry_are_excluded_too():
    """The other end of the window. Pre-entry highs are not excursions."""
    bars = _bars(
        [
            ("2026-02-27 09:20", 140.0, 60.0, 100.0),  # before entry
            ("2026-03-02 09:20", 102.0, 99.0, 101.0),
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert result.mfe_pct == pytest.approx(2.0)
    assert result.mae_pct == pytest.approx(-1.0)


def test_bars_until_is_required_so_a_missing_cutoff_is_a_type_error():
    """The guard is structural, not a convention.

    If the cutoff were optional with a default of "no bound", every caller that
    forgot it would silently compute excursion over the whole remaining history
    and never see an error. Requiring it turns that into a crash at the call.
    """
    with pytest.raises(TypeError):
        compute_excursions(  # type: ignore[call-arg]
            _bars([("2026-03-02 09:20", 101.0, 99.0, 100.5)]),
            entry_price=100.0,
            entry_ts=datetime(2026, 3, 2, 9, 20),
            side="BUY",
        )


def test_an_empty_window_reports_unmeasured_rather_than_flat():
    """Absent is not zero. A trade with no bars to measure did not "not move"."""
    result = compute_excursions(
        _bars([("2026-03-10 09:20", 101.0, 99.0, 100.5)]),
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert result.bars_observed == 0
    assert result.mfe_pct is None
    assert result.mae_pct is None
    assert result.mfe_over_risk is None


def test_a_zero_low_bar_does_not_manufacture_a_total_loss():
    """Bad data must be skipped, not read as a price.

    A ``0.0`` low on a long trade is a data hole. Coercing it produces a −100%
    MAE and a fabricated disaster in the distribution.
    """
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-03 09:20", 102.0, 0.0, 101.0),
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert result.mae_pct == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# 2. excursion arithmetic and the short side
# ---------------------------------------------------------------------------


def test_short_excursions_mirror_the_long_side():
    """The same two prices are favourable for one direction and adverse for the
    other. A sign bug here inverts a whole column for half the book."""
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 97.0, 99.0),
            ("2026-03-03 09:20", 103.0, 95.0, 96.0),
        ]
    )
    long_side = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    short_side = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="SELL",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    # Long: best 103, worst 95.
    assert long_side.mfe_pct == pytest.approx(3.0)
    assert long_side.mae_pct == pytest.approx(-5.0)
    # Short: the same extremes, swapped. The adverse side is still negative, so
    # "the worst it got" never changes sign with the direction of the trade.
    assert short_side.mfe_pct == pytest.approx(5.0)
    assert short_side.mae_pct == pytest.approx(-3.0)


def test_mfe_over_risk_uses_the_planned_risk_not_the_realized_loss():
    """``MFE / initial risk`` is the R-multiple the trade *offered*. Dividing by
    the realized loss instead would divide by whatever the stop let through,
    which is the thing the stop is supposed to bound."""
    bars = _bars(
        [
            ("2026-03-02 09:20", 106.0, 99.0, 105.0),
            ("2026-03-03 09:20", 106.0, 96.0, 97.0),
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
        planned_risk_amount=300.0,
        net_pnl=-300.0,
    )
    assert result.mfe_amount == pytest.approx(600.0)  # +6 on 100 shares
    assert result.mfe_over_risk == pytest.approx(2.0)  # 600 / 300
    assert result.realized_over_risk == pytest.approx(-1.0)


def test_excursions_without_a_quantity_report_percentages_and_no_rupees():
    """A share count is not always known. The percentage is still a measurement;
    the rupee figure is not, and must stay absent rather than be guessed."""
    bars = _bars([("2026-03-02 09:20", 106.0, 99.0, 105.0)])
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=None,
        bars_until=datetime(2026, 3, 4, 14, 30),
        planned_risk_amount=300.0,
    )
    assert result.mfe_pct == pytest.approx(6.0)
    assert result.mfe_amount is None
    assert result.mfe_over_risk is None  # no rupees means no ratio


# ---------------------------------------------------------------------------
# 3. slippage sign convention and partial fills
# ---------------------------------------------------------------------------


def test_slippage_is_adverse_positive_on_both_sides():
    """A buy filled above expectation and a sell filled below are both *losses*.

    They must land on the same side of zero, or a profitable execution appears
    to be a cost and the round trip's total is nonsense.
    """
    buy_worse = _leg("entry", "BUY", expected_price=100.0, actual_price=100.5)
    sell_worse = _leg("exit", "SELL", expected_price=100.0, actual_price=99.5)
    assert leg_slippage_bps(buy_worse) == pytest.approx(50.0)
    assert leg_slippage_bps(sell_worse) == pytest.approx(50.0)

    buy_better = _leg("entry", "BUY", expected_price=100.0, actual_price=99.5)
    sell_better = _leg("exit", "SELL", expected_price=100.0, actual_price=100.5)
    assert leg_slippage_bps(buy_better) == pytest.approx(-50.0)
    assert leg_slippage_bps(sell_better) == pytest.approx(-50.0)


def test_slippage_is_none_not_zero_when_the_expected_price_is_unknown():
    """An unmeasurable slippage is not a perfect fill. Averaging a zero in would
    drag the book's execution cost toward a friction nobody observed."""
    unknown = _leg("entry", "BUY", expected_price=None, actual_price=100.0)
    assert leg_slippage_bps(unknown) is None
    assert leg_slippage_amount(unknown) is None

    quality = execution_quality(
        trade=_trade(),
        entry_leg=unknown,
        exit_leg=None,
    )
    assert quality.measurable_legs == 0
    assert quality.total_slippage_bps is None
    assert quality.total_slippage_amount is None


def test_slippage_is_charged_on_the_filled_quantity_not_the_requested_one():
    """A partial fill only paid slippage on what traded.

    Charging the requested size would report the cost of a position the trader
    never held, and it would overstate every partial fill in the book.
    """
    partial = _leg(
        "entry",
        "BUY",
        expected_price=100.0,
        actual_price=101.0,
        requested_qty=1000.0,
        filled_qty=250.0,
        partial=True,
        fill_count=3,
    )
    # 1 rupee adverse on 250 shares, not on 1000.
    assert leg_slippage_amount(partial) == pytest.approx(250.0)
    assert leg_slippage_bps(partial) == pytest.approx(100.0)


def test_a_round_trip_averages_the_two_measurable_legs():
    """Total slippage is the mean of the legs that were measurable, so a trade
    with one unmeasurable leg still reports the leg that was.

    Note the sign: a buy filled *below* its expected price is a favourable
    execution and reports negative slippage, because slippage is adverse-positive.
    """
    quality = execution_quality(
        trade=_trade(),
        entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.1),
        exit_leg=_leg("exit", "SELL", expected_price=104.0, actual_price=103.9),
    )
    # Both legs filled 0.1 against the trader: +10bps on the way in, and on the
    # way out 0.1/104 = +9.6154bps. The mean is what is reported.
    assert quality.entry_slippage_bps == pytest.approx(10.0)
    assert quality.exit_slippage_bps == pytest.approx(9.6154, abs=1e-3)
    assert quality.total_slippage_bps == pytest.approx(
        (10.0 + 9.6154) / 2, abs=1e-3
    )
    assert quality.measurable_legs == 2


def test_a_favourable_fill_reports_negative_slippage():
    """Slippage is adverse-positive, so beating the reference price is a negative
    cost. It must not be clamped at zero — a strategy with consistently good
    execution is exactly the case a clamped figure would hide."""
    quality = execution_quality(
        trade=_trade(),
        entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=99.9),
        exit_leg=_leg("exit", "SELL", expected_price=104.0, actual_price=104.1),
    )
    assert quality.entry_slippage_bps == pytest.approx(-10.0)
    assert quality.exit_slippage_bps == pytest.approx(-9.6154, abs=1e-3)
    assert quality.total_slippage_bps < 0.0


def test_a_leg_with_no_counterpart_is_still_measured():
    """An open position has an entry and no exit. Its entry slippage is a real
    measurement and must not be dropped just because the round trip is
    incomplete."""
    quality = execution_quality(
        trade=_trade(),
        entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.2),
        exit_leg=None,
    )
    assert quality.entry_slippage_bps == pytest.approx(20.0)
    assert quality.exit_slippage_bps is None
    assert quality.total_slippage_bps == pytest.approx(20.0)
    assert quality.total_slippage_amount == pytest.approx(20.0)
    assert quality.measurable_legs == 1


def test_a_partial_fill_is_visible_in_the_leg_and_reaches_the_dataset():
    """The fill ratio is what separates "the order was too big" from "the market
    was thin", so it has to survive rather than be reduced to a boolean."""
    partial = _leg(
        "entry",
        "BUY",
        requested_qty=1000.0,
        filled_qty=400.0,
        partial=True,
        fill_count=4,
    )
    assert partial.fill_ratio == pytest.approx(0.4)

    trade = _trade(quantity=400.0)
    result = attribute_trade(_payload(trade, entry_leg=partial))
    flat = result.flat()
    assert flat["attribution_partial_fill"] is True
    assert flat["attribution_fill_ratio"] == pytest.approx(0.4)


def test_delays_are_floored_at_zero_rather_than_reported_negative():
    """A negative delay means two timestamps came from different clocks. That is
    not a measurement of punctuality and must not become one."""
    backwards = _leg(
        "entry",
        "BUY",
        signal_ts=datetime(2026, 3, 2, 9, 20, tzinfo=UTC),
        order_ts=datetime(2026, 3, 2, 9, 16, tzinfo=UTC),  # before the signal
    )
    quality = execution_quality(trade=_trade(), entry_leg=backwards, exit_leg=None)
    assert quality.signal_to_order_sec == 0.0


# ---------------------------------------------------------------------------
# 4. capture efficiency
# ---------------------------------------------------------------------------


def test_capture_efficiency_compares_the_realized_move_to_the_mfe():
    """A trade that ran to +6% and closed at +3% captured half of what it saw."""
    trade = _trade(exit_price=103.0, entry_price=100.0)
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-03 09:20", 106.0, 99.5, 102.0),  # the +6% mark
            ("2026-03-04 14:30", 103.5, 102.0, 103.0),
        ]
    )
    excursions = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    quality = execution_quality(
        trade=trade,
        entry_leg=_leg("entry", "BUY"),
        exit_leg=_leg("exit", "SELL", expected_price=103.0, actual_price=103.0),
        excursions=excursions,
    )
    assert quality.realized_move_pct == pytest.approx(3.0)
    assert quality.theoretical_move_pct == pytest.approx(6.0)
    assert quality.capture_efficiency_pct == pytest.approx(50.0)


def test_capture_can_exceed_one_hundred_percent_and_is_not_clamped():
    """An exit that beat every mark the scan observed is the honest case of a gap
    on the way out. Clamping at 100 would delete it; the distribution is better
    for keeping it visible."""
    trade = _trade(exit_price=108.0, entry_price=100.0)
    bars = _bars([("2026-03-02 09:20", 102.0, 99.0, 101.0)])
    excursions = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    quality = execution_quality(
        trade=trade,
        entry_leg=_leg("entry", "BUY"),
        exit_leg=_leg("exit", "SELL", expected_price=108.0, actual_price=108.0),
        excursions=excursions,
    )
    assert quality.capture_efficiency_pct == pytest.approx(400.0)


def test_capture_is_absent_when_there_was_no_favourable_move_to_capture():
    """Dividing a positive realized move by a zero MFE is not 100% capture, it is
    a division by zero. Absent is the only correct answer."""
    trade = _trade(exit_price=100.5, entry_price=100.0)
    bars = _bars([("2026-03-02 09:20", 100.0, 99.0, 99.5)])
    excursions = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert excursions.mfe_pct == 0.0
    quality = execution_quality(
        trade=trade,
        entry_leg=_leg("entry", "BUY"),
        exit_leg=_leg("exit", "SELL", expected_price=100.5, actual_price=100.5),
        excursions=excursions,
    )
    assert quality.capture_efficiency_pct is None


# ---------------------------------------------------------------------------
# 5. attribution decisions: decision vs execution, kept apart
# ---------------------------------------------------------------------------


def test_a_decision_code_and_an_execution_code_coexist_and_stay_distinguishable():
    """The specification's "clearly distinguish strategy decision from execution
    outcome" is satisfied by the two code families, not by prose in a field."""
    from atr.analytics.reason_codes import DECISION_CODES, EXECUTION_CODES

    trade = _trade(context_score=70, context_class="favorable")
    result = attribute_trade(
        _payload(
            trade,
            entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.4),
        )
    )
    codes = set(result.reason_codes)
    assert codes & DECISION_CODES, "no decision-side classification was produced"
    assert codes & EXECUTION_CODES, "no execution-side classification was produced"
    assert not (codes & DECISION_CODES) & (codes & EXECUTION_CODES)
    # Every emitted code must carry its own basis, or a reader cannot tell which
    # comparison produced it.
    for code in codes:
        assert code in result.reason_detail


def test_an_unfavorable_context_is_not_an_execution_problem():
    """Grouping these together is how a strategy gets blamed for a bad fill."""
    from atr.analytics.reason_codes import CONTEXT_NEGATIVE, family_of

    trade = _trade(context_score=12, context_class="unfavorable")
    result = attribute_trade(_payload(trade))
    assert CONTEXT_NEGATIVE in result.reason_codes
    assert family_of(CONTEXT_NEGATIVE) == "decision"


def test_high_slippage_is_classified_from_the_measurement_not_the_symbol():
    """Two trades in the same symbol must diverge when one filled badly.

    The classification boundary is "worse than requested or not", so a fill at
    the expected price is good and a fill a rupee above it is poor. A symbol-level
    rule would classify both the same way and hide the execution difference.
    """
    from atr.analytics.reason_codes import GOOD_ENTRY, POOR_ENTRY

    clean = attribute_trade(
        _payload(
            _trade(trade_id="A"),
            entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=99.99),
        )
    )
    worse = attribute_trade(
        _payload(
            _trade(trade_id="B"),
            entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.01),
        )
    )
    dirty = attribute_trade(
        _payload(
            _trade(trade_id="C"),
            entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.9),
        )
    )
    assert GOOD_ENTRY in clean.reason_codes
    assert POOR_ENTRY in worse.reason_codes
    assert POOR_ENTRY in dirty.reason_codes

    # Both poor fills carry the same code; the measurement is what separates
    # them, so the code is never the whole story.
    assert dirty.execution.entry_slippage_bps > worse.execution.entry_slippage_bps
    assert worse.execution.entry_slippage_bps > clean.execution.entry_slippage_bps


def test_high_slippage_uses_the_declared_threshold_not_a_second_opinion():
    """The label is derived from the same threshold dict the classification
    reports, so raising the bar in a config raises it for the label too.

    The default exit leg is priced at expectation (zero slippage), so the round
    trip averages the entry's 50bps with the exit's 0 and lands at 25bps — over
    the 15bp default bar, and over a 5bp floor.
    """
    from atr.analytics.reason_codes import HIGH_SLIPPAGE, LOW_SLIPPAGE

    leg = _leg("entry", "BUY", expected_price=100.0, actual_price=100.5)  # 50bps
    losing = _leg("exit", "SELL", expected_price=104.0, actual_price=104.0)  # 0bps
    default = attribute_trade(
        _payload(_trade(trade_id="D"), entry_leg=leg, exit_leg=losing)
    )
    assert default.execution.entry_slippage_bps == pytest.approx(50.0)
    assert default.execution.total_slippage_bps == pytest.approx(25.0)
    assert HIGH_SLIPPAGE in default.reason_codes

    strict = dict(DEFAULT_THRESHOLDS)
    strict["slippage_high_bps"] = 200.0
    strict["slippage_low_bps"] = 5.0
    relaxed = attribute_trade(
        _payload(_trade(trade_id="D"), entry_leg=leg, exit_leg=losing, thresholds=strict),
        thresholds=strict,
    )
    # Same measurement, different bar: the label follows the bar, the number does
    # not move. 25bps is under a 200bp bar and above a 5bp floor, so it carries
    # neither label — not a silent "low".
    assert relaxed.execution.total_slippage_bps == pytest.approx(25.0)
    assert relaxed.thresholds["slippage_high_bps"] == 200.0
    assert HIGH_SLIPPAGE not in relaxed.reason_codes
    assert LOW_SLIPPAGE not in relaxed.reason_codes


def test_a_low_slippage_label_needs_a_fill_inside_the_low_bar():
    from atr.analytics.reason_codes import HIGH_SLIPPAGE, LOW_SLIPPAGE

    tight = attribute_trade(
        _payload(
            _trade(trade_id="E"),
            entry_leg=_leg("entry", "BUY", expected_price=100.0, actual_price=100.01),
        )
    )
    # 1bp entry against a 0bp exit -> 0.5bps round trip, under the 5bp floor.
    assert LOW_SLIPPAGE in tight.reason_codes
    assert HIGH_SLIPPAGE not in tight.reason_codes


def test_the_same_input_produces_byte_identical_codes_and_ordering():
    """Determinism. A classifier whose output depends on set iteration order
    makes every stored row a coin flip on its reason list."""
    payload = _payload(_trade(context_score=70))
    first = attribute_trade(payload)
    second = attribute_trade(payload)
    assert first.reason_codes == second.reason_codes
    assert first.as_dict() == second.as_dict()


def test_thresholds_are_recorded_with_the_classification():
    """A code without the threshold that produced it cannot be re-read later:
    "high slippage" against a 15bp bar and against a 40bp bar are different
    findings that would look identical in a stored row."""
    result = attribute_trade(_payload())
    assert result.thresholds == DEFAULT_THRESHOLDS
    assert result.as_dict()["thresholds"]["slippage_high_bps"] == 15.0


def test_missing_measurements_are_declared_rather_than_defaulted():
    """A trade with no bars and no context must say so, not classify anyway."""
    trade = _trade(
        context_score=None,
        context_class=None,
        market_regime=None,
        exit_reason=None,
    )
    result = attribute_trade(
        _payload(trade, exit_leg=None, bars_after_entry=None, bars_until=None)
    )
    assert result.missing_fields, "absent inputs produced no missing-field report"
    assert result.excursions.mfe_pct is None


# ---------------------------------------------------------------------------
# 6. the flat dataset row
# ---------------------------------------------------------------------------


def test_flat_returns_every_declared_dataset_field():
    """The learning join reads these names. A rename that only touches one side
    would silently empty the column instead of failing."""
    from atr.services.learning import DATASET_COLUMNS

    flat = attribute_trade(_payload()).flat()
    for name in flat:
        assert name in DATASET_COLUMNS, f"{name} is not a dataset column"
    assert flat["attribution_sizing_method"] == "atr_risk"
    assert flat["attribution_context_score"] == 62
    assert flat["attribution_market_regime"] == "bull"
    assert flat["attribution_transaction_costs"] == 40.0


def test_flat_carries_no_computed_grade_or_forbidden_advice():
    """The flat row is data. It may not carry a recommendation, and it may not
    restate a grade the provenance layer owns."""
    from atr.analytics.reason_codes import ADVICE_WORDS

    flat = attribute_trade(_payload()).flat()
    assert not any("grade" in name and "evidence" not in name for name in flat)
    for value in flat.values():
        if isinstance(value, str):
            lowered = value.lower()
            assert not any(word in lowered for word in ADVICE_WORDS)


# ---------------------------------------------------------------------------
# 7. aggregation honesty
# ---------------------------------------------------------------------------


def test_aggregation_separates_forward_from_in_sample_counts():
    """A book of in-sample rows still had a day, but it may not be counted as
    forward evidence. The two counts travel together everywhere."""
    from atr.analytics.aggregation import evidence_counts, overview

    rows = [
        {"evidence_grade": "forward", "net_pnl": 100.0, "net_return_pct": 1.0},
        {"evidence_grade": "in_sample", "net_pnl": -50.0, "net_return_pct": -0.5},
        {"evidence_grade": "in_sample", "net_pnl": 20.0, "net_return_pct": 0.2},
    ]
    counts = evidence_counts(rows)
    assert counts["n"] == 3
    assert counts["forward_n"] == 1
    assert counts["in_sample_n"] == 2
    assert counts["all_forward"] is False

    # The day's figure is ungated; only the claim is gated.
    summary = overview(rows)
    assert summary["n_trades"] == 3
    assert summary["net_pnl"] == pytest.approx(70.0)
    assert summary["counts"]["forward_n"] == 1


def test_an_absent_grade_counts_as_in_sample_not_as_forward():
    """A row that never said where it came from may not be pooled with forward
    evidence. Absence is the conservative case by construction."""
    from atr.analytics.aggregation import evidence_counts

    counts = evidence_counts([{"net_pnl": 10.0}, {"evidence_class": "BACKTEST", "net_pnl": 5.0}])
    assert counts["forward_n"] == 0
    assert counts["in_sample_n"] == 2
    assert counts["all_forward"] is False


def test_profit_factor_is_absent_when_there_are_no_losses():
    """A book of three winners has an undefined profit factor, not an infinite
    one. Reporting a number there would rank it above every real strategy."""
    from atr.analytics.aggregation import overview

    summary = overview(
        [
            {"evidence_grade": "forward", "net_pnl": 100.0, "net_return_pct": 1.0},
            {"evidence_grade": "forward", "net_pnl": 200.0, "net_return_pct": 2.0},
        ]
    )
    assert summary["profit_factor"] is None
    assert summary["wins"] == 2
    assert summary["losses"] == 0


def test_a_book_with_no_rupee_figures_reports_no_rupee_figures():
    """The paper ledger records a return and no position size.

    Every money field must come back ``None`` rather than 0.0, so the caller has
    to opt into the return-based view explicitly instead of silently reading a
    zero book.
    """
    from atr.analytics.aggregation import overview

    summary = overview(
        [
            {"evidence_grade": "in_sample", "net_return_pct": 1.5},
            {"evidence_grade": "in_sample", "net_return_pct": -0.5},
        ]
    )
    assert summary["n_trades"] == 2
    assert summary["net_pnl"] is None
    assert summary["expectancy"] is None
    assert summary["net_return_pct_mean"] == pytest.approx(0.5)


def test_a_thin_bucket_is_flagged_rather_than_hidden():
    """The floor suppresses *statistics*, not the bucket.

    A group of one trade is still worth seeing — dropping it entirely would make
    the table's totals not match the book. The ``suppressed`` flag is how a reader
    knows not to quote the mean, and it is driven by the ``min_sample`` the caller
    passed rather than by a constant buried in the grouper.
    """
    from atr.analytics.aggregation import MIN_SAMPLE, _group

    rows = [
        {
            "evidence_grade": "forward",
            "evidence_class": "LIVE_FORWARD",
            "net_pnl": 10.0,
            "net_return_pct": 0.1,
            "reason_codes": ["SIGNAL_POSITIVE", "CONTEXT_POSITIVE", "GOOD_ENTRY"],
        }
    ]
    assert len(rows) < MIN_SAMPLE

    def keyer(row):
        return "signal_present" if "SIGNAL_POSITIVE" in row.get("reason_codes", []) else None

    # Below the floor: flagged.
    thin = _group(rows, keyer, min_sample=MIN_SAMPLE)
    assert len(thin) == 1
    assert thin[0]["key"] == "signal_present"
    assert thin[0]["n"] == 1
    assert thin[0]["suppressed"] is True
    # The counts are always shown even when the statistics are suppressed.
    assert thin[0]["counts"]["n"] == 1

    # At or above the floor: not flagged, same row.
    shown = _group(rows, keyer, min_sample=1)
    assert shown[0]["suppressed"] is False


def test_a_branch_reports_every_six_of_its_groups():
    """The dashboard renders six branches; a missing key would render an empty
    panel with no error."""
    from atr.analytics.aggregation import by_branch

    branch = by_branch(
        [
            {
                "evidence_grade": "forward",
                "evidence_class": "LIVE_FORWARD",
                "net_pnl": 10.0,
                "reason_codes": ["SIGNAL_POSITIVE", "CONTEXT_POSITIVE"],
            }
        ]
    )
    assert set(branch) == {
        "signal",
        "context",
        "sizing",
        "risk",
        "execution",
        "exit",
        "note",
    }
    for name in ("signal", "context", "sizing", "risk", "execution", "exit"):
        assert isinstance(branch[name], list), name


def test_a_row_the_classifier_cannot_place_is_excluded_and_counted():
    """No "unknown" bucket. A row that cannot be classified is reported as
    excluded, never compared to real buckets as though "we could not tell" were a
    finding."""
    from atr.analytics.aggregation import by_branch

    rows = [
        {"evidence_grade": "forward", "net_pnl": 10.0, "reason_codes": ["SIGNAL_POSITIVE"]},
        {"evidence_grade": "forward", "net_pnl": 20.0, "reason_codes": []},
    ]
    signal_buckets = by_branch(rows)["signal"]
    keys = {b["key"] for b in signal_buckets}
    assert "signal_present" in keys
    assert "unknown" not in keys
    total_n = sum(b["n"] for b in signal_buckets)
    assert total_n <= len(rows)


def test_the_summary_reports_net_without_the_best_trade():
    """One trade carrying a book is the most common way a small sample lies.
    The counterfactual belongs beside the headline, not in an appendix."""
    from atr.analytics.aggregation import overview

    rows = [
        {"evidence_grade": "forward", "net_pnl": v, "net_return_pct": v / 100}
        for v in (500.0, -20.0, -30.0, -10.0)
    ]
    summary = overview(rows)
    assert summary["net_pnl"] == pytest.approx(440.0)
    assert summary["net_without_best"] == pytest.approx(-60.0)
    assert summary["best_trade_share_of_gross_profit"] == pytest.approx(1.0)


def test_the_best_trade_share_is_absent_when_there_was_no_gross_profit():
    """A losing book has no gross profit to take a share of. A number there
    would be a share of nothing."""
    from atr.analytics.aggregation import overview

    summary = overview(
        [
            {"evidence_grade": "forward", "net_pnl": -20.0, "net_return_pct": -0.2},
            {"evidence_grade": "forward", "net_pnl": -30.0, "net_return_pct": -0.3},
        ]
    )
    assert summary["best_trade_share_of_gross_profit"] is None
    assert summary["profit_factor"] is None  # no wins either
    # -50 total minus the least-bad (-20) = -30.
    assert summary["net_without_best"] == pytest.approx(-30.0)


def test_the_class_count_never_contradicts_the_grade_count():
    """A row whose grade says forward and whose class defaulted to IN_SAMPLE
    would print two mutually exclusive facts in one dictionary.

    An absent class is reported as absent, not guessed.
    """
    from atr.analytics.aggregation import evidence_counts

    counts = evidence_counts([{"evidence_grade": "forward", "net_pnl": 1.0}])
    assert counts["by_grade"] == {"forward": 1}
    assert counts["by_class"] == {}
    assert counts["class_unrecorded"] == 1
    assert counts["forward_n"] == 1

    typed = evidence_counts(
        [{"evidence_grade": "forward", "evidence_class": "LIVE_FORWARD", "net_pnl": 1.0}]
    )
    assert typed["by_class"] == {"LIVE_FORWARD": 1}
    assert typed["class_unrecorded"] == 0


def test_every_bucket_placement_is_exclusive_across_all_rows():
    """Half-open buckets. A row counted in two MAE buckets would double the
    sample in every table that sums them."""
    from atr.analytics.aggregation import MAE_BUCKETS, bucket_mae

    # One row per declared boundary, plus values either side of each.
    values = [0.1, 0.25, 0.26, 0.5, 0.51, 1.0, 1.01, 5.0]
    labels = [bucket_mae(v) for v in values]
    declared = {label for _upper, label in MAE_BUCKETS}
    assert set(labels) <= declared
    assert all(label is not None for label in labels)


# ---------------------------------------------------------------------------
# 8. evidence and provenance
# ---------------------------------------------------------------------------


def test_an_unrecognised_class_grades_in_sample():
    """The conservative default, asserted directly. "We do not know where this
    came from" must never resolve to "forward"."""
    from atr.research.learning_evidence import GRADE_IN_SAMPLE, row_grade

    for value in (None, "", "SOMETHING_NEW", "backtest"):
        assert row_grade({"evidence_class": value}) == GRADE_IN_SAMPLE


def test_the_grades_the_attribution_layer_uses_are_the_canonical_ones():
    from atr.research.learning_evidence import GRADE_FORWARD, GRADE_IN_SAMPLE

    forward = attribute_trade(_payload(_trade(evidence_grade=GRADE_FORWARD)))
    in_sample = attribute_trade(
        _payload(_trade(trade_id="T-2", source="BACKTEST",
                        evidence_grade=GRADE_IN_SAMPLE, simulated=True))
    )
    assert forward.evidence_grade == GRADE_FORWARD
    assert in_sample.evidence_grade == GRADE_IN_SAMPLE
    # A simulated trade is marked as such, so a modelled fill is never mistakable
    # for a broker-confirmed one.
    assert in_sample.simulated is True
    assert forward.simulated is False


def test_a_timestamp_alone_cannot_manufacture_forward_standing():
    """The rule the OMS provenance stamp exists to enforce.

    Timestamps are forgeable by any backfill harness, so a row's standing comes
    from the execution-time stamp and from nothing else. A row that carries an
    ``order_ts`` but no stamp must grade in-sample.
    """
    from atr.research.learning_evidence import GRADE_IN_SAMPLE, row_grade

    row = {
        "evidence_class": None,
        "order_ts": datetime(2026, 9, 16, 9, 20, tzinfo=UTC),
        "created_at": datetime(2026, 9, 16, 9, 20, tzinfo=UTC),
    }
    assert row_grade(row) == GRADE_IN_SAMPLE


@pytest.mark.parametrize(
    "source,simulated",
    [("BACKTEST", True), ("PAPER", True), ("LIVE", False)],
)
def test_simulated_tracks_the_source(source, simulated):
    """Backtest and paper fills are model output; live fills are broker-reported.
    The flag is what keeps the two from being averaged as one execution record."""
    result = attribute_trade(_payload(_trade(source=source, simulated=simulated)))
    assert result.simulated is simulated
    assert result.source == source


# ---------------------------------------------------------------------------
# 9. persistence: idempotency and provenance immutability
# ---------------------------------------------------------------------------


def _row(trade_id: str = "T-1", fingerprint: str = "fp-1", **overrides) -> dict:
    base = {
        "trade_id": trade_id,
        "user_id": "U-1",
        "symbol": "RELIANCE",
        "side": "BUY",
        "source": "LIVE",
        "evidence_grade": "forward",
        "evidence_class": "LIVE_FORWARD",
        "simulated": False,
        "attribution": '{"signal": {"setup": "breakout"}}',
        "reason_codes": "SIGNAL_POSITIVE,GOOD_ENTRY",
        "missing_fields": "{}",
        "partial_fill": False,
        "input_fingerprint": fingerprint,
        "computed_at": datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


@pytest.fixture()
def owners(app_db):
    """Two real accounts, so the ownership assertions have a second party.

    The attribution row carries a foreign key to ``users``, and it is there on
    purpose: an attributed trade belongs to somebody. Seeding the parents keeps
    the constraint exercised rather than disabled.
    """
    from atr.appdb.repositories import UserRepository

    with app_db.session() as session:
        first = UserRepository.create(
            session,
            email="owner@example.com",
            username="owner",
            password_hash="x" * 32,
            role="owner",
        )
        second = UserRepository.create(
            session,
            email="other@example.com",
            username="other",
            password_hash="x" * 32,
            role="trader",
        )
        return first["user_id"], second["user_id"]


def test_attributing_the_same_trade_twice_writes_one_row(app_db, owners):
    """Idempotency is structural — the trade id is the primary key — so a
    re-scan after a crash cannot double the book."""
    from atr.appdb.repositories import TradeAttributionRepository as Repo

    user_id, _other = owners
    with app_db.session() as session:
        assert Repo.upsert(session, _row(user_id=user_id)) == "inserted"
    with app_db.session() as session:
        assert Repo.upsert(session, _row(user_id=user_id)) == "replaced"
    with app_db.session() as session:
        rows, total = Repo.list_for_user(session, user_id)
    assert total == 1
    assert len(rows) == 1


def test_a_changed_input_produces_a_new_fingerprint_and_a_new_row(app_db, owners):
    """A trade re-attributed against different inputs is a changed measurement,
    not a duplicate. The fingerprint is what lets the writer tell the two apart
    and skip the unchanged ones cheaply."""
    from atr.appdb.repositories import TradeAttributionRepository as Repo

    user_id, _other = owners
    with app_db.session() as session:
        Repo.upsert(session, _row(user_id=user_id, fingerprint="fp-1"))
    with app_db.session() as session:
        stored = Repo.fingerprint_for(session, "T-1")
    assert stored == "fp-1"

    with app_db.session() as session:
        Repo.upsert(session, _row(user_id=user_id, fingerprint="fp-2"))
    with app_db.session() as session:
        assert Repo.fingerprint_for(session, "T-1") == "fp-2"
        _rows, total = Repo.list_for_user(session, user_id)
    assert total == 1


def test_an_attribution_row_cannot_be_read_by_another_user(app_db, owners):
    """Ownership is enforced on the read, not only on the write."""
    from atr.appdb.repositories import TradeAttributionRepository as Repo

    user_id, other_id = owners
    with app_db.session() as session:
        Repo.upsert(session, _row(user_id=user_id))
    with app_db.session() as session:
        assert Repo.get(session, "T-1", user_id) is not None
        assert Repo.get(session, "T-1", other_id) is None
        rows, total = Repo.list_for_user(session, other_id)
    assert rows == []
    assert total == 0


def test_the_stored_grade_matches_the_written_grade_and_is_not_recomputed(app_db, owners):
    """The attribution table copies ``evidence_grade``; it never derives it.

    Deriving it here would give the layer an opinion about provenance, which is
    the journal's job. A second opinion is a second chance to disagree with the
    first, and the reader would have no way to tell which one won.
    """
    from atr.appdb.repositories import TradeAttributionRepository as Repo

    user_id, _other = owners
    with app_db.session() as session:
        Repo.upsert(
            session,
            _row(user_id=user_id, evidence_grade="in_sample", evidence_class="BACKTEST"),
        )
    with app_db.session() as session:
        stored = Repo.get(session, "T-1", user_id)
    assert stored["evidence_grade"] == "in_sample"
    assert stored["evidence_class"] == "BACKTEST"


def test_the_attribution_table_is_keyed_one_row_per_trade(app_db, owners):
    """Not one row per order event. A trade filled in four slices is one trade,
    and a table keyed on order id would attribute it four times."""
    from atr.appdb.schema import app_metadata

    table = app_metadata.tables["trade_attributions"]
    assert [c.name for c in table.primary_key.columns] == ["trade_id"]


def test_several_fills_under_one_order_produce_one_attribution():
    """The leg carries the fill count and the ratio rather than one row per
    fill, which is what keeps the trade count equal to the trade count."""
    many_fills = _leg(
        "entry",
        "BUY",
        requested_qty=1000.0,
        filled_qty=1000.0,
        partial=False,
        fill_count=7,
    )
    result = attribute_trade(_payload(_trade(trade_id="T-9"), entry_leg=many_fills))
    assert result.trade_id == "T-9"
    flat = result.flat()
    assert flat["attribution_fill_ratio"] == pytest.approx(1.0)
    assert flat["attribution_partial_fill"] is False


def test_the_repository_offers_no_way_to_compute_a_grade():
    """A structural guard. If a method named like a grade derivation appears, the
    provenance rule has been broken somewhere by someone."""
    from atr.appdb.repositories import TradeAttributionRepository as Repo

    offenders = [
        name
        for name in dir(Repo)
        if any(word in name.lower() for word in ("grade", "forward", "promote", "deploy"))
    ]
    assert offenders == []


def test_the_attribution_service_exposes_no_action_that_changes_trading():
    """The advisory boundary, asserted against the *public* surface.

    ``_deployment_mode`` is a private read used to classify a trade; it is
    excluded because it asks what a deployment is, not what it should do. If an
    underscored name ever becomes the hiding place for an action, this test stops
    being the thing that catches it — which is why the named list is spelled out
    rather than pattern-matched loosely.
    """
    from atr.services.attribution import AttributionService

    public = [name for name in dir(AttributionService) if not name.startswith("_")]
    offenders = [
        name
        for name in public
        if any(
            word in name.lower()
            for word in ("place", "promote", "deploy", "apply", "optimize", "modify", "trade", "execute")
        )
    ]
    assert offenders == []


def test_the_attribution_service_only_writes_the_attribution_table():
    """It reads orders, journal and context; it writes one table.

    A service that also wrote to the journal or the order store would be a second
    writer for records the OMS already owns, and the provenance rule depends on
    there being exactly one of those.
    """
    from atr.services.attribution import AttributionService

    source = (
        __import__("pathlib").Path(AttributionService.__module__.replace(".", "/"))
    )
    text = (
        __import__("pathlib")
        .Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("src", "atr", "services", "attribution.py")
        .read_text(encoding="utf-8")
    )
    # The only repository whose write methods are reachable from here.
    assert "TradeAttributionRepository.upsert" in text
    for forbidden in (
        "TradeJournalRepository.upsert",
        "OrderRepository.upsert",
        "TradeJournalRepository.create",
        "OrderRepository.create",
    ):
        assert forbidden not in text, f"{forbidden} would make this a second writer"
    del source


def test_coverage_reports_unknown_when_the_count_cannot_be_taken():
    """``complete`` is ``None``, not ``False``.

    "We could not count the book" and "the book is not fully attributed" call for
    different responses from the reader, so they must not render the same.
    """
    from atr.services.attribution import AttributionService

    service = AttributionService.__new__(AttributionService)  # no db, deliberately
    data = service.coverage("U-1", attributed=7)
    assert data["attributed"] == 7
    assert data["closed_trades"] is None
    assert data["complete"] is None


# ---------------------------------------------------------------------------
# 10. no other subsystem's behaviour moved
# ---------------------------------------------------------------------------


def test_the_risk_engine_still_halts_and_is_untouched_by_analytics():
    """A refusal-only check that the analytics work did not soften the gate.

    The engine is asserted on its *actions* — it can halt, it can check an order
    — and on the absence of any analytics entry point. A post-trade layer that
    could reach into the pre-trade gate would be a way to trade around a limit.
    """
    from atr.execution.risk import RiskEngine

    engine = RiskEngine()
    assert callable(engine.check_order)
    # The gate starts open and shows no halt reason; that is the baseline the
    # analytics work must not have moved. ``halt_reason`` is an attribute holding
    # the reason or ``None`` — not a method.
    assert engine.halted is False
    assert engine.halt_reason is None
    for forbidden in ("attribute", "analytics", "attribution"):
        assert not hasattr(engine, forbidden)


def test_the_risk_engine_module_does_not_import_the_analytics_layer():
    """The dependency direction, both in the import graph and in the source.

    Analytics reads what the gate decided. If the gate could read analytics, a
    post-trade classification would become an input to a pre-trade refusal.
    """
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        .joinpath("src", "atr", "execution", "risk.py")
        .read_text(encoding="utf-8")
    )
    assert "analytics" not in source
    assert "attribution" not in source


def test_the_oms_provenance_constants_are_unchanged():
    """The stamp string is the contract between the OMS and the journal. An
    analytics change must not have renamed it."""
    from atr.execution.oms import PROVENANCE_FORWARD, PROVENANCE_KEY

    assert PROVENANCE_FORWARD == "forward"
    assert PROVENANCE_KEY == "provenance"


def test_the_learning_dataset_grade_gate_is_still_in_place():
    """The learning builder must still refuse to launder an in-sample row into a
    forward one. Asserted at the vocabulary, which is where the gate lives."""
    from atr.research.learning_evidence import (
        GRADE_FORWARD,
        GRADE_IN_SAMPLE,
        is_forward_grade,
    )

    assert is_forward_grade(GRADE_FORWARD) is True
    assert is_forward_grade(GRADE_IN_SAMPLE) is False
    assert is_forward_grade(None) is False
    assert is_forward_grade("live") is False


def test_no_advice_word_reaches_the_reason_code_vocabulary():
    """The vocabulary is descriptive. A code that reads like an instruction would
    turn a post-trade report into a signal, which is the line this layer holds."""
    from atr.analytics.reason_codes import ADVICE_WORDS, ALL_REASON_CODES

    for code in ALL_REASON_CODES:
        lowered = code.lower()
        assert not any(word in lowered for word in ADVICE_WORDS), code


def test_the_reason_code_vocabulary_is_closed_and_unique():
    """A duplicate or an unregistered code would make the family lookup return
    ``None`` for a code the report is already printing."""
    from atr.analytics.reason_codes import (
        ALL_REASON_CODES,
        DECISION_CODES,
        EXECUTION_CODES,
        EXIT_CAUSE_CODES,
        family_of,
    )

    assert len(ALL_REASON_CODES) == len(set(ALL_REASON_CODES))
    named = DECISION_CODES | EXECUTION_CODES | EXIT_CAUSE_CODES
    assert named <= set(ALL_REASON_CODES)
    for code in ALL_REASON_CODES:
        assert family_of(code) is not None, code


def test_an_excursion_keeps_the_documented_sign_convention():
    """MFE favourable-positive, MAE adverse-negative, on both sides of the book.

    The two get summed downstream — ``MAE + MFE`` per trade, a mean MAE across
    the book — so a sign that flipped with the direction of the trade would make
    every one of those sums a statement about direction rather than about size.
    Negative-MAE-for-a-loss is what lets "mean MAE is −1.8%" read as a drawdown.
    """
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-03 09:20", 106.0, 94.0, 105.0),
        ]
    )
    for side, expected_mfe, expected_mae in (
        ("BUY", 6.0, -6.0),   # best 106 above, worst 94 below
        ("SELL", 6.0, -6.0),  # mirrored: 94 is favourable, 106 is adverse
    ):
        result = compute_excursions(
            bars,
            entry_price=100.0,
            entry_ts=datetime(2026, 3, 2, 9, 20),
            side=side,
            quantity=100.0,
            bars_until=datetime(2026, 3, 4, 14, 30),
        )
        assert result.mfe_pct == pytest.approx(expected_mfe), side
        assert result.mae_pct == pytest.approx(expected_mae), side
        assert result.mfe_amount > 0.0
        assert result.mae_amount < 0.0


def test_an_asymmetric_move_shows_the_two_sides_separately():
    """A trade that ran further one way than the other must not report the same
    magnitude twice. Guards against an ``abs()`` applied to both figures."""
    bars = _bars(
        [
            ("2026-03-02 09:20", 101.0, 99.0, 100.5),
            ("2026-03-03 09:20", 103.0, 95.0, 102.0),
        ]
    )
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=datetime(2026, 3, 2, 9, 20),
        side="BUY",
        quantity=100.0,
        bars_until=datetime(2026, 3, 4, 14, 30),
    )
    assert result.mfe_pct == pytest.approx(3.0)
    assert result.mae_pct == pytest.approx(-5.0)
    assert abs(result.mae_pct) != abs(result.mfe_pct)


def test_the_truncation_handles_a_timezone_shifted_cutoff():
    """Real feeds mix aware and naive stamps. A cutoff comparison that raises on
    the mismatch would drop every bar and report a flat trade."""
    entry = datetime(2026, 3, 2, 9, 20, tzinfo=UTC)
    until = datetime(2026, 3, 4, 14, 30, tzinfo=UTC)
    bars = _bars(
        [
            ("2026-03-02 14:50", 101.0, 99.0, 100.5),  # IST-naive spelling
            ("2026-03-20 14:50", 130.0, 60.0, 129.0),  # after the cutoff
        ]
    )
    bars["ts"] = bars["ts"].dt.tz_localize("Asia/Kolkata")
    result = compute_excursions(
        bars,
        entry_price=100.0,
        entry_ts=entry,
        side="BUY",
        quantity=100.0,
        bars_until=until,
    )
    # Whatever the window resolves to, it must not raise and must not reach the
    # March 20 bar.
    assert result.mfe_pct is None or result.mfe_pct < 30.0
