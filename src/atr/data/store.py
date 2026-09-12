"""Postgres + TimescaleDB persistence layer.

Why TimescaleDB: minute bars for a few hundred symbols accumulate fast (a
single symbol-year of 1m data is ~98k rows). Hypertables give cheap time-range
scans, automatic partitioning, and continuous aggregates for multi-timeframe
resampling directly in SQL instead of in Python.

The module is import-safe: no connection is created until you actually use it,
so the backtester and unit tests run fine with no database present.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from atr.config.settings import Settings, get_settings
from atr.core.enums import AssetClass, OptionType
from atr.core.models import Fill, Instrument, Order

metadata = MetaData()

instruments = Table(
    "instruments",
    metadata,
    Column("symbol", String(64), primary_key=True),
    Column("asset_class", String(16), nullable=False),
    Column("currency", String(8), nullable=False, default="USD"),
    Column("exchange", String(16), nullable=False, default="SMART"),
    Column("primary_exchange", String(16)),
    Column("multiplier", Double, nullable=False, default=1.0),
    Column("tick_size", Double, nullable=False, default=0.01),
    Column("min_quantity", Double, nullable=False, default=1.0),
    Column("quantity_step", Double, nullable=False, default=1.0),
    Column("expiry", DateTime),
    Column("strike", Double),
    Column("option_type", String(4)),
    Column("underlying", String(32)),
    Column("conid", Integer),
    Column("local_symbol", String(64)),
    Column("trading_class", String(32)),
    Column("initial_margin_per_unit", Double, default=0.0),
    Column("maintenance_margin_per_unit", Double, default=0.0),
)

bars = Table(
    "bars",
    metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("open", Double, nullable=False),
    Column("high", Double, nullable=False),
    Column("low", Double, nullable=False),
    Column("close", Double, nullable=False),
    Column("volume", Double, default=0.0),
    Column("vwap", Double),
    Column("open_interest", Double),
    # The partition column (`ts`) must be part of the unique index for
    # TimescaleDB to accept the hypertable.
    PrimaryKeyConstraint("ts", "symbol", "timeframe", name="bars_pk"),
)

ticks = Table(
    "ticks",
    metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("last", Double, nullable=False),
    Column("bid", Double),
    Column("ask", Double),
    Column("bid_size", Double),
    Column("ask_size", Double),
    Column("volume", Double),
    PrimaryKeyConstraint("ts", "symbol", name="ticks_pk"),
)

orders_table = Table(
    "orders",
    metadata,
    Column("order_id", String(32), primary_key=True),
    Column("symbol", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("quantity", Double, nullable=False),
    Column("order_type", String(16), nullable=False),
    Column("limit_price", Double),
    Column("stop_price", Double),
    Column("tif", String(8)),
    Column("status", String(24), nullable=False),
    Column("filled_quantity", Double, default=0.0),
    Column("avg_fill_price", Double, default=0.0),
    Column("broker_order_id", Integer),
    Column("tag", String(64)),
    Column("created_at", DateTime(timezone=True)),
    Column("reject_reason", String(255)),
)

fills_table = Table(
    "fills",
    metadata,
    Column("fill_id", String(32), primary_key=True),
    Column("order_id", String(32), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("quantity", Double, nullable=False),
    Column("price", Double, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("commission", Double, default=0.0),
    Column("slippage", Double, default=0.0),
    Column("realized_pnl", Double, default=0.0),
)


class Database:
    """Thin wrapper around SQLAlchemy engine + session factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine | None = None
        self._session_factory: sessionmaker | None = None

    # ------------------------------------------------------------------
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.settings.db_url,
                pool_size=self.settings.db_pool_size,
                max_overflow=10,
                pool_pre_ping=True,
                echo=self.settings.db_echo,
                future=True,
                # DB is optional — fail fast so /health never blocks.
                connect_args={"connect_timeout": 2},
            )
        return self._engine

    def session(self) -> Session:
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.engine, expire_on_commit=False, future=True
            )
        return self._session_factory()

    # ------------------------------------------------------------------
    def init_schema(self, enable_timescale: bool = True) -> None:
        """Create tables and, if the extension exists, hypertables.

        Safe to call repeatedly.
        """
        with self.engine.begin() as conn:
            if enable_timescale:
                try:
                    conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
                except Exception:  # pragma: no cover - depends on server
                    conn.rollback()
            metadata.create_all(conn)
            if enable_timescale:
                for table, dim in (("bars", "ts"), ("ticks", "ts")):
                    try:
                        conn.execute(
                            text(
                                "SELECT create_hypertable(:t, :d, if_not_exists => TRUE, "
                                "migrate_data => TRUE)"
                            ),
                            {"t": table, "d": dim},
                        )
                    except Exception:  # pragma: no cover
                        conn.rollback()
                try:
                    conn.execute(
                        text("ALTER TABLE bars SET (timescaledb.compress, "
                             "timescaledb.compress_segmentby = 'symbol')")
                    )
                except Exception:  # pragma: no cover
                    conn.rollback()

    def health(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------
# Repositories
# --------------------------------------------------------------------------


class InstrumentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, inst: Instrument) -> None:
        row = {
            "symbol": inst.symbol,
            "asset_class": inst.asset_class.value,
            "currency": inst.currency,
            "exchange": inst.exchange,
            "primary_exchange": inst.primary_exchange,
            "multiplier": inst.multiplier,
            "tick_size": inst.tick_size,
            "min_quantity": inst.min_quantity,
            "quantity_step": inst.quantity_step,
            "expiry": inst.expiry,
            "strike": inst.strike,
            "option_type": inst.option_type.value if inst.option_type else None,
            "underlying": inst.underlying,
            "conid": inst.conid,
            "local_symbol": inst.local_symbol,
            "trading_class": inst.trading_class,
            "initial_margin_per_unit": inst.initial_margin_per_unit,
            "maintenance_margin_per_unit": inst.maintenance_margin_per_unit,
        }
        stmt = pg_insert(instruments).values(row)
        stmt = stmt.on_conflict_do_update(index_elements=["symbol"], set_=row)
        with self.db.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_many(self, items: list[Instrument]) -> None:
        for item in items:
            self.upsert(item)

    def get(self, symbol: str) -> Instrument | None:
        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(instruments).where(instruments.c.symbol == symbol.upper())
            ).mappings().first()
        return _row_to_instrument(row) if row else None

    def all(self) -> list[Instrument]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(instruments)).mappings().all()
        return [_row_to_instrument(r) for r in rows]


