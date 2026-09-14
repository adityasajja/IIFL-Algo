"""Control-plane tables (SQLAlchemy Core).

Conventions that hold across every table here:

* **Timestamps are naive UTC.** SQLite has no timezone-aware datetime type, so a
  mixed set of aware/naive values round-trips differently on SQLite and Postgres.
  Everything is written naive-UTC by :func:`atr.appdb.engine.utcnow` and the API
  layer appends ``Z`` on the way out.
* **No plaintext secrets.** ``users.password_hash`` is a scrypt digest;
  ``sessions.token_hash`` and ``api_keys.key_hash`` are SHA-256 digests. The raw
  value is shown to its owner exactly once and is not recoverable from the row.
* **Ownership is a column, not a convention.** Every user-scoped table carries
  ``user_id`` so a repository read can filter on it rather than trusting the
  caller to check.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

app_metadata = MetaData()

users = Table(
    "users",
    app_metadata,
    Column("user_id", String(32), primary_key=True),
    Column("email", String(255), nullable=False, unique=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("display_name", String(128)),
    Column("password_hash", Text, nullable=False),
    Column("role", String(16), nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("mfa_enabled", Boolean, nullable=False, default=False),
    #: Base32 TOTP seed. Stored encrypted when a secret key is configured; the
    #: encryption is a property of the service, not of this table.
    Column("mfa_secret", Text),
    #: Highest TOTP counter already accepted. Without it, a code observed in
    #: transit can be replayed for the remainder of its 30-second window.
    Column("mfa_last_counter", Integer),
    Column("failed_logins", Integer, nullable=False, default=0),
    Column("locked_until", DateTime),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("last_login_at", DateTime),
)

sessions = Table(
    "sessions",
    app_metadata,
    Column("session_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    #: sha256(raw token). A database dump does not yield a usable session.
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime),
    Column("revoked_at", DateTime),
    Column("ip", String(64)),
    Column("user_agent", String(255)),
    #: False for a password-only login that still owes a second factor. Such a
    #: session may enrol MFA but is refused by every permission check.
    Column("mfa_satisfied", Boolean, nullable=False, default=False),
    Index("ix_sessions_user", "user_id"),
    Index("ix_sessions_expires", "expires_at"),
)

api_keys = Table(
    "api_keys",
    app_metadata,
    Column("key_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("label", String(64), nullable=False),
    #: The only part of the key that is stored in the clear, so a UI can show
    #: "which key is this" without the row being usable as a credential.
    Column("prefix", String(12), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("scopes", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime),
    Column("last_used_at", DateTime),
    Column("revoked_at", DateTime),
    Index("ix_api_keys_user", "user_id"),
)

watchlists = Table(
    "watchlists",
    app_metadata,
    Column("watchlist_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("name", String(64), nullable=False),
    Column("exchange", String(16), nullable=False, default="NSEEQ"),
    Column("is_default", Boolean, nullable=False, default=False),
    Column("sort_order", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("user_id", "name", name="uq_watchlist_user_name"),
    Index("ix_watchlists_user", "user_id"),
)

watchlist_items = Table(
    "watchlist_items",
    app_metadata,
    Column("item_id", String(32), primary_key=True),
    Column(
        "watchlist_id",
        String(32),
        ForeignKey("watchlists.watchlist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("symbol", String(64), nullable=False),
    Column("sort_order", Integer, nullable=False, default=0),
    Column("note", String(255)),
    Column("added_at", DateTime, nullable=False),
    UniqueConstraint("watchlist_id", "symbol", name="uq_wl_item_symbol"),
    Index("ix_wl_items_order", "watchlist_id", "sort_order"),
)

watchlist_columns = Table(
    "watchlist_columns",
    app_metadata,
    Column("column_id", String(32), primary_key=True),
    Column(
        "watchlist_id",
        String(32),
        ForeignKey("watchlists.watchlist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("key", String(32), nullable=False),
    Column("sort_order", Integer, nullable=False, default=0),
    Column("width", Integer),
    Column("visible", Boolean, nullable=False, default=True),
    UniqueConstraint("watchlist_id", "key", name="uq_wl_column_key"),
)

user_preferences = Table(
    "user_preferences",
    app_metadata,
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True),
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

audit_events = Table(
    "audit_events",
    app_metadata,
    Column("event_id", String(32), primary_key=True),
    Column("ts", DateTime, nullable=False),
    Column("user_id", String(32)),
    #: Denormalised on purpose: the actor's email must survive the user row being
    #: deleted, or the trail stops explaining who did what.
    Column("actor", String(64)),
    Column("action", String(64), nullable=False),
    Column("target_type", String(32)),
    Column("target_id", String(64)),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Column("request_id", String(32)),
    Column("result", String(16), nullable=False),
    Column("detail", Text),
    Column("ip", String(64)),
    Index("ix_audit_ts", "ts"),
    Index("ix_audit_user_ts", "user_id", "ts"),
    Index("ix_audit_action", "action"),
)


# ===========================================================================
# Trading tier — the order management system
# ===========================================================================
#
# Two design decisions worth stating, because both are load-bearing and both
# differ from the obvious first draft:
#
# 1. **`orders.status` is a projection, not the truth.** The truth is the
#    `order_events` log; the column is a cache of the last event's `to_status`,
#    written inside the same transaction as that event. It exists so a list view
#    does not have to fold the whole log, and there is a test asserting the two
#    agree. The brief's rule — "do not treat order status as a single mutable
#    field" — is satisfied by the log being authoritative and the column being
#    derived, not by removing the column.
#
# 2. **There is no `fills` table.** A fill *is* an `order_events` row whose
#    `to_status` is PARTIALLY_FILLED or FILLED with `filled_qty` and
#    `filled_price` set. Positions and P&L are folds over those rows. A separate
#    fill table would be a second copy of the same facts with no way to tell
#    which one is right when they disagree. (The Postgres `orders`/`fills` DDL in
#    `atr/data/store.py` is superseded by these tables — it was never called by
#    anything, and is now documented as deprecated.)

orders = Table(
    "orders",
    app_metadata,
    Column("order_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    #: Which deployment produced this order. Null for a manual order.
    Column("deployment_id", String(32), ForeignKey("deployments.deployment_id", ondelete="SET NULL")),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Column("signal_id", String(32)),
    #: Ties the whole chain together: signal → risk → intent → order → fill.
    Column("correlation_id", String(32)),
    Column("symbol", String(64), nullable=False),
    Column("exchange", String(16), nullable=False, default="NSEEQ"),
    Column("asset_class", String(16), nullable=False, default="EQUITY"),
    Column("side", String(4), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("order_type", String(12), nullable=False, default="MARKET"),
    Column("limit_price", Float),
    Column("stop_price", Float),
    Column("tif", String(8), nullable=False, default="DAY"),
    Column("product", String(16)),
    #: PAPER or LIVE. A paper order and a live order are the same shape; only the
    #: venue differs, which is what makes the paper engine worth having.
    Column("mode", String(8), nullable=False, default="PAPER"),
    #: Projection of the newest event. See note 1 above.
    Column("status", String(24), nullable=False),
    Column("broker_order_id", String(64)),
    Column("filled_quantity", Float, nullable=False, default=0.0),
    Column("avg_fill_price", Float, nullable=False, default=0.0),
    #: Price at the moment the order was raised, for slippage against the fill.
    Column("requested_price", Float),
    Column("tag", String(64)),
    Column("reject_reason", String(255)),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("submitted_at", DateTime),
    Column("completed_at", DateTime),
    Index("ix_orders_user_created", "user_id", "created_at"),
    Index("ix_orders_status", "status"),
    Index("ix_orders_broker", "broker_order_id"),
    Index("ix_orders_deployment", "deployment_id"),
    Index("ix_orders_correlation", "correlation_id"),
)

order_events = Table(
    "order_events",
    app_metadata,
    Column("order_event_id", String(32), primary_key=True),
    Column("order_id", String(32), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False),
    #: Monotonic per order, and the reason the lifecycle is *deterministically*
    #: reconstructable. Two transitions can share a timestamp — a reject is
    #: written microseconds after the submit — so ordering by `ts` alone is a
    #: tie-break waiting to happen. `UNIQUE(order_id, seq)` makes the log a
    #: sequence rather than a bag of timestamps.
    Column("seq", Integer, nullable=False),
    Column("from_status", String(24)),
    Column("to_status", String(24), nullable=False),
    Column("ts", DateTime, nullable=False),
    #: Exchange timestamp, when the broker supplies one.
    Column("broker_ts", DateTime),
    Column("ack_ts", DateTime),
    Column("fill_ts", DateTime),
    Column("requested_price", Float),
    Column("filled_price", Float),
    Column("filled_qty", Float),
    Column("slippage_bps", Float),
    #: The frictions actually charged on this fill, in rupees. Recorded rather
    #: than recomputed from a cost model at read time: a paper fill's commission
    #: is a *fact about what happened*, and recomputing it would silently restate
    #: history every time a rate changed. It is also what lets the paper ledger be
    #: a faithful fold over this table — positions, cash and net P&L all derive
    #: from rows here, with no second state store to drift.
    Column("commission", Float),
    #: signal→submit, submit→ack, ack→fill. Stored per transition so execution
    #: latency is a measurement rather than a claim.
    Column("latency_ms", Integer),
    Column("reject_reason", String(255)),
    #: The broker's own payload, kept verbatim for post-mortems.
    Column("raw", Text),
    #: Who or what caused the transition: "oms", "broker", "reconciler", "user".
    Column("source", String(16), nullable=False, default="oms"),
    Column("correlation_id", String(32)),
    Column("request_id", String(32)),
    UniqueConstraint("order_id", "seq", name="uq_order_event_seq"),
    Index("ix_order_events_order", "order_id", "seq"),
    Index("ix_order_events_ts", "ts"),
)

order_intents = Table(
    "order_intents",
    app_metadata,
    Column(
        "idempotency_key",
        String(64),
        primary_key=True,
    ),
    Column("order_id", String(32), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Column("signal_id", String(32)),
    Column("created_at", DateTime, nullable=False),
    #: The primary key *is* the mechanism. There is deliberately no separate
    #: unique index on `order_id`: one intent maps to one order, and a second
    #: intent for the same order would be a bug rather than a constraint
    #: violation, so it is caught in code where the message can explain itself.
    Index("ix_order_intents_order", "order_id"),
)

deployments = Table(
    "deployments",
    app_metadata,
    Column("deployment_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("strategy_id", String(32), nullable=False),
    Column("strategy_version", Integer, nullable=False),
    Column("mode", String(8), nullable=False),
    Column("status", String(12), nullable=False),
    Column("capital", Float, nullable=False),
    Column("broker_account", String(32)),
    #: JSON: symbols, exchange, risk overrides, session window. Enough to
    #: reproduce the deployment's behaviour from the row alone.
    Column("config", Text),
    Column("started_at", DateTime),
    Column("stopped_at", DateTime),
    Column("stop_reason", String(255)),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Index("ix_deployments_user", "user_id"),
    Index("ix_deployments_strategy", "strategy_id", "strategy_version"),
    Index("ix_deployments_status", "status"),
)

reconciliation_runs = Table(
    "reconciliation_runs",
    app_metadata,
    Column("run_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("ts", DateTime, nullable=False),
    #: orders | positions | holdings | funds | all
    Column("scope", String(32), nullable=False),
    Column("internal_count", Integer),
    Column("broker_count", Integer),
    Column("mismatches", Integer, nullable=False, default=0),
    #: ok | warning | critical. A run with mismatches is never silently `ok`.
    Column("severity", String(16), nullable=False, default="ok"),
    #: JSON list of individual differences, so a mismatch is a fact with a shape
    #: rather than a log line someone has to grep for.
    Column("detail", Text),
    Column("error", String(255)),
    Column("duration_ms", Integer),
    Index("ix_recon_ts", "ts"),
    Index("ix_recon_user_ts", "user_id", "ts"),
    Index("ix_recon_severity", "severity"),
)

trade_journal = Table(
    "trade_journal",
    app_metadata,
    Column("trade_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("deployment_id", String(32)),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Column("symbol", String(64), nullable=False),
    Column("asset_class", String(16), nullable=False, default="EQUITY"),
    Column("side", String(4), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("entry_ts", DateTime, nullable=False),
    Column("exit_ts", DateTime),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float),
    Column("gross_pnl", Float),
    Column("net_pnl", Float),
    #: Stored rather than derived: both need the intra-trade path, which the
    #: order-event log does not preserve once a position is netted.
    Column("mfe", Float),
    Column("mae", Float),
    Column("duration_sec", Integer),
    Column("regime", String(24)),
    Column("signal_reason", Text),
    Column("slippage_bps", Float),
    Column("notes", Text),
    Column("created_at", DateTime, nullable=False),
    Index("ix_journal_user_exit", "user_id", "exit_ts"),
    Index("ix_journal_strategy", "strategy_id", "strategy_version"),
    Index("ix_journal_symbol", "symbol"),
)


# ===========================================================================
# Strategy tier
# ===========================================================================
strategies = Table(
    "strategies",
    app_metadata,
    Column("strategy_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("name", String(128), nullable=False),
    Column("description", Text),
    #: nocode | rules | code | options
    Column("kind", String(16), nullable=False),
    #: For a `code` strategy, the registry key it maps to (e.g. "ema_cross").
    #: Null for a strategy defined entirely by its JSON rules.
    Column("engine_key", String(64)),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("archived_at", DateTime),
    UniqueConstraint("user_id", "name", name="uq_strategy_user_name"),
    Index("ix_strategies_user", "user_id"),
)

strategy_versions = Table(
    "strategy_versions",
    app_metadata,
    Column(
        "strategy_id",
        String(32),
        ForeignKey("strategies.strategy_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("version", Integer, primary_key=True),
    Column("author_user_id", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
    #: Canonical JSON of rules / params. Canonical means sorted keys and no
    #: insignificant whitespace, so the hash is stable across processes.
    Column("definition", Text, nullable=False),
    #: sha256 of `definition`. Two identical versions are a mistake, not a new
    #: version, and are rejected rather than stored twice.
    Column("definition_hash", String(64), nullable=False),
    Column("change_note", Text),
    Column("is_deployed", Boolean, nullable=False, default=False),
    Index("ix_strategy_versions_hash", "definition_hash"),
    # Immutability is enforced by the absence of an UPDATE path in the repository
    # and by this primary key, not by a trigger. A rule that lives only in a
    # convention is a rule that will be broken.
)

backtest_runs = Table(
    "backtest_runs",
    app_metadata,
    Column("run_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    #: The registry key for a run launched against a built-in strategy. At least
    #: one of `engine_key` / `(strategy_id, strategy_version)` is always set.
    Column("engine_key", String(64)),
    Column("created_at", DateTime, nullable=False),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    #: QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED
    Column("status", String(12), nullable=False, default="QUEUED"),
    Column("progress", Float, nullable=False, default=0.0),
    #: JSON: dates, symbols, capital, costs. Enough to reproduce the run.
    Column("config", Text, nullable=False),
    #: sha256 of the price data used. A metric without this is not reproducible.
    Column("data_fingerprint", String(64)),
    #: JSON summary metrics, written once on completion.
    Column("metrics", Text),
    Column("error", Text),
    Index("ix_backtest_user_created", "user_id", "created_at"),
    Index("ix_backtest_status", "status"),
)


# ===========================================================================
# Screener tier
# ===========================================================================
screener_scans = Table(
    "screener_scans",
    app_metadata,
    Column("scan_id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("name", String(64), nullable=False),
    Column("description", Text),
    #: JSON: the nested condition tree, universe and ranking. The tree shape is
    #: owned by `atr.screener`, which validates it on the way in.
    Column("definition", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("user_id", "name", name="uq_screener_user_name"),
    Index("ix_screener_user", "user_id"),
)


# ===========================================================================
# System tier
# ===========================================================================
system_state = Table(
    "system_state",
    app_metadata,
    #: A key/value row for state that belongs to *the installation*, not to a
    #: user. It exists because the kill switch and the execution mode were an
    #: in-process dict: a restart silently re-armed trading and put the platform
    #: back in paper mode, so a decision an operator made for safety was undone by
    #: a crash. `user_preferences` cannot hold them — its key is (user_id, key),
    #: and there is no user these belong to.
    Column("key", String(64), primary_key=True),
    Column("value", Text),
    Column("updated_at", DateTime, nullable=False),
    #: Who last changed it. An operator action with no actor is not an audit trail.
    Column("updated_by", String(64)),
    #: Free text, mandatory for the consequential changes (going live, engaging
    #: the kill switch). The *caller* enforces that; the column just holds it.
    Column("reason", Text),
    Index("ix_system_state_updated", "updated_at"),
)

__all__ = [
    "api_keys",
    "app_metadata",
    "audit_events",
    "backtest_runs",
    "deployments",
    "order_events",
    "order_intents",
    "orders",
    "reconciliation_runs",
    "screener_scans",
    "sessions",
    "strategies",
    "strategy_versions",
    "system_state",
    "trade_journal",
    "user_preferences",
    "users",
    "watchlist_columns",
    "watchlist_items",
    "watchlists",
]
