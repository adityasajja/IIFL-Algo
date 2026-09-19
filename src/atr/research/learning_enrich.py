"""Point-in-time features for a trade's entry bar — and the look-ahead guard.

The single rule this module exists to enforce
---------------------------------------------

**A feature for a trade may only use bars at or before the trade's entry.**

This is not a stylistic preference. If a trade that entered on 2026-03-04 is
described using the ATR computed through 2026-03-31, then every bucket built on
that ATR is contaminated: the feature already knows how the trade ended. A
backtest of such a rule scores beautifully and fails live, and — worse — the
failure is invisible in the output, because the contaminated number looks
exactly like the clean one.

So the enforcement is structural rather than documentary. Every lookup goes
through :func:`_as_of` and :func:`metrics_at`, both of which **truncate the
frame by timestamp first and then compute the indicator from the truncated
frame**. Computing the full series and then indexing it is the same arithmetic
today and quietly wrong the moment someone adds a warmup period, so it is not
done that way. ``tests/test_learning_enrich.py`` pins this with a frame whose
future bars are wildly different from its past, so a look-ahead would be
unmistakable rather than subtle.

Why recompute per trade rather than vectorise
---------------------------------------------

A vectorised version would compute each indicator once per symbol and then look
the value up. That is ~100x faster over a large backtest and it is exactly the
implementation that invites the bug above. Symbol-trade counts here are small
(one frame per symbol, a handful of trades each), so the truncate-and-recompute
cost is acceptable — and correctness in a dataset that will be used to decide
what to trade is worth more than the seconds.

What is deliberately absent
---------------------------

**VWAP and India VIX are not computed, and not estimated.** The local cache holds
*daily* bars timestamped 09:15, so there is no intraday volume distribution to
build a session VWAP from — a "daily VWAP" would be the rule's own arithmetic
wearing a misleading name. And there is no ``INDIAVIX`` series in the cache at
all. Both are reported as missing per trade, because
*"do not invent data that does not exist"* is the constraint that makes the rest
of the dataset trustworthy. ``atr.strategy.indicators.vwap`` exists and is
correct; it requires intraday bars, and it must not be handed daily ones to
satisfy a checklist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import pandas as pd

from atr.research.learning_attribution import PriceContext

#: How many bars of history each indicator needs before it is reported.
#: RSI's default window is 14 and it is EWM-based, so it is defined from bar 2 —
#: this floor exists so an RSI computed on four bars is not presented as though
#: it were computed on thirty. Below it, the feature is None.
MIN_BARS_RSI = 20
MIN_BARS_ATR = 15
MIN_BARS_VOLUME = 21
MIN_BARS_SMA_FAST = 20
MIN_BARS_SMA_SLOW = 50


@dataclass(frozen=True)
class MissingFeatures:
    """Which requested features could not be produced, and why.

    Carried on every dataset row so a reader never has to guess whether a blank
    means "zero", "not applicable" or "we did not have the data". The reason
    strings are the same for every row that lacks the same feature, so they are
    safe to count and group.
    """

    reasons: dict[str, str] = field(default_factory=dict)

    def add(self, feature: str, reason: str) -> None:
        self.reasons.setdefault(feature, reason)

    def as_dict(self) -> dict[str, str]:
        return dict(self.reasons)

    @property
    def features(self) -> list[str]:
        return sorted(self.reasons)


#: Reason codes. Strings rather than an enum so they survive JSON round-trips
#: unchanged and a reader of the raw payload can act on them.
REASON_NO_HISTORY = "no cached price history for this symbol"
REASON_INSUFFICIENT_BARS = "fewer bars before entry than the indicator needs"
REASON_NOT_IN_SOURCE = "the source table does not carry this field"
REASON_NO_INTRADAY = "requires intraday bars; the cache holds daily bars only"
REASON_SERIES_ABSENT = "no cached series for this instrument"
REASON_REASON_ABSENT = "the strategy recorded no signal reason"


def _as_of(frame: pd.DataFrame, when: Any) -> pd.DataFrame:
    """The frame truncated to bars at or before ``when``.

    The whole point of the module. Note it compares the *bar timestamp*, not the
    bar index, and note the frame is sorted first: a cache file written out of
    order would otherwise make ``tail()`` return the wrong bar, and the error
    would be silent.
    """
    if frame is None or frame.empty:
        return frame
    stamp = pd.to_datetime(frame["ts"], errors="coerce")
    work = frame.assign(ts=stamp).dropna(subset=["ts"]).sort_values("ts")
    cutoff = pd.to_datetime(when)
    if cutoff is pd.NaT:
        return work.iloc[0:0]
    # A naive cutoff against tz-aware bars (or the reverse) compares as False for
    # every row and yields an empty frame — which reads as "no history" rather
    # than "timezone mismatch". Normalise both sides to naive before comparing.
    if getattr(cutoff, "tzinfo", None) is not None:
        cutoff = cutoff.tz_localize(None)
    bar_tz = getattr(work["ts"].dtype, "tz", None)
    if bar_tz is not None:
        work = work.assign(ts=work["ts"].dt.tz_localize(None))
    return work[work["ts"] <= cutoff]


def _rsi(frame: pd.DataFrame) -> float | None:
    if len(frame) < MIN_BARS_RSI:
        return None
    from atr.strategy.indicators import rsi

    value = rsi(frame["close"]).iloc[-1]
    return float(value) if pd.notna(value) else None


def _atr_pct(frame: pd.DataFrame) -> float | None:
    """ATR as a percentage of close — comparable across price levels."""
    if len(frame) < MIN_BARS_ATR:
        return None
    from atr.strategy.indicators import atr

    value = float(atr(frame["high"], frame["low"], frame["close"]).iloc[-1])
    close = float(frame["close"].iloc[-1])
    if not close or pd.isna(value):
        return None
    return value / close * 100.0


def _volume_multiple(frame: pd.DataFrame, lookback: int = 20) -> float | None:
    """Today's volume over the mean of the previous ``lookback`` sessions.

    The comparison set excludes the current bar (``shift(1)``), matching
    ``signals.rules._prior_volume``. Including the current bar would make a
    volume spike dilute itself, so the same 2x that fired the rule would be
    reported as, say, 1.6x — the dataset and the rule would disagree about the
    same trade.
    """
    if len(frame) < lookback + 1:
        return None
    prior = frame["volume"].shift(1).rolling(lookback).mean().iloc[-1]
    volume = float(frame["volume"].iloc[-1])
    if not prior or pd.isna(prior):
        return None
    return volume / float(prior)


def _gap_pct(frame: pd.DataFrame) -> float | None:
    """Open versus the previous close, in percent.

    Requires the two bars immediately before the entry bar to be consecutive
    sessions. Without that check, a trade entered after a long gap in the cache
    would report a multi-week gap as an overnight one.
    """
    if len(frame) < 2:
        return None
    today, yesterday = frame.iloc[-1], frame.iloc[-2]
    prev_close = float(yesterday["close"])
    if not prev_close:
        return None
    sessions_apart = (today["ts"] - yesterday["ts"]).days
    if sessions_apart > 4:  # a weekend plus a holiday is the practical maximum
        return None
    return (float(today["open"]) / prev_close - 1.0) * 100.0


def _sma(frame: pd.DataFrame, window: int) -> float | None:
    if len(frame) < window:
        return None
    from atr.strategy.indicators import sma

    value = sma(frame["close"], window).iloc[-1]
    return float(value) if pd.notna(value) else None


def metrics_at(
    frame: pd.DataFrame,
    when: Any,
    *,
    fast: int = 20,
    slow: int = 50,
    volume_lookback: int = 20,
) -> tuple[PriceContext, MissingFeatures]:
    """Every entry-time feature this module can honestly produce.

    ``frame`` is the symbol's full cached history; ``when`` is the trade's entry
    timestamp. Only bars at or before ``when`` are used, for every field.
    """
    missing = MissingFeatures()
    if frame is None or frame.empty:
        for feature in _ALL_FEATURES:
            missing.add(feature, REASON_NO_HISTORY)
        return PriceContext(), missing

    window = _as_of(frame, when)
    if window.empty:
        for feature in _ALL_FEATURES:
            missing.add(feature, REASON_INSUFFICIENT_BARS)
        return PriceContext(), missing

    bars = len(window)
    last_ts = window["ts"].iloc[-1]

    rsi_value = _rsi(window)
    atr_value = _atr_pct(window)
    vol_value = _volume_multiple(window, volume_lookback)
    gap_value = _gap_pct(window)
    fast_value = _sma(window, fast)
    slow_value = _sma(window, slow)

    if rsi_value is None:
        missing.add("rsi", REASON_INSUFFICIENT_BARS)
    if atr_value is None:
        missing.add("atr_pct", REASON_INSUFFICIENT_BARS)
    if vol_value is None:
        missing.add("relative_volume", REASON_INSUFFICIENT_BARS)
    if gap_value is None:
        missing.add("gap_pct", REASON_INSUFFICIENT_BARS)
    if fast_value is None:
        missing.add("sma_fast", REASON_INSUFFICIENT_BARS)
    if slow_value is None:
        missing.add("sma_slow", REASON_INSUFFICIENT_BARS)

    # Requested by the user and genuinely unavailable from this source. Listed
    # unconditionally so the dataset says so rather than leaving a silent blank.
    missing.add("vwap_relationship", REASON_NO_INTRADAY)
    missing.add("india_vix", REASON_SERIES_ABSENT)

    above = None
    if slow_value is not None:
        above = float(window["close"].iloc[-1]) > slow_value

    return (
        PriceContext(
            as_of_ts=last_ts,
            close=float(window["close"].iloc[-1]),
            rsi=rsi_value,
            atr_pct=atr_value,
            volume_multiple=vol_value,
            gap_pct=gap_value,
            sma_fast=fast_value,
            sma_slow=slow_value,
            above_slow_sma=above,
            bars_available=bars,
        ),
        missing,
    )


_ALL_FEATURES = (
    "rsi",
    "atr_pct",
    "relative_volume",
    "gap_pct",
    "sma_fast",
    "sma_slow",
    "vwap_relationship",
    "india_vix",
)


def trend_at(frame: pd.DataFrame, when: Any, *, window: int = 50) -> float | None:
    """``close / SMA(window) - 1`` at the entry bar, in percent.

    A signed trend-strength measure, rather than a boolean "above the average".
    The boolean loses the difference between a stock one tick above its average
    and one 20% above it, and those are different trades.
    """
    truncated = _as_of(frame, when)
    if truncated.empty or len(truncated) < window:
        return None
    line = _sma(truncated, window)
    if not line:
        return None
    return (float(truncated["close"].iloc[-1]) / line - 1.0) * 100.0


def benchmark_provenance_at(
    benchmarks: dict[str, pd.DataFrame], when: Any, *, window: int = 50
) -> tuple[float | None, dict[str, Any]]:
    """Returns (trend_pct, provenance_dict) explicitly distinguishing actual index vs ETF proxy.

    Never mislabels an ETF proxy as the official cash index.
    """
    for symbol in ("NIFTY 50", "NIFTY-EQ", "NIFTY50"):
        frame = benchmarks.get(symbol)
        if frame is not None and not frame.empty:
            val = trend_at(frame, when, window=window)
            if val is not None:
                return val, {
                    "symbol": symbol,
                    "kind": "BENCHMARK_ACTUAL_INDEX",
                    "is_proxy": False,
                    "tracking_target": "NIFTY 50",
                }
    for symbol in ("NIFTYBEES-EQ", "NIFTYBEES", "MONIFTY500-EQ"):
        frame = benchmarks.get(symbol)
        if frame is not None and not frame.empty:
            val = trend_at(frame, when, window=window)
            if val is not None:
                return val, {
                    "symbol": symbol,
                    "kind": "BENCHMARK_ETF_PROXY",
                    "is_proxy": True,
                    "tracking_target": "NIFTY 50" if "BEES" in symbol else "NIFTY 500",
                }
    return None, {
        "symbol": "UNKNOWN",
        "kind": "BENCHMARK_ETF_PROXY",
        "is_proxy": True,
        "tracking_target": "NIFTY 50",
    }


def relative_strength_at(
    stock_frame: pd.DataFrame,
    bench_frame: pd.DataFrame | None,
    when: Any,
    *,
    window: int = 20,
) -> float | None:
    """Stock return minus benchmark return over the trailing `window` bars as of `when`."""
    if stock_frame is None or bench_frame is None:
        return None
    s_win = _as_of(stock_frame, when)
    b_win = _as_of(bench_frame, when)
    if len(s_win) < window + 1 or len(b_win) < window + 1:
        return None
    s_close = float(s_win["close"].iloc[-1])
    s_prev = float(s_win["close"].iloc[-(window + 1)])
    b_close = float(b_win["close"].iloc[-1])
    b_prev = float(b_win["close"].iloc[-(window + 1)])
    if s_prev <= 0 or b_prev <= 0:
        return None
    s_ret = (s_close / s_prev - 1.0) * 100.0
    b_ret = (b_close / b_prev - 1.0) * 100.0
    return round(s_ret - b_ret, 2)


def benchmark_trend(
    benchmarks: dict[str, pd.DataFrame], when: Any, *, window: int = 50
) -> tuple[float | None, str | None]:
    """Backwards-compatible helper returning (trend_pct, symbol)."""
    trend, prov = benchmark_provenance_at(benchmarks, when, window=window)
    return trend, prov.get("symbol") if trend is not None else None


def label_regime(trend_pct: float | None, atr_pct: float | None, *, atr_median: float | None) -> str | None:
    """A coarse regime label from the benchmark trend and the trade's own vol.

    Deliberately not the breadth classifier in ``research/self_learning.py``.
    That classifier is a *current-state* reading over the whole universe, and
    applying today's reading to a trade from six months ago would be exactly the
    look-ahead this module refuses. This labels each trade from the state of the
    benchmark at that trade's own entry, which is the only version that can be
    used as a feature.

    ``None`` when the inputs are absent — an unlabelled trade is honest; a
    trade labelled from a guess is not.
    """
    if trend_pct is None:
        return None
    if atr_median and atr_pct is not None and atr_pct > atr_median * 1.5:
        return "high_volatility"
    if trend_pct > 2.0:
        return "uptrend"
    if trend_pct < -2.0:
        return "downtrend"
    return "sideways"


def bucket_atr(atr_pct: float | None, median: float | None) -> str | None:
    """Half-median buckets for volatility. None when either input is missing."""
    if atr_pct is None or not median:
        return None
    ratio = atr_pct / median
    if ratio < 0.7:
        return "low"
    if ratio < 1.3:
        return "normal"
    if ratio < 2.0:
        return "elevated"
    return "extreme"


def bucket_relative_volume(value: float | None) -> str | None:
    """RVOL buckets aligned with the rule's own default threshold of 1.5.

    The boundaries are not arbitrary: ``EntryRules.volume_multiple`` defaults to
    1.5, so a finding like "trades below 1.2x lose money" maps directly onto a
    proposal to raise that threshold. Buckets that do not correspond to a
    decision the user could actually make are buckets that produce
    recommendations nobody can act on.
    """
    if value is None:
        return None
    if value < 1.0:
        return "below_average"
    if value < 1.5:
        return "1.0-1.5"
    if value < 2.0:
        return "1.5-2.0"
    if value < 3.0:
        return "2.0-3.0"
    return "3.0_plus"


def bucket_rsi(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 30:
        return "oversold_lt30"
    if value < 45:
        return "30_45"
    if value < 55:
        return "45_55"
    if value < 70:
        return "55_70"
    return "overbought_ge70"


def time_of_day(when: datetime | None) -> str | None:
    """Session bucket for the entry.

    The cache timestamps every daily bar at 09:15, so for backtest trades this
    is always the open. It is still computed rather than assumed, because
    journal trades carry real intraday execution times and the two must share a
    vocabulary.
    """
    if when is None:
        return None
    minute = when.hour * 60 + when.minute
    if minute < 9 * 60 + 30:
        return "open"
    if minute < 11 * 60:
        return "morning"
    if minute < 13 * 60 + 30:
        return "midday"
    if minute < 15 * 60:
        return "afternoon"
    return "close"


def build_sector_table(root: Any) -> dict[str, str]:
    """``ticker -> industry`` from the niftyindices index CSVs.

    Reads the CSVs directly rather than the instrument master, so the mapping is
    available even when the master snapshot is cold and so a test can point at
    its own fixture directory. The canonicalisation matches the master's, so the
    two agree about ``M&M`` and ``RELIANCE-EQ``.
    """
    import csv
    from pathlib import Path

    from atr.instruments.service import canonical_symbol

    universe_dir = Path(root) / "universe"
    out: dict[str, str] = {}
    if not universe_dir.is_dir():
        return out
    for csv_path in sorted(universe_dir.glob("ind_*list.csv")):
        try:
            with csv_path.open("r", encoding="utf-8", errors="replace") as handle:
                for row in csv.DictReader(handle):
                    symbol = canonical_symbol(row.get("Symbol") or "")
                    industry = (row.get("Industry") or "").strip()
                    if symbol and industry:
                        out.setdefault(symbol, industry)
        except Exception:  # noqa: BLE001 — a bad CSV must not lose the others
            continue
    return out


def median_or_none(values: Iterable[float | None]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2.0


__all__ = [
    "MIN_BARS_ATR",
    "MIN_BARS_RSI",
    "MIN_BARS_SMA_FAST",
    "MIN_BARS_SMA_SLOW",
    "MIN_BARS_VOLUME",
    "MissingFeatures",
    "REASON_INSUFFICIENT_BARS",
    "REASON_NO_HISTORY",
    "REASON_NO_INTRADAY",
    "REASON_NOT_IN_SOURCE",
    "REASON_REASON_ABSENT",
    "REASON_SERIES_ABSENT",
    "bucket_atr",
    "bucket_relative_volume",
    "bucket_rsi",
    "build_sector_table",
    "benchmark_trend",
    "label_regime",
    "median_or_none",
    "metrics_at",
    "time_of_day",
    "trend_at",
]