def _row_to_instrument(row) -> Instrument:
    return Instrument(
        symbol=row["symbol"],
        asset_class=AssetClass(row["asset_class"]),
        currency=row["currency"],
        exchange=row["exchange"],
        primary_exchange=row["primary_exchange"],
        multiplier=row["multiplier"],
        tick_size=row["tick_size"],
        min_quantity=row["min_quantity"],
        quantity_step=row["quantity_step"],
        expiry=row["expiry"].date() if row["expiry"] else None,
        strike=row["strike"],
        option_type=OptionType(row["option_type"]) if row["option_type"] else None,
        underlying=row["underlying"],
        conid=row["conid"],
        local_symbol=row["local_symbol"],
        trading_class=row["trading_class"],
        initial_margin_per_unit=row["initial_margin_per_unit"] or 0.0,
        maintenance_margin_per_unit=row["maintenance_margin_per_unit"] or 0.0,
    )


class BarRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pd.DataFrame, timeframe: str) -> int:
        """Bulk upsert a long-format DataFrame of bars.

        Returns the number of rows written.
        """
        if df.empty:
            return 0
        df = df.copy()
        df["timeframe"] = timeframe
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        cols = ["ts", "symbol", "timeframe", "open", "high", "low", "close", "volume",
                "vwap", "open_interest"]
        for col in ("volume", "vwap", "open_interest"):
            if col not in df.columns:
                df[col] = None
        records = df[cols].where(pd.notnull(df[cols]), None).to_dict("records")

        stmt = pg_insert(bars).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts", "symbol", "timeframe"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "vwap": stmt.excluded.vwap,
                "open_interest": stmt.excluded.open_interest,
            },
        )
        with self.db.engine.begin() as conn:
            conn.execute(stmt)
        return len(records)

    def get(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        stmt = (
            select(bars)
            .where(
                bars.c.symbol == symbol.upper(),
                bars.c.timeframe == timeframe,
                bars.c.ts >= start,
                bars.c.ts <= end,
            )
            .order_by(bars.c.ts)
        )
        with self.db.engine.connect() as conn:
            return pd.DataFrame(conn.execute(stmt).mappings().all())

    def count(self) -> int:
        with self.db.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(bars)).scalar() or 0

    def delete_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> int:
        stmt = delete(bars).where(
            bars.c.symbol == symbol.upper(),
            bars.c.timeframe == timeframe,
            bars.c.ts >= start,
            bars.c.ts <= end,
        )
        with self.db.engine.begin() as conn:
            return conn.execute(stmt).rowcount or 0


class TradeRepository:
    """Persists orders and fills for reconciliation and post-trade analytics."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_order(self, order: Order) -> None:
        row = {
            "order_id": order.order_id,
            "symbol": order.instrument.symbol,
            "side": order.side.name,
            "quantity": order.quantity,
            "order_type": order.order_type.value,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
            "tif": order.tif.value,
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "avg_fill_price": order.avg_fill_price,
            "broker_order_id": order.broker_order_id,
            "tag": order.tag,
            "created_at": order.created_at,
            "reject_reason": order.reject_reason,
        }
        stmt = pg_insert(orders_table).values(row).on_conflict_do_update(
            index_elements=["order_id"], set_=row
        )
        with self.db.engine.begin() as conn:
            conn.execute(stmt)

    def save_fill(self, fill: Fill, realized_pnl: float = 0.0) -> None:
        row = {
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "symbol": fill.instrument.symbol,
            "side": fill.side.name,
            "quantity": fill.quantity,
            "price": fill.price,
            "ts": fill.ts,
            "commission": fill.commission,
            "slippage": fill.slippage,
            "realized_pnl": realized_pnl,
        }
        stmt = pg_insert(fills_table).values(row).on_conflict_do_update(
            index_elements=["fill_id"], set_=row
        )
        with self.db.engine.begin() as conn:
            conn.execute(stmt)
