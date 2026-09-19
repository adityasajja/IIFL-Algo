# atr — Data Model

Companion to `docs/ARCHITECTURE.md`. Sections 1, 3 and 4 are **normative** — the
DDL there matches `src/atr/appdb/schema.py` exactly, table for table, and is
covered by `tests/test_appdb_trading.py` and `tests/test_watchlists.py`. Section 2
is a **specification** for a later phase: it exists so the design is settled before
the code is written, not to claim the tables exist.

The control plane currently holds **19 tables**: `users`, `sessions`, `api_keys`,
`watchlists`, `watchlist_items`, `watchlist_columns`, `user_preferences`,
`audit_events` (§1) and `orders`, `order_events`, `order_intents`, `deployments`,
`reconciliation_runs`, `trade_journal`, `strategies`, `strategy_versions`,
`backtest_runs`, `screener_scans` (§3–4) and `system_state` (§6).and `system_state` (§6).

---

## 1. Control plane — `atr/appdb` (Phase 1, shipped)

Store: SQLite by default (`data/app.db`), Postgres by setting `APP_DB_URL`.
Low volume, relational, zero setup. The market-data and trading tiers stay on
Postgres/TimescaleDB — they have a different shape and a different scale, and
merging them would force TimescaleDB on every install.

### Design rules

- **No plaintext secrets, ever.** Passwords are scrypt digests; session tokens and
  API keys are stored as SHA-256 digests and the raw value is shown exactly once.
- **Timestamps are UTC.** Stored naive-UTC (SQLite has no tz-aware type); the API
  layer attaches `Z` on the way out.
- **Deletes cascade.** A user's watchlists die with the user; enforced in SQLite
  with `PRAGMA foreign_keys=ON`, which is off by default and is set explicitly.
- **Append-only where the record must survive.** `audit_events` is never updated.

### 1.1 `users`

```sql
CREATE TABLE users (
    user_id       VARCHAR(32)  PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    username      VARCHAR(64)  NOT NULL UNIQUE,
    display_name  VARCHAR(128),
    password_hash TEXT         NOT NULL,   -- scrypt$n$r$p$salt_b64$hash_b64
    role          VARCHAR(16)  NOT NULL,   -- owner|admin|trader|researcher|viewer
    is_active     BOOLEAN      NOT NULL DEFAULT 1,
    mfa_enabled   BOOLEAN      NOT NULL DEFAULT 0,
    mfa_secret    TEXT,                    -- base32 TOTP seed, encrypted at rest
    failed_logins INTEGER      NOT NULL DEFAULT 0,
    locked_until  TIMESTAMP,               -- brute-force backoff
    created_at    TIMESTAMP    NOT NULL,
    updated_at    TIMESTAMP    NOT NULL,
    last_login_at TIMESTAMP
);
```

`role` is a single column rather than a join table because the permission model is
a fixed matrix (section 1.7), not an editable graph. If roles become
user-defined, this becomes `roles` + `user_roles` — a migration, not a redesign.

### 1.2 `sessions`

```sql
CREATE TABLE sessions (
    session_id    VARCHAR(32) PRIMARY KEY,
    user_id       VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash    CHAR(64)    NOT NULL UNIQUE,  -- sha256(token); token itself never stored
    created_at    TIMESTAMP   NOT NULL,
    expires_at    TIMESTAMP   NOT NULL,
    last_seen_at  TIMESTAMP,
    revoked_at    TIMESTAMP,
    ip            VARCHAR(64),
    user_agent    VARCHAR(255),
    mfa_satisfied BOOLEAN     NOT NULL DEFAULT 0
);
CREATE INDEX ix_sessions_user     ON sessions(user_id);
CREATE INDEX ix_sessions_expires  ON sessions(expires_at);
```

Why opaque tokens and not JWT: a session must be **revocable**. A JWT is valid
until it expires, so "log out everywhere" and "this token leaked" have no answer
short of rotating a signing key. The cost is one indexed lookup per request,
which is nothing at this scale.

`mfa_satisfied` exists because a password-only login must be able to produce a
session that can *enrol* MFA but not *act*. That intermediate state is what makes
the second factor mandatory rather than optional.

### 1.3 `api_keys`

```sql
CREATE TABLE api_keys (
    key_id       VARCHAR(32) PRIMARY KEY,
    user_id      VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    label        VARCHAR(64) NOT NULL,
    prefix       VARCHAR(12) NOT NULL,   -- "atr_ab12cd" — the only part shown in the UI
    key_hash     CHAR(64)    NOT NULL UNIQUE,
    scopes       TEXT        NOT NULL,   -- comma-separated permissions
    created_at   TIMESTAMP   NOT NULL,
    expires_at   TIMESTAMP,
    last_used_at TIMESTAMP,
    revoked_at   TIMESTAMP
);
CREATE INDEX ix_api_keys_user ON api_keys(user_id);
```

Key format: `atr_<prefix>_<secret>`. Only `prefix` is stored in the clear, so the
UI can show "which key is this" without the database being able to reconstruct it.

### 1.4 `watchlists` / `watchlist_items` / `watchlist_columns`

```sql
CREATE TABLE watchlists (
    watchlist_id VARCHAR(32) PRIMARY KEY,
    user_id      VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name         VARCHAR(64) NOT NULL,
    exchange     VARCHAR(16) NOT NULL DEFAULT 'NSEEQ',
    is_default   BOOLEAN     NOT NULL DEFAULT 0,
    sort_order   INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMP   NOT NULL,
    updated_at   TIMESTAMP   NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE watchlist_items (
    item_id      VARCHAR(32) PRIMARY KEY,
    watchlist_id VARCHAR(32) NOT NULL REFERENCES watchlists(watchlist_id) ON DELETE CASCADE,
    symbol       VARCHAR(64) NOT NULL,
    sort_order   INTEGER     NOT NULL DEFAULT 0,
    note         VARCHAR(255),
    added_at     TIMESTAMP   NOT NULL,
    UNIQUE (watchlist_id, symbol)
);
CREATE INDEX ix_wl_items_order ON watchlist_items(watchlist_id, sort_order);

CREATE TABLE watchlist_columns (
    column_id    VARCHAR(32) PRIMARY KEY,
    watchlist_id VARCHAR(32) NOT NULL REFERENCES watchlists(watchlist_id) ON DELETE CASCADE,
    key          VARCHAR(32) NOT NULL,   -- ltp|change_pct|volume|rsi14|ema20|atr14|oi|iv|…
    sort_order   INTEGER     NOT NULL DEFAULT 0,
    width        INTEGER,
    visible      BOOLEAN     NOT NULL DEFAULT 1,
    UNIQUE (watchlist_id, key)
);
```

