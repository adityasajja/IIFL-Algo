"""Reading structure out of ``signal_reason`` without inventing any.

The strings in this file are copied verbatim from the f-string templates in
``atr.signals.rules.eval_entry`` and ``SmaCrossoverStrategy.describe_signal``.
That matters: a parser tested against a hand-written example that resembles the
real format will pass while the real format fails, which is the class of bug
that puts a wrong number in a features column and leaves it there.

The last group of tests is about the failure mode that matters most — text the
parser does *not* understand must produce absent features, not approximate ones.
"""

from __future__ import annotations

import pytest

from atr.research import learning_attribution as attribution


TREND_PULLBACK = "uptrend (SMA20 1,431.82 > SMA50 1,420.55) with RSI 47"
BREAKOUT = "within 2.0% of the 63-bar high 1,431.82 on 2.4x average volume"
OVERSOLD = "RSI 28 but still above SMA100 1,420.55"
SMA_CROSS = "SMA 10 (1,431.82) crossed above SMA 30 (1,420.55) with close at 1,433.00"


# ---------------------------------------------------------------------------
# each real format
# ---------------------------------------------------------------------------


def test_trend_pullback_yields_its_setup_and_numbers():
    result = attribution.extract_reason(TREND_PULLBACK)
    assert result.setup == attribution.SETUP_TREND_PULLBACK
    assert result.rsi == 47.0
    assert result.sma_fast == 1431.82
    assert result.sma_slow == 1420.55
    # The window sizes are not features; they must not leak into a price field.
    assert result.sma_fast != 20
    assert result.sma_slow != 50


def test_breakout_yields_the_relative_volume_the_rule_fired_on():
    """This is the field the whole RVOL breakdown depends on."""
    result = attribution.extract_reason(BREAKOUT)
    assert result.setup == attribution.SETUP_BREAKOUT
    assert result.volume_multiple == 2.4
    assert result.prior_high == 1431.82
    assert result.proximity_pct == 2.0


def test_oversold_yields_its_rsi_and_long_average():
    result = attribution.extract_reason(OVERSOLD)
    assert result.setup == attribution.SETUP_OVERSOLD
    assert result.rsi == 28.0
    assert result.sma_long == 1420.55


def test_sma_crossover_yields_both_averages_as_prices():
    """Regression: reading the slow *window* as the slow *price*.

    The pattern captures the window (30) before the price (1,420.55), and a
    group index off by one returned 30.0 as a moving average — an error that
    would have put a plausible-looking integer into a price column.
    """
    result = attribution.extract_reason(SMA_CROSS)
    assert result.setup == attribution.SETUP_SMA_CROSS
    assert result.sma_fast == 1431.82
    assert result.sma_slow == 1420.55
    assert result.sma_slow != 30.0


def test_thousands_separators_are_read_not_truncated():
    """Indian grouping is two-digit after the first break: 1,43,182.00."""
    result = attribution.extract_reason(
        "uptrend (SMA20 1,43,182.50 > SMA50 1,42,055.25) with RSI 51"
    )
    assert result.sma_fast == 143182.50
    assert result.sma_slow == 142055.25


def test_normalise_setup_agrees_with_extract_reason():
    for text in (TREND_PULLBACK, BREAKOUT, OVERSOLD, SMA_CROSS, "nothing here"):
        assert attribution.normalise_setup(text) == attribution.extract_reason(text).setup


# ---------------------------------------------------------------------------
# the honesty rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", None, "   "])
def test_an_absent_reason_produces_nothing_rather_than_raising(text):
    """A strategy that cannot explain itself is legal. It is not an error."""
    result = attribution.extract_reason(text)
    assert result.setup == attribution.SETUP_UNKNOWN
    assert result.rsi is None
    assert result.volume_multiple is None
    assert result.parsed_anything is False


def test_unrecognised_text_yields_absent_features_not_guessed_ones():
    """The critical failure mode: a wrong number is worse than no number."""
    result = attribution.extract_reason("took the trade because it looked strong")
    assert result.setup == attribution.SETUP_UNKNOWN
    assert result.volume_multiple is None
    assert result.rsi is None
    assert result.sma_fast is None


def test_a_bare_rsi_is_salvaged_from_unrecognised_text():
    """The one field whose pattern cannot be confused with a neighbouring value."""
    result = attribution.extract_reason("entered long, RSI 55, sector looked strong")
    assert result.rsi == 55.0
    assert result.setup == attribution.SETUP_UNKNOWN
    assert result.volume_multiple is None


def test_a_volume_number_without_the_breakout_phrase_is_not_a_volume_multiple():
    """Guards against matching '2.4x' in a sentence about something else.

    A loose pattern here would attribute any multiplier to relative volume, and
    a bucket built on it would describe the wrong trades.
    """
    result = attribution.extract_reason("position sized at 2.4x the usual risk")
    assert result.volume_multiple is None


def test_the_matched_set_records_what_was_read():
    """Distinguishes 'the reason stayed silent' from 'the parser failed'."""
    assert attribution.extract_reason(TREND_PULLBACK).matched == frozenset(
        {"setup", "rsi", "sma_fast", "sma_slow"}
    )
    assert attribution.extract_reason("nothing").matched == frozenset()


def test_a_reason_naming_a_setup_always_names_its_setup_in_matched():
    for text in (TREND_PULLBACK, BREAKOUT, OVERSOLD, SMA_CROSS):
        assert "setup" in attribution.extract_reason(text).matched


def test_extraction_is_deterministic_and_order_independent():
    """Called twice, same answer — no module-level state, no caching bugs."""
    first = attribution.extract_reason(BREAKOUT).as_dict()
    _ = attribution.extract_reason(TREND_PULLBACK)
    second = attribution.extract_reason(BREAKOUT).as_dict()
    assert first == second


def test_case_is_ignored():
    result = attribution.extract_reason(BREAKOUT.upper())
    assert result.setup == attribution.SETUP_BREAKOUT
    assert result.volume_multiple == 2.4
