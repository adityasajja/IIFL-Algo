"""Strategy Experiment Lab API endpoints (``/api/v1/experiments``).

Endpoints:
- GET    /                        - List experiments with optional strategy_id / status filter
- POST   /                        - Create experiment from recommendation or manual parameter changes
- GET    /{experiment_id}         - Get full experiment report (side-by-side, curves, distributions, explanation)
- POST   /{experiment_id}/run     - Run experiment evaluation under identical assumptions
- POST   /{experiment_id}/approve - Operator explicitly approves experiment
- POST   /{experiment_id}/reject  - Operator rejects experiment with reason
- POST   /{experiment_id}/apply   - Apply approved experiment to create immutable version V_{N+1}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import CurrentPrincipal, require_permission
from atr.auth.rbac import Permission
from atr.services.experiment import StrategyExperimentService

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


def _service() -> StrategyExperimentService:
    return StrategyExperimentService()


class CreateExperimentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(description="Target strategy ID")
    source_version: int | None = Field(default=None, description="Source strategy version, defaults to latest")
    parameter_changes: dict[str, Any] | None = Field(default=None, description="Parameter diffs: {name: {current, proposed}} or {name: value}")
    name: str | None = Field(default=None, description="Optional experiment name")
    reason: str | None = Field(default=None, description="Hypothesis or reason for change")
    recommendation_id: str | None = Field(default=None, description="Optional link to source optimization recommendation")


class RejectExperimentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, description="Optional rejection explanation")


@router.get(
    "",
    summary="List strategy experiments",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def list_experiments(
    strategy_id: str | None = Query(default=None, description="Filter by strategy ID"),
    exp_status: str | None = Query(default=None, alias="status", description="Filter by status (e.g. COMPLETED, APPROVED)"),
    limit: int = Query(default=50, ge=1, le=200),
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    items = service.list_experiments(strategy_id=strategy_id, status=exp_status, limit=limit)
    return {
        "experiments": items,
        "count": len(items),
    }


@router.post(
    "",
    summary="Create a new strategy experiment",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def create_experiment(
    body: CreateExperimentPayload,
    principal: CurrentPrincipal,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    try:
        exp = service.create_experiment(
            strategy_id=body.strategy_id,
            source_version=body.source_version,
            parameter_changes=body.parameter_changes,
            creator_user_id=principal.user_id,
            name=body.name,
            reason=body.reason,
            recommendation_id=body.recommendation_id,
        )
        return exp
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/{experiment_id}",
    summary="Get detailed experiment comparison and explanation",
    dependencies=[Depends(require_permission(Permission.STRATEGY_READ))],
)
def get_experiment(
    experiment_id: str,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    exp = service.get_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found")
    return exp


@router.post(
    "/{experiment_id}/run",
    summary="Execute experiment under identical baseline and candidate assumptions",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def run_experiment(
    experiment_id: str,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    try:
        updated = service.run_experiment(experiment_id)
        return updated
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{experiment_id}/approve",
    summary="Explicitly approve an experiment candidate",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def approve_experiment(
    experiment_id: str,
    principal: CurrentPrincipal,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    try:
        return service.approve_experiment(experiment_id, user_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{experiment_id}/reject",
    summary="Reject an experiment candidate",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def reject_experiment(
    experiment_id: str,
    principal: CurrentPrincipal,
    body: RejectExperimentPayload | None = None,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    try:
        reason = body.reason if body else None
        return service.reject_experiment(experiment_id, user_id=principal.user_id, reason=reason)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/{experiment_id}/apply",
    summary="Apply an approved experiment to create immutable version V(N+1)",
    dependencies=[Depends(require_permission(Permission.STRATEGY_WRITE))],
)
def apply_experiment(
    experiment_id: str,
    principal: CurrentPrincipal,
    service: StrategyExperimentService = Depends(_service),
) -> dict[str, Any]:
    try:
        return service.apply_experiment(experiment_id, user_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
