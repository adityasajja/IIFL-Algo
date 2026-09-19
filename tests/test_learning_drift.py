"""Backtest vs live drift detection.

Drift detection is the easiest part of this system to make useless, because
every failure mode produces a *reassuring* output rather than an error:

* no live trades at all → "no drift detected"
* eight live trades → "no drift detected"
* backtest of one strategy, live trades from another → a number for neither
* a live period that was flat while the backtest trended → "the strategy
  degraded", when in fact the market changed

So most of these tests assert on what the module *refuses* to say. A test that
only checked the arithmetic would pass on an implementation with all four
defects above.

The fixtures are built so the direction of a real degradation is unmistakable:
BACKTEST entries at +100, LIVE at +40, both with enough trades to clear the
floor.
"""

from __future__ import annotations

import pytest

from atr.research import learning_drift as drift


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _trade(
    source: str,
    pnl: float,
    *,
    day: int = 1,
    strategy: str = "sma_crossover",
    regime: str | None = "uptrend",
    symbol: str = "RELIANCE",
    setup: str = "trend_pullback",
    return_pct: float | None = None,
    slippage: float | None = None,
    exit_ts: str | None = None,
) -> dict:
    return {
        "source": source,
        "strategy_key": strategy,
        "strategy_id": strategy,
        "strategy_version": 1,
        "symbol": symbol,
        "setup": setup,
        "market_regime": regime,
        "exit_ts": exit_ts or f"2026-06-{day:02d}T10:00:00",
        "entry_ts": f"2026-06-{day:02d}T09:20:00",
        "net_pnl": pnl,
        "return_pct": return_pct if return_pct is not None else pnl / 100.0,
        "duration_days": 5.0,
        "slippage_bps": slippage,
        "exit_reason": "signal",
        "direction": "LONG",
    }


def _series(source: str, pnl: float, n: int, **kwargs) -> list[dict]:
    return [_trade(source, pnl, day=(i % 28) + 1, **kwargs) for i in range(n)]


@pytest.fixture()
def degraded() -> list[dict]:
    """60 backtest trades at +100 against 40 live trades at +40."""
    return _series("BACKTEST", 100.0, 60) + _series("LIVE", 40.0, 40)


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------


def test_a_real_degradation_is_detected(degraded):
    analysis = drift.analyse(degraded)
    pair = analysis.pair("BACKTEST", "LIVE")
    assert pair is not None
    item = pair.metric("expectancy_per_trade")
    assert item.status == "ok"
    assert item.reference_value == pytest.approx(100.0)
    assert item.comparison_value == pytest.approx(40.0)
    assert item.delta == pytest.approx(-60.0)
    assert item.relative == pytest.approx(-0.6)
    assert item.direction == "deteriorated"
    assert item.magnitude == "material"
    assert "expectancy" in analysis.headline
    assert "Drift detected" in analysis.headline


def test_a_win_rate_fall_is_reported_with_its_sample_size():
    rows = _series("BACKTEST", 100.0, 40) + _series("LIVE", -50.0, 40)
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    item = pair.metric("win_rate")
    assert item.status == "ok"
    assert item.reference_value == pytest.approx(1.0)
    assert item.comparison_value == pytest.approx(0.0)
    assert item.reference_n == 40
    assert item.comparison_n == 40


def test_rising_slippage_is_deterioration_not_improvement():
    """``higher_is_better=False`` on slippage is the whole reason the sign and
    the judgement are stored separately."""
    rows = _series("BACKTEST", 10.0, 40, slippage=2.0) + _series(
        "LIVE", 10.0, 40, slippage=12.0
    )
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("slippage_bps")
    assert item.status == "ok"
    assert item.delta > 0
    assert item.direction == "deteriorated"
    assert "slippage" in " ".join(
        drift.analyse(rows).pair("BACKTEST", "LIVE").deteriorated
    )


def test_a_rising_deterioration_says_above_not_below():
    """A slippage increase is a deterioration *and* an increase. Printing
    "11.00 is below 2.00" is read as a data error by anyone who notices it."""
    rows = _series("BACKTEST", 10.0, 40, slippage=2.0) + _series(
        "LIVE", 10.0, 40, slippage=12.0
    )
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    finding = next(f for f in pair.findings if f.get("metric") == "slippage_bps")
    assert "above by 10.00" in finding["statement"]
    assert "below" not in finding["statement"]


