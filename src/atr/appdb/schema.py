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
    #: Why the trade ended — the closing order's own reason ("stop_loss",
    #: "target", "time_stop"). A property of the decision rather than of the
    #: price path, so it cannot be recovered from the fills: it is recorded or
    #: it is absent. Added after the table shipped; see
    #: ``AppDatabase.ADDITIVE_COLUMNS`` for how an existing database gets it.
    Column("exit_reason", String(32)),
    Column("slippage_bps", Float),
    #: How independent this record is: ``forward`` when the opening order was
    #: raised live — the OMS stamps the ``NEW`` event with the wall-clock instant
    #: it created the order, and the journal copies that verdict here — and
    #: ``in_sample`` otherwise. A row that cannot demonstrate it was written
    #: before its outcome is graded ``in_sample``, because the alternative is an
    #: in-sample measurement wearing a forward label. Read by
    #: ``atr.services.learning``, which is the only consumer that may treat the
    #: column as evidence. Added after the table shipped; see
    #: ``AppDatabase.ADDITIVE_COLUMNS`` for how an existing database gets it.
    Column("evidence_grade", String(16)),
    Column("notes", Text),
    Column("created_at", DateTime, nullable=False),
    Index("ix_journal_user_exit", "user_id", "exit_ts"),
    Index("ix_journal_strategy", "strategy_id", "strategy_version"),
    Index("ix_journal_symbol", "symbol"),
)

