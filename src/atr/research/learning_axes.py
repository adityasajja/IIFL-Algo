"""The axes the performance analysis is allowed to slice a strategy along.

Why the axes are declared rather than inferred
---------------------------------------------

An analysis that groups by "whatever columns exist" will silently produce a
breakdown by ``trade_id`` the first time someone adds a unique column, and a
breakdown by a 3-value bucket the size of the good sample. Declaring each axis
with its label, its value function and — critically — **how many values it can
take** is what lets the analysis apply a multiple-comparisons correction that
means something: ``comparisons`` in :mod:`atr.research.learning_stats` is only
honest if the number of buckets actually examined is known in advance.

What every axis must satisfy
----------------------------

1. **It must be point-in-time.** A value may depend only on data at or before the
   trade's entry. The regime axis is the interesting case: the breadth
   classifier in ``research/self_learning.py`` reads the *whole universe as of
   now*, and reusing it here would stamp a trade from six months ago with
   today's market state. That would make every regime finding a statement about
   today wearing the label of the past.
2. **It must be groupable from the dataset alone.** If an axis needs a network
   call or a fresh scan, it does not belong here; the dataset is meant to be
   reproducible from its inputs.
3. **A missing value produces no bucket.** Never a ``None`` bucket, and never an
   ``"unknown"`` bucket that is then compared to the rest as though "we did not
   know" were a market condition. Rows with a missing axis value are reported as
   *excluded* with a count, so the reader sees how much of the sample the axis
   covers.

The axes that are requested but not offered
-------------------------------------------

* **Sector strength** is derivable from the sector axis plus a same-day
  cross-sectional mean, but only for symbols present in the universe CSVs (501
  of 3,089 cached symbols) and only when several trades share a sector on the
  same day. For the current sample that would be a bucket of one or two trades,
  which the ``MIN_SAMPLE`` floor would suppress anyway. It is therefore reported
  as a *missing feature* rather than built — see ``SECTOR_STRENGTH_REASON``.
* **VWAP relationship** and **India VIX** have no source at all, for the reasons
  given in ``atr.research.learning_enrich``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atr.research.learning_enrich import (
    REASON_NO_INTRADAY,
    REASON_SERIES_ABSENT,
    bucket_atr,
    bucket_relative_volume,
    bucket_rsi,
)

#: Why the sector-strength axis is not offered. Stated once, here, so the
#: dataset, the report and the tests all quote the same sentence.
SECTOR_STRENGTH_REASON = (
    "sector strength needs same-day peers in the same sector; the universe CSVs "
    "cover 501 of the cached symbols and the current sample does not produce "
    "enough same-day same-sector trades to form a bucket"
)


@dataclass(frozen=True)
class Axis:
    name: str
    label: str
    value: Callable[[Any], str | None]
    max_values: int
    from_column: bool = False


def _column(name: str) -> Callable[[Any], str | None]:
    def read(row: dict[str, Any]) -> str | None:
        value = row.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    return read


def _numeric_bucket(column: str, boundaries: list[tuple[float, str]]) -> Callable[[Any], str | None]:
    """Bucket a numeric column by ascending upper bounds."""
    def read(row: dict[str, Any]) -> str | None:
        value = row.get(column)
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        for upper, label in boundaries:
            if number <= upper:
                return label
        return boundaries[-1][1] if boundaries else None
    return read


# ---------------------------------------------------------------------------
# Axis declarations
# ---------------------------------------------------------------------------

AXIS_STRATEGY = Axis(name="strategy", label="Strategy",
    value=lambda row: str(row.get("strategy_key") or row.get("strategy_id") or ""), max_values=50)
AXIS_SETUP = Axis(name="setup", label="Entry setup", value=_column("setup"), max_values=5)
AXIS_EXIT_REASON = Axis(name="exit_reason", label="Exit reason", value=_column("exit_reason"), max_values=5)
AXIS_DOW = Axis(name="day_of_week", label="Day of week", value=_column("day_of_week"), max_values=5)
AXIS_TIME_OF_DAY = Axis(name="time_of_day", label="Time of day", value=_column("time_of_day"), max_values=5)
AXIS_SOURCE = Axis(name="source", label="Evidence source", value=_column("source"), max_values=3)

# Computed from the enriched row
AXIS_REGIME = Axis(name="market_regime", label="Market regime at entry", value=_column("market_regime"), max_values=4)
AXIS_RVOL = Axis(name="rvol_bucket", label="Relative volume at entry",
    value=lambda row: row.get("rvol_bucket") or bucket_relative_volume(row.get("relative_volume")), max_values=5)
AXIS_ATR = Axis(name="atr_bucket", label="Volatility (ATR / median)",
    value=lambda row: row.get("atr_bucket") or bucket_atr(row.get("atr_pct"), row.get("_atr_median")), max_values=4)
AXIS_RSI = Axis(name="rsi_bucket", label="RSI at entry",
    value=lambda row: row.get("rsi_bucket") or bucket_rsi(row.get("rsi")), max_values=5)

AXIS_GAP = Axis(name="gap_bucket", label="Opening gap %", value=_numeric_bucket(
    "gap_pct", [(-2.0, "large_down"), (-0.5, "modest_down"), (0.5, "flat"), (2.0, "modest_up"), (float("inf"), "large_up")]),
    max_values=5)
AXIS_TREND = Axis(name="trend_bucket", label="Trend strength (close vs SMA50)", value=_numeric_bucket(
    "trend_pct", [(-10.0, "well_below"), (-2.0, "below"), (2.0, "at_line"), (10.0, "above"), (float("inf"), "well_above")]),
    max_values=5)
AXIS_SECTOR = Axis(name="sector", label="Sector", value=_column("sector"), max_values=20)
AXIS_DIRECTION = Axis(name="direction", label="Direction", value=_column("direction"), max_values=2)
AXIS_SYMBOL = Axis(name="symbol", label="Symbol", value=_column("symbol"), max_values=None)
AXIS_SECTOR_STRENGTH = Axis(name="sector_strength", label="Sector relative strength vs NIFTY", value=_numeric_bucket(
    "sector_relative_strength", [(-2.0, "lagging_weak"), (0.0, "lagging_mild"), (2.0, "leading_mild"), (float("inf"), "leading_strong")]),
    max_values=4)
AXIS_STOCK_RS = Axis(name="stock_relative_strength", label="Stock relative strength vs NIFTY", value=_numeric_bucket(
    "stock_relative_strength", [(-5.0, "significantly_weaker"), (0.0, "weaker"), (5.0, "stronger"), (float("inf"), "significantly_stronger")]),
    max_values=4)
AXIS_MARKET_BREADTH = Axis(name="market_breadth", label="Market breadth at entry", value=_numeric_bucket(
    "market_breadth", [(40.0, "depressed_lt40"), (55.0, "neutral_40_55"), (70.0, "healthy_55_70"), (float("inf"), "broad_ge70")]),
    max_values=4)
AXIS_VOLATILITY_REGIME = Axis(name="volatility_regime", label="Market volatility regime", value=_column("volatility_regime"), max_values=4)

# Post-trade attribution axes (read attribution_* columns)
AXIS_MAE_BUCKET = Axis(name="mae_bucket", label="Maximum adverse excursion (R)", value=_numeric_bucket(
    "attribution_mae_r", [(0.25, "mae_le_0.25r"), (0.5, "mae_0.25_0.5r"), (0.75, "mae_0.5_0.75r"), (1.0, "mae_0.75_1.0r"), (float("inf"), "mae_gt_1.0r")]),
    max_values=5)
AXIS_MFE_BUCKET = Axis(name="mfe_bucket", label="Maximum favourable excursion (R)", value=_numeric_bucket(
    "attribution_mfe_over_risk", [(0.5, "mfe_le_0.5r"), (1.0, "mfe_0.5_1.0r"), (1.5, "mfe_1.0_1.5r"), (2.5, "mfe_1.5_2.5r"), (float("inf"), "mfe_gt_2.5r")]),
    max_values=5)
AXIS_SLIPPAGE_BUCKET = Axis(name="slippage_bucket", label="Total execution slippage", value=_numeric_bucket(
    "attribution_total_slippage_bps", [(0.0, "favourable_or_zero"), (5.0, "slippage_le_5bps"), (15.0, "slippage_5_15bps"), (40.0, "slippage_15_40bps"), (float("inf"), "slippage_gt_40bps")]),
    max_values=5)
AXIS_ENTRY_QUALITY = Axis(name="entry_quality", label="Entry execution quality", value=_column("attribution_entry_quality"), max_values=3)
AXIS_EXECUTION_QUALITY = Axis(name="execution_quality", label="Overall execution quality", value=_column("attribution_execution_quality"), max_values=3)
AXIS_EXIT_QUALITY = Axis(name="exit_quality", label="Exit execution quality", value=_column("attribution_exit_quality"), max_values=4)
AXIS_CAPTURE_EFFICIENCY = Axis(name="capture_efficiency", label="Share of the available move captured", value=_numeric_bucket(
    "attribution_capture_efficiency_pct", [(0.0, "gave_back_all"), (40.0, "captured_le_40pct"), (70.0, "captured_40_70pct"), (90.0, "captured_70_90pct"), (float("inf"), "captured_gt_90pct")]),
    max_values=5)
AXIS_SIZING_METHOD = Axis(name="sizing_method", label="Position sizing method", value=_column("attribution_sizing_method"), max_values=6)
AXIS_RISK_BUCKET = Axis(name="risk_bucket", label="Realized risk per trade", value=_numeric_bucket(
    "attribution_realized_risk_pct", [(0.5, "risk_le_0.5pct"), (1.0, "risk_0.5_1.0pct"), (2.0, "risk_1.0_2.0pct"), (float("inf"), "risk_gt_2.0pct")]),
    max_values=4)
AXIS_HOLDING = Axis(name="holding_bucket", label="Holding period", value=_numeric_bucket(
    "duration_days", [(1.0, "intraday_1d"), (5.0, "days_1_5"), (20.0, "weeks_1_4"), (60.0, "months_1_3"), (float("inf"), "months_3_plus")]),
    max_values=5)
AXIS_ATTRIBUTION_HOLDING = Axis(name="attribution_holding_bucket", label="Holding period (from the fill)", value=_numeric_bucket(
    "attribution_holding_sec", [(3600.0, "intraday_lt_1h"), (6 * 3600.0, "intraday_1_6h"), (86400.0, "up_to_1d"), (5 * 86400.0, "days_1_5"), (float("inf"), "beyond_5d")]),
    max_values=5)
AXIS_ATTRIBUTION_SOURCE = Axis(name="attribution_source", label="Evidence source of the attributed trade", value=_column("source"), max_values=3)


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

#: Axes that slice on the post-trade attribution layer. Requested by name.
ATTRIBUTION_AXES: tuple[Axis, ...] = (
    AXIS_MAE_BUCKET, AXIS_MFE_BUCKET, AXIS_SLIPPAGE_BUCKET, AXIS_ENTRY_QUALITY,
    AXIS_EXECUTION_QUALITY, AXIS_EXIT_QUALITY, AXIS_CAPTURE_EFFICIENCY,
    AXIS_SIZING_METHOD, AXIS_RISK_BUCKET, AXIS_ATTRIBUTION_HOLDING,
)

#: Named axes a caller can ask for by string, for a CLI or a query parameter.
AXES_BY_NAME: dict[str, Axis] = {
    axis.name: axis
    for axis in (
        AXIS_STRATEGY, AXIS_SETUP, AXIS_REGIME, AXIS_RVOL, AXIS_ATR, AXIS_RSI,
        AXIS_GAP, AXIS_TREND, AXIS_SECTOR, AXIS_SECTOR_STRENGTH, AXIS_STOCK_RS,
        AXIS_MARKET_BREADTH, AXIS_VOLATILITY_REGIME, AXIS_EXIT_REASON, AXIS_DOW,
        AXIS_TIME_OF_DAY, AXIS_HOLDING, AXIS_DIRECTION,
        *ATTRIBUTION_AXES,
        AXIS_SYMBOL, AXIS_SOURCE, AXIS_ATTRIBUTION_SOURCE,
    )
}

#: The axes the performance analysis scans by default.
DEFAULT_AXES: tuple[Axis, ...] = (
    AXIS_STRATEGY, AXIS_SETUP, AXIS_REGIME, AXIS_RVOL, AXIS_ATR, AXIS_RSI,
    AXIS_GAP, AXIS_TREND, AXIS_SECTOR, AXIS_SECTOR_STRENGTH, AXIS_STOCK_RS,
    AXIS_MARKET_BREADTH, AXIS_VOLATILITY_REGIME, AXIS_EXIT_REASON, AXIS_DOW,
    AXIS_TIME_OF_DAY, AXIS_HOLDING, AXIS_DIRECTION,
)


def axes_from_names(names: list[str] | None) -> list[Axis]:
    """Resolve requested axis names, ignoring unknown ones rather than raising."""
    if not names:
        return list(DEFAULT_AXES)
    resolved: list[Axis] = []
    for n in names:
        axis = AXES_BY_NAME.get(str(n).strip().lower())
        if axis is not None and axis not in resolved:
            resolved.append(axis)
    return resolved or list(DEFAULT_AXES)


def axis_coverage(axis: Axis, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """``(rows_with_a_value, rows_scanned)`` for one axis."""
    have = sum(1 for row in rows if axis.value(row) is not None)
    return have, len(rows)


def requested_but_unavailable() -> dict[str, str]:
    return {
        "vwap_relationship": REASON_NO_INTRADAY,
        "india_vix": REASON_SERIES_ABSENT,
        "sector_strength": SECTOR_STRENGTH_REASON,
    }


__all__ = [
    "AXES_BY_NAME", "ATTRIBUTION_AXES", "AXIS_SYMBOL", "AXIS_SOURCE", "Axis", "DEFAULT_AXES",
    "SECTOR_STRENGTH_REASON", "axis_coverage", "axes_from_names", "requested_but_unavailable",
]
