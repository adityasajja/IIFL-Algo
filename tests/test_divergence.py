"""Tests for `atr.signals.divergence` — RSI divergence detection.

The naive "RSI is higher at the second low" test false-positives constantly:
RSI is bounded and mean-reverting, so it very often reads *higher* at a
second, lower price low simply because it can't keep making unboundedly
lower lows the way price can, and an isolated single-day spike that pins RSI
near its floor makes the next reading look artificially higher by
comparison no matter what price does next. These tests exist specifically
to pin down that the guards against both failure modes actually hold, using
constructed price paths where the "right" answer is unambiguous by
inspection of the underlying RSI values.
"""

from __future__ import annotations

import pandas as pd

from atr.signals.divergence import detect_rsi_divergence


def _bar(close: float) -> dict:
    return {"open": close, "high": close + 0.3, "low": close - 0.3, "close": close}


def _leg(start: float, n: int, net_per_bar: float, chop: float) -> tuple[list[dict], float]:
    """`n` bars trending by `net_per_bar` on average, with alternating chop
    superimposed so the leg has real up days in it (a purely monotonic move
    saturates Wilder RSI near its floor/ceiling regardless of how gentle the
    per-bar move is — a real market leg practically never does that).
    """
    rows = []
    price = start
    for i in range(n):
        price += net_per_bar + (chop if i % 3 == 0 else -chop / 2)
        rows.append(_bar(price))
    return rows, price


def _lead_in(n: int = 6, price: float = 100.0) -> list[dict]:
    return [_bar(price + (0.1 if i % 2 == 0 else -0.1)) for i in range(n)]


def test_genuine_bullish_divergence_is_detected():
    """A choppy-but-steep first leg, a bounce, then a slower/choppier second
    leg that still undercuts the first low's price — real momentum
    exhaustion, price and RSI genuinely disagreeing.
    """
    leg1, p1 = _leg(100, 12, -1.0, 1.5)
    bounce, p2 = _leg(p1, 6, 1.8, 0.0)
    leg2, p3 = _leg(p2, 26, -0.6, 1.6)
    assert p3 < p1  # the test only means anything if this is a real lower low
    tail = [_bar(p3 + i * 1.0) for i in range(1, 3)]

    frame = pd.DataFrame(_lead_in() + leg1 + bounce + leg2 + tail)
    out = detect_rsi_divergence(frame, window=2, lookback=200, rsi_period=14)
    assert out["bullish_divergence"] == 1.0
    assert out["bearish_divergence"] == 0.0


def test_a_sustained_uniform_downtrend_is_not_flagged_as_divergence():
    """RSI ticking up a few points at each successive lower low of an
    otherwise perfectly ordinary, confirming downtrend must not read as
    divergence — this is the false-positive mode a bare sign comparison
    falls into.
    """
    rows = []
    price = 150.0
    rows += [_bar(price) for _ in range(4)]
    for _ in range(4):
        for _ in range(5):
            price -= 4
            rows.append(_bar(price))
        for _ in range(2):
            price += 1.5
            rows.append(_bar(price))

    frame = pd.DataFrame(rows)
    out = detect_rsi_divergence(frame, window=2, lookback=200, rsi_period=5)
    assert out["bullish_divergence"] == 0.0


def test_an_rsi_reading_pinned_at_the_floor_is_not_treated_as_the_extreme_to_compare_from():
    """A single sharp crash pins RSI near 0 — the next reading will look
    higher by comparison no matter what happens next, since RSI can't go
    much lower than that. That saturation artifact must not read as
    divergence just because the arithmetic sign works out.
    """
    rows = []
    rows += _lead_in()
    rows += [_bar(100 - i * 4) for i in range(1, 6)]   # crash: RSI pinned near 0
    rows += [_bar(80 + i * 3) for i in range(1, 6)]    # bounce
    rows += [_bar(95 - i * 4) for i in range(1, 6)]    # a second, confirming crash
    rows += [_bar(75 + i * 1.5) for i in range(1, 3)]

    frame = pd.DataFrame(rows)
    out = detect_rsi_divergence(frame, window=2, lookback=200, rsi_period=5)
    assert out["bullish_divergence"] == 0.0


def test_too_little_history_returns_zero_not_an_error():
    frame = pd.DataFrame([_bar(100), _bar(101), _bar(99)])
    out = detect_rsi_divergence(frame)
    assert out == {"bullish_divergence": 0.0, "bearish_divergence": 0.0}
