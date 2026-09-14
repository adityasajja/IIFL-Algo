"""Audit routes — ``/api/v1/audit``.

Reads the queryable table through :class:`~atr.services.audit.AuditService`, not
the JSONL file. The file remains the restart-proof sink that ``GET /audit``
(legacy) reads; this is the same facts with filters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from atr.api.deps import require_permission
from atr.auth.rbac import Permission
from atr.services.audit import AuditService

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

_READ = Depends(require_permission(Permission.AUDIT_READ))


@router.get("/actions", dependencies=[_READ])
def actions() -> dict[str, Any]:
    """Distinct action names, for building a filter."""
    return {"actions": AuditService().actions()}


@router.get("/events", dependencies=[_READ])
def events(
    user_id: str | None = None,
    action: str | None = None,
    result: str | None = Query(default=None, pattern="^(success|failure|denied|pending)$"),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Filtered, newest-first page of audit events."""
    rows, total = AuditService().query(
        user_id=user_id,
        action=action,
        result=result,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return {"events": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/events/{event_id}", dependencies=[_READ])
def event(event_id: str) -> dict[str, Any]:
    row = AuditService().get(event_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"detail": "no such audit event", "code": "not_found"},
        )
    return row
