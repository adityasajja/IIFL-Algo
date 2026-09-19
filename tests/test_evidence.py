"""Tests for the honesty layer.

These tests encode the specific failures this repo actually committed, so that
each one is caught mechanically rather than by remembering to look. Every case
here corresponds to a result that was once printed and believed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from atr.research.evidence import (
    Evidence,
    RegimeResult,
    SelectionBias,
    Stability,
    UniverseProvenance,
    assess,
    buy_and_hold,
    regime_split,
    selection_bias,
    stability,
    summarise,
)


def _clean_provenance() -> UniverseProvenance:
    return UniverseProvenance(
        n_names=120, selected_on_full_window=False, history_bars=1631,
        survivorship_biased=False,
    )


def _good_evidence(**overrides) -> Evidence:
    """A result that passes everything, so a test can break one thing at a time."""
    base = dict(
        claim="candidate",
        headline_pct=20.0,
        provenance=_clean_provenance(),
        n_trials=3,
        oos_sharpe=1.20,
        benchmark_sharpe=0.90,
        p_edge_real=0.97,
        max_drawdown_pct=12.0,
        benchmark_drawdown_pct=18.0,
    )
    base.update(overrides)
    return Evidence(**base)


# --------------------------------------------------------------------------
# The baseline
# --------------------------------------------------------------------------


def test_a_clean_result_is_credible():
    verdict = assess(_good_evidence())
    assert verdict.credible, verdict.describe()


# --------------------------------------------------------------------------
# Universe selection — the +356 point gap
# --------------------------------------------------------------------------


def test_full_window_selection_disqualifies_immediately():
    """The liquid-120 panel was chosen using end-of-window turnover."""
    ev = _good_evidence(
        provenance=UniverseProvenance(
            n_names=120, selected_on_full_window=True, history_bars=1631,
        )
    )
    verdict = assess(ev)
    assert not verdict.credible
    assert any("not quotable" in r for r in verdict.reasons)


def test_survivorship_bias_disqualifies():
    ev = _good_evidence(
        provenance=UniverseProvenance(
            n_names=18, survivorship_biased=True, history_bars=1631,
        )
    )
    verdict = assess(ev)
    assert not verdict.credible
    assert any("dead names" in r or "end-of-window" in r for r in verdict.reasons)


def test_measured_selection_gap_is_itself_disqualifying():
    """Even without a provenance flag, a measured gap kills the level."""
    ev = _good_evidence(bias=selection_bias(
        selected_returns=pd.Series([0.01] * 252),
        unbiased_returns=pd.Series([0.002] * 252),
        n_unbiased=18,
    ))
    verdict = assess(ev)
    assert not verdict.credible
    assert any("selection bias" in r for r in verdict.reasons)


def test_selection_gap_arithmetic_and_threshold():
    bias = selection_bias(
        selected_returns=pd.Series([0.0] * 10),
        unbiased_returns=pd.Series([0.0] * 10),
        n_unbiased=18,
    )
    assert bias.gap_pct == pytest.approx(0.0, abs=1e-9)
    assert not bias.meaningful
    assert "roughly usable" in bias.describe()


def test_large_gap_is_flagged_meaningful():
    selected = pd.Series([0.01] * 252)   # ~ factor 12
    unbiased = pd.Series([0.001] * 252)  # ~ factor 1.15
    bias = selection_bias(selected, unbiased, n_unbiased=18)
    assert bias.gap_pct > 25
    assert bias.meaningful
    assert "not quotable" in bias.describe()


# --------------------------------------------------------------------------
# Regime — the test that caught the high-vol factor and the overlays
# --------------------------------------------------------------------------


def test_regime_split_detects_a_leverage_factor():
    """The real shape: wins when the market rises, loses when it falls."""
    rng = np.random.default_rng(0)
    market = pd.Series(rng.normal(0.004, 0.01, 400))
    strategy = market * 1.5 + pd.Series(np.r_[rng.normal(0, 0.0005, 200),
                                               rng.normal(0, 0.0005, 200)])
    result = regime_split(strategy, market)
    assert result.up_excess_pct > 0
    assert result.down_excess_pct < 0
    assert result.is_leverage
    assert "LEVERAGE, not edge" in result.describe()


def test_regime_split_recognises_a_real_edge():
    """Excess that persists in both directions is not leverage."""
    rng = np.random.default_rng(1)
    market = pd.Series(rng.normal(0.004, 0.01, 400))
    strategy = market + 0.002  # constant genuine outperformance
    result = regime_split(strategy, market)
    assert not result.is_leverage
    assert result.up_excess_pct > 0
    assert result.down_excess_pct > 0
    assert "edge holds both ways" in result.describe()


def test_regime_split_handles_all_up_market():
    """A window with no down months must not crash or claim robustness."""
    market = pd.Series([0.01] * 50)
    strategy = market * 1.2
    result = regime_split(strategy, market)
    assert result.down_n == 0
    assert result.down_excess_pct == 0.0
    # With no down observation the factor is unproven, and is_leverage is True
    # because down_excess <= 0 -- deliberately conservative.
    assert result.is_leverage


def test_leverage_factor_is_rejected_end_to_end():
    ev = _good_evidence(regime=RegimeResult(
        up_n=45, down_n=21, up_excess_pct=4.07, down_excess_pct=-2.06,
        up_win_rate=0.69, down_win_rate=0.48,
    ))
    verdict = assess(ev)
    assert not verdict.credible
    assert any("LEVERAGE" in r for r in verdict.reasons)


# --------------------------------------------------------------------------
# Stability — the five overlays
# --------------------------------------------------------------------------


def test_stability_requires_two_thirds_positive():
    good = stability([1.0, 2.0, 0.5, 3.0, 1.5, -0.2])
    assert good.consistent
    bad = stability([1.0, -2.0, -0.5, -3.0, 1.5, -0.2])
    assert not bad.consistent
    assert "1/6" in bad.describe() or "2/6" in bad.describe()


def test_stability_needs_enough_blocks_to_judge():
    """Two positive blocks is not stability; it is a coincidence."""
    assert not stability([1.0, 2.0]).consistent
    assert stability([1.0, 2.0, 3.0, 4.0]).consistent


def test_empty_stability_is_not_consistent():
    assert not Stability().consistent
    assert "no sub-periods" in Stability().describe()


def test_unstable_result_is_rejected_end_to_end():
    ev = _good_evidence(stability=stability([2.0, -1.0, -2.0, -0.5, -3.0]))
    verdict = assess(ev)
    assert not verdict.credible
    assert any("unstable" in r for r in verdict.reasons)


# --------------------------------------------------------------------------
# Multiple testing
# --------------------------------------------------------------------------


def test_search_with_weak_confidence_is_rejected():
    ev = _good_evidence(n_trials=48, p_edge_real=0.343)
    verdict = assess(ev)
    assert not verdict.credible
    assert any("trials" in r for r in verdict.reasons)


def test_search_can_still_pass_with_strong_confidence():
    """Trials are not disqualifying by themselves — only with weak evidence."""
    ev = _good_evidence(n_trials=48, p_edge_real=0.96)
    assert assess(ev).credible


def test_missing_correction_is_treated_as_unproven():
    ev = _good_evidence(p_edge_real=float("nan"))
    verdict = assess(ev)
    assert not verdict.credible
    assert any("unproven" in r for r in verdict.reasons)


def test_low_confidence_fails():
    assert not assess(_good_evidence(p_edge_real=0.80)).credible


# --------------------------------------------------------------------------
# Benchmark and risk
# --------------------------------------------------------------------------


def test_losing_to_buy_and_hold_is_a_failure():
    ev = _good_evidence(oos_sharpe=0.60, benchmark_sharpe=1.05)
    verdict = assess(ev)
    assert not verdict.credible
    assert any("buy-and-hold" in r for r in verdict.reasons)


def test_excessive_drawdown_is_a_failure():
    assert not assess(_good_evidence(max_drawdown_pct=55.0)).credible


def test_deeper_drawdown_than_benchmark_is_called_out():
    """The high-vol factor had -25.9% against the market's -18.1%."""
    ev = _good_evidence(max_drawdown_pct=25.9, benchmark_drawdown_pct=18.1)
    reasons = assess(ev).reasons
    assert any("paid for in risk" in r for r in reasons)


