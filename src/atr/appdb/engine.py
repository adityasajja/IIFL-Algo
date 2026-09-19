"""Engine, session factory and SQLite pragmas for the control-plane store.

The store is import-safe: no connection is opened until something asks for a
session, so importing :mod:`atr.appdb` in a unit test or a backtest costs
nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from loguru import logger
from sqlalchemy import Engine, create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from atr.appdb.schema import app_metadata, users

#: Repo root, used to resolve a relative SQLite path consistently no matter what
#: the process working directory happens to be.
ROOT = Path(__file__).resolve().parents[3]


def utcnow() -> datetime:
    """Naive UTC.

    Every timestamp in this package goes through here. Storing aware datetimes
    would round-trip differently on SQLite (which drops the offset) than on
    Postgres, so a value written on one and read on the other would silently
    shift. Naive-UTC is the one representation both agree on.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _resolve_url(url: str) -> str:
    """Turn a relative SQLite path into an absolute one."""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        raw = url[len(prefix) :]
        if raw and raw != ":memory:" and not Path(raw).is_absolute():
            path = (ROOT / raw).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path.as_posix()}"
    return url


class AppDatabase:
    """Thin wrapper around a SQLAlchemy engine + session factory."""

    def __init__(self, url: str | None = None, *, echo: bool = False) -> None:
        self._explicit_url = url
        self._echo = echo
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._prepared = False

    # ------------------------------------------------------------------ url
    @property
    def url(self) -> str:
        if self._explicit_url:
            return _resolve_url(self._explicit_url)
        from atr.config.settings import get_settings

        return _resolve_url(get_settings().app_db_url)

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    # --------------------------------------------------------------- engine
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            url = self.url
            kwargs: dict = {"echo": self._echo, "future": True}
            if url.startswith("sqlite"):
                # A file-backed SQLite connection pool hands the same connection
                # to different worker threads; FastAPI runs sync routes in a
                # threadpool, so check_same_thread must be off or requests fail
                # intermittently under any real concurrency.
                kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15.0}
                if ":memory:" in url:
                    # An in-memory database lives and dies with its connection,
                    # so it must be a single shared one or every session sees an
                    # empty schema.
                    kwargs["poolclass"] = StaticPool
            else:
                kwargs["pool_pre_ping"] = True
                kwargs["pool_size"] = 5
                kwargs["max_overflow"] = 5
            self._engine = create_engine(url, **kwargs)
            if url.startswith("sqlite"):
                self._install_sqlite_pragmas(self._engine)
        return self._engine

    @staticmethod
    def _install_sqlite_pragmas(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record) -> None:  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            try:
                # OFF by default in SQLite, which silently disables every
                # ON DELETE CASCADE in the schema — deleting a user would leave
                # their watchlists behind.
                cursor.execute("PRAGMA foreign_keys=ON")
                # WAL lets a reader proceed during a write; without it the
                # dashboard's polling reads collide with audit writes.
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=15000")
                # Ultra-low latency memory mapping: map up to 256MB into RAM for zero-copy reads
                cursor.execute("PRAGMA mmap_size=268435456")
                cursor.execute("PRAGMA cache_size=-64000")  # 64MB page cache in RAM
                cursor.execute("PRAGMA temp_store=MEMORY")
            finally:
                cursor.close()
            # Hand transaction control to us. pysqlite otherwise emits its own
            # BEGIN lazily and — the part that matters — treats a `SAVEPOINT` as
            # the outermost transaction, so **releasing the outermost savepoint
            # commits immediately**. The caller's later `rollback()` then undoes
            # only what came after it, and a write that was supposed to be
            # provisional survives the failure.
            #
            # That is not theoretical: the order service writes an event inside a
            # savepoint and then evaluates the risk gate, and if the gate raised,
            # the VALIDATING event was committed while its projection was rolled
            # back — leaving an order at NEW with a transition in its log that the
            # log is supposed to be the source of truth for. Measured directly:
            # `tests/test_appdb_engine.py::test_a_released_savepoint_is_undone_by_a_later_rollback`.
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _on_begin(conn) -> None:  # pragma: no cover - driver hook
            # With `isolation_level = None` the driver no longer opens the
            # transaction, so we do. This is the documented pairing: without it
            # every statement would autocommit and savepoints would be pointless.
            conn.exec_driver_sql("BEGIN")

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        return self._session_factory

    # --------------------------------------------------------------- schema
    #: Columns added to a table *after* it first shipped, as
    #: ``{table: ((column, ddl_type), ...)}``.
    #:
    #: ``create_all`` creates a missing table and does nothing at all to an
    #: existing one. So a column added to the model is silently absent from every
    #: database created before it — and the symptom is not a clear error at
    #: start-up but a ``no such column`` on every read of that table, in
    #: production, on the operator's machine. Declaring the additions here makes
    #: that impossible.
    #:
    #: Additive only, and deliberately not a migration framework: there is no
    #: rename, no type change, no backfill. A column that needs any of those
    #: needs a decision about the data already in it, which is not something to
    #: do implicitly at start-up.
    ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
        "trade_journal": (
            ("exit_reason", "VARCHAR(32)"),
            ("evidence_grade", "VARCHAR(16)"),
        ),
    }

    def prepare(self) -> None:
        """Create the schema if it is missing, and apply additive columns.

        Idempotent. Safe to call on every request, which is what
        :meth:`session` does.
        """
        if not self._prepared:
            app_metadata.create_all(self.engine)
            self._reconcile_additive_columns()
            self._prepared = True

    def _reconcile_additive_columns(self) -> None:
        """Add any declared post-release column that the database lacks.

        Failing to add one is logged rather than raised: a read-only database
        (or a dialect that refuses ``ALTER TABLE ADD COLUMN``) should not stop
        the application from starting. The failure is loud in the log and the
        affected reads degrade to "missing feature", which is the honest outcome
        — but it must not be silent, so it is logged at error level.
        """
        try:
            inspector = inspect(self.engine)
        except Exception as exc:
            logger.error("schema: could not inspect for additive columns: %s", exc)
            raise RuntimeError(f"schema inspection failed: {exc}")
        for table, columns in self.ADDITIVE_COLUMNS.items():
            try:
                if not inspector.has_table(table):
                    continue
                existing = {column["name"] for column in inspector.get_columns(table)}
                missing = [(name, ddl) for name, ddl in columns if name not in existing]
                if not missing:
                    continue
                with self.engine.begin() as conn:
                    for name, ddl in missing:
                        conn.execute(
                            text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
                        )
                logger.info(
                    "schema: added {} to {}", ", ".join(name for name, _ in missing), table
                )
            except Exception as exc:
                logger.error(
                    "schema: could not add columns to %s: %s — reads of those "
                    "columns will report them missing",
                    table,
                    exc,
                )
                raise RuntimeError(f"schema migration failed for {table}: {exc}")

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session. Commits on clean exit, rolls back on raise."""
        self.prepare()
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --------------------------------------------------------------- probes
    def health(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - a health probe must never raise
            return False

    def user_count(self) -> int:
        """How many accounts exist. Drives first-run bootstrap."""
        try:
            self.prepare()
            with self.engine.connect() as conn:
                return int(conn.execute(select(func.count()).select_from(users)).scalar() or 0)
        except Exception:  # noqa: BLE001 - an unreadable store must not 500 the UI
            return 0

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._prepared = False


@lru_cache(maxsize=1)
def get_app_db() -> AppDatabase:
    return AppDatabase()


def reset_app_db_cache() -> None:
    """Dispose the cached singleton and forget it. Tests use this."""
    with contextlib.suppress(Exception):
        # Best effort: teardown must not mask the test's own failure with a
        # secondary one from a half-open engine.
        get_app_db().dispose()
    get_app_db.cache_clear()


__all__ = ["AppDatabase", "get_app_db", "reset_app_db_cache", "utcnow"]
