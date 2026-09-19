"""Buy ideas, restricted to the setups that held up in the historical study."""

from __future__ import annotations

from typing import Any

from atr.insights.evidence import BUY_SETUPS

# Best evidence first. Ranking inside a setup is by how extreme it is.
_TIER = {"strong_rs": 3, "oversold": 2, "resting_leader": 1}


def classify(ctx: Any, nifty_1m_pct: float) -> tuple[str, float] | None:
    """``(setup_key, score)`` for a stock, or None if it matches nothing with evidence."""
    rs = float(ctx.relative_strength_nifty_20d)
    ret20 = rs + nifty_1m_pct  # the stock's own 20-day return
    if rs >= 25:
        return "strong_rs", rs
    if ret20 <= -20:
        return "oversold", -ret20
    if (
        rs >= 10
        and ctx.above_ema50
        and -12 <= float(ctx.from_52w_high_pct) <= -3
        and -3 <= float(ctx.change_1d_pct) <= 1
    ):
        return "resting_leader", rs
    return None


def buy_ideas(
    contexts: dict[str, Any],
    *,
    nifty_1m_pct: float,
    regime: str,
    watchlist: set[str] | None = None,
    limit: int = 5,
    per_setup: int = 2,
) -> list[dict[str, Any]]:
    """The strongest few ideas, spread across setups so one theme doesn't fill the list."""
    watchlist = watchlist or set()
    buckets: dict[str, list[tuple[float, Any]]] = {k: [] for k in BUY_SETUPS}
    for ctx in contexts.values():
        hit = classify(ctx, nifty_1m_pct)
        if hit:
            buckets[hit[0]].append((hit[1], ctx))

    picked: list[tuple[str, Any]] = []
    for key in sorted(BUY_SETUPS, key=lambda k: -_TIER[k]):
        ranked = sorted(buckets[key], key=lambda t: -t[0])[:per_setup]
        picked.extend((key, ctx) for _, ctx in ranked)

    ideas: list[dict[str, Any]] = []
    for key, ctx in picked[:limit]:
        setup = BUY_SETUPS[key]
        stop_pct = round(1.5 * float(ctx.atr_pct), 1)
        note = ""
        if regime == "BEARISH_TREND":
            note = "The market is falling, and even strong stocks can fall with it. Keep the size small."
        ideas.append(
            {
                "symbol": ctx.symbol.replace("-EQ", ""),
                "setup": key,
                "setup_label": setup["label"],
                "evidence": setup["evidence"],
                "risk": setup["risk"],
                "market_note": note,
                "price": ctx.close,
                "change_1d_pct": ctx.change_1d_pct,
                "vs_nifty_20d_pct": round(float(ctx.relative_strength_nifty_20d), 1),
                # The stock's own 20-day move: what "oversold" is defined by.
                "move_20d_pct": round(float(ctx.relative_strength_nifty_20d) + nifty_1m_pct, 1),
                "from_high_pct": ctx.from_52w_high_pct,
                "sector": ctx.sector,
                "stop_pct": stop_pct,
                "stop_price": round(float(ctx.close) * (1 - stop_pct / 100), 2),
                "on_watchlist": ctx.symbol in watchlist or ctx.symbol.replace("-EQ", "") in watchlist,
            }
        )
    return ideas
