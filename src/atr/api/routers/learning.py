"""Learning routes — ``/api/v1/learning``.

What this surface is for
------------------------

Phases 1 to 3 of the learning engine produce four artefacts: a normalised
dataset, a per-axis performance analysis, a daily report, and a source-drift
comparison. This router is how they reach a screen.

**Read-only, and deliberately so.** There is no ``POST``, ``PUT``, ``PATCH`` or
``DELETE`` in this file. The user's constraint was that the learning engine may
never modify a live strategy, place an order, or bypass the risk engine — and a
write route is exactly where that guarantee would be lost, because a route is
the one surface a client can reach without going through the service's own
guards. A test asserts the method set is empty.

**One request per screen.** ``GET /learning/overview`` returns the dataset
summary, the report and the drift verdict together. The alternative — the
dashboard fetching them one at a time — lets a panel render a report built
against one dataset next to a drift verdict built against another, and the two
disagree for as long as the operator looks at them.

**Every payload says what it could not measure.** The response carries
``limited`` and ``limitations`` at the top level, because the current live
database holds zero trades and the honest answer is a well-formed empty one. A
client that renders this screen without reading those fields renders a blank
page; a client that reads them can say why.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.learning import LearningError, LearningService, get_learning_service

logger = logging.getLogger("atr.api.learning")

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])

#: Reading an outcome is reading a strategy's behaviour, so the read permission
#: is the strategy one. A new permission would mean a role that can see every
#: trade's P&L but not the strategy that produced it, which is not a useful
#: distinction to be able to grant.
_READ = require_permission(Permission.STRATEGY_READ)


def _fail(exc: LearningError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


@router.get("/status")
def learning_status(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """Whether there is anything to learn from, without building the dataset.

    Cheap by design. The dashboard calls this first so it can say "no closed
    trades yet" immediately instead of timing out on a full build over an empty
    book.
    """
    return get_learning_service().status()


@router.get("/dataset")
def learning_dataset(
    strategy: str | None = Query(None, description="unused; kept for a stable shape"),
    limit: int = Query(20_000, ge=1, le=100_000),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """The dataset's shape: how many trades, from which sources, missing what."""
    try:
        dataset = get_learning_service().dataset(refresh=True)
    except LearningError as exc:
        raise _fail(exc) from exc
    return dataset.summary()