Two deliberate choices:

- **`sort_order` is a plain integer, rewritten as a block on reorder.** A gap-based
  or fractional-index scheme is the usual reflex, and it is not needed: a watchlist
  is tens of rows, so renumbering all of them in one transaction is free and
  cannot drift. Drift is the failure mode that fractional indices exist to avoid,
  and here there is nothing to avoid.
- **`key` is validated against a registry in code, not by a foreign key.** The set
  of computable columns is a property of the indicator layer, and a DB constraint
  would have to be migrated every time an indicator is added. An unknown key is
  rejected at the API boundary with the list of valid keys.

### 1.5 `user_preferences`

```sql
CREATE TABLE user_preferences (
    user_id    VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    key        VARCHAR(64) NOT NULL,
    value      TEXT        NOT NULL,   -- JSON
    updated_at TIMESTAMP   NOT NULL,
    PRIMARY KEY (user_id, key)
);
```

### 1.6 `audit_events`

```sql
CREATE TABLE audit_events (
    event_id         VARCHAR(32) PRIMARY KEY,
    ts               TIMESTAMP   NOT NULL,
    user_id          VARCHAR(32),
    actor            VARCHAR(64),            -- email, or "system"
    action           VARCHAR(64) NOT NULL,   -- login.success, watchlist.delete, order.place, …
    target_type      VARCHAR(32),
    target_id        VARCHAR(64),
    strategy_id      VARCHAR(32),
    strategy_version INTEGER,
    request_id       VARCHAR(32),
    result           VARCHAR(16) NOT NULL,   -- success|failure|denied
    detail           TEXT,                   -- JSON
    ip               VARCHAR(64)
);
CREATE INDEX ix_audit_ts      ON audit_events(ts);
CREATE INDEX ix_audit_user_ts ON audit_events(user_id, ts);
```

The existing `data/audit/audit.jsonl` continues to be written — it is the
restart-proof trail the execution-mode gate depends on, and `tests/test_execution_mode.py`
pins it. `audit_events` is the *queryable* mirror. Two writers is a real cost and
is accepted deliberately: a file survives a database that is not configured, and a
table answers "show me every denied request last week", which a JSONL scan does not.

### 1.7 Role → permission matrix

Permissions are `resource:action`. A role is a set of them. `owner` is a superset
of `admin`; `viewer` can read and nothing else.

| Permission | viewer | researcher | trader | admin | owner |
|---|:--:|:--:|:--:|:--:|:--:|
| `market:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `instrument:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `watchlist:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `watchlist:write` | | ✓ | ✓ | ✓ | ✓ |
| `strategy:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `strategy:write` | | ✓ | ✓ | ✓ | ✓ |
| `backtest:run` | | ✓ | ✓ | ✓ | ✓ |
| `screener:run` | | ✓ | ✓ | ✓ | ✓ |
| `order:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `order:place` | | | ✓ | ✓ | ✓ |
| `order:cancel` | | | ✓ | ✓ | ✓ |
| `position:squareoff` | | | ✓ | ✓ | ✓ |
| `risk:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `risk:configure` | | | | ✓ | ✓ |
| `algo:start` | | | ✓ | ✓ | ✓ |
| `algo:stop` | | | ✓ | ✓ | ✓ |
| `execution_mode:change` | | | | ✓ | ✓ |
| `user:read` | | | | ✓ | ✓ |
| `user:write` | | | | ✓ | ✓ |
| `apikey:manage` | | ✓ | ✓ | ✓ | ✓ |
| `audit:read` | | | | ✓ | ✓ |
| `system:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `system:configure` | | | | | ✓ |

Two separations are load-bearing and are the reason this is a matrix and not a
boolean `is_admin`:

- **`order:place` is not implied by `strategy:write`.** Research and execution are
  different authorities — a researcher can build and validate a strategy and still
  be unable to send an order. The brief asks for exactly this separation.
- **`execution_mode:change` is admin-only.** Flipping paper → live is the single
  most consequential action in the product; it requires a role, a reason string,
  and an audit row.

---

## 2. Market data tier (Postgres + TimescaleDB)

**Shipped today** (`src/atr/data/store.py`): `instruments`, `bars`, `ticks`,
`orders`, `fills`. Hypertables on `bars`/`ticks`, compression policies, and a
`bars_pk` that includes the partition column `ts` as TimescaleDB requires.

**Specified, not built:**

```sql
-- Option chain snapshots. One row per (ts, underlying, expiry, strike).
CREATE TABLE option_chain (
    ts             TIMESTAMPTZ NOT NULL,
    underlying     VARCHAR(32) NOT NULL,
    expiry         DATE        NOT NULL,
    strike         DOUBLE PRECISION NOT NULL,
    option_type    VARCHAR(4)  NOT NULL,        -- CE | PE
    ltp            DOUBLE PRECISION,
    volume         DOUBLE PRECISION,
    oi             DOUBLE PRECISION,
    oi_change      DOUBLE PRECISION,
    iv             DOUBLE PRECISION,
    delta DOUBLE PRECISION, gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION, vega  DOUBLE PRECISION,
    bid DOUBLE PRECISION, ask DOUBLE PRECISION,
    bid_qty DOUBLE PRECISION, ask_qty DOUBLE PRECISION,
    PRIMARY KEY (ts, underlying, expiry, strike, option_type)
);
SELECT create_hypertable('option_chain', 'ts');
```

Greeks are **stored, not recomputed on read**. Two reasons: the broker's IV is
the one that priced the market, and a re-derived delta that disagrees with the
broker's is a bug report you cannot win. Store both when they differ.

```sql
-- Corporate actions. Drives historical adjustment.
CREATE TABLE corporate_actions (
    action_id    VARCHAR(32) PRIMARY KEY,
    symbol       VARCHAR(64) NOT NULL,
    exchange     VARCHAR(16) NOT NULL,
    action_type  VARCHAR(16) NOT NULL,   -- SPLIT|BONUS|DIVIDEND|RIGHTS|MERGER|SYMBOL_CHANGE|DELIST
    ex_date      DATE        NOT NULL,
    record_date  DATE,
    ratio_from   DOUBLE PRECISION,       -- 1 for a 1:5 split
    ratio_to     DOUBLE PRECISION,       -- 5
    amount       DOUBLE PRECISION,       -- dividend per share
    new_symbol   VARCHAR(64),
    raw          TEXT                    -- source payload, for audit
);

