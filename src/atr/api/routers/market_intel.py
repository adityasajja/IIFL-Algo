"""Market Intelligence routes — ``/api/v1/market-intel``.

Indian cash-equity market-context endpoints. No F&O, no options.

Endpoints
---------
GET /api/v1/market-intel/summary
    Full market overview: NIFTY trend, breadth, A/D ratio, 52w highs/lows,
    regime classification with explicit raw measurements and benchmark provenance.

GET /api/v1/market-intel/sectors
    All sector rankings with optional sort key.

GET /api/v1/market-intel/stocks
    Stock leaders / breakout leaderboard, sorted by relative-strength-vs-NIFTY,
    RVOL, or proximity to 52-week high.

GET /api/v1/market-intel/stock/{symbol}
    Full contextual breakdown for a single cash-equity symbol.

GET /api/v1/market-intel/regime-config
    The currently active regime model version and its thresholds.

GET /api/v1/market-intel/strategy-context
    How a strategy performs under a specific market condition (axis + optional
    bucket label). Delegates to :meth:`LearningService.query_market_context_performance`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission

logger = logging.getLogger("atr.api.market_intel")

router = APIRouter(prefix="/api/v1/market-intel", tags=["market-intel"])

_READ = require_permission(Permission.STRATEGY_READ)

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_svc: Any = None
_learning_svc: Any = None


def _get_svc() -> Any:
    global _svc
    if _svc is None:
        from atr.market_intel.service import MarketIntelService

        _svc = MarketIntelService()
    return _svc


def _get_learning() -> Any:
    from atr.services.learning import get_learning_service

    return get_learning_service()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _abort(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"detail": detail})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary")
def market_intel_summary(
    force_refresh: bool = Query(False, description="Bypass TTL cache"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Full market overview.

    Returns:
    - NIFTY 50 trend %, above/below EMA20/EMA50
    - NIFTY 500 breadth: advancing / declining counts + A/D ratio
    - % of universe above EMA20, EMA50, SMA200
    - 52-week highs vs lows count
    - Market volatility (ATR%, volatility ratio)
    - Market trend strength score
    - Sector participation %
    - Regime classification with raw measurements **and** explicit benchmark provenance
    - Top/bottom sectors, top breakouts, top RS stocks
    """
    try:
        svc = _get_svc()
        summary, _sectors, _stocks = svc.compute_all(force_refresh=force_refresh)
        payload = summary.as_dict()
        return {"ok": True, "data": payload}
    except Exception as exc:
        logger.exception("market-intel summary error")
        raise _abort(500, f"market intelligence summary failed: {exc}") from exc