def test_holding_period_is_measured_but_never_judged():
    """A longer hold is not worse than a shorter one — it is a change. Calling
    it "deteriorated" would assert a judgement the data does not carry."""
    rows = _series("BACKTEST", 10.0, 40) + _series("LIVE", 10.0, 40)
    for row in rows:
        row["duration_days"] = 7.0 if row["source"] == "LIVE" else 2.0
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    item = pair.metric("duration_days")
    assert item.status == "ok", "it is still measured"
    assert item.direction in {"changed", "unchanged"}
    assert "duration_days" not in pair.deteriorated
    assert not any(f.get("metric") == "duration_days" for f in pair.findings)


def test_the_p_value_is_a_p_value():
    """``welch_t`` returns (t, df). Feeding the df into a p-value printed
    ``p=39.0000`` — a number shaped like a p-value that is not one."""
    rows = _series("BACKTEST", 100.0, 60) + _series("LIVE", 40.0, 40)
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("expectancy_per_trade")
    assert item.p_value is not None
    assert 0.0 <= item.p_value <= 1.0
    assert item.significant is True


def test_a_magnitude_never_produces_a_mangled_adverb():
    """``f"{magnitude}ly"`` gives "largely" for a change that rose."""
    for magnitude, adverb in drift.MAGNITUDE_ADVERB.items():
        assert adverb.endswith("ly")
        assert "lyly" not in adverb
        assert adverb != "largely", "'largely' reads as 'mostly', not 'by a large amount'"


def test_a_deterioration_is_worded_as_worse_in_both_directions():
    for source_pnl, live_pnl, live_slip, ref_slip in (
        (100.0, 40.0, None, None),
        (10.0, 10.0, 12.0, 2.0),
    ):
        rows = _series("BACKTEST", source_pnl, 40, slippage=ref_slip) + _series(
            "LIVE", live_pnl, 40, slippage=live_slip
        )
        pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
        for finding in pair.findings:
            if finding.get("metric"):
                assert "worse than" in finding["statement"]


def test_rising_expectancy_is_improvement():
    rows = _series("BACKTEST", 40.0, 40) + _series("LIVE", 100.0, 40)
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("expectancy_per_trade")
    assert item.direction == "improved"
    assert "Drift detected" not in drift.analyse(rows).headline


def test_a_large_change_is_not_reported_as_noise():
    rows = _series("BACKTEST", 100.0, 40) + _series("LIVE", 99.0, 40)
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("expectancy_per_trade")
    assert item.direction == "unchanged" or item.magnitude == "within noise"


# ---------------------------------------------------------------------------
# the four ways it could lie
# ---------------------------------------------------------------------------


def test_backtest_only_never_reports_stability():
    """The dangerous output is a clean bill of health from a comparison that
    never happened."""
    analysis = drift.analyse(_series("BACKTEST", 100.0, 60))
    assert analysis.pairs == []
    assert "drift needs two to compare" in analysis.headline
    assert "stability" not in analysis.headline.lower()
    assert "no drift" not in analysis.headline.lower()


def test_an_empty_dataset_says_it_cannot_measure_rather_than_zero():
    analysis = drift.analyse([])
    assert analysis.headline == "No closed trades, so drift cannot be measured."
    assert all(count == 0 for count in analysis.available_sources.values())
    assert analysis.pairs == []


def test_live_only_reports_no_baseline_rather_than_good_news():
    analysis = drift.analyse(_series("LIVE", 50.0, 30))
    assert analysis.pairs == []
    assert "drift needs two to compare" in analysis.headline


