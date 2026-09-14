"""The schema itself: what exists, and that the docs agree with it.

``create_all`` is happy to create a schema that no repository can use, and the
docs are happy to describe tables that do not exist. These tests are the cheap
check that neither has drifted — a table the code writes and the document does
not mention is a table nobody will find.
"""

from __future__ import annotations

import pathlib

from sqlalchemy import inspect

from atr.appdb.schema import app_metadata

DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs" / "DATA_MODEL.md"

#: Every table the control plane owns. Pinned so a rename is a deliberate act.
EXPECTED_TABLES = frozenset(
    {
        # §1 control plane
        "users",
        "sessions",
        "api_keys",
        "watchlists",
        "watchlist_items",
        "watchlist_columns",
        "user_preferences",
        "audit_events",
        # §3 trading tier
        "orders",
        "order_events",
        "order_intents",
        "deployments",
        "reconciliation_runs",
        "trade_journal",
        # §4 strategy tier
        "strategies",
        "strategy_versions",
        "backtest_runs",
        "screener_scans",
        # §6 system tier
        "system_state",
    }
)


def _created_tables() -> set[str]:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    app_metadata.create_all(engine)
    return set(inspect(engine).get_table_names())


def test_create_all_produces_exactly_the_expected_tables():
    assert _created_tables() == set(EXPECTED_TABLES)


def test_the_data_model_document_describes_every_table(app_db):
    """A table the code writes and the doc does not mention is one nobody finds."""
    if not DOCS.exists():  # pragma: no cover - docs ship with the repo
        return
    text = DOCS.read_text(encoding="utf-8")
    missing = sorted(name for name in EXPECTED_TABLES if f"`{name}`" not in text)
    assert missing == [], f"docs/DATA_MODEL.md does not mention: {missing}"


def test_there_is_no_fills_table(app_db):
    """A fill is an ``order_events`` row; a second copy could disagree with the first."""
    assert "fills" not in _created_tables()
    assert "order_events" in _created_tables()


def test_order_events_carries_the_sequence_constraint(app_db):
    """``UNIQUE(order_id, seq)`` is what makes the log a sequence, not a bag."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    app_metadata.create_all(engine)
    inspector = inspect(engine)
    uniques = inspector.get_unique_constraints("order_events")
    assert any(set(u["column_names"]) == {"order_id", "seq"} for u in uniques), uniques


def test_strategy_versions_is_keyed_on_the_pair(app_db):
    """The composite key is half of how version immutability is enforced."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    app_metadata.create_all(engine)
    pk = inspect(engine).get_pk_constraint("strategy_versions")["constrained_columns"]
    assert set(pk) == {"strategy_id", "version"}


def test_order_intents_is_keyed_on_the_idempotency_key(app_db):
    """The primary key *is* the duplicate guard."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    app_metadata.create_all(engine)
    pk = inspect(engine).get_pk_constraint("order_intents")["constrained_columns"]
    assert pk == ["idempotency_key"]