@router.get("/sectors")
def market_intel_sectors(
    sort_by: str = Query(
        "relative_strength_1d",
        description=(
            "Sort key: relative_strength_1d | relative_strength_1m | "
            "return_1d_pct | return_1w_pct | return_1m_pct | volume_multiple | "
            "advance_decline_ratio | above_ema50_pct | breakout_count"
        ),
    ),
    descending: bool = Query(True),
    force_refresh: bool = Query(False),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """All sector rankings.

    Returns every sector ranked by the chosen metric.  The raw values
    (return %, breadth %, relative strength %) are always included so the
    caller can re-sort client-side without a round-trip.
    """
    _VALID_SORT = {
        "relative_strength_1d",
        "relative_strength_1m",
        "return_1d_pct",
        "return_1w_pct",
        "return_1m_pct",
        "volume_multiple",
        "advance_decline_ratio",
        "above_ema50_pct",
        "breakout_count",
    }
    if sort_by not in _VALID_SORT:
        raise _abort(400, f"sort_by must be one of: {', '.join(sorted(_VALID_SORT))}")

    try:
        svc = _get_svc()
        _summary, sectors, _stocks = svc.compute_all(force_refresh=force_refresh)
        ranked = sorted(
            sectors,
            key=lambda s: (getattr(s, sort_by, None) or 0),
            reverse=descending,
        )
        return {
            "ok": True,
            "sort_by": sort_by,
            "descending": descending,
            "count": len(ranked),
            "sectors": [s.as_dict() for s in ranked],
        }
    except Exception as exc:
        logger.exception("market-intel sectors error")
        raise _abort(500, f"sector rankings failed: {exc}") from exc


@router.get("/stocks")
def market_intel_stocks(
    sort_by: str = Query(
        "relative_strength_nifty_20d",
        description=(
            "Sort key: relative_strength_nifty_20d | relative_volume | "
            "from_52w_high_pct | atr_pct | trend_pct | change_1d_pct"
        ),
    ),
    descending: bool = Query(True),
    breakouts_only: bool = Query(False, description="Restrict to stocks where is_breakout=True"),
    above_ema50_only: bool = Query(False),
    sector: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    force_refresh: bool = Query(False),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Stock leaders and breakout leaderboard.

    A live, ranked view of which NSE cash-equity names are exhibiting the
    strongest relative-strength, highest relative volume, or are closest to
    their 52-week highs — with all numeric context fields attached.
    """
    _VALID_SORT = {
        "relative_strength_nifty_20d",
        "relative_volume",
        "from_52w_high_pct",
        "atr_pct",
        "trend_pct",
        "change_1d_pct",
    }
    if sort_by not in _VALID_SORT:
        raise _abort(400, f"sort_by must be one of: {', '.join(sorted(_VALID_SORT))}")

    try:
        svc = _get_svc()
        _summary, _sectors, stocks_map = svc.compute_all(force_refresh=force_refresh)

        rows = list(stocks_map.values())
        if breakouts_only:
            rows = [s for s in rows if s.is_breakout]
        if above_ema50_only:
            rows = [s for s in rows if s.above_ema50]
        if sector:
            rows = [s for s in rows if (s.sector or "").lower() == sector.lower()]

        # Sort — ``from_52w_high_pct`` is negative (distance below high), so
        # ascending gives the stocks closest to 52w high.
        if sort_by == "from_52w_high_pct":
            rows = sorted(rows, key=lambda s: getattr(s, sort_by, -999), reverse=(not descending))
        else:
            rows = sorted(rows, key=lambda s: (getattr(s, sort_by, None) or 0), reverse=descending)

        rows = rows[:limit]
        return {
            "ok": True,
            "sort_by": sort_by,
            "filters": {
                "breakouts_only": breakouts_only,
                "above_ema50_only": above_ema50_only,
                "sector": sector,
            },
            "count": len(rows),
            "stocks": [s.as_dict() for s in rows],
        }
    except Exception as exc:
        logger.exception("market-intel stocks error")
        raise _abort(500, f"stock leaderboard failed: {exc}") from exc


@router.get("/stock/{symbol}")
def market_intel_stock(
    symbol: str,
    force_refresh: bool = Query(False),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Full contextual breakdown for one cash-equity symbol.

    Returns all ``StockContext`` fields: trend, RS vs NIFTY (20d), RVOL,
    ATR%, 52w high/low distances, gap, sector, sector RS, market breadth,
    and the benchmark provenance used for all relative calculations.
    """
    try:
        svc = _get_svc()
        _summary, _sectors, stocks_map = svc.compute_all(force_refresh=force_refresh)

        # Normalise incoming symbol (accept with or without -EQ suffix)
        from atr.instruments.service import canonical_symbol
        clean = canonical_symbol(symbol)
        ctx = stocks_map.get(clean)
        if ctx is None:
            raise _abort(404, f"symbol '{symbol}' not found in cached universe")

        return {"ok": True, "data": ctx.as_dict()}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("market-intel stock/{} error", symbol)
        raise _abort(500, f"stock context failed: {exc}") from exc


@router.get("/regime-config")
def market_intel_regime_config(
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """The active regime model version and all configurable thresholds.

    Every market regime classification is tagged with this version so a
    historical observation can be replayed under the model that produced it.
    """
    try:
        svc = _get_svc()
        cfg = svc.get_regime_config()
        import dataclasses
        return {"ok": True, "regime_config": dataclasses.asdict(cfg)}
    except Exception as exc:
        logger.exception("market-intel regime-config error")
        raise _abort(500, f"regime config read failed: {exc}") from exc


@router.get("/strategy-context")
def market_intel_strategy_context(
    strategy: str | None = Query(None, description="strategy_key or strategy_id to filter"),
    condition_axis: str = Query(
        ...,
        description=(
            "Market-context axis to slice on: market_regime | nifty_trend_bucket | "
            "breadth_bucket | sector_strength_bucket | stock_rs_bucket | volatility_regime"
        ),
    ),
    condition_value: str | None = Query(
        None,
        description="Specific bucket label to query (omit to return all buckets)",
    ),
    grades: str | None = Query(
        None,
        description="Comma-separated evidence grades to include: forward,in_sample (default: both)",
    ),
    metric: str | None = Query(None, description="Outcome metric: net_pnl or return_pct"),
    min_sample: int = Query(
        10,
        ge=1,
        description="Minimum trades per bucket to publish a finding",
    ),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Strategy performance conditioned on a market context.

    Answers questions such as:
    - *How does [Strategy] perform when NIFTY is in uptrend vs sideways?*
    - *Does this breakout setup work better when the stock is stronger than NIFTY?*
    - *How does performance differ in strong vs weak sectors?*

    Each returned bucket carries: ``n``, ``n_forward``, ``mean``, ``median``,
    ``win_rate``, ``profit_factor``, ``ci_low/ci_high``, and an
    ``evidence_note`` (``forward`` / ``in_sample_only`` /
    ``insufficient_forward_observations``) so the caller never confuses a
    backtest artefact for out-of-sample evidence.
    """
    _VALID_AXES = {
        "market_regime",
        "regime",
        "volatility_regime",
        "stock_rs",
        "stock_rs_bucket",
        "sector_strength",
        "sector_strength_bucket",
        "market_breadth",
        "breadth_bucket",
        "nifty_trend",
        "nifty_trend_bucket",
    }
    if condition_axis not in _VALID_AXES:
        raise _abort(
            400,
            f"condition_axis must be one of: {', '.join(sorted(_VALID_AXES))}",
        )

    grade_list = [g.strip() for g in grades.split(",")] if grades else None

    try:
        result = _get_learning().query_market_context_performance(
            strategy=strategy,
            condition_axis=condition_axis,
            condition_value=condition_value,
            grades=grade_list,
            metric=metric,
            min_sample=min_sample,
        )
        return {"ok": True, **result}
    except Exception as exc:
        logger.exception("market-intel strategy-context error")
        raise _abort(500, f"strategy context query failed: {exc}") from exc