#: The post-trade attribution of one closed trade — the nine-branch object from
#: :mod:`atr.analytics.attribution`, persisted.
#:
#: **Why a separate table rather than more columns on ``trade_journal``.** The
#: journal answers *what happened*: a position opened, it closed, it made this
#: much. Attribution answers *why, and which part of the chain did it*, and it is
#: a different kind of record in three ways that matter:
#:
#: 1. **It is derived, not primary.** A journal row is written by the fold and is
#:    true the instant it exists. An attribution row is computed from the journal,
#:    the order-event log and the price cache, so it can be absent (not yet
#:    computed) or superseded (a late fill arrived). Putting it on the journal
#:    would make *"this trade has no net P&L"* and *"this trade has not been
#:    attributed"* the same sentence.
#: 2. **It has a version and a provenance of its own.** ``computed_at`` and
#:    ``input_fingerprint`` say when it was derived and from what; a re-run with
#:    an unchanged fingerprint is a no-op, and a re-run with a changed one is a
#:    replacement. Neither concept exists on the journal.
#: 3. **It is keyed one-per-trade by construction.** ``trade_id`` is the primary
#:    key, which is what makes attribution **idempotent at the database level** —
#:    a second computation cannot create a second row, whatever the caller does.
#:
#: The attribution row never *generates* evidence. ``evidence_grade`` here is a
#: **copy of the journal's verdict**, taken so a query can filter on it without a
#: join; the service reads the journal's column and refuses to accept a grade
#: from any other source. A row whose copied grade disagreed with its journal row
#: would be a second opinion about provenance, and provenance is not a matter of
#: opinion.
trade_attributions = Table(
    "trade_attributions",
    app_metadata,
    Column("trade_id", String(32), primary_key=True),
    Column(
        "user_id",
        String(32),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("deployment_id", String(32)),
    Column("symbol", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    #: BACKTEST | PAPER | LIVE — the venue the trade came from.
    Column("source", String(16), nullable=False),
    #: forward | in_sample. **Copied from ``trade_journal.evidence_grade``**, never
    #: computed here. Duplicated into this table so a forward-only query does not
    #: have to join; the service is the only writer and takes the value from the
    #: journal. See the module docstring of ``atr.services.attribution``.
    Column("evidence_grade", String(16)),
    #: PAPER_FORWARD | LIVE_FORWARD | IN_SAMPLE | BACKTEST — the class the grade
    #: was derived from, kept so a reader can see *why* the grade is what it is.
    Column("evidence_class", String(16)),
    #: True when the fills were produced by a matching model rather than reported
    #: by a broker. Backtest and paper are both simulated; live is not. Carried so
    #: a comparison can filter simulated from real without consulting a second
    #: table.
    Column("simulated", Boolean, nullable=False, default=True),

    # ── the nine branches, frozen as JSON ──────────────────────────────────
    #: The full attribution object: the TRADE tree, the excursion set, the
    #: reason codes and their stated basis, the missing fields, and the
    #: thresholds used. Stored whole rather than exploded into ~80 columns
    #: because the tree's *shape* is the deliverable and a reader wants it
    #: intact; the fields the learning axes actually slice on are lifted into
    #: their own columns below, and those are the only ones a query needs.
    Column("attribution", Text, nullable=False),
    #: The codes, flattened and comma-joined, so ``LIKE`` can find a code without
    #: parsing every blob. A denormalisation with a single writer, which is the
    #: only kind that is safe.
    Column("reason_codes", Text),
    Column("missing_fields", Text),

    # ── lifted columns: what the learning axes slice on ────────────────────
    Column("entry_quality", String(16)),
    Column("execution_quality", String(24)),
    Column("mfe_pct", Float),
    Column("mae_pct", Float),
    Column("mfe_amount", Float),
    Column("mae_amount", Float),
    Column("mfe_over_risk", Float),
    Column("realized_over_risk", Float),
    Column("capture_efficiency_pct", Float),
    Column("entry_slippage_bps", Float),
    Column("exit_slippage_bps", Float),
    Column("total_slippage_bps", Float),
    Column("transaction_costs", Float),
    Column("cost_pct", Float),
    Column("signal_to_order_sec", Float),
    Column("order_to_fill_sec", Float),
    Column("holding_sec", Integer),
    Column("partial_fill", Boolean, nullable=False, default=False),
    Column("fill_ratio", Float),
    Column("sizing_method", String(32)),
    Column("sizing_cap_reason", String(64)),
    Column("realized_risk_pct", Float),
    Column("planned_risk_amount", Float),
    Column("context_score", Integer),
    Column("context_class", String(24)),
    Column("market_regime", String(24)),
    Column("sector", String(64)),
    Column("sector_strength", Float),
    Column("stock_relative_strength", Float),
    Column("rvol", Float),
    Column("atr_pct", Float),
    Column("exit_reason", String(32)),

    # ── bookkeeping ────────────────────────────────────────────────────────
    #: Hash of everything the computation read. A re-run that produces the same
    #: fingerprint is a no-op rather than a rewrite, which is what makes
    #: "attribute every closed trade" safe to schedule: the second run over an
    #: unchanged book writes nothing.
    Column("input_fingerprint", String(64), nullable=False),
    Column("computed_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Index("ix_attr_user_computed", "user_id", "computed_at"),
    Index("ix_attr_user_grade", "user_id", "evidence_grade"),
    Index("ix_attr_strategy", "symbol"),
    Index("ix_attr_regime", "market_regime"),
    Index("ix_attr_sector", "sector"),
)


# ===========================================================================
# Signal context (Context-Aware Signal Engine)
# ===========================================================================
# One row per enriched signal. Read-only from the signal engine's point of view:
# it is written after the signal fires and never retroactively changes. ``run_id``
# + ``trade_id`` link a backtest context to its trade row; ``order_id`` /
# ``signal_id`` link a live/paper context to the order it produced.
signal_contexts = Table(
    "signal_contexts",
    app_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    #: Stable identifier of the signal, established by whoever raised it. For a
    #: paper deployment this is the bar key (``SYMBOL:YYYY-MM-DD``); for a
    #: backtest run it is ``RUN_ID:SEQ``.
    Column("signal_id", String(64), nullable=False),
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Column("symbol", String(64), nullable=False),
    #: BUY | SELL
    Column("action", String(8), nullable=False),
    #: LIVE | PAPER | BACKTEST
    Column("signal_source", String(16), nullable=False),
    Column("signal_ts", String(40), nullable=False),
    #: The SignalContextModelConfig version that produced this record. A change
    #: of scoring weights must not reinterpret existing rows.
    Column("context_model_version", String(24), nullable=False),
    #: STRONG_CONTEXT | NEUTRAL_CONTEXT | WEAK_CONTEXT | INSUFFICIENT_DATA
    Column("context_class", String(24), nullable=False),
    Column("context_score", Integer, nullable=False, default=0),
    Column("max_possible_score", Integer, nullable=False, default=0),
    Column("has_insufficient_data", Boolean, nullable=False, default=False),
    #: Linkage back to the source of the signal, for outcome joins.
    Column("run_id", String(32)),
    Column("trade_id", Integer),
    Column("order_id", String(32)),
    #: JSON snapshots — kept as text so the record is self-describing and a new
    #: scoring version can reclassify old rows without more columns.
    Column("market_context", Text),
    Column("sector_context", Text),
    Column("stock_context", Text),
    Column("score_breakdown", Text),
    Column("missing_fields", Text),
    Column("benchmark_provenance", Text),
    Column("created_at", DateTime, nullable=False),
    #: One context per signal. Live/paper enrichment is insert-once: a re-fire of
    #: the same bar must dedupe rather than double-count in analytics. Backtest
    #: rows are replaced wholesale per run, so they never collide on this key.
    UniqueConstraint("user_id", "signal_id", name="uq_signal_context_user_signal"),
    Index("ix_signal_ctx_user_ts", "user_id", "signal_ts"),
    Index("ix_signal_ctx_symbol", "symbol"),
    Index("ix_signal_ctx_source", "signal_source"),
    Index("ix_signal_ctx_run_trade", "run_id", "trade_id"),
    Index("ix_signal_ctx_signal_id", "signal_id"),
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

backtest_trades = Table(
    "backtest_trades",
    app_metadata,
    # Auto-increment surrogate: a round trip has no natural single-column key
    # (the same symbol can enter twice at the same timestamp on different
    # rebalances, and FIFO splits one fill across several lots).
    Column("trade_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        String(32),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("symbol", String(64), nullable=False),
    #: LONG | SHORT. Stored explicitly rather than inferred from quantity sign,
    #: because a closed short has a positive exit delta and no sign to read.
    Column("direction", String(6), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("entry_ts", DateTime, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_ts", DateTime),
    Column("exit_price", Float),
    Column("gross_pnl", Float, nullable=False, default=0.0),
    Column("commission", Float, nullable=False, default=0.0),
    Column("net_pnl", Float, nullable=False, default=0.0),
    Column("return_pct", Float),
    Column("duration_days", Float),
    #: Why it was exited: stop_loss | take_profit | trailing_stop | signal | end_of_test
    Column("exit_reason", String(24)),
    #: The signal conditions that caused the entry, as prose. This is the field
    #: that answers "why did this trade happen" a year later, and the one the
    #: user asked for explicitly.
    Column("signal_reason", Text),
    #: Strategy identity stamped per trade as well as per run: a trade row is
    #: read on its own, and "which version produced this?" must not require a
    #: join that the reader might not think to make.
    Column("strategy_id", String(32)),
    Column("strategy_version", Integer),
    Index("ix_bt_trades_run", "run_id", "seq"),
    Index("ix_bt_trades_symbol", "symbol"),
)

backtest_curves = Table(
    "backtest_curves",
    app_metadata,
    Column("curve_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        String(32),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    #: equity | drawdown | exposure | cash. One row set per kind per run.
    Column("kind", String(16), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("ts", DateTime, nullable=False),
    Column("value", Float, nullable=False),
    Index("ix_bt_curves_run", "run_id", "kind", "seq"),
)

backtest_monthly = Table(
    "backtest_monthly",
    app_metadata,
    Column("row_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        String(32),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("year", Integer, nullable=False),
    Column("month", Integer, nullable=False),
    #: Percent return for the month. NULL means the run did not cover it, which
    #: is different from 0.0% and must not be rendered as a flat month.
    Column("return_pct", Float),
    Index("ix_bt_monthly_run", "run_id", "year", "month"),
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


# ===========================================================================
# Portfolio tier — per-user capital allocation policy
# ===========================================================================
portfolio_policies = Table(
    "portfolio_policies",
    app_metadata,
    #: One row per user. User-scoped deliberately: ``system_state`` belongs to
    #: the installation (kill switch, execution mode), while capital
    #: allocation and portfolio limits belong to whoever owns the paper book.
    Column("user_id", String(32), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True),
    #: The policy as JSON: portfolio limits, conflict handling and strategy
    #: priorities. Validated key-by-key on write by the portfolio service, so a
    #: stored row is always one the gate understands.
    Column("policy", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    #: Who last changed it. A limit change with no actor is not an audit trail.
    Column("updated_by", String(64)),
    #: Free text, mandatory on write. Changing what the platform may risk is a
    #: consequential act; the caller enforces the requirement, the column holds it.
    Column("reason", Text),
)


# ===========================================================================
# Learning tier — observations & research findings
# ===========================================================================
learning_observations = Table(
    "learning_observations",
    app_metadata,
    Column("observation_id", String(36), primary_key=True),
    Column("strategy_id", String(32), nullable=False),
    Column("strategy_version", Integer, nullable=True),
    Column("date", String(10), nullable=False),  # YYYY-MM-DD
    Column("created_at", DateTime, nullable=False),
    Column("metric", String(64), nullable=False),
    Column("condition_bucket", String(128), nullable=False),  # e.g. "rsi_bucket:low" or "rvol_bucket:>2.0"
    Column("sample_size", Integer, nullable=False),
    Column("statistical_result", Text, nullable=False),  # JSON dict with win_rate, mean, median, etc.
    Column("evidence_class", String(32), nullable=False),  # PAPER_FORWARD, BACKTEST, IN_SAMPLE, LIVE_FORWARD
    Column("confidence", Float, nullable=True),
    Column("source_trades", Text, nullable=True),  # JSON list of trade IDs
    Index("ix_learning_obs_strat", "strategy_id", "strategy_version"),
    Index("ix_learning_obs_date", "date"),
    Index("ix_learning_obs_evidence", "evidence_class"),
)

optimization_recommendations = Table(
    "optimization_recommendations",
    app_metadata,
    Column("recommendation_id", String(36), primary_key=True),
    Column(
        "strategy_id",
        String(32),
        ForeignKey("strategies.strategy_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_strategy_version", Integer, nullable=False),
    Column("target_strategy_version", Integer, nullable=True),
    Column("parameter", String(64), nullable=False),
    Column("current_value", Float, nullable=False),
    Column("proposed_value", Float, nullable=False),
    Column("reason", Text, nullable=False),
    Column("source_observations", Text, nullable=False),  # JSON list of observation details
    Column("sample_size", Integer, nullable=False),
    Column("baseline_metrics", Text, nullable=False),  # JSON dict
    Column("candidate_metrics", Text, nullable=False),  # JSON dict
    Column("walk_forward_metrics", Text, nullable=False),  # JSON dict
    Column("robustness_results", Text, nullable=False),  # JSON dict
    Column("confidence", String(16), nullable=True),
    Column("status", String(16), nullable=False, default="PROPOSED"),
    Column("rejection_reason", Text, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("reviewed_by", String(64), nullable=True),
    Column("reviewed_at", DateTime, nullable=True),
    Index("ix_opt_rec_strategy", "strategy_id", "source_strategy_version"),
    Index("ix_opt_rec_status", "status"),
    Index("ix_opt_rec_created", "created_at"),
)

strategy_experiments = Table(
    "strategy_experiments",
    app_metadata,
    Column("experiment_id", String(36), primary_key=True),
    Column(
        "strategy_id",
        String(32),
        ForeignKey("strategies.strategy_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_version", Integer, nullable=False),
    Column("target_version", Integer, nullable=True),
    Column(
        "recommendation_id",
        String(36),
        ForeignKey("optimization_recommendations.recommendation_id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("creator_user_id", String(64), nullable=False),
    Column("name", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("parameter_changes", Text, nullable=False),  # JSON dict of changes
    Column("baseline_definition", Text, nullable=False),  # JSON dict
    Column("candidate_definition", Text, nullable=False),  # JSON dict
    Column("status", String(20), nullable=False, default="CREATED"),
    Column("rejection_reason", Text, nullable=True),
    Column("results", Text, nullable=True),  # JSON dict containing metrics comparison, curves, distributions, regimes
    Column("explanation", Text, nullable=True),  # JSON dict: what changed, why proposed, what experiment found
    Column("error", Text, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("reviewed_by", String(64), nullable=True),
    Column("reviewed_at", DateTime, nullable=True),
    Index("ix_exp_strategy", "strategy_id", "source_version"),
    Index("ix_exp_status", "status"),
    Index("ix_exp_created", "created_at"),
)

__all__ = [
    "api_keys",
    "app_metadata",
    "audit_events",
    "backtest_curves",
    "backtest_monthly",
    "backtest_runs",
    "backtest_trades",
    "deployments",
    "learning_observations",
    "optimization_recommendations",
    "order_events",
    "order_intents",
    "orders",
    "portfolio_policies",
    "reconciliation_runs",
    "screener_scans",
    "sessions",
    "signal_contexts",
    "strategies",
    "strategy_experiments",
    "strategy_versions",
    "system_state",
    "trade_attributions",
    "trade_journal",
    "user_preferences",
    "users",
    "watchlist_columns",
    "watchlist_items",
    "watchlists",
]



