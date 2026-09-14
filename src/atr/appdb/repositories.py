"""Repositories for the control-plane tables.

Every method takes a :class:`~sqlalchemy.orm.Session` so the caller owns the
transaction boundary — a use case that writes a user *and* an audit row must be
able to do both atomically, which a repository that opened its own session could
not offer.

Two conventions matter more than the code:

* **Reads are user-scoped.** :meth:`WatchlistRepository.get` takes a ``user_id``
  and filters on it. There is deliberately no ``get_by_id()`` that returns any
  row, because a method that can return someone else's data is a method someone
  will eventually call from a route that forgot to check.
* **Writes return the affected row count.** A zero row count is how the caller
  learns "not yours" without a second query, and it is also what makes the
  404-not-403 rule cheap to implement.

The trading-tier repositories (:class:`OrderRepository`, :class:`OrderEventRepository`,
:class:`OrderIntentRepository`) add three more, and they are the ones that matter
most because they guard money:

* **The event log is authoritative.** :meth:`OrderEventRepository.append` is the
  only thing in the codebase that writes ``orders.status``, and it writes it as a
  projection of the event in the same transaction. There is deliberately no
  ``OrderRepository.update()``: a generic setter on the status column is the
  escape hatch that would let a caller change an order's state without leaving a
  trace of who did it or why.
* **Uniqueness is enforced by the database, not by a check-then-insert.** Both
  idempotency and strategy-version dedupe race between the check and the write, so
  both rely on a primary key or unique constraint and treat the resulting
  ``IntegrityError`` as the *expected* outcome rather than an error. A
  ``SELECT`` first and an ``INSERT`` after is two statements with a gap in the
  middle, and the gap is exactly where the duplicate order lives.
* **Timestamps are naive UTC.** See the note in :mod:`atr.appdb.schema`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from atr.appdb.engine import utcnow
from atr.appdb.schema import (
    api_keys,
    audit_events,
    backtest_runs,
    deployments,
    order_events,
    order_intents,
    orders,
    reconciliation_runs,
    screener_scans,
    sessions,
    strategies,
    strategy_versions,
    system_state,
    trade_journal,
    user_preferences,
    users,
    watchlist_columns,
    watchlist_items,
    watchlists,
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _one(row: Any) -> dict[str, Any] | None:
    return None if row is None else dict(row._mapping)


def _many(rows: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- users
class UserRepository:
    """Accounts. Password material is opaque here — hashing lives in ``atr.auth``."""

    @staticmethod
    def create(
        session: Session,
        *,
        email: str,
        username: str,
        password_hash: str,
        role: str,
        display_name: str | None = None,
        mfa_secret: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        row = {
            "user_id": _new_id(),
            # Normalised on write so uniqueness is case-insensitive without a
            # functional index (which SQLite and Postgres spell differently).
            "email": email.strip().lower(),
            "username": username.strip(),
            "display_name": display_name or username.strip(),
            "password_hash": password_hash,
            "role": role,
            "is_active": True,
            "mfa_enabled": False,
            "mfa_secret": mfa_secret,
            "failed_logins": 0,
            "locked_until": None,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
        }
        session.execute(insert(users).values(**row))
        return row

    @staticmethod
    def get(session: Session, user_id: str) -> dict[str, Any] | None:
        return _one(session.execute(select(users).where(users.c.user_id == user_id)).first())

    @staticmethod
    def get_by_identifier(session: Session, identifier: str) -> dict[str, Any] | None:
        """Look up by email or username, case-insensitively on either."""
        ident = identifier.strip()
        stmt = select(users).where(
            (users.c.email == ident.lower()) | (users.c.username == ident)
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_all(session: Session) -> list[dict[str, Any]]:
        return _many(session.execute(select(users).order_by(users.c.created_at)).all())

    @staticmethod
    def update(session: Session, user_id: str, **fields: Any) -> int:
        if not fields:
            return 0
        if "email" in fields and fields["email"]:
            fields["email"] = str(fields["email"]).strip().lower()
        fields["updated_at"] = utcnow()
        result = session.execute(update(users).where(users.c.user_id == user_id).values(**fields))
        return int(result.rowcount or 0)

    @staticmethod
    def count(session: Session) -> int:
        return int(session.execute(select(func.count()).select_from(users)).scalar() or 0)

    @staticmethod
    def count_by_role(session: Session, role: str) -> int:
        stmt = select(func.count()).select_from(users).where(users.c.role == role)
        return int(session.execute(stmt).scalar() or 0)

    @staticmethod
    def register_failure(
        session: Session, user_id: str, *, max_attempts: int, lockout_minutes: int
    ) -> dict[str, Any]:
        """Increment the failure counter, locking the account at the threshold.

        Returns the post-update counters so the caller can tell the user how many
        attempts remain. The lock is time-based rather than permanent so a
        legitimate user who fat-fingered a password is not locked out forever,
        and so support has an answer that is not "edit the database".
        """
        current = UserRepository.get(session, user_id)
        if current is None:
            return {"failed_logins": 0, "locked_until": None}
        failed = int(current.get("failed_logins") or 0) + 1
        locked_until = None
        if failed >= max_attempts:
            locked_until = utcnow() + timedelta(minutes=lockout_minutes)
        session.execute(
            update(users)
            .where(users.c.user_id == user_id)
            .values(failed_logins=failed, locked_until=locked_until, updated_at=utcnow())
        )
        return {"failed_logins": failed, "locked_until": locked_until}

    @staticmethod
    def register_success(session: Session, user_id: str) -> None:
        session.execute(
            update(users)
            .where(users.c.user_id == user_id)
            .values(
                failed_logins=0,
                locked_until=None,
                last_login_at=utcnow(),
                updated_at=utcnow(),
            )
        )

    @staticmethod
    def delete(session: Session, user_id: str) -> int:
        return int(session.execute(delete(users).where(users.c.user_id == user_id)).rowcount or 0)


# ------------------------------------------------------------------------ sessions
class SessionRepository:
    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
        mfa_satisfied: bool = True,
    ) -> dict[str, Any]:
        now = utcnow()
        row = {
            "session_id": _new_id(),
            "user_id": user_id,
            "token_hash": token_hash,
            "created_at": now,
            "expires_at": expires_at,
            "last_seen_at": now,
            "revoked_at": None,
            "ip": (ip or "")[:64] or None,
            "user_agent": (user_agent or "")[:255] or None,
            "mfa_satisfied": mfa_satisfied,
        }
        session.execute(insert(sessions).values(**row))
        return row

    @staticmethod
    def get_by_token_hash(session: Session, token_hash: str) -> dict[str, Any] | None:
        stmt = select(sessions).where(sessions.c.token_hash == token_hash)
        return _one(session.execute(stmt).first())

    @staticmethod
    def get(session: Session, session_id: str) -> dict[str, Any] | None:
        stmt = select(sessions).where(sessions.c.session_id == session_id)
        return _one(session.execute(stmt).first())

    @staticmethod
    def touch(session: Session, session_id: str, when: datetime | None = None) -> None:
        session.execute(
            update(sessions)
            .where(sessions.c.session_id == session_id)
            .values(last_seen_at=when or utcnow())
        )

    @staticmethod
    def set_mfa_satisfied(session: Session, session_id: str, satisfied: bool = True) -> None:
        session.execute(
            update(sessions)
            .where(sessions.c.session_id == session_id)
            .values(mfa_satisfied=satisfied)
        )

    @staticmethod
    def revoke(session: Session, session_id: str) -> int:
        result = session.execute(
            update(sessions)
            .where(sessions.c.session_id == session_id, sessions.c.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return int(result.rowcount or 0)

    @staticmethod
    def revoke_all_for_user(session: Session, user_id: str) -> int:
        result = session.execute(
            update(sessions)
            .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return int(result.rowcount or 0)

    @staticmethod
    def list_for_user(session: Session, user_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        stmt = select(sessions).where(sessions.c.user_id == user_id)
        if active_only:
            stmt = stmt.where(sessions.c.revoked_at.is_(None), sessions.c.expires_at > utcnow())
        return _many(session.execute(stmt.order_by(sessions.c.created_at.desc())).all())

    @staticmethod
    def purge_expired(session: Session, *, older_than_days: int = 7) -> int:
        """Delete long-expired rows. Keeps the table from growing forever."""
        cutoff = utcnow() - timedelta(days=older_than_days)
        result = session.execute(delete(sessions).where(sessions.c.expires_at < cutoff))
        return int(result.rowcount or 0)


# ------------------------------------------------------------------------ api keys
class ApiKeyRepository:
    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        label: str,
        prefix: str,
        key_hash: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = {
            "key_id": _new_id(),
            "user_id": user_id,
            "label": label[:64],
            "prefix": prefix[:12],
            "key_hash": key_hash,
            "scopes": ",".join(sorted(set(scopes))),
            "created_at": utcnow(),
            "expires_at": expires_at,
            "last_used_at": None,
            "revoked_at": None,
        }
        session.execute(insert(api_keys).values(**row))
        return row

    @staticmethod
    def get_by_hash(session: Session, key_hash: str) -> dict[str, Any] | None:
        stmt = select(api_keys).where(api_keys.c.key_hash == key_hash)
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(session: Session, user_id: str) -> list[dict[str, Any]]:
        stmt = select(api_keys).where(api_keys.c.user_id == user_id).order_by(api_keys.c.created_at.desc())
        return _many(session.execute(stmt).all())

    @staticmethod
    def touch(session: Session, key_id: str) -> None:
        session.execute(update(api_keys).where(api_keys.c.key_id == key_id).values(last_used_at=utcnow()))

    @staticmethod
    def revoke(session: Session, key_id: str, user_id: str) -> int:
        result = session.execute(
            update(api_keys)
            .where(
                api_keys.c.key_id == key_id,
                api_keys.c.user_id == user_id,
                api_keys.c.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------- watchlists
class WatchlistRepository:
    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        name: str,
        exchange: str = "NSEEQ",
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        existing = WatchlistRepository.list_for_user(session, user_id)
        row = {
            "watchlist_id": _new_id(),
            "user_id": user_id,
            "name": name.strip()[:64],
            "exchange": exchange,
            # The first list a user makes becomes their default, so "add to
            # watchlist" has an answer before the user has expressed a preference.
            "is_default": len(existing) == 0,
            "sort_order": len(existing),
            "created_at": now,
            "updated_at": now,
        }
        session.execute(insert(watchlists).values(**row))
        if columns:
            WatchlistRepository.set_columns(session, row["watchlist_id"], columns)
        return row

    @staticmethod
    def list_for_user(session: Session, user_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(watchlists)
            .where(watchlists.c.user_id == user_id)
            .order_by(watchlists.c.sort_order, watchlists.c.created_at)
        )
        rows = _many(session.execute(stmt).all())
        for row in rows:
            row["item_count"] = WatchlistRepository.item_count(session, row["watchlist_id"])
        return rows

    @staticmethod
    def get(session: Session, watchlist_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(watchlists).where(
            watchlists.c.watchlist_id == watchlist_id, watchlists.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def update(session: Session, watchlist_id: str, user_id: str, **fields: Any) -> int:
        allowed = {"name", "exchange", "is_default", "sort_order"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return 0
        if "name" in values and values["name"]:
            values["name"] = str(values["name"]).strip()[:64]
        values["updated_at"] = utcnow()
        result = session.execute(
            update(watchlists)
            .where(watchlists.c.watchlist_id == watchlist_id, watchlists.c.user_id == user_id)
            .values(**values)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def delete(session: Session, watchlist_id: str, user_id: str) -> int:
        result = session.execute(
            delete(watchlists).where(
                watchlists.c.watchlist_id == watchlist_id, watchlists.c.user_id == user_id
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def set_default(session: Session, watchlist_id: str, user_id: str) -> int:
        """Exactly one default per user, set in one transaction."""
        session.execute(
            update(watchlists).where(watchlists.c.user_id == user_id).values(is_default=False)
        )
        result = session.execute(
            update(watchlists)
            .where(watchlists.c.watchlist_id == watchlist_id, watchlists.c.user_id == user_id)
            .values(is_default=True, updated_at=utcnow())
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ items
    @staticmethod
    def item_count(session: Session, watchlist_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(watchlist_items)
            .where(watchlist_items.c.watchlist_id == watchlist_id)
        )
        return int(session.execute(stmt).scalar() or 0)

    @staticmethod
    def items(session: Session, watchlist_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(watchlist_items)
            .where(watchlist_items.c.watchlist_id == watchlist_id)
            .order_by(watchlist_items.c.sort_order, watchlist_items.c.added_at)
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def add_items(
        session: Session, watchlist_id: str, symbols: list[str]
    ) -> tuple[list[str], list[str]]:
        """Append symbols, skipping ones already present.

        Returns ``(added, skipped)``. Duplicates are reported rather than raising
        so a multi-symbol paste is not all-or-nothing: adding five names of which
        one is already there should add four.
        """
        existing = {row["symbol"] for row in WatchlistRepository.items(session, watchlist_id)}
        next_order = (
            int(
                session.execute(
                    select(func.max(watchlist_items.c.sort_order)).where(
                        watchlist_items.c.watchlist_id == watchlist_id
                    )
                ).scalar()
                or -1
            )
            + 1
        )
        added: list[str] = []
        skipped: list[str] = []
        now = utcnow()
        for symbol in symbols:
            clean = symbol.strip().upper()
            if not clean or clean in existing:
                skipped.append(clean)
                continue
            session.execute(
                insert(watchlist_items).values(
                    item_id=_new_id(),
                    watchlist_id=watchlist_id,
                    symbol=clean,
                    sort_order=next_order,
                    note=None,
                    added_at=now,
                )
            )
            existing.add(clean)
            added.append(clean)
            next_order += 1
        return added, skipped

    @staticmethod
    def remove_item(session: Session, watchlist_id: str, symbol: str) -> int:
        result = session.execute(
            delete(watchlist_items).where(
                watchlist_items.c.watchlist_id == watchlist_id,
                watchlist_items.c.symbol == symbol.strip().upper(),
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def reorder_items(session: Session, watchlist_id: str, symbols: list[str]) -> int:
        """Rewrite ``sort_order`` from the given sequence.

        The whole order is supplied rather than a move operation, which makes the
        call idempotent: a retry after a dropped response cannot reorder twice.
        Symbols not in the list keep their relative order after the listed ones,
        so a client that sends a partial list does not lose rows.
        """
        current = WatchlistRepository.items(session, watchlist_id)
        by_symbol = {row["symbol"]: row for row in current}
        ordered = [s.strip().upper() for s in symbols if s.strip().upper() in by_symbol]
        seen = set(ordered)
        ordered.extend(row["symbol"] for row in current if row["symbol"] not in seen)
        updated = 0
        for index, symbol in enumerate(ordered):
            row = by_symbol[symbol]
            if row["sort_order"] == index:
                continue
            session.execute(
                update(watchlist_items)
                .where(watchlist_items.c.item_id == row["item_id"])
                .values(sort_order=index)
            )
            updated += 1
        return updated

    # ---------------------------------------------------------------- columns
    @staticmethod
    def columns(session: Session, watchlist_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(watchlist_columns)
            .where(watchlist_columns.c.watchlist_id == watchlist_id)
            .order_by(watchlist_columns.c.sort_order)
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def set_columns(session: Session, watchlist_id: str, keys: list[str]) -> list[dict[str, Any]]:
        """Replace the column configuration wholesale."""
        session.execute(delete(watchlist_columns).where(watchlist_columns.c.watchlist_id == watchlist_id))
        seen: set[str] = set()
        for index, key in enumerate(keys):
            clean = key.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            session.execute(
                insert(watchlist_columns).values(
                    column_id=_new_id(),
                    watchlist_id=watchlist_id,
                    key=clean,
                    sort_order=index,
                    width=None,
                    visible=True,
                )
            )
        return WatchlistRepository.columns(session, watchlist_id)


# -------------------------------------------------------------------------- audit
class AuditRepository:
    @staticmethod
    def append(
        session: Session,
        *,
        action: str,
        result: str = "success",
        user_id: str | None = None,
        actor: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        request_id: str | None = None,
        detail: dict[str, Any] | str | None = None,
        ip: str | None = None,
        ts: datetime | None = None,
    ) -> str:
        event_id = _new_id()
        if isinstance(detail, dict):
            detail_text: str | None = json.dumps(detail, default=str)
        else:
            detail_text = detail
        session.execute(
            insert(audit_events).values(
                event_id=event_id,
                ts=ts or utcnow(),
                user_id=user_id,
                actor=(actor or "")[:64] or None,
                action=action[:64],
                target_type=target_type,
                target_id=target_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                request_id=request_id,
                result=result[:16],
                detail=detail_text,
                ip=(ip or "")[:64] or None,
            )
        )
        return event_id

    @staticmethod
    def query(
        session: Session,
        *,
        user_id: str | None = None,
        action: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = []
        if user_id:
            conditions.append(audit_events.c.user_id == user_id)
        if action:
            conditions.append(audit_events.c.action == action)
        if result:
            conditions.append(audit_events.c.result == result)
        if since:
            conditions.append(audit_events.c.ts >= since)
        if until:
            conditions.append(audit_events.c.ts <= until)

        total = int(
            session.execute(
                select(func.count()).select_from(audit_events).where(*conditions)
            ).scalar()
            or 0
        )
        stmt = (
            select(audit_events)
            .where(*conditions)
            .order_by(audit_events.c.ts.desc())
            .limit(max(1, min(limit, 1000)))
            .offset(max(0, offset))
        )
        return _many(session.execute(stmt).all()), total

    @staticmethod
    def get(session: Session, event_id: str) -> dict[str, Any] | None:
        stmt = select(audit_events).where(audit_events.c.event_id == event_id)
        return _one(session.execute(stmt).first())

    @staticmethod
    def distinct_actions(session: Session) -> list[str]:
        stmt = select(audit_events.c.action).distinct().order_by(audit_events.c.action)
        return [str(r[0]) for r in session.execute(stmt).all()]


# -------------------------------------------------------------------- preferences
class PreferenceRepository:
    @staticmethod
    def get(session: Session, user_id: str, key: str, default: Any = None) -> Any:
        stmt = select(user_preferences.c.value).where(
            user_preferences.c.user_id == user_id, user_preferences.c.key == key
        )
        raw = session.execute(stmt).scalar()
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_all(session: Session, user_id: str) -> dict[str, Any]:
        stmt = select(user_preferences).where(user_preferences.c.user_id == user_id)
        out: dict[str, Any] = {}
        for row in _many(session.execute(stmt).all()):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = None
        return out

    @staticmethod
    def set(session: Session, user_id: str, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        existing = session.execute(
            select(user_preferences.c.key).where(
                user_preferences.c.user_id == user_id, user_preferences.c.key == key
            )
        ).first()
        if existing is None:
            session.execute(
                insert(user_preferences).values(
                    user_id=user_id, key=key, value=payload, updated_at=utcnow()
                )
            )
        else:
            session.execute(
                update(user_preferences)
                .where(user_preferences.c.user_id == user_id, user_preferences.c.key == key)
                .values(value=payload, updated_at=utcnow())
            )


# ===========================================================================
# Trading tier
# ===========================================================================
#: Statuses from which no further transition is legal. Kept here rather than in
#: the OMS because two writers need it: the OMS to refuse the transition, and
#: these repositories to set `completed_at` and to stop accepting progress.
TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})
TERMINAL_BACKTEST_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class IdempotencyConflict(RuntimeError):
    """An idempotency key exists but belongs to a different caller.

    This should be unreachable — the key is derived from fields that are already
    user-scoped — and it is raised rather than swallowed because the alternative
    is returning *another user's order* to this caller, which is the one failure
    mode a duplicate-order guard must never have.
    """


# The key's *derivation* lives in ``atr.execution.oms`` alongside ``OrderDraft``,
# because it is a statement about intent rather than about storage and the route
# layer needs it without being allowed to import this module. Only the storage of
# the key belongs here, where its primary key is.


def _json_or_text(value: dict[str, Any] | str | None) -> str | None:
    """Accept a dict or a pre-serialised string; store text either way."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def _insert_tolerating_duplicate(session: Session, stmt: Any) -> bool:
    """Run an INSERT inside a savepoint, returning False if the constraint fired.

    The savepoint matters: without it a failed insert poisons the caller's
    transaction, and the caller's transaction is the one that has to stay usable
    so it can go and read the row that already exists.

    A duplicate returns ``False`` rather than raising, because for both callers
    here a duplicate is the *expected* outcome — an idempotent retry, or a
    sequence allocator losing a race. Any other integrity error is re-raised
    unchanged so the real cause is not hidden behind a boolean.
    """
    try:
        with session.begin_nested():
            session.execute(stmt)
        return True
    except IntegrityError:
        return False