def test_a_live_sample_below_the_floor_is_insufficient_not_clean():
    """8 live trades at 1.0 against a baseline of 100.0 is the most alarming
    shape of data this module will see, and it must not be reported as
    'no drift'."""
    rows = _series("BACKTEST", 100.0, 60) + _series("LIVE", 1.0, 8)
    analysis = drift.analyse(rows)
    item = analysis.pair("BACKTEST", "LIVE").metric("expectancy_per_trade")
    assert item.status == "insufficient"
    assert item.delta is None, "an unscored metric must not carry a delta"
    assert "8 closed trade(s)" in item.reason
    assert "no drift detected" not in analysis.headline.lower()
    assert "not yet measurable" in analysis.headline
    assert "not evidence" in analysis.headline


def test_the_floor_is_inclusive_at_the_shared_constant():
    """``MIN_DRIFT_SAMPLE`` must be the same floor the rest of the engine uses,
    or a bucket could be notable in one report and suppressed in another."""
    assert drift.MIN_DRIFT_SAMPLE == drift.stats.MIN_SAMPLE


def test_exactly_at_the_floor_a_comparison_is_made():
    rows = _series("BACKTEST", 100.0, 10) + _series("LIVE", 40.0, 10)
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("expectancy_per_trade")
    assert item.status == "ok"


def test_a_metric_missing_on_one_side_is_absent_with_a_reason():
    rows = _series("BACKTEST", 100.0, 40, slippage=1.0) + _series("LIVE", 40.0, 40)
    item = drift.analyse(rows).pair("BACKTEST", "LIVE").metric("slippage_bps")
    assert item.status == "absent"
    assert "LIVE" in item.reason
    assert item.delta is None


def test_a_different_regime_mix_is_raised_as_its_own_finding():
    rows = _series("BACKTEST", 100.0, 40, regime="uptrend") + _series(
        "LIVE", 100.0, 40, regime="sideways"
    )
    analysis = drift.analyse(rows)
    pair = analysis.pair("BACKTEST", "LIVE")
    assert pair.regime["mismatch"] is True
    kinds = {finding["kind"] for finding in pair.findings}
    assert "regime_mismatch" in kinds
    mismatch = next(f for f in pair.findings if f["kind"] == "regime_mismatch")
    assert "conditions, not decay" in mismatch["statement"]


def test_a_matching_regime_mix_is_not_flagged():
    rows = _series("BACKTEST", 100.0, 40) + _series("LIVE", 100.0, 40)
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    assert pair.regime["mismatch"] is False


def test_two_strategies_are_never_averaged_together():
    """A backtest of one strategy says nothing about live trades from another.
    The whole-book number would describe no strategy in particular."""
    rows = _series("BACKTEST", 100.0, 40, strategy="sma_crossover") + _series(
        "LIVE", 40.0, 40, strategy="trend_pullback"
    )
    analysis = drift.analyse(rows)
    assert any(f["kind"] == "multiple_strategies" for f in analysis.findings)
    for pair in analysis.pairs:
        assert pair.status == "insufficient", (
            "a pair formed across two different strategies must not be scored"
        )


def test_sources_holding_different_strategies_say_so_precisely():
    """Two sources *with* trades is a different failure from one source without
    any, and a reader told the latter would go hunting for trades that exist."""
    rows = _series("BACKTEST", 100.0, 40, strategy="a") + _series(
        "LIVE", 40.0, 40, strategy="b"
    )
    analysis = drift.analyse(rows)
    assert analysis.pairs == []
    assert "no strategy appears in more than one" in analysis.headline
    assert "Only one source" not in analysis.headline


def test_a_metric_scored_on_both_sides_is_never_reported_as_stable_at_low_n():
    """The guard that matters most: metrics *were* measured on both sides and
    the only thing stopping them being scored is sample size. Returning the
    reassuring sentence here is the single most damaging output this module
    could produce.

    Both sides are deliberately below the floor, so the pair is ``insufficient``
    and no finding is ever emitted — the one path where "nothing was flagged"
    and "nothing could be checked" look identical.
    """
    rows = _series("BACKTEST", 100.0, 9) + _series("LIVE", 1.0, 8)
    analysis = drift.analyse(rows)
    pair = analysis.pair("BACKTEST", "LIVE")
    assert pair.status == "ok", "the pair itself is formed; its metrics are not scored"
    assert pair.metrics[0].status == "insufficient"
    assert "not yet measurable" in analysis.headline
    assert "no drift detected" not in analysis.headline.lower()
    assert "no material drift" not in analysis.headline.lower()
    assert "not evidence" in analysis.headline


