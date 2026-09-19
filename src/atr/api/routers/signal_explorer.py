"""Signal Explorer routes — ``/api/v1/signal-context``.

Read-only access to the Context-Aware Signal Engine's record. Nothing here
mutates a strategy, a signal, or an order; the whole surface is a projection of
what the engine already recorded.

Endpoints
---------
GET /api/v1/signal-context/model
    The active, versioned scoring model (criteria, weights, thresholds).

GET /api/v1/signal-context/signals
    Recent enriched signals, newest first, with optional source / strategy /
    context-class filters. Each item carries the market, sector and stock
    snapshots plus the per-criterion score breakdown.

GET /api/v1/signal-context/signals/{signal_id}
    One signal's full context and score breakdown.

GET /api/v1/signal-context/analytics
    Bucketed outcome analytics by ``dimension`` (``context_class``,
    ``score_band``, ``regime``, ``sector_rs``, ``stock_rs``, ``breadth``,
    ``volatility``). Counts are always reported; statistics are suppressed
    until the sample supports them, and every bucket says whether its evidence
    is forward or in-sample.

GET /api/v1/signal-context/effectiveness
    Whether the score and its features accompany different forward outcomes.
    Score bands and feature buckets are measured against their complements with
    a Bonferroni correction across the declared axes; in-sample (backtest)
    results are reported separately. A measurement, never a scoring change.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.signal_context.analytics import DIMENSIONS

logger = logging.getLogger("atr.api.signal_explorer")

router = APIRouter(prefix="/api/v1/signal-context", tags=["signal-context"])

_READ = require_permission(Permission.STRATEGY_READ)

_VALID_SOURCES = {"LIVE", "PAPER", "BACKTEST"}
_VALID_CLASSES = {
    "STRONG_CONTEXT",
    "NEUTRAL_CONTEXT",
    "WEAK_CONTEXT",
    "INSUFFICIENT_DATA",
}
_VALID_METRICS = {"return_pct", "net_pnl"}


def _get_service() -> Any:
    from atr.signal_context.service import get_signal_context_service

    return get_signal_context_service()


def _abort(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"detail": detail})


@router.get("/model")
def signal_context_model(
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """The active scoring model: version, criteria and thresholds."""
    try:
        return {"ok": True, "data": _get_service().model()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-context model read failed")
        raise _abort(500, f"model read failed: {exc}") from exc


@router.get("/signals")
def signal_context_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None, description="LIVE | PAPER | BACKTEST"),
    strategy_id: str | None = Query(None),
    context_class: str | None = Query(
        None, description="STRONG_CONTEXT | NEUTRAL_CONTEXT | WEAK_CONTEXT | INSUFFICIENT_DATA"
    ),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Recent enriched signals, newest first."""
    if source and source.upper() not in _VALID_SOURCES:
        raise _abort(400, f"source must be one of: {', '.join(sorted(_VALID_SOURCES))}")
    if context_class and context_class.upper() not in _VALID_CLASSES:
        raise _abort(
            400, f"context_class must be one of: {', '.join(sorted(_VALID_CLASSES))}"
        )
    try:
        result = _get_service().list_signals(
            principal.user_id,
            limit=limit,
            offset=offset,
            source=source.upper() if source else None,
            strategy_id=strategy_id,
            klass=context_class.upper() if context_class else None,
        )
        return {"ok": True, "data": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-context list failed")
        raise _abort(500, f"signal list failed: {exc}") from exc


@router.get("/signals/{signal_id}")
def signal_context_signal(
    signal_id: str,
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """One signal's full context and score breakdown."""
    try:
        row = _get_service().get_signal(principal.user_id, signal_id)
        if row is None:
            raise _abort(404, f"signal '{signal_id}' not found")
        return {"ok": True, "data": row}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-context detail failed")
        raise _abort(500, f"signal detail failed: {exc}") from exc


@router.get("/analytics")
def signal_context_analytics(
    dimension: str = Query(
        "context_class",
        description=(
            "Bucketing dimension: context_class | score_band | regime | "
            "sector_rs | stock_rs | breadth | volatility"
        ),
    ),
    strategy_id: str | None = Query(None),
    strategy_version: int | None = Query(None, ge=1),
    source: str | None = Query(None, description="LIVE | PAPER | BACKTEST"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Outcome analytics bucketed by signal-context dimension.

    Each bucket reports ``n``, ``n_forward`` and ``n_in_sample`` always. The
    outcome statistics (mean, median, win rate, profit factor, 95% CI) are
    ``null`` and the bucket marked ``suppressed`` until there is enough
    evidence; ``evidence_note`` says ``forward``, ``in_sample_only`` or
    ``insufficient_forward_observations`` so a backtest artefact is never
    presented as out-of-sample proof.
    """
    if dimension not in DIMENSIONS:
        raise _abort(400, f"dimension must be one of: {', '.join(DIMENSIONS)}")
    if source and source.upper() not in _VALID_SOURCES:
        raise _abort(400, f"source must be one of: {', '.join(sorted(_VALID_SOURCES))}")
    try:
        result = _get_service().analytics(
            principal.user_id,
            dimension=dimension,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            source=source.upper() if source else None,
        )
        return {"ok": True, "data": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-context analytics failed")
        raise _abort(500, f"analytics failed: {exc}") from exc


@router.get("/effectiveness")
def signal_context_effectiveness(
    strategy_id: str | None = Query(None),
    strategy_version: int | None = Query(None, ge=1),
    source: str | None = Query(None, description="LIVE | PAPER | BACKTEST"),
    metric: str = Query("return_pct", description="return_pct | net_pnl"),
    min_sample: int = Query(10, ge=1, le=1000),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Whether a higher context score accompanies a different forward outcome.

    Score bands and feature buckets are measured against their complements with
    a Bonferroni correction across the declared axes. In-sample results are
    returned separately and never combined with forward evidence. This is a
    measurement of the active model, not a change to it.
    """
    if metric not in _VALID_METRICS:
        raise _abort(400, f"metric must be one of: {', '.join(sorted(_VALID_METRICS))}")
    if source and source.upper() not in _VALID_SOURCES:
        raise _abort(400, f"source must be one of: {', '.join(sorted(_VALID_SOURCES))}")
    try:
        result = _get_service().effectiveness(
            principal.user_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            source=source.upper() if source else None,
            metric=metric,
            min_sample=min_sample,
        )
        return {"ok": True, "data": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-context effectiveness failed")
        raise _abort(500, f"effectiveness failed: {exc}") from exc