-- Market calendar. Never hardcoded in the frontend.
CREATE TABLE market_calendar (
    exchange    VARCHAR(16) NOT NULL,
    cal_date    DATE        NOT NULL,
    session     VARCHAR(24) NOT NULL,   -- REGULAR|MUHURAT|SPECIAL|CLOSED
    open_at     TIMESTAMPTZ,
    close_at    TIMESTAMPTZ,
    note        VARCHAR(128),
    PRIMARY KEY (exchange, cal_date, session)
);

-- Expiry calendar, derived from the contract master but materialised because
-- every options query needs "the nearest expiry" cheaply.
CREATE TABLE expiry_calendar (
    underlying  VARCHAR(32) NOT NULL,
    expiry      DATE        NOT NULL,
    kind        VARCHAR(8)  NOT NULL,   -- WEEKLY|MONTHLY
    PRIMARY KEY (underlying, expiry)
);
```

Adjustment policy, stated so it cannot be quietly wrong: **prices are stored
unadjusted and adjusted on read**, using `corporate_actions`. Storing adjusted
prices destroys the ability to reconstruct what actually traded, and a
back-adjusted series changes retroactively every time a new action occurs. The
backtester applies the adjustment factor at load time and records the factor used
in the run metadata, so a result is reproducible.

---

## 3. Trading tier

**Shipped 2026-09-14:** `orders`, `order_events`, `order_intents`, `deployments`,
`reconciliation_runs`, `trade_journal` — all in `atr/appdb/schema.py`, with
repositories in `atr/appdb/repositories.py` and tests in
`tests/test_appdb_trading.py`.

Three deviations from the original spec in this section, each for a reason:

1. **`order_events.seq` was added.** The spec ordered the log by `(order_id, ts)`.
   That is not a total order: a reject is written microseconds after the submit it
   rejects, and two transitions can legitimately share a timestamp. Ordering by
   `ts` alone is a tie-break waiting to happen, so the log carries an explicit
   per-order sequence with `UNIQUE(order_id, seq)`, and `(order_id, ts)` is kept as
   a secondary index.
2. **There is no `fills` table.** A fill *is* an `order_events` row whose
   `to_status` is `PARTIALLY_FILLED` or `FILLED` with `filled_qty`/`filled_price`
   set. Positions and P&L are folds over those rows. A separate fill table would be
   a second copy of the same facts with no way to tell which one is right when they
   disagree. The Postgres `orders`/`fills` DDL in `atr/data/store.py` is
   **deprecated** — nothing ever called it.
3. **`order_intents` gained `user_id`.** See the note on key derivation below.

```sql
-- One row per transition, append-only. `orders.status` is a projection of the
-- newest row here, written in the same transaction — the log is authoritative,
-- the column is derived.
CREATE TABLE orders (
    order_id        VARCHAR(32) PRIMARY KEY,
    user_id         VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    deployment_id   VARCHAR(32) REFERENCES deployments(deployment_id) ON DELETE SET NULL,
    strategy_id     VARCHAR(32),
    strategy_version INTEGER,
    signal_id       VARCHAR(32),
    correlation_id  VARCHAR(32),   -- signal → risk → intent → order → fill
    symbol          VARCHAR(64) NOT NULL,
    exchange        VARCHAR(16) NOT NULL DEFAULT 'NSEEQ',
    asset_class     VARCHAR(16) NOT NULL DEFAULT 'EQUITY',
    side            VARCHAR(4)  NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    order_type      VARCHAR(12) NOT NULL DEFAULT 'MARKET',
    limit_price     DOUBLE PRECISION,
    stop_price      DOUBLE PRECISION,
    tif             VARCHAR(8)  NOT NULL DEFAULT 'DAY',
    product         VARCHAR(16),
    mode            VARCHAR(8)  NOT NULL DEFAULT 'PAPER',  -- PAPER|LIVE
    status          VARCHAR(24) NOT NULL,                  -- projection; see above
    broker_order_id VARCHAR(64),
    filled_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_fill_price  DOUBLE PRECISION NOT NULL DEFAULT 0,
    requested_price DOUBLE PRECISION,   -- price at raise time, for slippage
    tag             VARCHAR(64),
    reject_reason   VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    submitted_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE TABLE order_events (
    order_event_id VARCHAR(32) PRIMARY KEY,
    order_id       VARCHAR(32) NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    seq            INTEGER     NOT NULL,   -- monotonic per order; the real ordering
    from_status    VARCHAR(24),
    to_status      VARCHAR(24) NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    broker_ts      TIMESTAMPTZ,          -- exchange timestamp when the broker gives one
    ack_ts         TIMESTAMPTZ,
    fill_ts        TIMESTAMPTZ,
    requested_price DOUBLE PRECISION,
    filled_price    DOUBLE PRECISION,
    filled_qty      DOUBLE PRECISION,    -- CUMULATIVE, as the broker reports it
    slippage_bps    DOUBLE PRECISION,
    commission      DOUBLE PRECISION,    -- rupees, as charged; see below
    latency_ms      INTEGER,             -- signal→submit, submit→ack, ack→fill
    reject_reason   VARCHAR(255),
    raw             TEXT,                -- the broker's own payload, verbatim
    source          VARCHAR(16) NOT NULL DEFAULT 'oms',  -- oms|broker|reconciler|user
    correlation_id  VARCHAR(32),
    request_id      VARCHAR(32),
    UNIQUE (order_id, seq)
);
CREATE INDEX ix_order_events_order ON order_events(order_id, seq);
CREATE INDEX ix_order_events_ts    ON order_events(ts);

-- Idempotency guard. The primary key IS the mechanism, not a convention.
CREATE TABLE order_intents (
    idempotency_key CHAR(64) PRIMARY KEY,
    order_id        VARCHAR(32) NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    user_id         VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    strategy_id     VARCHAR(32),
    strategy_version INTEGER,
    signal_id       VARCHAR(32),
    created_at      TIMESTAMPTZ NOT NULL
);
```

`filled_qty` is **cumulative**, not incremental, because that is the convention the
broker uses: the parsed field and the raw payload cannot then disagree. A single
fill's size is the difference between consecutive events, which `seq` makes
unambiguous.

`commission` was added on 2026-09-14, when the paper engine landed. It is the
frictions **actually charged**, in rupees, and it is recorded rather than recomputed
from a cost model at read time. Two reasons, and the second is the load-bearing one:

* recomputing would silently restate history every time a rate changed;
* it is what lets the paper ledger be a **faithful fold** over this table —
  positions, cash and net P&L all derive from these rows, with no second state store
  to drift. `atr/services/paper.py` replays them into the existing
  `atr.backtest.portfolio.Portfolio`, which asserts `equity == cash + market value`
  after every fill.

**Key derivation.** The key is
`sha256(user_id | strategy_id | strategy_version | signal_id | symbol | side | leg_index)`.
`user_id` is added to the tuple originally specified here because a strategy may be
a *shared registry* entry: two accounts running `engine_key="ema_cross"` would
otherwise produce the same key for the same signal, and the second account would be
handed the first account's order id. See `idempotency_key_for` in
`atr/appdb/repositories.py`.

The guard is the `INSERT` itself — a `SELECT` first would leave a window between
the check and the write, and a duplicate order is exactly what gets created inside
that window. A duplicate returns the existing order and `created=False`; it does not
raise, because a retry is the case this exists for.

```sql
-- Per-strategy capital allocation and lifecycle.
CREATE TABLE deployments (
    deployment_id  VARCHAR(32) PRIMARY KEY,
    user_id        VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    strategy_id    VARCHAR(32) NOT NULL,
    strategy_version INTEGER NOT NULL,
    mode           VARCHAR(8)  NOT NULL,   -- PAPER|LIVE
    status         VARCHAR(12) NOT NULL,   -- PENDING|RUNNING|PAUSED|STOPPED|ERROR
    capital        DOUBLE PRECISION NOT NULL,
    broker_account VARCHAR(32),
    config         TEXT,                   -- JSON: symbols, exchange, risk overrides
    started_at     TIMESTAMPTZ,
    stopped_at     TIMESTAMPTZ,
    stop_reason    VARCHAR(255),
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);
```

`stop_reason` is **mandatory** on the stop path and is rejected if blank. A
deployment that stopped for no recorded reason is a deployment nobody can explain
afterwards, and "why did this stop trading?" is the first question asked when
something is wrong. `config` is stored so the deployment's behaviour is
reproducible from the row alone.

```sql
-- Reconciliation runs. Each run stores what differed, so a mismatch is a fact
-- with a timestamp rather than a transient log line.
CREATE TABLE reconciliation_runs (
    run_id         VARCHAR(32) PRIMARY KEY,
    user_id        VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL,
    scope          VARCHAR(32) NOT NULL,   -- orders|positions|holdings|funds|all
    internal_count INTEGER, broker_count INTEGER,
    mismatches     INTEGER NOT NULL DEFAULT 0,
    severity       VARCHAR(16) NOT NULL DEFAULT 'ok',  -- ok|warning|critical
    detail         TEXT,                   -- JSON list of the individual differences
    error          VARCHAR(255),
    duration_ms    INTEGER
);
```

`severity` is not cosmetic and the repository enforces it: recording a run with
mismatches (or an error) as `ok` raises. A reconciliation that found differences and
recorded `ok` is indistinguishable from one that found nothing, and the silent
version is the failure mode to avoid.

```sql
-- Trading journal. One row per trade; `exit_ts IS NULL` means still open.
CREATE TABLE trade_journal (
    trade_id       VARCHAR(32) PRIMARY KEY,
    user_id        VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    deployment_id  VARCHAR(32),
    strategy_id    VARCHAR(32),
    strategy_version INTEGER,
    symbol         VARCHAR(64) NOT NULL,
    asset_class    VARCHAR(16) NOT NULL DEFAULT 'EQUITY',
    side           VARCHAR(4)  NOT NULL,
    quantity       DOUBLE PRECISION NOT NULL,
    entry_ts       TIMESTAMPTZ NOT NULL,
    exit_ts        TIMESTAMPTZ,
    entry_price    DOUBLE PRECISION NOT NULL,
    exit_price     DOUBLE PRECISION,
    gross_pnl      DOUBLE PRECISION,
    net_pnl        DOUBLE PRECISION,
    mfe            DOUBLE PRECISION,   -- max favourable excursion
    mae            DOUBLE PRECISION,   -- max adverse excursion
    duration_sec   INTEGER,
    regime         VARCHAR(24),        -- from the regime engine
    signal_reason  TEXT,
    exit_reason    VARCHAR(32),        -- the closing order's own reason
    slippage_bps   DOUBLE PRECISION,   -- mean of the measurable legs
    evidence_grade VARCHAR(16),        -- 'forward' | 'in_sample'
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL
);
```

`mfe`/`mae` are stored rather than derived because they require the intra-trade
path, which the order-event log does not preserve once a position is netted.
`duration_sec` *is* derived, from `entry_ts`/`exit_ts` at close time, so the two
cannot disagree. `exit_reason`, `slippage_bps` and `evidence_grade` were added
after the table shipped; `AppDatabase.ADDITIVE_COLUMNS` is how an existing
database gets them.

### `evidence_grade` — the column that decides how much a trade is worth

`forward` when the opening order was raised live, `in_sample` otherwise. It is
**not** derived from timestamps: a replay harness writes historical market
timestamps and can write them consistently, so a reader has no way to tell a
genuine record from a well-forged one. Instead the OMS stamps every order it
raises, on the `NEW` event's `raw` payload, with
`{"provenance": "forward", "recorded_at": <wall clock>}` — and the journal copies
that verdict onto the episode it opens. Anything inserted into the table by
another route simply does not have the stamp, and **absence grades in-sample**,
because over-claiming independence is silent and under-claiming it is merely
conservative.

The four-value `evidence_class` (BACKTEST, IN_SAMPLE, PAPER_FORWARD,
LIVE_FORWARD) and the two-value `evidence_grade` (`forward`, `in_sample`) are
defined once, in `atr/research/learning_evidence.py`, and the grade is derived
from the class by `grade_of` — so a record cannot carry a grade that disagrees
with its class. `atr.services.learning` stamps both onto every dataset row.

---

## 4. Strategy tier

**Shipped 2026-09-14:** `strategies`, `strategy_versions`, `backtest_runs`,
`screener_scans`. The Python registry (`atr.strategy.registry`) is unchanged and
still the execution path for a `code` strategy — a persisted strategy either points
at a registry entry via `engine_key` or is defined entirely by its JSON rules.

**Added 2026-09-15:** `backtest_trades`, `backtest_curves`, `backtest_monthly` —
the per-trade and per-bar artefacts of a run, so the results page can paginate a
trade list and a single trade is an indexed lookup rather than a parsed blob.

```sql
CREATE TABLE strategies (
    strategy_id   VARCHAR(32) PRIMARY KEY,
    user_id       VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name          VARCHAR(128) NOT NULL,
    description   TEXT,
    kind          VARCHAR(16) NOT NULL,   -- nocode|rules|code|options
    engine_key    VARCHAR(64),            -- registry key for kind='code'
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    archived_at   TIMESTAMPTZ,
    UNIQUE (user_id, name)
);

-- Never overwritten. A version is immutable once created.
CREATE TABLE strategy_versions (
    strategy_id    VARCHAR(32) NOT NULL REFERENCES strategies(strategy_id) ON DELETE CASCADE,
    version        INTEGER     NOT NULL,
    author_user_id VARCHAR(32) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    definition     TEXT        NOT NULL,   -- canonical JSON of rules / params / code
    definition_hash CHAR(64)   NOT NULL,   -- sha256 of definition; dedupes identical versions
    change_note    TEXT,
    is_deployed    BOOLEAN     NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy_id, version)
);
```

`engine_key` was added so a persisted strategy can name the registry class that
executes it, which is what lets `BACKTEST → PAPER → LIVE` run one strategy
*definition* through one rule layer rather than three implementations.

Immutability is enforced by **the absence of an UPDATE path in the repository**, not
by a trigger: `StrategyRepository.create_version()` only inserts, the
`(strategy_id, version)` primary key makes a collision a hard error, and there is no
`update_version`/`delete_version` to call. A rule that lives only in a convention is
a rule that will be broken. Renaming or re-describing a strategy is allowed — that
is metadata, not behaviour — and is deliberately the only writable surface.

`definition` is stored in **canonical form**: sorted keys, no insignificant
whitespace, `ensure_ascii=False`, UTF-8. Two definitions that differ only in key
order therefore hash the same and the second is refused with the version number that
already holds it, rather than being stored as version 3 next to an identical
version 2. Version numbers are `max + 1`, so a refused attempt does not leave a gap.

```sql
-- Every backtest run, with enough metadata to reproduce it.
CREATE TABLE backtest_runs (
    run_id         VARCHAR(32) PRIMARY KEY,
    user_id        VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    strategy_id    VARCHAR(32),            -- nullable: a registry run has no strategy row
    strategy_version INTEGER,
    engine_key     VARCHAR(64),            -- at least one target must be set
    created_at     TIMESTAMPTZ NOT NULL,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    status         VARCHAR(12) NOT NULL DEFAULT 'QUEUED',  -- QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED
    progress       DOUBLE PRECISION NOT NULL DEFAULT 0,
    config         TEXT NOT NULL,          -- JSON: dates, symbols, capital, costs
    data_fingerprint CHAR(64),             -- sha256 of the price panel used
    metrics        TEXT,                   -- JSON summary, written once on completion
    error          TEXT
);
```

Three changes from the original spec:

* `strategy_id`/`strategy_version` became **nullable** and `engine_key` was added,
  because a run may target a built-in registry strategy that has no persisted row.
  The "at least one target" rule is enforced in `BacktestRunRepository.create()`.
* `status`/`progress`/`started_at`/`finished_at`/`error` were added: the brief
  requires asynchronous runs with pollable state, and a run whose only record is
  "a row exists" cannot report `QUEUED` vs `RUNNING` vs `FAILED`.
* `equity_curve`/`trade_count`/`oos`/`fold` were **dropped from the row**. The
  equity curve, the trade list, the monthly returns and the exposure series are
  per-bar/per-trade artefacts; they do not belong as multi-megabyte JSON in a
  control-plane row that every list query then has to avoid reading. The row
  keeps the *summary*; `metrics` is the JSON blob the dashboard renders.

  **The effective parameters are resolved, not read (added 2026-09-15).** The
  trade-detail read originally reported `config["params"]` verbatim. For a run
  pinned to a saved version that is `{}`, because a version's parameters live in
  `strategy_versions.definition` and never in the run's own config — so the
  trade view answered "which parameters ran?" with nothing. It now merges the
  stored definition first and the run's explicit overrides on top, mirroring
  `BacktestRunner.build_strategy`. A version backtest can therefore say what the
  version contained.

  **Where they went instead (revised 2026-09-15).** This section originally said
  they should live "in a file keyed by `run_id`". They are in three tables
  instead: `backtest_trades`, `backtest_curves`, `backtest_monthly`. The reason
  is the requirement that a user can open *any* trade and see it — a file means
  parsing the whole artefact to serve page 2 of a 20,000-trade run, and it makes
  a per-trade lookup a linear scan. The tables give a `(run_id, seq)` index for
  free. `run_id` is a foreign key with `ON DELETE CASCADE`, so deleting a run
  cannot orphan its artefacts. The cost is three more tables in the control-plane
  database; the benefit is that the trade list is a query rather than a parse.

`backtest_trades` stores `signal_reason` and `exit_reason` as text. These are the
*why* of a trade — the signal conditions that opened it and the rule that closed
it — and they are the difference between a trade list and a P&L statement. They
are nullable because not every strategy can explain itself, and a row that says
"not recorded" is honest where a synthesised sentence would not be.

```sql
-- Per-trade artefacts of a run. Written once, when the run completes.
CREATE TABLE backtest_trades (
    trade_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       VARCHAR(32) NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,       -- 0-based, stable order within a run
    symbol       VARCHAR(32) NOT NULL,
    direction    VARCHAR(6)  NOT NULL,   -- LONG | SHORT, stored rather than inferred
    quantity     DOUBLE PRECISION NOT NULL,
    entry_ts     TIMESTAMPTZ NOT NULL,
    entry_price  DOUBLE PRECISION NOT NULL,
    exit_ts      TIMESTAMPTZ,
    exit_price   DOUBLE PRECISION,
    gross_pnl    DOUBLE PRECISION,
    commission   DOUBLE PRECISION,
    net_pnl      DOUBLE PRECISION,
    return_pct   DOUBLE PRECISION,
    duration_days DOUBLE PRECISION,
    exit_reason  VARCHAR(32),            -- signal | stop_loss | take_profit | trailing_stop
    signal_reason TEXT,                  -- the strategy's own words, or NULL
    strategy_id  VARCHAR(32),
    strategy_version INTEGER
);

