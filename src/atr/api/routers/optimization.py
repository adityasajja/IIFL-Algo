"""Controlled strategy optimization and recommendation routes (``/api/v1/optimization``).

Endpoints:
- GET    /parameters/{strategy_id}               - View adaptive parameters
- POST   /candidates/{strategy_id}               - Generate parameter candidates from forward evidence
- POST   /run/{strategy_id}                      - Run full optimization evaluation cycle
- GET    /recommendations/{strategy_id}          - List recommendations
- GET    /recommendation/{recommendation_id}     - Get detailed recommendation
- POST   /recommendation/{recommendation_id}/approve - User approves recommendation
- POST   /recommendation/{recommendation_id}/reject  - User rejects recommendation
- POST   /recommendation/{recommendation_id}/apply   - User applies recommendation (creates V_{N+1})
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import CurrentPrincipal, require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.optimization import OptimizationService

router = APIRouter(prefix="/api/v1/optimization", tags=["optimization"])


def _service() -> OptimizationService:
    return OptimizationService()


class RejectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, description="Optional user rejection reason")


class OptimizationRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = Field(default=None, description="Source strategy version, defaults to latest")
    min_sample_size: int = Field(default=10, ge=5, description="Minimum sample size of forward observations")


@router.get(
    "/parameters/{strategy_id}",
    summary="List adaptive parameters explicitly marked on a strategy version",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def get_adaptive_parameters(
    strategy_id: str,
    version: int | None = Query(default=None, description="Strategy version, defaults to latest"),
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    try:
        return service.discover_adaptive_parameters(strategy_id, version=version)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/candidates/{strategy_id}",
    summary="Generate candidate parameter changes based on forward learning evidence",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def generate_candidates(
    strategy_id: str,
    payload: OptimizationRunPayload | None = None,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    version = payload.version if payload else None
    min_sample = payload.min_sample_size if payload else 10
    try:
        candidates = service.generate_candidates(strategy_id, version=version, min_sample_size=min_sample)
        return {
            "strategy_id": strategy_id,
            "version": version,
            "candidates": [c.to_dict() for c in candidates],
            "count": len(candidates),
        }
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/run/{strategy_id}",
    summary="Run controlled optimization cycle (Backtest -> Walk-Forward -> Robustness -> Recommendation)",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def run_optimization(
    strategy_id: str,
    payload: OptimizationRunPayload | None = None,
    principal: Principal = CurrentPrincipal,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    version = payload.version if payload else None
    min_sample = payload.min_sample_size if payload else 10
    try:
        recommendations = service.run_optimization(
            strategy_id,
            version=version,
            user_id=principal.user_id,
            min_sample_size=min_sample,
        )
        return {
            "strategy_id": strategy_id,
            "recommendations": recommendations,
            "count": len(recommendations),
        }
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/recommendations/{strategy_id}",
    summary="List stored optimization recommendations for a strategy",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def list_recommendations(
    strategy_id: str,
    rec_status: str | None = Query(default=None, alias="status", description="Filter by status (e.g. RECOMMENDED, APPROVED)"),
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    items = service.list_recommendations(strategy_id, status=rec_status)
    return {
        "strategy_id": strategy_id,
        "recommendations": items,
        "count": len(items),
    }


@router.get(
    "/recommendation/{recommendation_id}",
    summary="Get detailed recommendation record",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def get_recommendation(
    recommendation_id: str,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    rec = service.get_recommendation(recommendation_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found")
    return rec


@router.post(
    "/recommendation/{recommendation_id}/approve",
    summary="User explicitly approves an optimization recommendation",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def approve_recommendation(
    recommendation_id: str,
    principal: Principal = CurrentPrincipal,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    try:
        return service.approve_recommendation(recommendation_id, user_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/recommendation/{recommendation_id}/reject",
    summary="User rejects an optimization recommendation",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def reject_recommendation(
    recommendation_id: str,
    payload: RejectPayload | None = None,
    principal: Principal = CurrentPrincipal,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    reason = payload.reason if payload else None
    try:
        return service.reject_recommendation(recommendation_id, user_id=principal.user_id, reason=reason)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/recommendation/{recommendation_id}/apply",
    summary="Apply an approved recommendation to create a new immutable strategy version (V_N -> V_{N+1})",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def apply_recommendation(
    recommendation_id: str,
    principal: Principal = CurrentPrincipal,
    service: OptimizationService = Depends(_service),
) -> dict[str, Any]:
    try:
        return service.apply_recommendation(recommendation_id, author_user_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


class DailyLearningCyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str | None = Field(default=None, description="Optional strategy filter")
    min_sample_size: int = Field(default=10, ge=5, description="Sample size floor")


@router.post(
    "/cycle/run",
    summary="Execute automated daily learning cycle across forward trading observations",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def run_daily_learning_cycle(
    payload: DailyLearningCyclePayload | None = None,
    principal: Principal = CurrentPrincipal,
) -> dict[str, Any]:
    from atr.services.daily_learning import get_daily_learning_service

    service = get_daily_learning_service()
    strat_id = payload.strategy_id if payload else None
    min_sample = payload.min_sample_size if payload else 10

    report = service.run_cycle(
        strategy_id=strat_id,
        min_sample_size=min_sample,
        user_id=principal.user_id,
    )
    return report.to_dict()

