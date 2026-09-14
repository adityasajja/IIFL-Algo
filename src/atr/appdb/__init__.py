"""Control-plane persistence: users, sessions, watchlists, audit.

Deliberately separate from :mod:`atr.data.store`, which holds market and trading
data on Postgres + TimescaleDB. The two have different shapes and different
scales: bars accumulate by the million and want hypertables, while a user table
has tens of rows and must work on a fresh clone with nothing installed. Keeping
them apart is what lets this package default to a SQLite file and still be
Postgres-compatible.

See ``docs/DATA_MODEL.md`` section 1 for the normative DDL.
"""

from __future__ import annotations

from atr.appdb.engine import AppDatabase, get_app_db
from atr.appdb.repositories import (
    ApiKeyRepository,
    AuditRepository,
    PreferenceRepository,
    SessionRepository,
    UserRepository,
    WatchlistRepository,
)

__all__ = [
    "ApiKeyRepository",
    "AppDatabase",
    "AuditRepository",
    "PreferenceRepository",
    "SessionRepository",
    "UserRepository",
    "WatchlistRepository",
    "get_app_db",
]
