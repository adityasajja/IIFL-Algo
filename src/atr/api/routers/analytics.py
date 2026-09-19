"""Post-trade analytics routes — ``/api/v1/analytics``.

The read surface over the attribution layer: one closed trade explained across
nine branches, and the aggregates across a whole book.

Every route here is **read-only**, and that is a property this module asserts
rather than merely intends. There is no write endpoint, no re-attribution
trigger reachable from a route, and no route that can change a threshold
persistently — attribution is performed by the service on its own schedule, not
by a caller. ``tests/test_attribution_api.py`` enumerates the router's mutating
method set and fails the build if it ever becomes non-empty, the same guard the
learning router carries and for the same reason.

The scope rule
--------------

Every aggregate takes the same filters, and every aggregate reports the evidence
grades of the rows it was computed over. A caller may scope to ``forward``
explicitly; if they do not, a mixed book is summarised as a mixed book, with the
counts shown. What the surface will not do is silently blend the two and let a
mean over backfilled trades read as a forward finding.

There is deliberately no "best strategy" endpoint. ``/performance/by-*`` compares
a strategy across regimes, contexts, sectors, sizing methods and exits; the
comparison is the product and the verdict is withheld.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission

logger = logging.getLogger("atr.api.analytics")

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

_READ = require_permission(Permission.STRATEGY_READ)

_VALID_SOURCES = {"BACKTEST", "PAPER", "LIVE"}
_VALID_GRADES = {"forward", "in_sample"}
_VALID_ORDER_BY = {"computed_at", "net_pnl", "mfe_pct", "mae_pct", "entry_ts"}


def _get_service() -> Any:
    from atr.services.attribution import get_attribution_service

    return get_attribution_service()


def _abort(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"detail": detail})


def _filters(
    *,
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    symbol: str | None = None,
    source: str | None = None,
    provenance: str | None = None,
    market_regime: str | None = None,
    sector: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """The shared filter set, validated once so every route rejects identically.

    ``provenance`` is the API's name for the evidence grade. It is the field a
    caller most needs to get right — it decides whether a number may be quoted as
    a finding — so it is validated against the closed vocabulary rather than
    passed through, and a bad value is a 400 rather than a silent empty result.
    """
    if source and source.upper() not in _VALID_SOURCES:
        raise _abort(400, f"source must be one of: {', '.join(sorted(_VALID_SOURCES))}")
    if provenance and provenance.lower() not in _VALID_GRADES:
        raise _abort(
            400, f"provenance must be one of: {', '.join(sorted(_VALID_GRADES))}"
        )
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "source": source.upper() if source else None,
        "evidence_grade": provenance.lower() if provenance else None,
        "market_regime": market_regime,
        "sector": sector,
        "start": start,
        "end": end,
    }


def _scoped_rows(principal: Principal, **filters: Any) -> list[dict[str, Any]]:
    """Every attribution matching the filters, decoded.

    Unpaginated on purpose: these back aggregates over a whole book, and
    paginating an aggregation input is how a summary quietly becomes a summary of
    the first page. ``GET /trades`` is the paginated route; that is what a caller
    wanting a page asks for.
    """
    service = _get_service()
    return service.rows(principal.user_id, limit=1000, **filters) if filters else service.all_rows(
        principal.user_id
    )


def _rows_for_aggregate(
    principal: Principal,
    *,
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    symbol: str | None = None,
    source: str | None = None,
    provenance: str | None = None,
    market_regime: str | None = None,
    sector: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter in Python against the full book.

    The aggregates need every matching row, and the repository's filtered read is
    capped for pagination. Filtering the full set here keeps one code path — and
    one meaning for a filter — across all seven aggregate routes, rather than a
    repository query whose semantics drift from the endpoint's description.
    """
    filters = _filters(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        source=source,
        provenance=provenance,
        market_regime=market_regime,
        sector=sector,
        start=start,
        end=end,
    )
    rows = _get_service().all_rows(principal.user_id)
    return [row for row in rows if _matches(row, filters)]


def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    tree = row.get("attribution") or {}
    signal = tree.get("signal") or {}
    if filters.get("strategy_id") and str(signal.get("strategy_id") or "") != str(
        filters["strategy_id"]
    ):
        return False
    if filters.get("strategy_version") is not None:
        if signal.get("strategy_version") != filters["strategy_version"]:
            return False
    if filters.get("symbol") and _upper(row.get("symbol")) != _upper(filters["symbol"]):
        return False
    if filters.get("source") and str(row.get("source") or "").upper() != filters["source"]:
        return False
    if filters.get("evidence_grade"):
        if str(row.get("evidence_grade") or "in_sample").lower() != filters["evidence_grade"]:
            return False
    if filters.get("market_regime"):
        if row.get("market_regime") != filters["market_regime"]:
            return False
    if filters.get("sector"):
        if row.get("sector") != filters["sector"]:
            return False
    if filters.get("start") is not None:
        moment = row.get("computed_at")
        if moment is None or moment < _naive(filters["start"]):
            return False
    if filters.get("end") is not None:
        moment = row.get("computed_at")
        if moment is None or moment > _naive(filters["end"]):
            return False
    return True