-- Equity, drawdown and exposure as one row per (run, kind, bar).
CREATE TABLE backtest_curves (
    curve_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    VARCHAR(32) NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    kind      VARCHAR(12) NOT NULL,      -- equity | drawdown | exposure | cash
    seq       INTEGER NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    value     DOUBLE PRECISION NOT NULL
);

-- Calendar-month returns. NULL is "no bars that month", which is not 0.00%.
CREATE TABLE backtest_monthly (
    row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     VARCHAR(32) NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,
    return_pct DOUBLE PRECISION
);
```

`data_fingerprint` is the field that makes a backtest a *measurement*: without it
you cannot tell whether two runs differ because the strategy changed or because the
data did. It is nullable because a run that never produced a result has no
fingerprint, and inventing one would be a lie.

A completed run is **terminal**: `complete`/`fail`/`cancel`/`mark_running` all
filter on `status NOT IN ('COMPLETED','FAILED','CANCELLED')`, so a late straggler
from the worker cannot overwrite a finished run's metrics.

```sql
-- Saved screens. The condition tree is opaque to this layer; `atr.screener` owns
-- its shape and validates it on the way in.
CREATE TABLE screener_scans (
    scan_id     VARCHAR(32) PRIMARY KEY,
    user_id     VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        VARCHAR(64) NOT NULL,
    description TEXT,
    definition  TEXT NOT NULL,   -- JSON: nested AND/OR condition tree + universe
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, name)
);
```

---

## 5. Entity relationships

```
users ──1:N── sessions
      ──1:N── api_keys
      ──1:N── watchlists ──1:N── watchlist_items
      ──1:N── watchlist_columns ──N:1── watchlists
      ──1:N── user_preferences
      ──1:N── orders ──1:N── order_events
      │           └──1:1── order_intents
      ──1:N── deployments ──1:N── orders
      ──1:N── reconciliation_runs
      ──1:N── trade_journal
      ──1:N── strategies ──1:N── strategy_versions
      ──1:N── backtest_runs ──1:N── backtest_trades
      │                     ──1:N── backtest_curves
      │                     └──1:N── backtest_monthly
      └──1:N── screener_scans

