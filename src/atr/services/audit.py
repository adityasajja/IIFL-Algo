"""Audit query service.

The route layer may not touch SQL, so reading the audit trail goes through here.
That is not ceremony: it is what keeps "who is allowed to read the audit trail"
in one place, next to the query, rather than in a route that has to remember to
check it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from atr.appdb.engine import AppDatabase, get_app_db
from atr.appdb.repositories import AuditRepository


class AuditService:
    def __init__(self, db: AppDatabase | None = None) -> None:
        self._db = db

    @property
    def db(self) -> AppDatabase:
        return self._db or get_app_db()

    def query(
        self,
        *,
        user_id: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        with self.db.session() as session:
            return AuditRepository.query(
                session,
                user_id=user_id,
                action=action,
                result=result,
                # The API accepts tz-aware datetimes; storage is naive-UTC, so
                # normalise here rather than letting a comparison silently fail.
                since=since.replace(tzinfo=None) if since and since.tzinfo else since,
                until=until.replace(tzinfo=None) if until and until.tzinfo else until,
                limit=limit,
                offset=offset,
            )

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            return AuditRepository.get(session, event_id)

    def actions(self) -> list[str]:
        with self.db.session() as session:
            return AuditRepository.distinct_actions(session)


__all__ = ["AuditService"]