def _naive(value: datetime) -> datetime:
    """Drop tzinfo so a filter compares against the naive-UTC columns."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------


@router.get("/trades")
def list_attributed_trades(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    strategy_id: str | None = Query(None),
    strategy_version: int | None = Query(None, ge=1),
    symbol: str | None = Query(None),
    source: str | None = Query(None, description="BACKTEST | PAPER | LIVE"),
    provenance: str | None = Query(
        None, description="forward | in_sample — the evidence grade"
    ),
    market_regime: str | None = Query(None),
    sector: str | None = Query(None),
    start: datetime | None = Query(None, description="computed_at lower bound (ISO)"),
    end: datetime | None = Query(None, description="computed_at upper bound (ISO)"),
    reason_code: str | None = Query(None, description="a code from the vocabulary"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Attributed trades, newest first, with the headline summary of the page.

    The response carries ``items`` *and* the aggregate over everything the filters
    matched, because a reader arriving at this screen wants both a list and a
    sense of the book — and a page of twenty rows with no context is a list of
    twenty rows.
    """
    filters = _filters(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        source=source,
        provenance=provenance,
        market_regime=market_regime,
        sector=sector,
        start=start,
        end=end,
    )
    try:
        rows = _get_service().rows(
            principal.user_id,
            limit=limit,
            offset=offset,
            reason_code=reason_code,
            **{k: v for k, v in filters.items() if v is not None and k != "strategy_id" and k != "strategy_version"},
        )
        total = len(_rows_for_aggregate(principal, **_filter_kwargs(filters)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("analytics trades list failed")
        raise _abort(500, f"trades list failed: {exc}") from exc

    # The list applies every filter; the repository read above deliberately omits
    # the two filters it cannot express cheaply, so the rows are narrowed here
    # against the same predicate the aggregates use. One predicate, two callers.
    rows = [row for row in rows if _matches(row, filters)]
    return {
        "ok": True,
        "data": {
            "items": [_summary_of(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "filters": {k: _jsonable(v) for k, v in filters.items() if v is not None},
        },
    }


def _filter_kwargs(filters: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in filters.items() if v is not None}


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _summary_of(row: dict[str, Any]) -> dict[str, Any]:
    """The list-view shape of one attribution: enough to render a row, and the
    ids needed to open the detail.

    ``entry_ts`` and ``exit_ts`` come from the stored tree rather than from the
    columns: they are not lifted into the table, because no axis slices on them
    and the ordered read already sorts by ``computed_at``. A reader who wants them
    per row gets them here.
    """
    tree = row.get("attribution") or {}
    outcome = tree.get("outcome") or {}
    return {
        "trade_id": row.get("trade_id"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "source": row.get("source"),
        "evidence_grade": row.get("evidence_grade"),
        "evidence_class": row.get("evidence_class"),
        "simulated": row.get("simulated"),
        "holding_sec": row.get("holding_sec"),
        "exit_reason": row.get("exit_reason"),
        "net_pnl": outcome.get("net_pnl"),
        "net_return_pct": outcome.get("net_return_pct"),
        "gross_pnl": outcome.get("gross_pnl"),
        "mfe_pct": row.get("mfe_pct"),
        "mae_pct": row.get("mae_pct"),
        "mfe_over_risk": row.get("mfe_over_risk"),
        "realized_over_risk": row.get("realized_over_risk"),
        "capture_efficiency_pct": row.get("capture_efficiency_pct"),
        "entry_slippage_bps": row.get("entry_slippage_bps"),
        "exit_slippage_bps": row.get("exit_slippage_bps"),
        "total_slippage_bps": row.get("total_slippage_bps"),
        "transaction_costs": row.get("transaction_costs"),
        "context_score": row.get("context_score"),
        "context_class": row.get("context_class"),
        "market_regime": row.get("market_regime"),
        "sector": row.get("sector"),
        "sizing_method": row.get("sizing_method"),
        "realized_risk_pct": row.get("realized_risk_pct"),
        "partial_fill": row.get("partial_fill"),
        "fill_ratio": row.get("fill_ratio"),
        "entry_quality": row.get("entry_quality"),
        "execution_quality": row.get("execution_quality"),
        "reason_codes": row.get("reason_codes"),
        "computed_at": _jsonable(row.get("computed_at")),
    }


@router.get("/trades/{trade_id}/attribution")
def trade_attribution(
    trade_id: str,
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """One closed trade, explained across the nine branches.

    The whole object: the TRADE tree, the excursion set with its methodology
    fields, every reason code with the measurement and threshold that produced
    it, and the inputs that could not be resolved. A 404 for a trade that does not
    exist *or* that belongs to someone else — deliberately the same answer, so the
    route cannot be used to discover which trades exist.
    """
    try:
        row = _get_service().get(principal.user_id, trade_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("analytics attribution detail failed")
        raise _abort(500, f"attribution read failed: {exc}") from exc
    if row is None:
        raise _abort(404, f"no attribution for trade '{trade_id}'")
    return {
        "ok": True,
        "data": {
            **row,
            "computed_at": _jsonable(row.get("computed_at")),
            "updated_at": _jsonable(row.get("updated_at")),
        },
    }


# ---------------------------------------------------------------------------
# aggregates
# ---------------------------------------------------------------------------


@router.get("/summary")
def analytics_summary(
    strategy_id: str | None = Query(None),
    strategy_version: int | None = Query(None, ge=1),
    symbol: str | None = Query(None),
    source: str | None = Query(None, description="BACKTEST | PAPER | LIVE"),
    provenance: str | None = Query(None, description="forward | in_sample"),
    market_regime: str | None = Query(None),
    sector: str | None = Query(None),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Headline performance, with the evidence composition stated.

    ``counts`` always accompanies the figures: a mean computed over forty
    in-sample trades and two forward ones is a statement about the in-sample
    book, and a reader who cannot see that will read it as a finding.
    """
    from atr.analytics.aggregation import overview

    try:
        rows = _rows_for_aggregate(
            principal,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            source=source,
            provenance=provenance,
            market_regime=market_regime,
            sector=sector,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("analytics summary failed")
        raise _abort(500, f"summary failed: {exc}") from exc
    data = overview(rows)
    data["attribution_coverage"] = _coverage(principal, rows)
    return {"ok": True, "data": data}


def _coverage(principal: Principal, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the closed book has been attributed at all.

    Without this a summary over eleven rows out of four hundred closed trades
    reads as a summary of the book. The number a reader most needs is not the mean
    — it is how much of the record the mean covers. The count itself is drawn by
    the service, so this route stays free of storage access.
    """
    try:
        return _get_service().coverage(principal.user_id, len(rows))
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics: coverage unavailable: %s", exc)
        return {"attributed": len(rows), "closed_trades": None, "complete": None}


@router.get("/performance/by-strategy")
def performance_by_strategy(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
    market_regime: str | None = Query(None),
) -> dict[str, Any]:
    """Performance and attribution quality, per strategy version."""
    from atr.analytics.aggregation import by_branch, overview

    rows = _rows_for_aggregate(
        principal,
        strategy_id=strategy_id,
        source=source,
        provenance=provenance,
        market_regime=market_regime,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tree = row.get("attribution") or {}
        signal = tree.get("signal") or {}
        key = str(signal.get("strategy_id") or "unattributed")
        version = signal.get("strategy_version")
        label = f"{key}@v{version}" if version is not None else key
        grouped.setdefault(label, []).append(row)

    return {
        "ok": True,
        "data": {
            "strategies": [
                {
                    "key": key,
                    "n": len(bucket),
                    "summary": overview(bucket),
                    "branches": by_branch(bucket),
                }
                for key, bucket in sorted(grouped.items())
            ],
            "note": (
                "A comparison across strategies, not a ranking. A strategy missing "
                "from this list has no attributed trades, which is different from "
                "having performed badly."
            ),
        },
    }


@router.get("/performance/by-regime")
def performance_by_regime(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
    sector: str | None = Query(None),
) -> dict[str, Any]:
    """Performance cut by the market regime recorded at entry."""
    from atr.analytics.aggregation import overview

    rows = _rows_for_aggregate(
        principal,
        strategy_id=strategy_id,
        source=source,
        provenance=provenance,
        sector=sector,
    )
    return {
        "ok": True,
        "data": {
            "buckets": _cut(rows, lambda r: r.get("market_regime"), overview),
            "dimension": "market_regime",
        },
    }


@router.get("/performance/by-context")
def performance_by_context(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
) -> dict[str, Any]:
    """Performance cut by the recorded signal-context class and score band.

    The context is the *recorded* one — the classification the signal engine made
    at signal time, under a stated model version. It is not recomputed here, so
    this cannot silently restate history under a newer model.
    """
    from atr.analytics.aggregation import MIN_SAMPLE, overview

    rows = _rows_for_aggregate(
        principal,
        strategy_id=strategy_id,
        source=source,
        provenance=provenance,
    )
    return {
        "ok": True,
        "data": {
            "by_class": _cut(rows, lambda r: r.get("context_class"), overview),
            "by_score_band": _cut(rows, _score_band, overview, min_sample=MIN_SAMPLE),
            "by_score_band_note": (
                "Score bands are the recorded score bucketed, not a re-score under "
                "a different model version."
            ),
        },
    }


@router.get("/performance/by-sizing")
def performance_by_sizing(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
) -> dict[str, Any]:
    """Performance cut by sizing method and by whether a cap bound the size."""
    from atr.analytics.aggregation import overview

    rows = _rows_for_aggregate(
        principal, strategy_id=strategy_id, source=source, provenance=provenance
    )
    return {
        "ok": True,
        "data": {
            "by_method": _cut(rows, lambda r: r.get("sizing_method"), overview),
            "by_cap": _cut(rows, _cap_bucket, overview),
        },
    }


@router.get("/performance/by-execution")
def performance_by_execution(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
) -> dict[str, Any]:
    """Performance cut by execution quality, plus the slippage distribution.

    This is the route that answers the requirement's stated question directly:
    *does a profitable strategy with poor execution read differently from a
    strong execution of a weak signal?* It reads differently here because
    execution quality is its own dimension, never folded into a single score.
    """
    from atr.analytics.aggregation import MIN_SAMPLE, execution, overview

    rows = _rows_for_aggregate(
        principal, strategy_id=strategy_id, source=source, provenance=provenance
    )
    return {
        "ok": True,
        "data": {
            "by_quality": _cut(rows, lambda r: r.get("execution_quality"), overview),
            "by_entry_quality": _cut(rows, lambda r: r.get("entry_quality"), overview),
            "by_slippage_bucket": _cut(
                rows, _slippage_bucket, overview, min_sample=MIN_SAMPLE
            ),
            "distribution": execution(rows),
        },
    }


@router.get("/mae-mfe")
def mae_mfe_endpoint(
    principal: Principal = Depends(_READ),
    strategy_id: str | None = Query(None),
    source: str | None = Query(None),
    provenance: str | None = Query(None),
    symbol: str | None = Query(None),
    include_points: bool = Query(
        True,
        description="Include per-trade points. Off for a large book where only the summary is wanted.",
    ),
) -> dict[str, Any]:
    """MAE/MFE distributions and their relationship to the final P&L.

    ``methodology`` states how the figures were produced, because an excursion is
    only interpretable alongside the observation window it was measured over: MAE
    and MFE are measured on the bars between entry and exit, from each bar's high
    and low, truncated at the exit. A reader who assumed a tick-level or a
    close-only measurement would read the numbers wrong.
    """
    from atr.analytics.aggregation import mae_mfe as compute_mae_mfe

    rows = _rows_for_aggregate(
        principal,
        strategy_id=strategy_id,
        source=source,
        provenance=provenance,
        symbol=symbol,
    )
    data = compute_mae_mfe(rows)
    if not include_points:
        data.pop("points", None)
    data["methodology"] = {
        "window": "bars timestamped between entry and exit, both bounds inclusive",
        "source": "each bar's high and low, never its close",
        "sign": "MFE favourable-positive, MAE adverse-negative, both in the position's direction",
        "rupees": "the percentage figure applied to the position's entry value",
        "risk_multiple": "excursion in rupees divided by the planned risk amount",
        "look_ahead": (
            "the window stops at the exit, so a trade's excursion never includes "
            "price action the position did not live through"
        ),
        "missing": "no usable bars between entry and exit leaves every figure null",
    }
    return {"ok": True, "data": data}


def _cut(
    rows: list[dict[str, Any]],
    keyer: Any,
    summariser: Any,
    *,
    min_sample: int = 1,
) -> list[dict[str, Any]]:
    """Bucket rows by a key function, with counts always shown and statistics
    suppressed below ``min_sample``."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    excluded = 0
    for row in rows:
        key = keyer(row)
        if key is None:
            excluded += 1
            continue
        buckets.setdefault(str(key), []).append(row)
    out = []
    for key in sorted(buckets):
        bucket = buckets[key]
        summary = summariser(bucket)
        summary["key"] = key
        summary["suppressed"] = len(bucket) < min_sample
        out.append(summary)
    return out + ([{"key": "not_measured", "n": excluded, "suppressed": True}] if excluded else [])


def _score_band(row: dict[str, Any]) -> str | None:
    from atr.research.learning_context import score_band

    return score_band(row.get("context_score"))


def _cap_bucket(row: dict[str, Any]) -> str | None:
    reason = row.get("sizing_cap_reason")
    if reason:
        return "cap_bound"
    if row.get("sizing_method"):
        return "no_cap_recorded"
    return None


def _slippage_bucket(row: dict[str, Any]) -> str | None:
    from atr.analytics.aggregation import bucket_slippage

    return bucket_slippage(row.get("total_slippage_bps"))


__all__ = ["router"]