class OrderRepository:
    """Orders. Status is written only by :meth:`OrderEventRepository.append`."""

    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        symbol: str,
        side: str,
        quantity: float,
        mode: str = "PAPER",
        exchange: str = "NSEEQ",
        asset_class: str = "EQUITY",
        order_type: str = "MARKET",
        limit_price: float | None = None,
        stop_price: float | None = None,
        tif: str = "DAY",
        product: str | None = None,
        requested_price: float | None = None,
        deployment_id: str | None = None,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        signal_id: str | None = None,
        correlation_id: str | None = None,
        tag: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert an order *and* its opening ``NEW`` event.

        The two writes are one call on purpose. An order row with no events is
        malformed — the log is what the lifecycle is reconstructed from — and a
        caller that had to remember to write the first event would eventually
        forget. Starting status is not a parameter for the same reason.
        """
        mode = (mode or "PAPER").strip().upper()
        if mode not in ("PAPER", "LIVE"):
            raise ValueError(f"mode must be PAPER or LIVE, got {mode!r}")
        side = (side or "").strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if float(quantity) <= 0:
            raise ValueError("quantity must be positive")
        # Canonicalise on the way in, so `SL-M` and `STOP` are one stored value and
        # an unrecognised spelling is refused here rather than at a broker call.
        # This is the fix for `OrderType.SL_MARKET`, which did not exist: the
        # signal-execute route placed its entry leg and then raised on the stop.
        from atr.core.enums import OrderType

        order_type = OrderType.parse(order_type).value

        now = utcnow()
        order_id = _new_id()
        row = {
            "order_id": order_id,
            "user_id": user_id,
            "deployment_id": deployment_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "signal_id": signal_id,
            "correlation_id": correlation_id,
            "symbol": symbol.strip().upper(),
            "exchange": exchange,
            "asset_class": asset_class,
            "side": side,
            "quantity": float(quantity),
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "tif": tif,
            "product": product,
            "mode": mode,
            "status": "NEW",
            "broker_order_id": None,
            "filled_quantity": 0.0,
            "avg_fill_price": 0.0,
            "requested_price": requested_price,
            "tag": tag,
            "reject_reason": None,
            "created_at": now,
            "updated_at": now,
            "submitted_at": None,
            "completed_at": None,
        }
        session.execute(insert(orders).values(**row))
        OrderEventRepository.append(
            session,
            order_id=order_id,
            from_status=None,
            to_status="NEW",
            ts=now,
            requested_price=requested_price,
            source="oms",
            correlation_id=correlation_id,
            request_id=request_id,
        )
        return OrderRepository.get(session, order_id, user_id) or row

    @staticmethod
    def get(session: Session, order_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(orders).where(
            orders.c.order_id == order_id, orders.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def get_by_broker_order_id(
        session: Session, broker_order_id: str, user_id: str
    ) -> dict[str, Any] | None:
        stmt = select(orders).where(
            orders.c.broker_order_id == broker_order_id, orders.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session,
        user_id: str,
        *,
        status: str | None = None,
        symbol: str | None = None,
        deployment_id: str | None = None,
        correlation_id: str | None = None,
        mode: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [orders.c.user_id == user_id]
        if status:
            conditions.append(orders.c.status == status)
        if symbol:
            conditions.append(orders.c.symbol == symbol.strip().upper())
        if deployment_id:
            conditions.append(orders.c.deployment_id == deployment_id)
        if correlation_id:
            conditions.append(orders.c.correlation_id == correlation_id)
        if mode:
            conditions.append(orders.c.mode == mode.strip().upper())
        if since:
            conditions.append(orders.c.created_at >= since)

        total = int(
            session.execute(
                select(func.count()).select_from(orders).where(*conditions)
            ).scalar()
            or 0
        )
        stmt = (
            select(orders)
            .where(*conditions)
            .order_by(orders.c.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        return _many(session.execute(stmt).all()), total

    @staticmethod
    def open_orders(session: Session, user_id: str) -> list[dict[str, Any]]:
        """Orders that are neither filled nor cancelled nor rejected.

        Defined as "not in :data:`TERMINAL_ORDER_STATUSES`" rather than as a list
        of open statuses, so adding a status to the machine cannot silently leave
        orders out of reconciliation.
        """
        stmt = (
            select(orders)
            .where(
                orders.c.user_id == user_id,
                orders.c.status.notin_(sorted(TERMINAL_ORDER_STATUSES)),
            )
            .order_by(orders.c.created_at)
        )
        return _many(session.execute(stmt).all())


class OrderEventRepository:
    """The append-only order log. There is no update and no delete here."""

    @staticmethod
    def _next_seq(session: Session, order_id: str) -> int:
        stmt = select(func.max(order_events.c.seq)).where(
            order_events.c.order_id == order_id
        )
        return int(session.execute(stmt).scalar() or 0) + 1

    @staticmethod
    def append(
        session: Session,
        *,
        order_id: str,
        to_status: str,
        from_status: str | None = None,
        ts: datetime | None = None,
        broker_ts: datetime | None = None,
        ack_ts: datetime | None = None,
        fill_ts: datetime | None = None,
        requested_price: float | None = None,
        filled_price: float | None = None,
        filled_qty: float | None = None,
        slippage_bps: float | None = None,
        commission: float | None = None,
        latency_ms: int | None = None,
        reject_reason: str | None = None,
        raw: dict[str, Any] | str | None = None,
        source: str = "oms",
        correlation_id: str | None = None,
        request_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one transition and reproject ``orders`` from it.

        ``filled_qty`` is **cumulative**, not incremental — the same convention
        the broker uses, so the parsed field and the raw payload cannot disagree.
        A single fill's size is the difference between consecutive events, which
        ``seq`` makes unambiguous.

        Fields the event does not carry are left out of the projection rather
        than written as ``NULL``: an ``ACKNOWLEDGED`` event must not erase the
        ``requested_price`` recorded when the order was raised, or every slippage
        figure downstream becomes unmeasurable the moment an order is acked.
        """
        to_status = (to_status or "").strip().upper()
        if not to_status:
            raise ValueError("to_status is required")
        ts = ts or utcnow()
        event: dict[str, Any] = {
            "order_event_id": _new_id(),
            "order_id": order_id,
            "seq": 0,
            "from_status": from_status,
            "to_status": to_status,
            "ts": ts,
            "broker_ts": broker_ts,
            "ack_ts": ack_ts,
            "fill_ts": fill_ts,
            "requested_price": requested_price,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "slippage_bps": slippage_bps,
            "commission": commission,
            "latency_ms": latency_ms,
            "reject_reason": (reject_reason or None) and str(reject_reason)[:255],
            "raw": _json_or_text(raw),
            "source": (source or "oms")[:16],
            "correlation_id": correlation_id,
            "request_id": request_id,
        }
        # The retry is for the sequence allocator losing a race, which is the
        # only way `UNIQUE(order_id, seq)` can fire on a well-formed event.
        for attempt in range(2):
            event["seq"] = OrderEventRepository._next_seq(session, order_id)
            try:
                with session.begin_nested():
                    session.execute(insert(order_events).values(**event))
                break
            except IntegrityError:
                if attempt == 1:
                    raise
                continue
        OrderEventRepository._project(
            session, order_id, event, broker_order_id=broker_order_id
        )
        return event

    @staticmethod
    def _project(
        session: Session,
        order_id: str,
        event: dict[str, Any],
        *,
        broker_order_id: str | None = None,
    ) -> None:
        """Fold one event into ``orders``, in the caller's transaction.

        Written here, next to the insert, rather than by the OMS: two writes that
        must agree should not be two decisions made in two places. The invariant
        "``orders.status`` equals the newest event's ``to_status``" is asserted
        directly by ``tests/test_oms.py``.
        """
        values: dict[str, Any] = {
            "status": event["to_status"],
            "updated_at": event["ts"],
        }
        if event.get("filled_qty") is not None:
            values["filled_quantity"] = float(event["filled_qty"])
        if event.get("filled_price") is not None:
            values["avg_fill_price"] = float(event["filled_price"])
        if event.get("requested_price") is not None:
            values["requested_price"] = float(event["requested_price"])
        if event.get("reject_reason"):
            values["reject_reason"] = event["reject_reason"]
        if broker_order_id:
            values["broker_order_id"] = broker_order_id
        if event["to_status"] == "SUBMITTED":
            values["submitted_at"] = event["ts"]
        if event["to_status"] in TERMINAL_ORDER_STATUSES:
            values["completed_at"] = event["ts"]
        session.execute(
            update(orders).where(orders.c.order_id == order_id).values(**values)
        )

    @staticmethod
    def history(session: Session, order_id: str) -> list[dict[str, Any]]:
        """Every transition, oldest first. ``seq`` is the ordering authority."""
        stmt = (
            select(order_events)
            .where(order_events.c.order_id == order_id)
            .order_by(order_events.c.seq)
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def latest(session: Session, order_id: str) -> dict[str, Any] | None:
        stmt = (
            select(order_events)
            .where(order_events.c.order_id == order_id)
            .order_by(order_events.c.seq.desc())
            .limit(1)
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def current_status(session: Session, order_id: str) -> str | None:
        """Status folded from the log alone — the check that the projection is honest."""
        stmt = select(order_events.c.to_status).where(
            order_events.c.order_id == order_id
        ).order_by(order_events.c.seq.desc()).limit(1)
        value = session.execute(stmt).scalar()
        return None if value is None else str(value)

    @staticmethod
    def fills(session: Session, order_id: str) -> list[dict[str, Any]]:
        """Events that represent execution, not just a state change."""
        stmt = (
            select(order_events)
            .where(
                order_events.c.order_id == order_id,
                order_events.c.to_status.in_(("PARTIALLY_FILLED", "FILLED")),
                order_events.c.filled_qty.is_not(None),
            )
            .order_by(order_events.c.seq)
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def for_user(
        session: Session,
        user_id: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Every event on every one of a user's orders, newest last.

        Used by reconciliation and by the trade journal, both of which need the
        whole chain rather than one order at a time.
        """
        stmt = (
            select(order_events)
            .join(orders, orders.c.order_id == order_events.c.order_id)
            .where(orders.c.user_id == user_id)
        )
        if since:
            stmt = stmt.where(order_events.c.ts >= since)
        stmt = stmt.order_by(order_events.c.ts.desc()).limit(max(1, min(limit, 5000)))
        return _many(session.execute(stmt).all())


class OrderIntentRepository:
    """Durable idempotency. The primary key *is* the mechanism."""

    @staticmethod
    def get(session: Session, idempotency_key: str) -> dict[str, Any] | None:
        stmt = select(order_intents).where(
            order_intents.c.idempotency_key == idempotency_key
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def create_if_absent(
        session: Session,
        *,
        idempotency_key: str,
        order_id: str,
        user_id: str,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        signal_id: str | None = None,
    ) -> tuple[str, bool]:
        """Claim ``idempotency_key`` for ``order_id``.

        Returns ``(order_id, created)``. On a duplicate it returns the order that
        already owns the key and ``created=False`` — it does **not** raise,
        because a retry is the normal case this exists for, not an error.

        The guard is the INSERT itself. A ``SELECT`` first would leave a window
        between the check and the write, and a duplicate order is exactly what
        would be created inside that window.
        """
        now = utcnow()
        inserted = _insert_tolerating_duplicate(
            session,
            insert(order_intents).values(
                idempotency_key=idempotency_key,
                order_id=order_id,
                user_id=user_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                signal_id=signal_id,
                created_at=now,
            ),
        )
        if inserted:
            return order_id, True
        existing = OrderIntentRepository.get(session, idempotency_key)
        if existing is None:  # pragma: no cover - the row was there a moment ago
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key[:12]}… conflicted but cannot be read"
            )
        if existing["user_id"] != user_id:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key[:12]}… belongs to another account"
            )
        return str(existing["order_id"]), False

    @staticmethod
    def for_order(session: Session, order_id: str) -> dict[str, Any] | None:
        stmt = select(order_intents).where(order_intents.c.order_id == order_id)
        return _one(session.execute(stmt).first())


class DeploymentRepository:
    """A running strategy instance: what is live, on what capital, in what mode."""

    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        strategy_id: str,
        strategy_version: int,
        mode: str = "PAPER",
        capital: float,
        broker_account: str | None = None,
        config: dict[str, Any] | None = None,
        status: str = "PENDING",
    ) -> dict[str, Any]:
        mode = (mode or "PAPER").strip().upper()
        if mode not in ("PAPER", "LIVE"):
            raise ValueError(f"mode must be PAPER or LIVE, got {mode!r}")
        if float(capital) <= 0:
            raise ValueError("capital must be positive")
        now = utcnow()
        row = {
            "deployment_id": _new_id(),
            "user_id": user_id,
            "strategy_id": strategy_id,
            "strategy_version": int(strategy_version),
            "mode": mode,
            "status": status,
            "capital": float(capital),
            "broker_account": broker_account,
            "config": _json_or_text(config),
            "started_at": None,
            "stopped_at": None,
            "stop_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        session.execute(insert(deployments).values(**row))
        return row

    @staticmethod
    def get(
        session: Session, deployment_id: str, user_id: str
    ) -> dict[str, Any] | None:
        stmt = select(deployments).where(
            deployments.c.deployment_id == deployment_id,
            deployments.c.user_id == user_id,
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session, user_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(deployments).where(deployments.c.user_id == user_id)
        if status:
            stmt = stmt.where(deployments.c.status == status)
        stmt = stmt.order_by(deployments.c.created_at.desc())
        return _many(session.execute(stmt).all())

    @staticmethod
    def running(session: Session, user_id: str) -> list[dict[str, Any]]:
        return DeploymentRepository.list_for_user(session, user_id, status="RUNNING")

    @staticmethod
    def start(session: Session, deployment_id: str, user_id: str) -> int:
        """Start or resume a deployment.

        Only ``PENDING`` and ``PAUSED`` may be started. A **stopped** deployment is
        terminal: resuming it would mean the account's history continues after a
        decision to end it, and "why is this strategy still trading?" would have no
        answer. Restarting a stopped strategy is a new deployment, which is what
        ``DeploymentService.reset`` creates.
        """
        now = utcnow()
        result = session.execute(
            update(deployments)
            .where(
                deployments.c.deployment_id == deployment_id,
                deployments.c.user_id == user_id,
                deployments.c.status.in_(("PENDING", "PAUSED")),
            )
            .values(status="RUNNING", started_at=now, stopped_at=None,
                    stop_reason=None, updated_at=now)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def pause(session: Session, deployment_id: str, user_id: str, *, reason: str) -> int:
        """Pause a running deployment. ``reason`` is mandatory, as for stop.

        Pausing is a *decision about risk*, so it is recorded the same way stopping
        is. Only a running deployment can be paused; the filter is what makes the
        zero row count mean "not running" rather than "not yours".
        """
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("pausing a deployment requires a reason")
        now = utcnow()
        result = session.execute(
            update(deployments)
            .where(
                deployments.c.deployment_id == deployment_id,
                deployments.c.user_id == user_id,
                deployments.c.status == "RUNNING",
            )
            .values(status="PAUSED", stop_reason=reason[:255], updated_at=now)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def stop(session: Session, deployment_id: str, user_id: str, *, reason: str) -> int:
        """Stop a deployment. ``reason`` is mandatory and must say something.

        A deployment that stopped for no recorded reason is a deployment nobody
        can explain afterwards, and "why did this stop trading?" is the first
        question asked when something is wrong.
        """
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("stopping a deployment requires a reason")
        now = utcnow()
        result = session.execute(
            update(deployments)
            .where(
                deployments.c.deployment_id == deployment_id,
                deployments.c.user_id == user_id,
                deployments.c.status != "STOPPED",
            )
            .values(status="STOPPED", stopped_at=now, stop_reason=reason[:255],
                    updated_at=now)
        )
        return int(result.rowcount or 0)


class ReconciliationRepository:
    """Persisted reconciliation runs. A mismatch is a row, not a log line."""

    @staticmethod
    def record(
        session: Session,
        *,
        user_id: str,
        scope: str,
        mismatches: list[dict[str, Any]] | None = None,
        severity: str = "ok",
        internal_count: int | None = None,
        broker_count: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one run, refusing to call a run with differences ``ok``.

        The invariant is enforced here because it is the whole point of the
        table: a reconciliation that found mismatches and recorded ``ok`` is
        indistinguishable from one that found nothing, and the silent version is
        the failure mode the brief forbids.
        """
        details = list(mismatches or [])
        severity = (severity or "ok").strip().lower()
        if severity not in ("ok", "warning", "critical"):
            raise ValueError(f"severity must be ok/warning/critical, got {severity!r}")
        if details and severity == "ok":
            raise ValueError(
                f"{len(details)} mismatch(es) recorded with severity 'ok' — "
                "a run that found differences must not report itself clean"
            )
        if error and severity == "ok":
            raise ValueError("a run that errored must not report itself clean")
        row = {
            "run_id": _new_id(),
            "user_id": user_id,
            "ts": ts or utcnow(),
            "scope": scope,
            "internal_count": internal_count,
            "broker_count": broker_count,
            "mismatches": len(details),
            "severity": severity,
            "detail": _json_or_text(details) if details else None,
            "error": (error or None) and str(error)[:255],
            "duration_ms": duration_ms,
        }
        session.execute(insert(reconciliation_runs).values(**row))
        return row

    @staticmethod
    def latest(
        session: Session, user_id: str, *, scope: str | None = None
    ) -> dict[str, Any] | None:
        stmt = select(reconciliation_runs).where(
            reconciliation_runs.c.user_id == user_id
        )
        if scope:
            stmt = stmt.where(reconciliation_runs.c.scope == scope)
        stmt = stmt.order_by(reconciliation_runs.c.ts.desc()).limit(1)
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session,
        user_id: str,
        *,
        severity: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [reconciliation_runs.c.user_id == user_id]
        if severity:
            conditions.append(reconciliation_runs.c.severity == severity)
        total = int(
            session.execute(
                select(func.count()).select_from(reconciliation_runs).where(*conditions)
            ).scalar()
            or 0
        )
        stmt = (
            select(reconciliation_runs)
            .where(*conditions)
            .order_by(reconciliation_runs.c.ts.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        return _many(session.execute(stmt).all()), total

    @staticmethod
    def worst_unacknowledged(session: Session, user_id: str) -> dict[str, Any] | None:
        """The most recent run that was not clean, if it is still the most recent.

        Returns ``None`` once a clean run supersedes it, so the dashboard banner
        clears when the problem is fixed without anyone having to dismiss it.
        """
        latest = ReconciliationRepository.latest(session, user_id)
        if latest is None or latest["severity"] == "ok":
            return None
        return latest


class TradeJournalRepository:
    """Closed and open trades, with the path statistics a journal exists for."""

    @staticmethod
    def open_trade(
        session: Session,
        *,
        user_id: str,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        entry_ts: datetime | None = None,
        deployment_id: str | None = None,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        asset_class: str = "EQUITY",
        signal_reason: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "trade_id": _new_id(),
            "user_id": user_id,
            "deployment_id": deployment_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "symbol": symbol.strip().upper(),
            "asset_class": asset_class,
            "side": (side or "").strip().upper(),
            "quantity": float(quantity),
            "entry_ts": entry_ts or utcnow(),
            "exit_ts": None,
            "entry_price": float(entry_price),
            "exit_price": None,
            "gross_pnl": None,
            "net_pnl": None,
            "mfe": None,
            "mae": None,
            "duration_sec": None,
            "regime": None,
            "signal_reason": signal_reason,
            "slippage_bps": None,
            "notes": notes,
            "created_at": utcnow(),
        }
        session.execute(insert(trade_journal).values(**row))
        return row

    @staticmethod
    def close_trade(
        session: Session,
        trade_id: str,
        user_id: str,
        *,
        exit_price: float,
        gross_pnl: float,
        net_pnl: float | None = None,
        exit_ts: datetime | None = None,
        mfe: float | None = None,
        mae: float | None = None,
        slippage_bps: float | None = None,
        regime: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Close a trade. ``duration_sec`` is derived from the timestamps here.

        Derived rather than passed in so the two cannot disagree; the caller
        already had to supply ``exit_ts``.
        """
        trade = TradeJournalRepository.get(session, trade_id, user_id)
        if trade is None:
            return 0
        exit_ts = exit_ts or utcnow()
        entry_ts = trade["entry_ts"]
        duration = None
        if entry_ts is not None:
            duration = int(max(0.0, (exit_ts - entry_ts).total_seconds()))
        values: dict[str, Any] = {
            "exit_price": float(exit_price),
            "exit_ts": exit_ts,
            "gross_pnl": float(gross_pnl),
            "net_pnl": float(net_pnl) if net_pnl is not None else float(gross_pnl),
            "duration_sec": duration,
        }
        if mfe is not None:
            values["mfe"] = float(mfe)
        if mae is not None:
            values["mae"] = float(mae)
        if slippage_bps is not None:
            values["slippage_bps"] = float(slippage_bps)
        if regime is not None:
            values["regime"] = regime
        if notes is not None:
            values["notes"] = notes
        result = session.execute(
            update(trade_journal)
            .where(
                trade_journal.c.trade_id == trade_id,
                trade_journal.c.user_id == user_id,
                trade_journal.c.exit_ts.is_(None),
            )
            .values(**values)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def get(session: Session, trade_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(trade_journal).where(
            trade_journal.c.trade_id == trade_id, trade_journal.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session,
        user_id: str,
        *,
        deployment_id: str | None = None,
        strategy_id: str | None = None,
        symbol: str | None = None,
        closed_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [trade_journal.c.user_id == user_id]
        if deployment_id:
            conditions.append(trade_journal.c.deployment_id == deployment_id)
        if strategy_id:
            conditions.append(trade_journal.c.strategy_id == strategy_id)
        if symbol:
            conditions.append(trade_journal.c.symbol == symbol.strip().upper())
        if closed_only:
            conditions.append(trade_journal.c.exit_ts.is_not(None))
        total = int(
            session.execute(
                select(func.count()).select_from(trade_journal).where(*conditions)
            ).scalar()
            or 0
        )
        stmt = (
            select(trade_journal)
            .where(*conditions)
            .order_by(trade_journal.c.entry_ts.desc())
            .limit(max(1, min(limit, 1000)))
            .offset(max(0, offset))
        )
        return _many(session.execute(stmt).all()), total


# ===========================================================================
# Strategy tier
# ===========================================================================
class DuplicateDefinition(ValueError):
    """A strategy version with this definition already exists.

    Carries the version that already holds it so the API can answer "this is
    version 3 already" instead of a bare conflict — the useful half of a 409.
    """

    def __init__(self, existing_version: int) -> None:
        self.existing_version = int(existing_version)
        super().__init__(
            f"an identical definition already exists as version {existing_version}"
        )


def canonical_definition(definition: dict[str, Any] | str) -> str:
    """Serialise a strategy definition so equal rules produce an equal string.

    Sorted keys and no insignificant whitespace, because the hash of this string
    is what dedupes versions: two definitions that differ only in key order are
    the same strategy and must not be stored as version 3 and version 4.
    """
    if isinstance(definition, str):
        # Already a string: parse and re-serialise so a caller cannot smuggle in
        # a differently-spaced copy of the same rules and defeat the dedupe.
        definition = json.loads(definition)
    return json.dumps(definition, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def definition_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StrategyRepository:
    """Strategies and their immutable versions.

    Immutability of ``(strategy_id, version)`` is enforced two ways: the primary
    key makes a second row for the same version impossible, and there is simply
    no method here that updates ``definition``. Metadata — name, description —
    is editable, because renaming a strategy does not change what it does.
    """

    KINDS = frozenset({"nocode", "rules", "code", "options"})

    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        name: str,
        kind: str,
        description: str | None = None,
        engine_key: str | None = None,
    ) -> dict[str, Any]:
        kind = (kind or "").strip().lower()
        if kind not in StrategyRepository.KINDS:
            raise ValueError(
                f"kind must be one of {sorted(StrategyRepository.KINDS)}, got {kind!r}"
            )
        now = utcnow()
        row = {
            "strategy_id": _new_id(),
            "user_id": user_id,
            "name": name.strip()[:128],
            "description": description,
            "kind": kind,
            "engine_key": engine_key,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        session.execute(insert(strategies).values(**row))
        return row

    @staticmethod
    def get(session: Session, strategy_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(strategies).where(
            strategies.c.strategy_id == strategy_id, strategies.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session, user_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        stmt = select(strategies).where(strategies.c.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(strategies.c.archived_at.is_(None))
        stmt = stmt.order_by(strategies.c.name)
        rows = _many(session.execute(stmt).all())
        for row in rows:
            latest = StrategyRepository.latest_version(session, row["strategy_id"])
            row["latest_version"] = None if latest is None else latest["version"]
            row["version_count"] = StrategyRepository.version_count(
                session, row["strategy_id"]
            )
        return rows

    @staticmethod
    def name_taken(session: Session, user_id: str, name: str) -> bool:
        stmt = select(func.count()).select_from(strategies).where(
            strategies.c.user_id == user_id,
            func.lower(strategies.c.name) == name.strip().lower(),
        )
        return bool(session.execute(stmt).scalar())

    @staticmethod
    def update_meta(
        session: Session, strategy_id: str, user_id: str, **fields: Any
    ) -> int:
        """Rename / re-describe. Only these two fields are accepted."""
        allowed = {"name", "description"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return 0
        if values.get("name"):
            values["name"] = str(values["name"]).strip()[:128]
        values["updated_at"] = utcnow()
        result = session.execute(
            update(strategies)
            .where(
                strategies.c.strategy_id == strategy_id,
                strategies.c.user_id == user_id,
            )
            .values(**values)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def archive(session: Session, strategy_id: str, user_id: str) -> int:
        """Archive rather than delete: versions and runs reference this row."""
        now = utcnow()
        result = session.execute(
            update(strategies)
            .where(
                strategies.c.strategy_id == strategy_id,
                strategies.c.user_id == user_id,
                strategies.c.archived_at.is_(None),
            )
            .values(archived_at=now, updated_at=now)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def unarchive(session: Session, strategy_id: str, user_id: str) -> int:
        result = session.execute(
            update(strategies)
            .where(
                strategies.c.strategy_id == strategy_id,
                strategies.c.user_id == user_id,
            )
            .values(archived_at=None, updated_at=utcnow())
        )
        return int(result.rowcount or 0)

    # --------------------------------------------------------------- versions
    @staticmethod
    def create_version(
        session: Session,
        *,
        strategy_id: str,
        author_user_id: str,
        definition: dict[str, Any] | str,
        change_note: str | None = None,
    ) -> dict[str, Any]:
        """Append a new immutable version. Never updates an existing one.

        ``author_user_id`` is checked against the strategy's owner rather than
        trusted: the FK proves the strategy exists, not that this caller may add
        to it.
        """
        owner = session.execute(
            select(strategies.c.user_id).where(strategies.c.strategy_id == strategy_id)
        ).scalar()
        if owner is None:
            raise LookupError(f"no strategy {strategy_id}")
        if owner != author_user_id:
            raise PermissionError("strategy belongs to another account")

        canonical = canonical_definition(definition)
        digest = definition_hash(canonical)
        duplicate = session.execute(
            select(strategy_versions.c.version)
            .where(
                strategy_versions.c.strategy_id == strategy_id,
                strategy_versions.c.definition_hash == digest,
            )
            .limit(1)
        ).scalar()
        if duplicate is not None:
            raise DuplicateDefinition(int(duplicate))

        for attempt in range(2):
            version = (
                int(
                    session.execute(
                        select(func.max(strategy_versions.c.version)).where(
                            strategy_versions.c.strategy_id == strategy_id
                        )
                    ).scalar()
                    or 0
                )
                + 1
            )
            row = {
                "strategy_id": strategy_id,
                "version": version,
                "author_user_id": author_user_id,
                "created_at": utcnow(),
                "definition": canonical,
                "definition_hash": digest,
                "change_note": change_note,
                "is_deployed": False,
            }
            try:
                with session.begin_nested():
                    session.execute(insert(strategy_versions).values(**row))
                return row
            except IntegrityError:
                if attempt == 1:
                    raise
                continue
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def versions(session: Session, strategy_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(strategy_versions)
            .where(strategy_versions.c.strategy_id == strategy_id)
            .order_by(strategy_versions.c.version.desc())
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def version(
        session: Session, strategy_id: str, version: int
    ) -> dict[str, Any] | None:
        stmt = select(strategy_versions).where(
            strategy_versions.c.strategy_id == strategy_id,
            strategy_versions.c.version == int(version),
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def latest_version(
        session: Session, strategy_id: str
    ) -> dict[str, Any] | None:
        stmt = (
            select(strategy_versions)
            .where(strategy_versions.c.strategy_id == strategy_id)
            .order_by(strategy_versions.c.version.desc())
            .limit(1)
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def version_count(session: Session, strategy_id: str) -> int:
        stmt = select(func.count()).select_from(strategy_versions).where(
            strategy_versions.c.strategy_id == strategy_id
        )
        return int(session.execute(stmt).scalar() or 0)

    @staticmethod
    def mark_deployed(
        session: Session, strategy_id: str, version: int, *, deployed: bool = True
    ) -> int:
        """Flag whether a version is (or has been) deployed. Not a definition change."""
        result = session.execute(
            update(strategy_versions)
            .where(
                strategy_versions.c.strategy_id == strategy_id,
                strategy_versions.c.version == int(version),
            )
            .values(is_deployed=bool(deployed))
        )
        return int(result.rowcount or 0)


class BacktestRunRepository:
    """Backtest runs and their lifecycle. Metrics are written once, at completion."""

    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        config: dict[str, Any] | str,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        engine_key: str | None = None,
    ) -> dict[str, Any]:
        if not engine_key and (strategy_id is None or strategy_version is None):
            raise ValueError(
                "a run needs either engine_key or (strategy_id, strategy_version)"
            )
        now = utcnow()
        row = {
            "run_id": _new_id(),
            "user_id": user_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "engine_key": engine_key,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "status": "QUEUED",
            "progress": 0.0,
            "config": _json_or_text(config) or "{}",
            "data_fingerprint": None,
            "metrics": None,
            "error": None,
        }
        session.execute(insert(backtest_runs).values(**row))
        return row

    @staticmethod
    def get(session: Session, run_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(backtest_runs).where(
            backtest_runs.c.run_id == run_id, backtest_runs.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(
        session: Session,
        user_id: str,
        *,
        status: str | None = None,
        strategy_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [backtest_runs.c.user_id == user_id]
        if status:
            conditions.append(backtest_runs.c.status == status)
        if strategy_id:
            conditions.append(backtest_runs.c.strategy_id == strategy_id)
        total = int(
            session.execute(
                select(func.count()).select_from(backtest_runs).where(*conditions)
            ).scalar()
            or 0
        )
        stmt = (
            select(backtest_runs)
            .where(*conditions)
            .order_by(backtest_runs.c.created_at.desc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        return _many(session.execute(stmt).all()), total

    @staticmethod
    def _guard(run_id: str, user_id: str) -> list[Any]:
        """Conditions that only match a run which has not yet finished.

        Every setter uses these, so a finished run cannot be reopened and a
        completed run's metrics cannot be overwritten by a late straggler from
        the worker that produced them.
        """
        return [
            backtest_runs.c.run_id == run_id,
            backtest_runs.c.user_id == user_id,
            backtest_runs.c.status.notin_(sorted(TERMINAL_BACKTEST_STATUSES)),
        ]

    @staticmethod
    def mark_running(session: Session, run_id: str, user_id: str) -> int:
        result = session.execute(
            update(backtest_runs)
            .where(*BacktestRunRepository._guard(run_id, user_id))
            .values(status="RUNNING", started_at=utcnow())
        )
        return int(result.rowcount or 0)

    @staticmethod
    def set_progress(
        session: Session, run_id: str, user_id: str, progress: float
    ) -> int:
        value = max(0.0, min(1.0, float(progress)))
        result = session.execute(
            update(backtest_runs)
            .where(*BacktestRunRepository._guard(run_id, user_id))
            .values(progress=value)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def complete(
        session: Session,
        run_id: str,
        user_id: str,
        *,
        metrics: dict[str, Any],
        data_fingerprint: str | None = None,
    ) -> int:
        result = session.execute(
            update(backtest_runs)
            .where(*BacktestRunRepository._guard(run_id, user_id))
            .values(
                status="COMPLETED",
                progress=1.0,
                finished_at=utcnow(),
                metrics=_json_or_text(metrics),
                data_fingerprint=data_fingerprint,
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    def fail(session: Session, run_id: str, user_id: str, *, error: str) -> int:
        result = session.execute(
            update(backtest_runs)
            .where(*BacktestRunRepository._guard(run_id, user_id))
            .values(status="FAILED", finished_at=utcnow(), error=str(error)[:2000])
        )
        return int(result.rowcount or 0)

    @staticmethod
    def cancel(session: Session, run_id: str, user_id: str) -> int:
        result = session.execute(
            update(backtest_runs)
            .where(*BacktestRunRepository._guard(run_id, user_id))
            .values(status="CANCELLED", finished_at=utcnow())
        )
        return int(result.rowcount or 0)


# ===========================================================================
# Screener tier
# ===========================================================================
class ScreenerRepository:
    """Saved screens. The condition tree is opaque here — ``atr.screener`` owns it."""

    @staticmethod
    def create(
        session: Session,
        *,
        user_id: str,
        name: str,
        definition: dict[str, Any] | str,
        description: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        row = {
            "scan_id": _new_id(),
            "user_id": user_id,
            "name": name.strip()[:64],
            "description": description,
            "definition": _json_or_text(definition) or "{}",
            "created_at": now,
            "updated_at": now,
        }
        session.execute(insert(screener_scans).values(**row))
        return row

    @staticmethod
    def get(session: Session, scan_id: str, user_id: str) -> dict[str, Any] | None:
        stmt = select(screener_scans).where(
            screener_scans.c.scan_id == scan_id, screener_scans.c.user_id == user_id
        )
        return _one(session.execute(stmt).first())

    @staticmethod
    def list_for_user(session: Session, user_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(screener_scans)
            .where(screener_scans.c.user_id == user_id)
            .order_by(screener_scans.c.name)
        )
        return _many(session.execute(stmt).all())

    @staticmethod
    def name_taken(session: Session, user_id: str, name: str) -> bool:
        stmt = select(func.count()).select_from(screener_scans).where(
            screener_scans.c.user_id == user_id,
            func.lower(screener_scans.c.name) == name.strip().lower(),
        )
        return bool(session.execute(stmt).scalar())

    @staticmethod
    def update(
        session: Session,
        scan_id: str,
        user_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        definition: dict[str, Any] | str | None = None,
    ) -> int:
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name.strip()[:64]
        if description is not None:
            values["description"] = description
        if definition is not None:
            values["definition"] = _json_or_text(definition)
        if not values:
            return 0
        values["updated_at"] = utcnow()
        result = session.execute(
            update(screener_scans)
            .where(
                screener_scans.c.scan_id == scan_id,
                screener_scans.c.user_id == user_id,
            )
            .values(**values)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def delete(session: Session, scan_id: str, user_id: str) -> int:
        result = session.execute(
            delete(screener_scans).where(
                screener_scans.c.scan_id == scan_id,
                screener_scans.c.user_id == user_id,
            )
        )
        return int(result.rowcount or 0)


class SystemStateRepository:
    """Key/value state that belongs to the installation rather than to a user.

    Two things live here and both are safety-critical: the kill switch and the
    execution mode. They used to be an in-process dict, which meant a restart
    silently re-armed trading and dropped the platform back to paper mode — a
    safety decision undone by a crash. Durable storage is the fix.
    """

    @staticmethod
    def get(session: Session, key: str) -> dict[str, Any] | None:
        stmt = select(system_state).where(system_state.c.key == key)
        return _one(session.execute(stmt).first())

    @staticmethod
    def get_value(session: Session, key: str, default: Any = None) -> Any:
        """The JSON-decoded value, or ``default`` when unset or unreadable."""
        row = SystemStateRepository.get(session, key)
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    @staticmethod
    def set(
        session: Session,
        key: str,
        value: Any,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one key. ``reason`` is stored verbatim; the caller decides if it is required."""
        payload = json.dumps(value, default=str)
        now = utcnow()
        existing = SystemStateRepository.get(session, key)
        if existing is None:
            session.execute(
                insert(system_state).values(
                    key=key,
                    value=payload,
                    updated_at=now,
                    updated_by=(actor or "")[:64] or None,
                    reason=reason,
                )
            )
        else:
            session.execute(
                update(system_state)
                .where(system_state.c.key == key)
                .values(
                    value=payload,
                    updated_at=now,
                    updated_by=(actor or "")[:64] or None,
                    reason=reason,
                )
            )
        return {
            "key": key,
            "value": payload,
            "updated_at": now,
            "updated_by": actor,
            "reason": reason,
        }

    @staticmethod
    def all(session: Session) -> dict[str, Any]:
        """Every key, JSON-decoded. Unreadable values come back as ``None``."""
        out: dict[str, Any] = {}
        for row in _many(session.execute(select(system_state)).all()):
            try:
                out[row["key"]] = json.loads(row["value"]) if row["value"] else None
            except (TypeError, ValueError):
                out[row["key"]] = None
        return out


__all__ = [
    "ApiKeyRepository",
    "AuditRepository",
    "BacktestRunRepository",
    "DeploymentRepository",
    "DuplicateDefinition",
    "IdempotencyConflict",
    "OrderEventRepository",
    "OrderIntentRepository",
    "OrderRepository",
    "PreferenceRepository",
    "ReconciliationRepository",
    "ScreenerRepository",
    "SessionRepository",
    "StrategyRepository",
    "SystemStateRepository",
    "TERMINAL_BACKTEST_STATUSES",
    "TERMINAL_ORDER_STATUSES",
    "TradeJournalRepository",
    "UserRepository",
    "WatchlistRepository",
    "canonical_definition",
    "definition_hash",
]