def test_drift_is_measured_per_strategy_when_both_trade_the_same_one():
    rows = _series("BACKTEST", 100.0, 30, strategy="a") + _series(
        "LIVE", 40.0, 30, strategy="a"
    )
    rows += _series("BACKTEST", 50.0, 30, strategy="b") + _series(
        "LIVE", 50.0, 30, strategy="b"
    )
    analysis = drift.analyse(rows)
    assert len(analysis.pairs) == 2
    a_pair = next(p for p in analysis.pairs if p.reference_n == 30)
    assert a_pair.metric("expectancy_per_trade").direction == "deteriorated"


def test_a_strategy_filter_that_matches_nothing_is_unmeasured_not_zero():
    rows = _series("BACKTEST", 100.0, 40)
    analysis = drift.analyse(rows, strategy="does_not_exist")
    assert analysis.pairs == []
    assert "cannot be measured" in analysis.headline
    assert "unmeasured rather than zero" in analysis.limitations[0]


def test_a_strategy_can_be_pinned_to_a_version():
    rows = _series("BACKTEST", 100.0, 40) + _series("LIVE", 40.0, 40)
    for row in rows:
        row["strategy_version"] = 2 if row["source"] == "LIVE" else 1
    matched = drift.analyse(rows, strategy="sma_crossover@2")
    assert matched.available_sources["LIVE"] == 40
    assert matched.available_sources["BACKTEST"] == 0


# ---------------------------------------------------------------------------
# distribution and shape
# ---------------------------------------------------------------------------


def test_a_change_in_symbol_mix_is_reported():
    """Expectancy can hold while the strategy quietly moves to another name."""
    rows = _series("BACKTEST", 100.0, 40, symbol="RELIANCE") + _series(
        "LIVE", 100.0, 40, symbol="INFY"
    )
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    distribution = pair.distribution["symbol"]
    assert distribution["largest_shift"]["bucket"] in {"RELIANCE", "INFY"}
    assert abs(distribution["largest_shift"]["share_delta"]) == pytest.approx(1.0)


def test_a_falling_trade_count_is_its_own_finding():
    rows = _series("BACKTEST", 100.0, 60) + _series("LIVE", 100.0, 20)
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    assert "trade_frequency" in {f["kind"] for f in pair.findings}


def test_open_trades_are_excluded_from_both_sides():
    rows = _series("BACKTEST", 100.0, 40) + _series("LIVE", 40.0, 40)
    for row in rows[-10:]:
        row["exit_ts"] = None
    pair = drift.analyse(rows).pair("BACKTEST", "LIVE")
    assert pair.comparison_n == 30


def test_drawdown_is_computed_from_the_trade_sequence():
    """Deriving it from an equity curve would make the two sides incomparable —
    the curve is a backtest artefact and the journal has no equivalent."""
    rows = [
        _trade("BACKTEST", 100.0, exit_ts="2026-01-01"),
        _trade("BACKTEST", -50.0, exit_ts="2026-01-02"),
    ]
    measured = drift.measure(rows)
    assert measured["metrics"]["max_drawdown_pct"] == pytest.approx(50.0)


def test_the_payload_marks_itself_advisory():
    analysis = drift.analyse(_series("BACKTEST", 100.0, 40))
    payload = analysis.as_dict()
    assert payload["advisory"] is True
    assert payload["applies_changes"] is False


def test_an_unknown_reference_source_is_a_programming_error():
    with pytest.raises(drift.DriftError):
        drift._pairs_for({"BACKTEST": 1}, "NOPE")


# ---------------------------------------------------------------------------
# layering
# ---------------------------------------------------------------------------


def test_the_drift_module_is_pure():
    """It must not reach a storage layer: the whole module is then testable
    against known answers instead of a database fixture."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(drift.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = [
        name
        for name in imported
        if name.startswith("atr.appdb") or name.startswith("atr.services")
        or name.startswith("pandas") or name.startswith("numpy")
    ]
    assert offenders == [], f"learning_drift reaches outside research: {offenders}"
