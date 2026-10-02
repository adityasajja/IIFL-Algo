"""Cost drag as a first-class learning axis.

On 2026-09-28 the paper book lost money it never had a chance to make: 14
closed trades, gross −₹1,258, commissions −₹3,952. The dataset recorded the
gross but no analysis sliced on the costs, so the engine banked regime and RSI
buckets while the actual lesson — friction drowning edge at scalp frequency —
passed through unrecorded. These tests pin the axis that closes that gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atr.research.learning_axes import (
    ATTRIBUTION_AXES,
    AXES_BY_NAME,
    AXIS_COST_DRAG,
    DEFAULT_AXES,
)
from atr.services.learning import LearningDataset, PerformanceAnalysis


def test_cost_drag_bucket_boundaries() -> None:
    """Commission over |gross|, with the degenerate cases decided, not guessed."""
    value = AXIS_COST_DRAG.value
    assert AXIS_COST_DRAG.max_values == 4
    # Costs at or above the move itself: drowned.
    assert value({"gross_pnl": -100.0, "commission": 282.0}) == "cost_drowned"
    assert value({"gross_pnl": 100.0, "commission": 100.0}) == "cost_drowned"
    # Paid for nothing: gross is zero but the ticket was not free.
    assert value({"gross_pnl": 0.0, "commission": 50.0}) == "cost_drowned"
    assert value({"gross_pnl": -200.0, "commission": 150.0}) == "cost_heavy"
    assert value({"gross_pnl": 1000.0, "commission": 200.0}) == "cost_light"
    assert value({"gross_pnl": 1000.0, "commission": 50.0}) == "cost_trivial"
    # No commission recorded at all: free, not unknown-severity.
    assert value({"gross_pnl": 1000.0, "commission": 0.0}) == "cost_trivial"


def test_cost_drag_missing_values_produce_no_bucket() -> None:
    """Backtest rows carry no commission; they are excluded, never an "unknown" bucket."""
    value = AXIS_COST_DRAG.value
    assert value({"gross_pnl": 100.0}) is None
    assert value({"commission": 50.0}) is None
    assert value({}) is None
    assert value({"gross_pnl": "n/a", "commission": 50.0}) is None


def test_cost_drag_is_opt_in_not_default() -> None:
    """Sparse post-trade features stay out of the default scan (see
    test_attribution_chain), so the shared p-values are not deflated by an
    axis most rows cannot populate — while remaining requestable by name."""
    default_names = {axis.name for axis in DEFAULT_AXES}
    assert "cost_drag" not in default_names
    assert AXIS_COST_DRAG in ATTRIBUTION_AXES
    assert AXES_BY_NAME["cost_drag"] is AXIS_COST_DRAG


def _cost_rows() -> list[dict]:
    """Twelve closed paper trades drowned by costs, modelled on 2026-09-28."""
    rows = []
    for index in range(12):
        rows.append(
            {
                "source": "PAPER",
                "evidence_grade": "forward",
                "trade_ref": f"c{index}",
                "strategy_id": "triple_rsi",
                "symbol": "IFCI",
                "direction": "LONG",
                "entry_ts": datetime(2026, 9, 28, 4, 15) + timedelta(minutes=index),
                "exit_ts": datetime(2026, 9, 28, 4, 16) + timedelta(minutes=index),
                "gross_pnl": -100.0,
                "commission": 282.0,
                "net_pnl": -382.0,
            }
        )
    return rows


def test_cost_drag_breakdown_finds_the_drowned_bucket() -> None:
    """End to end: the axis the findings loop now scans reports the bucket."""
    dataset = LearningDataset(
        rows=_cost_rows(), missing_features={}, generated_at=datetime.now(UTC)
    )
    analysis = PerformanceAnalysis(dataset).analyse(axes=["cost_drag"])
    assert len(analysis.breakdowns) == 1
    breakdown = analysis.breakdowns[0]
    assert breakdown.axis == "cost_drag"
    drowned = [b for b in breakdown.buckets if b.get("label") == "cost_drowned"]
    assert len(drowned) == 1
    assert drowned[0]["n"] == 12