def test_signed_and_unsigned_drawdowns_compare_correctly():
    """Drawdown sign conventions must not invert the comparison.

    `Metrics.max_drawdown_pct` is signed negative; hand-computed figures are
    often written as positive magnitudes. The first version of this check
    compared them raw and reported a *shallower* drawdown as deeper.
    """
    signed = assess(_good_evidence(max_drawdown_pct=-16.1,
                                   benchmark_drawdown_pct=-35.9))
    assert any("shallower" in r for r in signed.reasons)
    assert not any("paid for in risk" in r for r in signed.reasons)

    unsigned = assess(_good_evidence(max_drawdown_pct=16.1,
                                     benchmark_drawdown_pct=35.9))
    assert any("shallower" in r for r in unsigned.reasons)
    assert not any("paid for in risk" in r for r in unsigned.reasons)


def test_mixed_sign_conventions_agree():
    """Signed candidate vs unsigned benchmark must give the same answer."""
    a = assess(_good_evidence(max_drawdown_pct=-25.9,
                              benchmark_drawdown_pct=18.1))
    b = assess(_good_evidence(max_drawdown_pct=25.9,
                              benchmark_drawdown_pct=-18.1))
    assert any("paid for in risk" in r for r in a.reasons)
    assert any("paid for in risk" in r for r in b.reasons)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _real_480pct_bias() -> SelectionBias:
    """The measured figures from research_survivorship.py."""
    return SelectionBias(
        selected_total_pct=480.7, unbiased_total_pct=124.1, n_unbiased=18
    )