audit_events ──(soft ref, user_id may be NULL for system events)── users
```

Watchlists and items are scoped by `user_id` at the query, not just at the
foreign key. Every repository read takes a `user_id` argument and filters on it;
there is no `get_by_id()` that returns someone else's row. That is the difference
between a check that can be forgotten and one that cannot be written.

The same rule extends to the trading tier, where forgetting it would expose
another account's positions rather than its watchlist: `OrderRepository.get`,
`DeploymentRepository.get`, `BacktestRunRepository.get`, `ScreenerRepository.get`,
`TradeJournalRepository.get` and every `list_for_user` all take a `user_id`. A
missing row and another account's row are deliberately indistinguishable — both
return `None`, so the API answers 404 rather than 403 and does not confirm that an
id exists.

One deliberate soft reference: `orders.strategy_id` / `orders.strategy_version` are
**not** foreign keys into `strategy_versions`. An order records what was running at
the time it was placed, and that record must not become unreadable because a
strategy row was later archived. The version's immutability is what makes the pair
meaningful; the FK would add a cascade that could erase trade history.

---

## 6. System tier

**Shipped 2026-09-14.** One small key/value table, for state that belongs to *the
installation* rather than to a user.

```sql
-- Key/value state for the installation. Holds the kill switch and the execution
-- mode, both of which used to be an in-process dict in atr/api/main.py.
CREATE TABLE system_state (
    key        VARCHAR(64) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by VARCHAR(64),
    reason     TEXT
);
CREATE INDEX ix_system_state_updated ON system_state(updated_at);
```

Why this is not `user_preferences`: that table's key is ``(user_id, key)``, and the
kill switch and execution mode have no user. Making them per-user would mean each
account could flip the platform to live independently, which is the opposite of
what a kill switch is for.

Two failures are fixed by making this durable rather than in-memory:

* **A restart used to re-arm trading.** The kill switch and the paper/live mode
  lived in a module-level dict, so bouncing the process silently cleared a decision
  an operator had made *because something was wrong*.
* **There was one reader per route.** Each endpoint consulted the dict itself, so
  "is the kill switch engaged?" had as many answers as there were routes — and the
  manual order route did not ask at all. It is now read through
  ``atr.services.risk.RiskStateService``, and the OMS's risk gate is built from it,
  so the switch applies to every order path.

Keys in use: ``risk.kill_switch``, ``risk.execution_mode``,
``risk.execution_mode.meta`` (who/when/why), ``risk.limits``.

---

## 8. Portfolio tier

**Shipped 2026-09-18.** One row per user, holding the capital-allocation
policy the portfolio risk gate enforces: portfolio limits, conflict handling
and strategy priorities.

```sql
CREATE TABLE portfolio_policies (
    user_id    VARCHAR(32) PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    policy     TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by VARCHAR(64),
    reason     TEXT
);
```

Why this is not `system_state`: that table belongs to the installation (kill
switch, execution mode), while capital allocation belongs to whoever owns the
paper book. Per-user rows keep one account's limits from constraining another.
No policy row means no portfolio limits — the gate passes everything through,
which is the pre-policy behaviour, and every figure the dashboard shows is
still computed.

---

## 5. Learning & Optimization tier (Shipped)

### 5.1 `learning_observations`
Persisted learning records representing in-sample, forward paper, and live trading observations.

```sql
CREATE TABLE learning_observations (
    observation_id   VARCHAR(36) PRIMARY KEY,
    trade_id         VARCHAR(36),
    strategy_id      VARCHAR(36) NOT NULL,
    strategy_version INTEGER,
    evidence_class   VARCHAR(32) NOT NULL,
    evidence_grade   VARCHAR(32) NOT NULL,
    is_forward       BOOLEAN NOT NULL,
    entry_ts         TIMESTAMP NOT NULL,
    exit_ts          TIMESTAMP,
    pnl              REAL,
    return_pct       REAL,
    features         TEXT,
    created_at       TIMESTAMP NOT NULL
);
```

### 5.2 `optimization_recommendations`
Controlled strategy optimization recommendations generated from empirical forward evidence.

```sql
CREATE TABLE optimization_recommendations (
    recommendation_id VARCHAR(36) PRIMARY KEY,
    hypothesis_id     VARCHAR(36) NOT NULL,
    strategy_id       VARCHAR(36) NOT NULL,
    from_version      INTEGER NOT NULL,
    to_version        INTEGER,
    status            VARCHAR(32) NOT NULL,
    candidate_changes TEXT NOT NULL,
    validation_report TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    approved_at       TIMESTAMP,
    approved_by       VARCHAR(36)
);
```

### 5.3 `strategy_experiments`

A controlled experiment measures a candidate strategy version against the
version it came from. The experiment **records and reports**; it never promotes
the candidate — approval is a separate, explicit act.

```sql
CREATE TABLE strategy_experiments (
    experiment_id         VARCHAR(36) PRIMARY KEY,
    strategy_id           VARCHAR(32) NOT NULL REFERENCES strategies(strategy_id) ON DELETE CASCADE,
    source_version        INTEGER NOT NULL,
    target_version        INTEGER,
    recommendation_id     VARCHAR(36) REFERENCES optimization_recommendations(recommendation_id) ON DELETE SET NULL,
    creator_user_id       VARCHAR(64) NOT NULL,
    name                  VARCHAR(128) NOT NULL,
    reason                TEXT NOT NULL,
    parameter_changes     TEXT NOT NULL,  -- JSON
    baseline_definition   TEXT NOT NULL,  -- JSON
    candidate_definition  TEXT NOT NULL,  -- JSON
    status                VARCHAR(20) NOT NULL DEFAULT 'CREATED',
    rejection_reason      TEXT,
    results               TEXT,           -- JSON: metrics comparison, curves, distributions
    explanation           TEXT,           -- JSON: what changed, why, what was found
    error                 TEXT,
    created_at            DATETIME NOT NULL,
    updated_at            DATETIME NOT NULL,
    reviewed_by           VARCHAR(64),
    reviewed_at           DATETIME
);
```

---

## 7. Signal context tier (Shipped)

**Shipped 2026-09-17.** One table, `signal_contexts`, records the market, sector
and stock context of every signal the platform raises, together with the
deterministic score the Context-Aware Signal Engine computed for it.

```sql
CREATE TABLE signal_contexts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    signal_id              VARCHAR(64) NOT NULL,
    strategy_id            VARCHAR(32),
    strategy_version       INTEGER,
    symbol                 VARCHAR(64) NOT NULL,
    action                 VARCHAR(8) NOT NULL,       -- BUY | SELL
    signal_source          VARCHAR(16) NOT NULL,      -- LIVE | PAPER | BACKTEST
    signal_ts              VARCHAR(40) NOT NULL,
    context_model_version  VARCHAR(24) NOT NULL,
    context_class          VARCHAR(24) NOT NULL,      -- STRONG | NEUTRAL | WEAK | INSUFFICIENT_DATA
    context_score          INTEGER NOT NULL DEFAULT 0,
    max_possible_score     INTEGER NOT NULL DEFAULT 0,
    has_insufficient_data  BOOLEAN NOT NULL DEFAULT FALSE,
    run_id                 VARCHAR(32),               -- backtest linkage
    trade_id               INTEGER,                   -- backtest_trades.trade_id
    order_id               VARCHAR(32),               -- orders linkage (live/paper)
    market_context         TEXT,                      -- JSON snapshot
    sector_context         TEXT,                      -- JSON snapshot
    stock_context          TEXT,                      -- JSON snapshot
    score_breakdown        TEXT,                      -- JSON, per-criterion
    missing_fields         TEXT,                      -- JSON list
    benchmark_provenance   TEXT,                      -- JSON
    created_at             DATETIME NOT NULL,
    UNIQUE (user_id, signal_id)
);
```

Why this exists and why it is shaped this way:

* **The score is not recomputable from the trades.** Breadth, sector strength and
  the benchmark provenance are not stored on a trade; without this table a
  historical signal's context is simply lost. The JSON snapshots make the record
  self-describing, so a future scoring version can reclassify old rows without a
  migration.
* **`signal_id` is the join.** A paper deployment stamps the bar key
  (``SYMBOL:YYYY-MM-DD``) on both the order (``orders.signal_id``) and the
  context, so an outcome can be resolved back to the context that preceded it.
  A backtest run uses ``RUN_ID:SEQ`` and links through ``trade_id``.
* **`context_model_version` is not decoration.** Scoring weights change; the row
  must say which model produced it or a comparison across time is meaningless.
* **Enrichment never gates trading.** This table is written after an order is
  placed (paper) or after a run completes (backtest), best-effort. A missing row
  is a lost annotation, never a blocked or altered signal.

---

## 8. Portfolio tier — Capital Allocation & Portfolio Risk

### 8.1 `portfolio_policies`

Stores per-user aggregate risk limits and signal conflict resolution policies.

```sql
CREATE TABLE portfolio_policies (
    user_id                  VARCHAR(32) PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    policy_json              TEXT NOT NULL,
    max_total_exposure       DOUBLE PRECISION,
    max_daily_loss           DOUBLE PRECISION,
    max_capital_per_strategy DOUBLE PRECISION,
    max_open_positions       INTEGER,
    max_stock_exposure       DOUBLE PRECISION,
    max_sector_exposure      DOUBLE PRECISION,
    max_correlated_exposure  DOUBLE PRECISION,
    conflict_resolution      VARCHAR(32) NOT NULL DEFAULT 'reject',
    created_at               DATETIME NOT NULL,
    updated_at               DATETIME NOT NULL
);
```

`portfolio_policies` provides persistence for portfolio-level risk parameters, siting above individual strategy limits and evaluated before OMS order dispatch.

---

## 8. Post-trade attribution tier (Shipped)

**Shipped 2026-09-17.** One table, `trade_attributions`, holds the nine-branch
attribution of every closed trade: what the signal was, what the context was, what
the entry actually cost, how the size was chosen, what risk was taken, what the
venue charged, why it ended, and what it made.

```sql
CREATE TABLE trade_attributions (
    trade_id                 VARCHAR(32) PRIMARY KEY,   -- trade_journal.trade_id
    user_id                  VARCHAR(32) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    deployment_id            VARCHAR(32),
    symbol                   VARCHAR(64) NOT NULL,
    side                     VARCHAR(4)  NOT NULL,      -- BUY | SELL (opening side)
    source                   VARCHAR(16) NOT NULL,      -- BACKTEST | PAPER | LIVE
    evidence_grade           VARCHAR(16),               -- forward | in_sample (copied)
    evidence_class           VARCHAR(16),               -- PAPER_FORWARD | ... (copied)
    simulated                BOOLEAN NOT NULL DEFAULT TRUE,

    attribution              TEXT NOT NULL,             -- JSON: the TRADE tree
    reason_codes             TEXT,                      -- comma-joined, for LIKE
    missing_fields           TEXT,                      -- JSON

    -- lifted columns: exactly what the learning axes slice on
    entry_quality            VARCHAR(16),               -- good | poor
    execution_quality        VARCHAR(24),               -- low_slippage | normal | high_slippage
    mfe_pct                  FLOAT,
    mae_pct                  FLOAT,
    mfe_amount               FLOAT,
    mae_amount               FLOAT,
    mfe_over_risk            FLOAT,
    realized_over_risk       FLOAT,
    capture_efficiency_pct   FLOAT,
    entry_slippage_bps       FLOAT,
    exit_slippage_bps        FLOAT,
    total_slippage_bps       FLOAT,
    transaction_costs        FLOAT,
    cost_pct                 FLOAT,
    signal_to_order_sec      FLOAT,
    order_to_fill_sec        FLOAT,
    holding_sec              INTEGER,
    partial_fill             BOOLEAN NOT NULL DEFAULT FALSE,
    fill_ratio               FLOAT,
    sizing_method            VARCHAR(32),
    sizing_cap_reason        VARCHAR(64),
    realized_risk_pct        FLOAT,
    planned_risk_amount      FLOAT,
    context_score            INTEGER,
    context_class            VARCHAR(24),
    market_regime            VARCHAR(24),
    sector                   VARCHAR(64),
    sector_strength          FLOAT,
    stock_relative_strength  FLOAT,
    rvol                     FLOAT,
    atr_pct                  FLOAT,
    exit_reason              VARCHAR(32),

    input_fingerprint        VARCHAR(64) NOT NULL,
    computed_at              DATETIME NOT NULL,
    updated_at               DATETIME NOT NULL
);
```

Why this exists and why it is shaped this way:

* **`trade_id` is the primary key, and that is the whole idempotency story.** A
  second attribution of a closed trade cannot create a second row, whatever the
  caller does. Re-attribution is a replacement, and an unchanged trade never
  reaches the write at all — `input_fingerprint` is compared first, so a scheduled
  sweep over a stable book writes nothing.
* **It is derived, so it is a separate table.** A journal row is true the instant
  the fold writes it; an attribution row is computed from the journal, the
  order-event log and the price cache, so it can be *absent* (not yet computed) or
  *superseded* (a late fill arrived). On the journal, "this trade has no net P&L"
  and "this trade has not been attributed" would be the same sentence.
* **The tree is stored whole; the sliceable fields are lifted.** The nine branches
  are the deliverable and a reader wants them intact and together, so they live in
  one JSON column. The ~40 fields below it are copied out because an axis has to
  be able to `GROUP BY` them, and a JSON path cannot be indexed portably across
  SQLite and Postgres.
* **`evidence_grade` is copied, never derived.** The service reads it from
  `trade_journal` and writes it here for query convenience. There is no code path
  that computes a grade on this table, and `TradeAttributionRepository` exposes no
  method that could: the one place a grade may be decided is the journal's own
  writer. A row whose grade disagreed with its journal row would be a second
  opinion about provenance.
* **`simulated` travels with the row.** A backtest fill and a paper fill are both
  produced by a model; a live fill was reported by a broker. The flag exists so a
  comparison between simulated and real execution can filter on it without
  consulting a second table — and so a viewer cannot present a simulated fill as
  real execution.

---

## 9. Migrations

`AppDatabase.prepare()` calls `create_all()`, which creates missing tables but
**never alters an existing one**. That is adequate while the schema is only ever
extended, and it is not adequate for a deployed instance once a column changes.

The trading tier is the point at which that stops being hypothetical: `orders`,
`order_events` and `strategy_versions` will hold records that must survive a schema
change. Introducing a real migration tool is tracked in
`docs/NEXT_STAGE_GAP_REPORT.md`; until it lands, any change to an existing table
must be accompanied by an explicit note here and a manual `ALTER TABLE`, not by
hoping `create_all` notices.