@router.get("/dataset/rows")
def learning_dataset_rows(
    limit: int = Query(200, ge=1, le=2_000),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """A page of the normalised rows, for the table view.

    ``missing_features`` is returned **per row**, not only as a column summary.
    A row that has no MFE and a row whose MFE is genuinely zero look the same in
    a table; the per-row list is what lets the client render the difference.
    """
    dataset = get_learning_service().dataset(refresh=True)
    rows = dataset.rows[:limit]
    return {
        "total": len(dataset.rows),
        "returned": len(rows),
        "columns": list(dataset.frame().columns),
        "rows": rows,
    }


@router.get("/performance")
def learning_performance(
    strategy: str | None = Query(None, description="strategy id, or id@version"),
    roles: str | None = Query(None, description="comma-separated backtest,paper,live"),
    grades: str | None = Query(
        None, description="comma-separated forward,in_sample — the evidence direction"
    ),
    axes: str | None = Query(None, description="comma-separated axis names"),
    metric: str = Query("net_pnl", description="outcome column the buckets are ranked on"),
    min_sample: int | None = Query(None, ge=1, description="override the sample floor"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Sliced performance, with a sample size and a correction on every finding.

    ``notable`` is the part to render prominently: it holds only buckets that
    cleared the sample floor *and* survived the multiple-comparisons correction.
    ``breakdowns`` holds everything including the suppressed buckets, each
    carrying the ``n`` that caused the suppression.

    ``?grades=forward`` is how a caller asks for the out-of-sample sample alone.
    It is the filter a finding should be published on, and it is separate from
    ``?roles=`` because the two answer different questions: roles ask *which
    system produced this* (backtest, paper, real money), grades ask *is it
    evidence at all*.
    """
    role_list = _split(roles)
    axis_list = _split(axes)
    _reject_unknown_axes(axis_list)
    try:
        analysis = get_learning_service().performance(
            strategy=strategy,
            roles=role_list,
            grades=_split(grades),
            axes=axis_list,
            metric=metric,
            min_sample=min_sample,
            refresh=True,
        )
    except LearningError as exc:
        raise _fail(exc) from exc
    return analysis.as_dict()


@router.get("/report")
def learning_report(
    window_days: int = Query(90, ge=1, le=750),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """The daily learning report.

    ``advisory`` is ``True`` and ``applies_changes`` is ``False`` in the payload
    itself, not only in this docstring — the payload is what a client branches
    on, and a report that returned parameter values would eventually be fed to
    something that applies them.
    """
    report = get_learning_service().daily_report(window_days=window_days, refresh=True)
    return report.as_dict()


@router.get("/drift")
def learning_drift(
    strategy: str | None = Query(None, description="strategy id, or id@version"),
    reference: str | None = Query(None, description="baseline source; defaults to BACKTEST"),
    roles: str | None = Query(None, description="restrict which sources take part"),
    min_sample: int | None = Query(None, ge=1),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Backtest vs paper vs live.

    Read ``headline`` first and ``status`` on each metric second. A metric whose
    sample is too small is ``insufficient`` with a ``reason`` and a ``delta`` of
    ``None`` — the client must render it as *unmeasured*, never as zero.
    """
    try:
        analysis = get_learning_service().drift(
            strategy=strategy,
            reference=reference,
            roles=_split(roles),
            min_sample=min_sample,
            refresh=True,
        )
    except LearningError as exc:
        raise _fail(exc) from exc
    return analysis.as_dict()


@router.get("/overview")
def learning_overview(
    window_days: int = Query(90, ge=1, le=750),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Everything the learning screen needs, from one dataset build.

    The dataset is built once and the three views are derived from that single
    object, so the report, the analysis and the drift verdict cannot describe
    different books.
    """
    service: LearningService = get_learning_service()
    dataset = service.dataset(refresh=True)
    report = service.daily_report(window_days=window_days, refresh=False)
    analysis = service.performance(refresh=False)
    drift = service.drift(refresh=False)

    limitations = list(report.limitations)
    if not dataset.rows:
        limitations.insert(
            0,
            "the dataset is empty: no backtest trade and no journal entry has "
            "been recorded yet, so every section below is correctly absent "
            "rather than zero",
        )

    bvf = service.backtest_vs_forward(refresh=False)
    evidence_counts = service.forward_evidence_counts(refresh=False)
    try:
        observations = service.list_observations(limit=50)
    except Exception:
        observations = []

    # Include latest automated daily learning cycle report
    from atr.services.daily_learning import get_daily_learning_service
    daily_service = get_daily_learning_service()
    latest_cycle = daily_service.get_latest_cycle()

    return {
        "generated_at": dataset.summary()["generated_at"],
        "empty": not dataset.rows,
        "limited": bool(limitations),
        "limitations": limitations,
        "dataset": dataset.summary(),
        "report": report.as_dict(),
        "analysis": analysis.as_dict(),
        "drift": drift.as_dict(),
        "backtest_vs_forward": bvf,
        "forward_evidence_counts": evidence_counts,
        "observations": observations,
        "latest_cycle": latest_cycle,
        "missing_features": dataset.missing_feature_reasons,
        # Stated in the payload so a client cannot infer that anything is
        # applied, whatever it renders.
        "advisory": True,
        "applies_changes": False,
    }


@router.get("/cycle/latest")
def learning_cycle_latest(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """Retrieve the latest automated daily learning cycle report and diagnostics."""
    from atr.services.daily_learning import get_daily_learning_service

    latest = get_daily_learning_service().get_latest_cycle()
    return {"latest_cycle": latest}


@router.get("/cycle/history")
def learning_cycle_history(
    limit: int = Query(10, ge=1, le=50),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Retrieve historical automated learning cycles for reproducibility and audit."""
    from atr.services.daily_learning import get_daily_learning_service

    history = get_daily_learning_service().get_cycle_history(limit=limit)
    return {"history": history, "count": len(history)}



@router.get("/backtest-vs-forward")
def learning_backtest_vs_forward(
    strategy: str | None = Query(None, description="strategy id or symbol"),
    strategy_version: int | None = Query(None, description="strategy version"),
    min_sample: int | None = Query(None, ge=1),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Side-by-side comparison of BACKTEST vs PAPER_FORWARD evidence."""
    try:
        return get_learning_service().backtest_vs_forward(
            strategy=strategy,
            strategy_version=strategy_version,
            min_sample=min_sample,
            refresh=True,
        )
    except LearningError as exc:
        raise _fail(exc) from exc


@router.get("/evidence-counts")
def learning_evidence_counts(
    strategy: str | None = Query(None, description="Optional strategy id to filter"),
    strategy_version: int | None = Query(None, description="Optional strategy version to filter"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Genuine Forward Observations counter broken down by strategy, version, today, 7d, and total."""
    return get_learning_service().forward_evidence_counts(
        strategy_id=strategy,
        strategy_version=strategy_version,
        user_id=principal.user_id,
        refresh=True,
    )


@router.get("/readiness")
def learning_readiness(
    strategy_id: str | None = Query(None, description="Optional strategy id to filter"),
    refresh: bool = Query(False, description="Rebuild the dataset first"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Forward-learning readiness, per strategy.

    Whether trustworthy evidence is accumulating: forward counts against the
    10/30/50 gates, research states (NOT READY → MINIMUM SAMPLE →
    ANALYSIS READY → OPTIMIZATION ELIGIBLE), weekly progression, and
    data-quality issues. States are research labels — this surface, like every
    other learning route, changes nothing.
    """
    try:
        return get_learning_service().readiness(
            strategy_id=strategy_id,
            refresh=refresh,
            user_id=principal.user_id,
        )
    except LearningError as exc:
        raise _fail(exc) from exc


@router.get("/observations")
def learning_observations(
    strategy: str | None = Query(None, description="filter by strategy"),
    strategy_version: int | None = Query(None, description="filter by strategy version"),
    evidence_class: str | None = Query(None, description="filter by evidence class"),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Persisted learning observation history."""
    service = get_learning_service()
    try:
        obs = service.list_observations(
            strategy_id=strategy,
            strategy_version=strategy_version,
            evidence_class=evidence_class,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("learning: could not list observations: %s", exc)
        obs = []
    return {
        "total": len(obs),
        "observations": obs,
    }


@router.get("/axes")
def learning_axes(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """The axes the analysis can slice by, and the ones it cannot.

    Served so the dashboard's axis picker offers exactly what the engine
    supports instead of a hardcoded list that silently goes stale, and so the
    unavailable axes can be shown with their reason rather than omitted — a
    missing VWAP control reads as a bug, an unavailable one reads as a finding.
    """
    from atr.research.learning_axes import AXES_BY_NAME, DEFAULT_AXES, requested_but_unavailable

    return {
        "available": {
            name: {"label": axis.label, "max_values": axis.max_values}
            for name, axis in sorted(AXES_BY_NAME.items())
        },
        "default": [axis.name for axis in DEFAULT_AXES],
        "unavailable": requested_but_unavailable(),
    }


def _reject_unknown_axes(names: list[str] | None) -> None:
    """400 on a typo'd axis rather than silently analysing the default set.

    ``axes_from_names`` ignores unknown names and falls back to every default —
    a reasonable choice for a report that must always render, and the wrong one
    over HTTP. A client asking for ``?axes=rvol_bukcet`` would receive a full,
    plausible analysis of eleven other axes and no indication that its own
    request was discarded. Returning the default set under a name the caller did
    not ask for is how a dashboard ends up confidently showing the wrong chart.
    """
    if not names:
        return
    from atr.research.learning_axes import AXES_BY_NAME

    unknown = [name for name in names if name.strip().lower() not in AXES_BY_NAME]
    if unknown:
        raise HTTPException(
            400,
            detail={
                "detail": (
                    f"unknown axis/axes {sorted(unknown)}; available: "
                    f"{', '.join(sorted(AXES_BY_NAME))}"
                ),
                "code": "unknown_axis",
                "unknown": sorted(unknown),
            },
        )


def _split(value: str | None) -> list[str] | None:
    """``"a,b"`` → ``["a", "b"]``; empty or absent → ``None`` (meaning "all").

    Returning ``None`` rather than an empty list matters: an empty list would
    filter every row out, so a caller passing ``?roles=`` would get a
    correctly-shaped page saying there are no trades — which is a lie about a
    database that is full.
    """
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if part] or None
