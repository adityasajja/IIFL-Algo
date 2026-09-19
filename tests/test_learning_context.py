"""Context-effectiveness analysis: bands, lift, suppression, evidence honesty.

The analysis exists to answer whether the context score carries information. The
tests here pin the three ways it could lie:

* a band compared to zero rather than to its complement;
* a small bucket dressed up as a finding;
* an in-sample (backtest) result presented as forward evidence.
"""

from __future__ import annotations

from atr.research.learning_context import (
    HIGH_BAND,
    LOW_BAND,
    SCORE_BANDS,
    ContextEffectiveness,
    evidence_class_for,
    max_drawdown,
    score_band,
)

_FEATURES = {
    "market_regime": "BULLISH_TREND",
    "market_breadth": 60.0,
    "sector_relative_strength": 1.0,
    "stock_relative_strength": 2.0,
    "relative_volume": 2.0,
    "volatility_regime": "normal",
}


def _row(score: int, value: float, *, evidence_class: str = "PAPER_FORWARD", **extra):
    row = {
        "context_score": score,
        "return_pct": value,
        "evidence_class": evidence_class,
        "strategy_id": "strat-1",
        **_FEATURES,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# score bands and evidence mapping
# ---------------------------------------------------------------------------

def test_score_band_places_every_boundary():
    assert score_band(100) == HIGH_BAND
    assert score_band(80) == HIGH_BAND
    assert score_band(79) == "60-79"
    assert score_band(60) == "60-79"
    assert score_band(59) == "40-59"
    assert score_band(40) == "40-59"
    assert score_band(39) == LOW_BAND
    assert score_band(0) == LOW_BAND


def test_score_band_refuses_to_place_a_missing_or_out_of_range_score():
    assert score_band(None) is None
    assert score_band("n/a") is None
    assert score_band(101) is None
    assert score_band(-1) is None


def test_evidence_class_mapping_never_upgrades_a_measurement():
    assert evidence_class_for("PAPER", "forward") == "PAPER_FORWARD"
    assert evidence_class_for("LIVE", "forward") == "LIVE_FORWARD"
    assert evidence_class_for("BACKTEST", "in_sample") == "BACKTEST"
    assert evidence_class_for("PAPER", "in_sample") == "IN_SAMPLE"
    assert evidence_class_for("BACKTEST", "forward") == "PAPER_FORWARD"


def test_max_drawdown_tracks_peak_to_trough():
    assert max_drawdown([1.0, 1.0, -3.0]) == 3.0
    assert max_drawdown([-2.0]) == 2.0
    assert max_drawdown([1.0, 2.0]) == 0.0
    assert max_drawdown([]) is None


# ---------------------------------------------------------------------------
# forward vs in-sample separation
# ---------------------------------------------------------------------------

def test_forward_and_in_sample_are_analysed_separately_never_combined():
    rows = [
        *[_row(90, 2.0, evidence_class="PAPER_FORWARD") for _ in range(12)],
        *[_row(90, -5.0, evidence_class="BACKTEST") for _ in range(12)],
    ]
    result = ContextEffectiveness().analyse(rows)

    assert result["forward_n"] == 12
    assert result["in_sample_n"] == 12

    forward_axis = next(a for a in result["axes"] if a["axis"] == "context_score")
    in_sample_axis = next(a for a in result["in_sample_axes"] if a["axis"] == "context_score")
    forward_bucket = next(
        b for b in forward_axis["buckets"] if b["label"] == HIGH_BAND
    )
    in_sample_bucket = next(
        b for b in in_sample_axis["buckets"] if b["label"] == HIGH_BAND
    )

    assert forward_bucket["n"] == 12
    assert forward_bucket["stats"]["mean"] == 2.0
    assert in_sample_bucket["n"] == 12
    assert in_sample_bucket["stats"]["mean"] == -5.0
    assert in_sample_bucket["significance"] == "in_sample_not_a_claim"


def test_in_sample_buckets_never_carry_a_claim():
    rows = [_row(90, value, evidence_class="BACKTEST") for value in (1.0, -1.0) * 8]
    result = ContextEffectiveness().analyse(rows)

    for axis in result["in_sample_axes"]:
        for bucket in axis["buckets"]:
            assert bucket["significance"] == "in_sample_not_a_claim"


# ---------------------------------------------------------------------------
# comparison to the complement, and suppression
# ---------------------------------------------------------------------------

def test_lift_is_measured_against_the_complement_not_zero():
    # Every trade earns +1 regardless of score. Against zero the high band looks
    # like a winner; against the trades it excludes the edge is exactly zero.
    rows = [
        *[_row(90, 1.0) for _ in range(12)],
        *[_row(10, 1.0) for _ in range(12)],
    ]
    result = ContextEffectiveness().analyse(rows)
    high = result["score_verdict"]["high_band"]

    assert high["stats"]["mean"] == 1.0
    assert high["lift"] == 0.0
    assert high["significance"] in ("not_significant", "not_tested")
    assert result["score_verdict"]["may_claim"] is False


def test_a_small_bucket_is_suppressed_rather_than_reported():
    rows = [_row(90, 5.0) for _ in range(4)] + [_row(10, -1.0) for _ in range(30)]
    result = ContextEffectiveness().analyse(rows)
    high_bucket = next(
        bucket
        for axis in result["axes"]
        if axis["axis"] == "context_score"
        for bucket in axis["buckets"]
        if bucket["label"] == HIGH_BAND
    )

    assert high_bucket["n"] == 4
    assert high_bucket["suppressed"] is True
    assert high_bucket["significance"] == "insufficient_sample"
    assert result["score_verdict"]["may_claim"] is False


def test_a_missing_score_is_excluded_and_never_bucketed():
    rows = [_row(90, 1.0) for _ in range(12)] + [
        _row(None, 1.0) for _ in range(3)  # type: ignore[arg-type]
    ]
    result = ContextEffectiveness().analyse(rows)
    axis = next(a for a in result["axes"] if a["axis"] == "context_score")

    assert axis["excluded"]["axis_missing"] == 3
    assert axis["coverage"]["with_value"] == 12
    labels = {bucket["label"] for bucket in axis["buckets"]}
    assert labels == {HIGH_BAND}
    assert None not in labels


def test_a_real_separation_is_detected_and_corrected():
    rows = [
        *[_row(95, 2.0) for _ in range(12)],
        *[_row(10, -2.0) for _ in range(12)],
    ]
    result = ContextEffectiveness().analyse(rows)
    verdict = result["score_verdict"]

    assert verdict["may_claim"] is True
    assert verdict["high_band"]["lift"] == 4.0
    assert verdict["high_band"]["significance"] in ("strong", "moderate")
    assert verdict["high_band"]["p_adjusted"] >= verdict["high_band"]["p_value"]
    assert verdict["monotonic_high_is_better"] is True


def test_bonferroni_count_is_the_declared_vocabulary_not_the_observed_buckets():
    rows = [_row(95, 1.0) for _ in range(12)]
    result = ContextEffectiveness().analyse(rows)

    expected = len(SCORE_BANDS) + sum(
        axis.max_values or 0
        for axis in ContextEffectiveness().axes
        if axis.name != "context_score"
    )
    assert result["bonferroni_comparisons"] == expected


# ---------------------------------------------------------------------------
# feature axes reuse the learning axes
# ---------------------------------------------------------------------------

def test_feature_axes_come_from_the_context_snapshot_columns():
    rows = [
        *[_row(90, 2.0, market_regime="BULLISH_TREND") for _ in range(12)],
        *[_row(20, -1.0, market_regime="BEARISH_TREND") for _ in range(12)],
    ]
    result = ContextEffectiveness().analyse(rows)
    regime = next(a for a in result["axes"] if a["axis"] == "market_regime")
    labels = {bucket["label"] for bucket in regime["buckets"]}

    assert labels == {"BULLISH_TREND", "BEARISH_TREND"}
    benchmark = next(b for b in regime["buckets"] if b["label"] == "BULLISH_TREND")
    assert benchmark["n"] == 12
    assert benchmark["stats"]["mean"] == 2.0


def test_no_forward_evidence_yields_no_claim():
    result = ContextEffectiveness().analyse(
        [_row(90, 1.0, evidence_class="BACKTEST") for _ in range(15)]
    )
    verdict = result["score_verdict"]

    assert verdict["may_claim"] is False
    assert "no forward" in verdict["statement"]
    assert any("no forward" in caveat for caveat in result["caveats"])