def test_summary_always_states_the_universe_and_the_bias():
    """A number must never be readable without its provenance."""
    ev = _good_evidence(
        bias=_real_480pct_bias(),
        regime=RegimeResult(45, 21, 4.07, -2.06, 0.69, 0.48),
    )
    text = summarise(ev)
    assert "universe" in text
    assert "selection bias" in text
    assert "regime" in text
    assert "trials run" in text


def test_summary_reports_not_credible_for_the_real_480pct_case():
    """Reconstruct the actual headline that was printed and believed."""
    ev = Evidence(
        claim="liquid-120 index",
        headline_pct=480.7,
        provenance=UniverseProvenance(
            n_names=120, selected_on_full_window=True, history_bars=1497,
            survivorship_biased=True,
        ),
        n_trials=1,
        oos_sharpe=1.81,
        benchmark_sharpe=1.81,
        p_edge_real=float("nan"),
        bias=_real_480pct_bias(),
        max_drawdown_pct=18.6,
        benchmark_drawdown_pct=18.6,
    )
    verdict = assess(ev)
    assert not verdict.credible
    text = summarise(ev)
    assert "NOT CREDIBLE" in text
    assert "+480.70%" in text


def test_buy_and_hold_is_equal_weight():
    prices = pd.DataFrame({"a": [100.0, 110.0, 121.0], "b": [100.0, 100.0, 100.0]})
    rets = buy_and_hold(prices)
    assert rets.iloc[1] == pytest.approx(0.05)


# --------------------------------------------------------------------------
# The /evidence endpoint
# --------------------------------------------------------------------------


def test_evidence_endpoint_reports_missing_file_with_a_hint(monkeypatch, tmp_path):
    from atr.api.legacy import validation as M

    monkeypatch.setattr(M, "_EVIDENCE_PATH", tmp_path / "absent.json")
    payload = M.evidence_report()
    assert payload["available"] is False
    assert "research_honest_verdicts" in payload["hint"]


def test_evidence_endpoint_counts_credible_findings(monkeypatch, tmp_path):
    import json

    from atr.api.legacy import validation as M

    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({
        "findings": [
            {"verdict": {"credible": False}},
            {"verdict": {"credible": False}},
            {"verdict": {"credible": True}},
        ]
    }), encoding="utf8")
    monkeypatch.setattr(M, "_EVIDENCE_PATH", path)

    payload = M.evidence_report()
    assert payload["available"] is True
    assert payload["total"] == 3
    assert payload["credible_count"] == 1


def test_evidence_endpoint_survives_corrupt_json(monkeypatch, tmp_path):
    from atr.api.legacy import validation as M

    path = tmp_path / "evidence.json"
    path.write_text("{not json", encoding="utf8")
    monkeypatch.setattr(M, "_EVIDENCE_PATH", path)

    payload = M.evidence_report()
    assert payload["available"] is False
    assert "error" in payload
