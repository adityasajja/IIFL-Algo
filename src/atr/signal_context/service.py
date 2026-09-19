"""Service for the Context-Aware Signal Engine: persistence + enrichment + analytics.

This is the only layer that talks to the store. It owns three jobs:

1. **Record** a computed ``SignalContext`` for a signal (live/paper or backtest),
   versioned by the scoring model that produced it.
2. **Enrich** signals with context — live signals at fire time from the Market
   Intelligence layer, backtest trades after a run completes, point-in-time and
   look-ahead-safe.
3. **Analyse** recorded contexts against resolved trade outcomes, separating
   forward evidence (live/paper, genuinely recorded against the book) from
   in-sample evidence (backtest) and suppressing statistics the sample is too
   small to support.

Nothing here ever blocks a signal, changes strategy parameters, or alters an
order. Enrichment failure is a logged, best-effort miss — never an error that
propagates into the caller's happy path.

Layout note: the engine is pure; the *market scan* (breadth, sector metrics at a
past date) is the expensive environment-dependent piece, and it lives here so
the engine -- and its tests -- never need a data root.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from atr.appdb.engine import utcnow
from atr.appdb.repositories import (
    BacktestArtefactRepository,
    BacktestRunRepository,
    SignalContextRepository,
    TradeJournalRepository,
)
from atr.instruments.service import canonical_symbol
from atr.market_intel.service import MarketIntelService
from atr.research import learning_stats as learning_stats
from atr.research.learning_context import (
    DEFAULT_CONTEXT_AXES,
    ContextEffectiveness,
    evidence_class_for,
)
from atr.research.learning_enrich import _as_of, build_sector_table
from atr.research.learning_evidence import (
    CLASS_BACKTEST,
    CLASS_IN_SAMPLE,
    CLASS_PAPER_FORWARD,
    forward_class,
)
from atr.research.learning_readiness import SIGNAL_MATCH_WINDOW_SEC
from atr.signal_context.analytics import (
    MIN_FORWARD_N,
    ContextRow,
    Outcome,
    build_buckets,
)
from atr.signal_context.engine import SignalContextEngine
from atr.strategy.indicators import ema as calc_ema

logger = logging.getLogger("atr.signal_context")

#: Scan cap, aligned with the Market Intelligence layer's own 500-symbol cap.
UNIVERSE_SCAN_CAP = 500
#: A sector needs at least this many counted members to be reported.
MIN_SECTOR_MEMBERS = 1


def _data_root() -> Path:
    """The configured data root (``ATR_DATA_ROOT`` or local ``data/``)."""
    return Path(os.environ.get("ATR_DATA_ROOT") or "data")


class SignalContextService:
    """Persistence, enrichment and analytics for signal contexts."""

    def __init__(
        self,
        db: Any = None,
        engine: SignalContextEngine | None = None,
        market_intel: MarketIntelService | None = None,
        data_root: Path | None = None,
    ) -> None:
        from atr.appdb.engine import get_app_db

        self.db = db or get_app_db()
        root = data_root or _data_root()
        self.data_root = root
        self.engine = engine or SignalContextEngine()
        if market_intel is not None:
            self.market_intel = market_intel
        else:
            self.market_intel = MarketIntelService(data_root=root)
        self._scan_cache: dict[str, dict[str, Any]] = {}
        self._scan_lock = threading.Lock()
        self._frames_cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------ model
    def model(self) -> dict[str, Any]:
        """The active scoring model, for UI + tests."""
        return self.engine.config.as_dict()

    # ------------------------------------------------------------------ saves
    def record(
        self,
        session_or_context: Any,
        context_or_user_id: Any = None,
        user_id: str | None = None,
        *,
        session: Any | None = None,
        run_id: str | None = None,
        trade_id: int | None = None,
    ) -> int:
        """Persist one context row. Returns row count (0 on a conflict).

        Supports both call styles:
          - record(session, context, user_id=...)
          - record(context, user_id, session=...)
        """
        if hasattr(session_or_context, "execute") or hasattr(session_or_context, "add"):
            # Called as record(session, context, user_id=...)
            sess = session_or_context
            ctx = context_or_user_id
            uid = user_id
        else:
            # Called as record(context, user_id, ...)
            ctx = session_or_context
            uid = context_or_user_id if isinstance(context_or_user_id, str) else user_id
            sess = session

        if uid is None:
            raise ValueError("user_id must be provided to record()")

        row = self._row_for(ctx, uid, run_id=run_id, trade_id=trade_id)
        if sess is not None:
            return SignalContextRepository.save(sess, row)
        with self.db.session() as s:
            return SignalContextRepository.save(s, row)

    def record_many_for_run(
        self,
        run_id: str,
        user_id: str,
        contexts: list[Any],
    ) -> int:
        """Replace a run's context rows wholesale (idempotent re-completion)."""
        rows = [
            self._row_for(ctx, user_id, run_id=run_id, trade_id=None)
            for ctx in contexts
        ]
        with self.db.session() as session:
            return SignalContextRepository.replace_for_run(session, run_id, rows)

    def _row_for(
        self,
        context: Any,
        user_id: str,
        *,
        run_id: str | None,
        trade_id: int | None,
    ) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "signal_id": context.signal_id,
            "strategy_id": context.strategy_id,
            "strategy_version": context.strategy_version,
            "symbol": context.symbol,
            "action": context.action,
            "signal_source": context.signal_source,
            "signal_ts": context.signal_ts,
            "context_model_version": context.context_model_version,
            "context_class": context.context_class.value,
            "context_score": context.context_score,
            "max_possible_score": context.max_possible_score,
            "has_insufficient_data": context.has_insufficient_data,
            "run_id": run_id,
            "trade_id": trade_id,
            "order_id": None,
            "market_context": json.dumps(asdict(context.market_context), default=str),
            "sector_context": (
                json.dumps(asdict(context.sector_context), default=str)
                if context.sector_context is not None
                else None
            ),
            "stock_context": json.dumps(asdict(context.stock_context), default=str),
            "score_breakdown": json.dumps(
                [asdict(c) for c in context.score_breakdown], default=str
            ),
            "missing_fields": json.dumps(context.missing_fields, default=str),
            "benchmark_provenance": json.dumps(
                context.benchmark_provenance, default=str
            ),
            "created_at": utcnow(),
        }

    # ------------------------------------------------------------------ reads
    def list_signals(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        strategy_id: str | None = None,
        klass: str | None = None,
    ) -> dict[str, Any]:
        with self.db.session() as session:
            items = SignalContextRepository.list(
                session,
                user_id,
                limit=limit,
                offset=offset,
                source=source,
                strategy_id=strategy_id,
                klass=klass,
            )
            total = SignalContextRepository.count(
                session,
                user_id,
                source=source,
                strategy_id=strategy_id,
                klass=klass,
            )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "model_version": self.engine.config.version,
        }

    def get_signal(self, user_id: str, signal_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            return SignalContextRepository.get(session, user_id, signal_id)

    # ---------------------------------------------------------------- analytics
    def analytics(
        self,
        user_id: str,
        *,
        dimension: str = "context_class",
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        source: str | None = None,
        session_factory: Any = None,
    ) -> dict[str, Any]:
        """Bucket analytics over recorded contexts and their resolved outcomes.

        ``dimension`` selects the bucketing (``context_class``,
        ``score_band``, ``regime``, ``sector_rs``, ``stock_rs``, ``breadth``,
        ``volatility``).
        Statistics are suppressed until the evidence supports them.
        """
        session_cm = (
            session_factory() if session_factory is not None else self.db.session()
        )
        with session_cm as session:
            rows = SignalContextRepository.analytics_rows(
                session, user_id, strategy_id=strategy_id, source=source
            )
            if strategy_version is not None:
                rows, outcomes = self._scope_to_arm(
                    session, user_id, rows, strategy_version
                )
            else:
                outcomes = self._resolve_outcomes(session, user_id, rows)

        pairs: list[tuple[ContextRow, Outcome | None]] = []
        for row in rows:
            pair = (self._to_context_row(row), outcomes.get(row["signal_id"]))
            pairs.append(pair)

        buckets = build_buckets(dimension, pairs)
        return {
            "dimension": dimension,
            "model_version": self.engine.config.version,
            "min_forward_n": MIN_FORWARD_N,
            "generated_at": utcnow().isoformat(),
            "buckets": buckets,
        }

    # ------------------------------------------------------------ effectiveness
    def effectiveness(
        self,
        user_id: str,
        *,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        source: str | None = None,
        metric: str = "return_pct",
        min_sample: int = learning_stats.MIN_SAMPLE,
        axes: tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        """Does a higher context score accompany a different outcome?

        A measurement only. The score, the weights, a strategy, a risk limit and
        an order are all untouched; findings are made from forward (paper/live)
        outcomes, in-sample (backtest) outcomes are reported separately, and a
        bucket below ``min_sample`` is suppressed rather than promoted.

        ``strategy_version`` scopes to one arm's traded signals (that
        version's orders and episodes) — not to the contexts that version
        recorded, which are shared across arms firing on the same signal.
        """
        rows = self._context_learning_rows(
            user_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            source=source,
        )
        analysis = ContextEffectiveness(
            metric=metric,
            min_sample=min_sample,
            axes=axes if axes is not None else DEFAULT_CONTEXT_AXES,
        )
        result = analysis.analyse(rows)
        result["model_version"] = self.engine.config.version
        return result

    def record_effectiveness_observations(
        self,
        user_id: str,
        *,
        strategy_id: str | None = None,
        min_sample: int = learning_stats.MIN_SAMPLE,
    ) -> list[dict[str, Any]]:
        """Persist the context findings the sample supports, and nothing weaker.

        Only forward buckets that clear ``min_sample`` and are not suppressed
        are written. No bucket writes a proposed value and no writer here can
        change a strategy; this is the same advisory ceiling the rest of the
        learning tier holds.
        """
        from atr.appdb.repositories import LearningObservationRepository

        rows = self._context_learning_rows(user_id, strategy_id=strategy_id)
        analysis = ContextEffectiveness(min_sample=min_sample).analyse(rows)
        axis_by_name = {axis.name: axis for axis in DEFAULT_CONTEXT_AXES}

        supported = [
            bucket
            for axis in analysis["axes"]
            for bucket in axis["buckets"]
            if not bucket.get("suppressed")
            and bucket.get("n", 0) >= min_sample
            and bucket.get("lift") is not None
        ]
        if not supported:
            return []

        today = utcnow().strftime("%Y-%m-%d")
        created: list[dict[str, Any]] = []
        with self.db.session() as session:
            for axis in analysis["axes"]:
                axis_obj = axis_by_name.get(axis["axis"])
                if axis_obj is None:
                    continue
                for bucket in axis["buckets"]:
                    if (
                        bucket.get("suppressed")
                        or bucket.get("n", 0) < min_sample
                        or bucket.get("lift") is None
                    ):
                        continue
                    refs = [
                        row.get("trade_ref")
                        for row in rows
                        if axis_obj.value(row) == bucket["label"]
                        and row.get(analysis["metric"]) is not None
                        and row.get("trade_ref")
                    ][:100]
                    p_value = bucket.get("p_adjusted")
                    significance = bucket.get("significance")
                    confidence = (
                        (1.0 - p_value)
                        if p_value is not None
                        else (0.75 if significance in ("strong", "moderate") else 0.5)
                    )
                    axis_name = str(axis["axis"])
                    condition_bucket = (
                        f"{axis_name}:{bucket['label']}"
                        if axis_name.startswith("context_")
                        else f"context_{axis_name}:{bucket['label']}"
                    )
                    created.append(
                        LearningObservationRepository.record(
                            session,
                            strategy_id=strategy_id or "ALL",
                            strategy_version=None,
                            date=today,
                            metric=analysis["metric"],
                            condition_bucket=condition_bucket,
                            sample_size=bucket["n"],
                            statistical_result=bucket,
                            evidence_class=(
                                bucket.get("evidence_class_forward")
                                or CLASS_PAPER_FORWARD
                            ),
                            confidence=confidence,
                            source_trades=refs,
                        )
                    )
            session.commit()
        return created

    def _scope_to_arm(
        self,
        session: Any,
        user_id: str,
        rows: list[dict[str, Any]],
        strategy_version: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Outcome]]:
        """Keep only the rows this arm actually traded.

        A context is shared reality — two arms can fire on the same signal —
        so scoping by the context's recorded version would drop the arm that
        recorded second. Instead the arm is defined by its orders: a live/paper
        row belongs to the arm when that version ordered its signal, and a
        backtest row belongs to it when the recording names the version
        exactly. Outcomes resolve through the same arm-scoped book.
        """
        outcomes = self._resolve_outcomes(
            session, user_id, rows, strategy_version=strategy_version
        )
        arm_orders = self._orders_for_contexts(
            session, user_id, rows, strategy_version=strategy_version
        )
        scoped = []
        for row in rows:
            if row.get("signal_source") == "BACKTEST":
                try:
                    if int(row.get("strategy_version")) != int(strategy_version):
                        continue
                except (TypeError, ValueError):
                    continue
                scoped.append(row)
            elif row.get("signal_id") in arm_orders:
                scoped.append(row)
        return scoped, outcomes

    def _context_learning_rows(
        self,
        user_id: str,
        *,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Stored contexts joined to their resolved outcomes, shaped for analysis."""
        with self.db.session() as session:
            rows = SignalContextRepository.analytics_rows(
                session, user_id, strategy_id=strategy_id, source=source
            )
            if strategy_version is not None:
                rows, outcomes = self._scope_to_arm(
                    session, user_id, rows, strategy_version
                )
            else:
                outcomes = self._resolve_outcomes(session, user_id, rows)
        return [
            self._learning_row(row, outcomes.get(row["signal_id"])) for row in rows
        ]

    def _learning_row(
        self, row: dict[str, Any], outcome: Outcome | None
    ) -> dict[str, Any]:
        """Flatten a context + outcome onto the columns the learning axes read."""
        market = row.get("market_context") or {}
        sector = row.get("sector_context") or {}
        stock = row.get("stock_context") or {}
        source = str(row.get("signal_source") or "").upper()

        ratio = market.get("volatility_ratio")
        try:
            ratio = float(ratio) if ratio is not None else None
        except (TypeError, ValueError):
            ratio = None
        volatility_regime = None
        if ratio is not None:
            volatility_regime = (
                "elevated"
                if ratio >= self.engine.config.volatility_elevated_ratio
                else "normal"
            )

        if outcome is not None:
            evidence_class = evidence_class_for(source, outcome.evidence_kind)
        elif source in ("LIVE", "PAPER"):
            evidence_class = forward_class(source)
        else:
            evidence_class = CLASS_BACKTEST if source == "BACKTEST" else CLASS_IN_SAMPLE

        return {
            "signal_id": row.get("signal_id"),
            "strategy_id": row.get("strategy_id"),
            "strategy_version": row.get("strategy_version"),
            "symbol": row.get("symbol"),
            "signal_source": source,
            "signal_ts": row.get("signal_ts"),
            "context_score": row.get("context_score"),
            "context_class": row.get("context_class"),
            "has_insufficient_data": row.get("has_insufficient_data"),
            "market_regime": market.get("regime"),
            "market_breadth": market.get("breadth_above_ema50_pct"),
            "volatility_regime": volatility_regime,
            "sector_relative_strength": sector.get("relative_strength_1m"),
            "stock_relative_strength": stock.get("relative_strength_nifty_20d"),
            "relative_volume": stock.get("relative_volume"),
            "atr_pct": stock.get("atr_pct"),
            "return_pct": outcome.return_pct if outcome is not None else None,
            "net_pnl": outcome.net_pnl if outcome is not None else None,
            "evidence_class": evidence_class,
            "trade_ref": row.get("trade_id") or row.get("signal_id"),
        }

    def _resolve_outcomes(
        self,
        session: Any,
        user_id: str,
        rows: list[dict[str, Any]],
        *,
        strategy_version: int | None = None,
    ) -> dict[str, Outcome]:
        """Match each context to a resolved outcome (or nothing).

        - Backtest contexts resolve through ``backtest_trades`` (in-sample).
        - Live/paper contexts resolve through the real book: the order that
          carries the same ``signal_id``, then the journal episode whose entry
          falls within that order's session (forward). No episode → no outcome.
        - With ``strategy_version``, only that arm's orders and episodes are
          eligible. Two arms can share one signal (same bar, same conditions);
          the context is shared reality, but each arm's outcome is its own.
        """
        out: dict[str, Outcome] = {}

        backtest_rows = [r for r in rows if r["signal_source"] == "BACKTEST"]
        if backtest_rows:
            trade_ids = [r["trade_id"] for r in backtest_rows if r.get("trade_id")]
            trades = self._trades_by_id(session, user_id, trade_ids)
            for row in backtest_rows:
                trade = trades.get(row.get("trade_id"))
                if trade is None:
                    continue
                net = trade.get("net_pnl", 0.0)
                out[row["signal_id"]] = Outcome(
                    net_pnl=float(net or 0.0),
                    return_pct=trade.get("return_pct"),
                    evidence_kind="in_sample",
                )

        live_rows = [r for r in rows if r["signal_source"] in ("LIVE", "PAPER")]
        if live_rows:
            episodes = self._episodes_for_contexts(session, user_id, live_rows)
            orders = self._orders_for_contexts(
                session, user_id, live_rows, strategy_version=strategy_version
            )
            for row in live_rows:
                episode = self._match_episode(
                    row, episodes, orders, strategy_version=strategy_version
                )
                if episode is None or episode.get("net_pnl") is None:
                    continue
                out[row["signal_id"]] = Outcome(
                    net_pnl=float(episode["net_pnl"] or 0.0),
                    return_pct=self._episode_return_pct(episode),
                    evidence_kind="forward",
                )

        return out

    @staticmethod
    def _episode_return_pct(episode: dict[str, Any]) -> float | None:
        """Percent return of a journal episode, derived from its own prices.

        ``trade_journal`` stores the realised ``net_pnl`` but not a percentage —
        that column belongs to ``backtest_trades`` — so the forward leg computes
        it here from the entry/exit pair the journal does store, signed by the
        episode's side. Without this the forward analysis would carry ``net_pnl``
        but no ``return_pct`` at all, and a percentage-based finding could not be
        made from a live or paper trade.
        """
        entry = episode.get("entry_price")
        exit_price = episode.get("exit_price")
        if entry is None or exit_price is None:
            return None
        try:
            entry = float(entry)
            exit_price = float(exit_price)
        except (TypeError, ValueError):
            return None
        if entry <= 0:
            return None
        change = (exit_price / entry - 1.0) * 100.0
        if str(episode.get("side") or "").upper() in ("SELL", "S"):
            change = -change
        return round(change, 4)

    def _trades_by_id(
        self, session: Any, user_id: str, trade_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]:
        if not trade_ids:
            return {}
        from sqlalchemy import select

        from atr.appdb.repositories import backtest_trades

        stmt = select(backtest_trades).where(backtest_trades.c.trade_id.in_(set(trade_ids)))
        rows = session.execute(stmt).mappings().all()
        return {r["trade_id"]: dict(r) for r in rows}

    def _orders_for_contexts(
        self,
        session: Any,
        user_id: str,
        rows: list[dict[str, Any]],
        *,
        strategy_version: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        signal_ids = {r["signal_id"] for r in rows if r.get("signal_id")}
        if not signal_ids:
            return {}
        from sqlalchemy import select

        from atr.appdb.repositories import orders

        conditions = [
            orders.c.user_id == user_id,
            orders.c.signal_id.in_(set(signal_ids)),
        ]
        if strategy_version is not None:
            # Arm scope: only this version's orders may resolve the outcome.
            # Two arms share a signal; each arm reads its own order.
            conditions.append(orders.c.strategy_version == int(strategy_version))
        stmt = (
            select(orders)
            .where(*conditions)
            .order_by(orders.c.created_at)
        )
        result: dict[str, dict[str, Any]] = {}
        for r in session.execute(stmt).mappings().all():
            # The oldest order per signal is the one the signal itself raised.
            result.setdefault(
                r["signal_id"],
                dict(r),
            )
        return result

    def _episodes_for_contexts(
        self, session: Any, user_id: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        episodes, _total = TradeJournalRepository.list_for_user(
            session,
            user_id,
            closed_only=True,
            limit=1000,
        )
        return episodes

    def _match_episode(
        self,
        row: dict[str, Any],
        episodes: list[dict[str, Any]],
        orders: dict[str, dict[str, Any]],
        *,
        strategy_version: int | None = None,
    ) -> dict[str, Any] | None:
        """An episode from the same user+strategy+symbol whose entry matches the
        order that carried this context's ``signal_id``."""
        order = orders.get(row["signal_id"])
        if order is None:
            return None
        order_ts = order.get("created_at")
        side = "SELL" if order.get("side") in ("SELL", "S") else "BUY"
        for episode in episodes:
            if episode.get("strategy_id") != row.get("strategy_id"):
                continue
            if strategy_version is not None:
                # Arm scope: an episode attributed to another version (or to
                # none) is not this arm's outcome, however close in time.
                try:
                    if int(episode.get("strategy_version")) != int(strategy_version):
                        continue
                except (TypeError, ValueError):
                    continue
            if episode.get("symbol") != row["symbol"]:
                continue
            if episode.get("side") not in (side, None):
                continue
            if order_ts is None or episode.get("entry_ts") is None:
                continue
            delta = abs(
                (episode["entry_ts"] - self._to_dt(order_ts)).total_seconds()
            )
            # Shared with the learning builder, which applies the same rule
            # from the outcome side (see ``_attach_recorded_context``): one
            # rule, two directions, no drift.
            if delta <= SIGNAL_MATCH_WINDOW_SEC:
                return episode
        return None

    def _to_context_row(self, row: dict[str, Any]) -> ContextRow:
        return ContextRow(
            context_class=row.get("context_class") or "INSUFFICIENT_DATA",
            context_score=int(row.get("context_score") or 0),
            has_insufficient_data=bool(row.get("has_insufficient_data")),
            market_context=row.get("market_context") or {},
            sector_context=row.get("sector_context") or {},
            stock_context=row.get("stock_context") or {},
        )

    @staticmethod
    def _to_dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if value is None:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    # ------------------------------------------------------------- live enrich
    def enrich_live(
        self,
        *,
        user_id: str,
        symbol: str,
        action: str,
        signal_ts: str,
        signal_id: str,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        stock_frame: pd.DataFrame | None = None,
        when: Any = None,
        signal_source: str = "PAPER",
    ) -> Any:
        """Build + persist a context for a live/paper signal. Best effort.

        Market breadth and sector measurements come from the Market Intelligence
        layer; the stock's own indicators are computed point-in-time from
        ``stock_frame`` at ``when``. A failure returns ``None`` and never
        propagates to the caller (the paper loop must not depend on this).
        """
        try:
            context = self._build_live_context(
                user_id=user_id,
                symbol=symbol,
                action=action,
                signal_ts=signal_ts,
                signal_id=signal_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                stock_frame=stock_frame,
                when=when,
                signal_source=signal_source,
            )
            if context is None:
                return None
            with self.db.session() as session:
                self.record(
                    session,
                    context,
                    user_id=user_id,
                )
            return context
        except Exception as exc:  # noqa: BLE001 — enrichment must never break the loop
            logger.warning("signal context enrichment failed for %s: %s", signal_id, exc)
            return None

    def _build_live_context(
        self,
        *,
        user_id: str,
        symbol: str,
        action: str,
        signal_ts: str,
        signal_id: str,
        strategy_id: str | None,
        strategy_version: int | None,
        stock_frame: pd.DataFrame | None,
        when: Any,
        signal_source: str,
    ) -> Any:
        clean = canonical_symbol(symbol)
        bench_frame, provenance = self.market_intel.get_benchmark_frame_and_provenance()
        summary = self.market_intel.get_summary()
        sectors = self.market_intel.get_sectors()
        stock_frames = stock_frame if stock_frame is not None else self._load_frame(clean)

        market = self._market_hint_from_summary(summary, provenance, bench_frame)
        sector = self._sector_hint_live(clean, summary, sectors)

        try:
            parsed_when = pd.to_datetime(when) if when is not None else pd.to_datetime(signal_ts)
        except Exception as exc:
            raise ValueError(f"cannot parse when={when!r} signal_ts={signal_ts!r}: {exc}")

        return self.engine.enrich(
            signal_id=signal_id,
            symbol=clean,
            action=action,
            signal_source=signal_source,
            signal_ts=signal_ts,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            stock_frame=stock_frames,
            bench_frame=bench_frame,
            market_hint=market,
            sector_hint=sector,
            when=parsed_when,
        )

    def _market_hint_from_summary(
        self, summary: Any, provenance: Any, bench_frame: pd.DataFrame | None
    ) -> Any:
        """Build a MarketContextSnapshot from the live scan.

        The benchmark trend / ATR / volatility are recomputed from ``bench_frame``
        by the engine (point-in-time), so only the whole-universe measurements —
        breadth, advance/decline and the regime label — are taken from the summary.
        The guards below exist because the live scanner reports ``0.0`` rather than
        ``None`` when it has no data: a measured zero must not be read as a real
        breadth of zero.
        """
        raw = summary.raw_measurements
        benchmark_gone = provenance is None or provenance.symbol == "UNAVAILABLE"
        universe_scanned = int(getattr(summary, "total_stocks_analyzed", 0) or 0) > 0

        breadth = (
            raw.breadth_above_ema50_pct
            if universe_scanned and raw.breadth_above_ema50_pct is not None
            else None
        )
        adv_dec = (
            raw.advance_decline_ratio
            if universe_scanned and raw.advance_decline_ratio is not None
            else None
        )
        regime = summary.regime.regime if not benchmark_gone else None

        return self.engine.build_market_snapshot(
            bench_frame,
            pd.to_datetime(summary.as_of),
            breadth_above_ema50_pct=breadth,
            breadth_above_ema20_pct=(
                raw.breadth_above_ema20_pct if universe_scanned else None
            ),
            advance_decline_ratio=adv_dec,
            regime=regime,
            benchmark_symbol=(
                provenance.symbol if not benchmark_gone else "UNAVAILABLE"
            ),
            regime_model_version=(
                raw.regime_model_version if not benchmark_gone else self.engine.config.version
            ),
            as_of=summary.as_of,
        )

    def _sector_hint_live(
        self, symbol: str, summary: Any, sectors: list[Any]
    ) -> Any | None:
        stock = self.market_intel.get_stock_context(symbol)
        sector_name = stock.sector if stock else None
        if not sector_name:
            return None
        sec = next((s for s in sectors if s.sector == sector_name), None)
        return self.engine.build_sector_snapshot(
            sector_name,
            pd.to_datetime(summary.as_of),
            return_1m_pct=sec.return_1m_pct if sec else None,
            relative_strength_1m=sec.relative_strength_1m if sec else None,
            breadth_above_ema50_pct=sec.above_ema50_pct if sec else None,
            volume_multiple=sec.volume_multiple if sec else None,
            trend=sec.trend if sec else None,
            as_of=summary.as_of,
        )

    # -------------------------------------------------------- backtest enrich
    def enrich_run(self, run_id: str, user_id: str) -> dict[str, Any]:
        """Enrich every trade of a completed run, point-in-time. Best effort.

        Called by the backtest worker after the run's artefacts are written.
        Returns a summary of what was recorded. Never raises: it is a post-run
        annotation, and a failure must not roll back the run's completion.
        """
        try:
            with self.db.session() as session:
                run = BacktestRunRepository.get(session, run_id, user_id)
                if run is None:
                    return {"run_id": run_id, "enriched": 0, "error": "run not found"}
                trades = BacktestArtefactRepository.trades(session, run_id)
            return self._enrich_trades(run_id, user_id, run, trades)
        except Exception as exc:  # noqa: BLE001 — annotation is not a gate
            logger.warning("signal context enrichment for run %s failed: %s", run_id, exc)
            return {"run_id": run_id, "enriched": 0, "error": str(exc)}

    def _enrich_trades(
        self,
        run_id: str,
        user_id: str,
        run: dict[str, Any],
        trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not trades:
            return {"run_id": run_id, "enriched": 0, "model_version": self.engine.config.version}

        bench_frame, _provenance = self.market_intel.get_benchmark_frame_and_provenance()
        market_scans: dict[str, dict[str, Any]] = self._scan_market_dates(
            [t["entry_ts"] for t in trades], bench_frame
        )
        sector_map = build_sector_table(self.data_root)

        contexts: list[tuple[Any, int | None]] = []
        for seq, trade in enumerate(trades):
            symbol = canonical_symbol(trade["symbol"])
            when = trade["entry_ts"]
            date_key = pd.to_datetime(when).date().isoformat()
            scan = market_scans.get(date_key)
            stock_frame = self._load_frame(symbol)
            market = (
                scan["market_snapshot"]
                if scan is not None
                else self.engine.build_market_snapshot(bench_frame, when)
            )
            sector_name = sector_map.get(symbol)
            sector = None
            if scan is not None and sector_name:
                sec = scan["sector_metrics"].get(sector_name)
                if sec:
                    sector = self.engine.build_sector_snapshot(
                        sector_name,
                        when,
                        return_1m_pct=sec.get("return_1m_pct"),
                        relative_strength_1m=sec.get("relative_strength_1m"),
                        breadth_above_ema50_pct=sec.get("above_ema50_pct"),
                        volume_multiple=sec.get("volume_multiple"),
                        trend=sec.get("trend"),
                        as_of=pd.to_datetime(when).isoformat(),
                    )

            context = self.engine.enrich(
                signal_id=f"{run_id}:{seq}",
                symbol=symbol,
                action="BUY" if str(trade.get("direction")).upper() == "LONG" else "SELL",
                signal_source="BACKTEST",
                signal_ts=pd.to_datetime(when).isoformat(),
                strategy_id=trade.get("strategy_id") or run.get("strategy_id"),
                strategy_version=trade.get("strategy_version") or run.get("strategy_version"),
                stock_frame=stock_frame,
                bench_frame=bench_frame,
                market_hint=market,
                sector_hint=sector,
                when=when,
            )
            contexts.append((context, trade.get("trade_id")))

        with self.db.session() as session:
            signal_ids = [c.signal_id for c, _tid in contexts]
            rows = [
                self._row_for(c, user_id, run_id=run_id, trade_id=tid)
                for c, tid in contexts
            ]
            written = SignalContextRepository.replace_for_run(session, run_id, rows)
        logger.info(
            "signal context: enriched {} trades for run {} (model {})",
            written,
            run_id,
            self.engine.config.version,
        )
        return {
            "run_id": run_id,
            "enriched": written,
            "signals": signal_ids,
            "model_version": self.engine.config.version,
        }

    # --------------------------------------------------------- market scanning
    def _scan_market_dates(
        self, entry_ts: Iterable[Any], bench_frame: pd.DataFrame | None
    ) -> dict[str, dict[str, Any]]:
        """Per-date market/sector measurements, computed point-in-time.

        For each distinct entry date the universe is scanned once (frames
        truncated at the trade's own timestamp), yielding breadth, advance/
        decline and per-sector metrics. Results are memoised per date so a run
        with many trades on one date pays the scan once.
        """
        dates = sorted({pd.to_datetime(t).date() for t in entry_ts if pd.notna(pd.to_datetime(t))})
        with self._scan_lock:
            missing = [d for d in dates if self._scan_cache.get(str(d)) is None]
            if not missing:
                return {str(d): self._scan_cache[str(d)] for d in dates}

        frames, sector_names = self._load_universe()
        universe = sorted(frames.keys())[:UNIVERSE_SCAN_CAP]
        if not universe:
            with self._scan_lock:
                for d in missing:
                    self._scan_cache[str(d)] = {
                        "breadth_ema50": None,
                        "advancing": 0,
                        "declining": 0,
                        "market_snapshot": self.engine.build_market_snapshot(bench_frame, pd.Timestamp(d)),
                        "sector_metrics": {},
                    }
            return {str(d): self._scan_cache[str(d)] for d in dates}

        for d in missing:
            when = pd.Timestamp(d)

            # Inline _benchmark_1m
            nifty_1m = None
            if bench_frame is not None and not bench_frame.empty:
                w = _as_of(bench_frame, when)
                if len(w) >= 22:
                    c, p = float(w["close"].iloc[-1]), float(w["close"].iloc[-22])
                    nifty_1m = (c / p - 1.0) * 100.0 if p > 0 else None

            sector_sums: dict[str, list[float]] = {}
            sector_above50: dict[str, list[bool]] = {}
            sector_vol: dict[str, list[float]] = {}

            above50 = 0
            advancing = 0
            declining = 0
            scanned = 0

            for sym, frame in frames.items():
                if sym not in universe:
                    continue
                truncated = _as_of(frame, when)
                if len(truncated) < 50:
                    continue
                closes = truncated["close"].astype(float)
                close = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                chg = (close / prev - 1.0) * 100.0 if prev > 0 else 0.0
                scanned += 1
                if chg > 0.05:
                    advancing += 1
                elif chg < -0.05:
                    declining += 1
                e50 = float(calc_ema(closes, 50).iloc[-1])
                if not pd.isna(e50) and close >= e50:
                    above50 += 1

                sec = sector_names.get(sym)
                if sec:
                    # Inline _return_over
                    if len(truncated) >= 23:
                        rc, rp = float(truncated["close"].iloc[-1]), float(truncated["close"].iloc[-23])
                        ret = (rc / rp - 1.0) * 100.0 if rp > 0 else None
                    else:
                        ret = None
                    if ret is not None:
                        sector_sums.setdefault(sec, []).append(ret)
                    sector_above50.setdefault(sec, []).append(close >= float(calc_ema(closes, 50).iloc[-1]))
                    sector_vol.setdefault(sec, []).append(float(truncated["volume"].iloc[-1]))

            breadth = round(above50 / scanned * 100.0, 1) if scanned else None
            market = self.engine.build_market_snapshot(
                bench_frame,
                when,
                breadth_above_ema50_pct=breadth,
                breadth_above_ema20_pct=None,
                advance_decline_ratio=round(advancing / max(declining, 1), 2),
            )

            sector_metrics: dict[str, dict[str, Any]] = {}
            for sec, returns in sector_sums.items():
                if len(returns) < MIN_SECTOR_MEMBERS:
                    continue
                ret_mean = sum(returns) / len(returns)
                rs = ret_mean - nifty_1m if nifty_1m is not None else None
                members = sector_above50.get(sec, [])
                above_pct = round(sum(1 for x in members if x) / len(members) * 100.0, 1) if members else None
                vols = sector_vol.get(sec, [])
                # Inline _volume_multiple and _sector_trend
                vm = round(sum(vols) / len(vols), 2) if vols else None
                if rs is not None and above_pct is not None:
                    trend = "BULLISH" if rs > 0 and above_pct >= 60 else ("BEARISH" if rs < 0 and above_pct <= 40 else "SIDEWAYS")
                else:
                    trend = "SIDEWAYS"
                sector_metrics[sec] = {
                    "return_1m_pct": round(ret_mean, 2),
                    "relative_strength_1m": round(rs, 2) if rs is not None else None,
                    "above_ema50_pct": above_pct,
                    "volume_multiple": vm,
                    "trend": trend,
                    "stock_count": len(returns),
                }

            with self._scan_lock:
                self._scan_cache[str(d)] = {
                    "breadth_ema50": breadth,
                    "advancing": advancing,
                    "declining": declining,
                    "market_snapshot": market,
                    "sector_metrics": sector_metrics,
                }

        with self._scan_lock:
            return {str(d): self._scan_cache[str(d)] for d in dates}

    def _load_universe(self) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        """All cached frames + sector map under this service's data root."""
        sector_names = build_sector_table(self.data_root)
        cache_dir = self.data_root / "iifl_daily" / "NSEEQ"
        frames: dict[str, pd.DataFrame] = {}
        if cache_dir.is_dir():
            for path in sorted(cache_dir.glob("*.parquet")):
                sym = path.stem.replace("-EQ", "").upper()
                if sym in frames:
                    continue
                try:
                    frames[sym] = pd.read_parquet(path)
                except Exception:  # noqa: BLE001
                    continue
        if not frames:
            for sym in sector_names:
                frame = self._load_frame(sym)
                if frame is not None:
                    frames[sym] = frame
        return frames, sector_names

    def _load_frame(self, symbol: str) -> pd.DataFrame | None:
        clean = canonical_symbol(symbol)
        if clean in self._frames_cache:
            return self._frames_cache[clean]
        cache_dir = self.data_root / "iifl_daily" / "NSEEQ"
        for cand in (f"{clean}-EQ", clean):
            path = cache_dir / f"{cand}.parquet"
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                    self._frames_cache[clean] = df
                    return df
                except Exception:  # noqa: BLE001
                    continue
        return None

_INSTANCE: SignalContextService | None = None
scan_lock = threading.Lock()

def get_signal_context_service() -> SignalContextService:
    global _INSTANCE
    with scan_lock:
        if _INSTANCE is None:
            _INSTANCE = SignalContextService()
        return _INSTANCE


def reset_signal_context_service() -> None:
    global _INSTANCE
    _INSTANCE = None


__all__ = [
    "SignalContextService",
    "UNIVERSE_SCAN_CAP",
    "get_signal_context_service",
    "reset_signal_context_service",
]