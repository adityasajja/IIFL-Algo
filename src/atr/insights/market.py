"""The market scenario in plain words, and what changed since you were last told."""

from __future__ import annotations

from typing import Any

from atr.insights.evidence import breadth_context

REGIME_WORD: dict[str, tuple[str, str]] = {
    "BULLISH_TREND": ("Rising", "good"),
    "BEARISH_TREND": ("Falling", "bad"),
    "SIDEWAYS": ("Flat", "flat"),
    "HIGH_VOLATILITY": ("Choppy", "warn"),
    "LOW_VOLATILITY": ("Calm", "flat"),
}

# Breadth zones, matching the ones the evidence was measured in.
_BUCKETS = [(0, 25, "very weak (under 25%)"), (25, 35, "weak (25-35%)"), (35, 50, "middling (35-50%)"),
            (50, 65, "healthy (50-65%)"), (65, 101, "strong (over 65%)")]


def _bucket(pct: float) -> int:
    for i, (lo, hi, _) in enumerate(_BUCKETS):
        if lo <= pct < hi:
            return i
    return len(_BUCKETS) - 1


def describe_market(summary: dict[str, Any]) -> dict[str, Any]:
    """A market summary (``MarketSummary.as_dict()``) -> a plain description."""
    regime = summary["regime"]["regime"]
    word, tone = REGIME_WORD.get(regime, (summary["regime"].get("label") or regime.title(), "flat"))
    breadth = float(summary["breadth_above_ema50_pct"])

    series = summary.get("benchmark_series") or []
    last = series[-1] if series else None
    vs50 = (last["c"] / last["s50"] - 1) * 100 if last and last.get("s50") else None
    vs200 = (last["c"] / last["s200"] - 1) * 100 if last and last.get("s200") else None

    history = summary.get("breadth_history") or []
    month_ago = history[-22]["pct"] if len(history) >= 22 else None
    headline = f"{breadth:.0f}% of stocks are above their 50-day average"
    if month_ago is not None and abs(breadth - month_ago) >= 5:
        headline += f", {'up' if breadth > month_ago else 'down'} from {month_ago:.0f}% a month ago"
    headline += "."

    return {
        "regime": regime,
        "word": word,
        "tone": tone,
        "headline": headline,
        "context": breadth_context(breadth),
        "breadth_pct": round(breadth, 1),
        "nifty_close": summary.get("nifty_close"),
        "nifty_change_1d_pct": summary.get("nifty_change_1d_pct"),
        "vs_50d_pct": round(vs50, 1) if vs50 is not None else None,
        "vs_200d_pct": round(vs200, 1) if vs200 is not None else None,
        "stocks_as_of": summary.get("stocks_as_of"),
    }


def signature(market: dict[str, Any]) -> dict[str, Any]:
    """The few facts whose change is worth a notification."""
    return {
        "regime": market["regime"],
        "word": market["word"],
        "breadth_bucket": _bucket(market["breadth_pct"]),
        "vs50": None if market["vs_50d_pct"] is None else ("above" if market["vs_50d_pct"] >= 0 else "below"),
        "vs200": None if market["vs_200d_pct"] is None else ("above" if market["vs_200d_pct"] >= 0 else "below"),
    }


def changes_since(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """Plain sentences for what differs from the last time the user was told. Empty on a first run."""
    if not previous:
        return []
    out: list[str] = []
    if previous.get("regime") != current["regime"]:
        out.append(f"The market turned {current['word'].lower()} (it was {str(previous.get('word', '')).lower()}).")
    for key, name in (("vs50", "50-day"), ("vs200", "200-day")):
        if previous.get(key) and current[key] and previous[key] != current[key]:
            out.append(f"The Nifty moved {current[key]} its {name} average.")
    if previous.get("breadth_bucket") is not None and previous["breadth_bucket"] != current["breadth_bucket"]:
        out.append(
            f"Breadth moved from {_BUCKETS[previous['breadth_bucket']][2]} to {_BUCKETS[current['breadth_bucket']][2]}."
        )
    return out
