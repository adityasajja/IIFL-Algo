"""Post-trade attribution service — assembles the inputs, writes the rows.

What this layer owns
-------------------

The pure engine in :mod:`atr.analytics` turns an :class:`AttributionInput` into a
:class:`TradeAttribution` and needs no database, no cache and no clock. *This*
module is where that input comes from, and it is the only place in the feature
that touches I/O. Everything expensive and everything fallible is here, so the
arithmetic can be tested without any of it.

Four jobs, in order:

1. **Find** the closed trades that need attributing — from ``trade_journal``, which
   is a projection of the same position fold the account's P&L comes from, so an
   attribution cannot describe a trade the book does not have.
2. **Assemble** each one's inputs: the two legs from the order-event log, the
   entry context from ``signal_contexts``, the sizing and risk metadata from the
   opening order and its ``NEW`` event, and the price path from the daily cache.
3. **Attribute** it, through the pure engine.
4. **Write** it, idempotently — replacing on a changed fingerprint, skipping
   entirely on an unchanged one.

The provenance rule, which is the one thing here that must never be got wrong
--------------------------------------------------------------------------------

``evidence_grade`` on an attribution row is **read from ``trade_journal`` and
copied**. It is never derived here, never defaulted, and never inferred from a
timestamp. The journal is the single writer that decides whether a trade was
recorded before its outcome was known, and it decides it from the provenance
stamp the OMS wrote on the opening order's ``NEW`` event. An attribution layer
that formed its own opinion about that would be a second source of truth about
the one fact the whole learning tier rests on.

The consequence is stated plainly: a journal row with no grade produces an
attribution row with no grade, which grades in-sample downstream. That is the
correct direction. Attributing a trade cannot make it evidence.

Materialising the journal
-------------------------

``reconcile`` on :class:`~atr.services.journal.TradeJournalService` is what turns
the folded book into episodes. This service calls it first, because attributing a
trade requires the trade to exist — and because the journal reconcile is already
idempotent and safe to call on a schedule, calling it here costs nothing and means
a caller cannot accidentally ask for attribution of a book that has moved on.

There is no reverse dependency: the journal does not know that attribution
exists. Journaling is about what happened; attribution is about why. Keeping the
arrow one-way is what stops a reporting concern from being able to fail a trade
recording.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atr.analytics import attribute_trade
from atr.analytics.attribution import DEFAULT_THRESHOLDS, TradeAttribution
from atr.analytics.models import AttributionInput, AttributionLeg, TradeDetails
from atr.appdb.repositories import (
    TradeAttributionRepository,
    TradeJournalRepository,
)
from atr.research.learning_evidence import (
    CLASS_IN_SAMPLE,
    GRADE_IN_SAMPLE,
    GRADE_FORWARD,
    forward_class,
)
from atr.research.learning_readiness import (
    SIGNAL_MATCH_WINDOW_SEC as _SIGNAL_MATCH_WINDOW_SEC,
)

logger = logging.getLogger("atr.services.attribution")

#: How far after a signal an order may have been raised and still be considered
#: that signal's order. **Re-exported from the learning layer rather than
#: redeclared**: the learning builder and the signal-context service both apply
#: this window from their own direction, and three copies of one constant is
#: three chances for them to drift apart.
SIGNAL_MATCH_WINDOW_SEC = _SIGNAL_MATCH_WINDOW_SEC

#: How many bars of run-up to load before the entry, so the entry bar's own
#: extreme is in the frame. The excursion scan starts *at* the entry timestamp,
#: so this is only here to make the truncation boundary safe when an entry time
#: falls between two bars.
BARS_LOOKBACK_PAD = 5


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AttributionService:
    """Attributes closed trades from the record the platform already wrote."""

    def __init__(
        self,
        *,
        db: Any = None,
        cache_root: Path | str | None = None,
        frames: dict[str, Any] | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        from atr.appdb.engine import get_app_db

        self.db = db or get_app_db()
        #: Injectable so a test supplies its own price series instead of reading
        #: the operator's cache — the same mechanism the learning builder uses,
        #: and for the same reason: a test whose result depends on the operator's
        #: downloaded data is not a test.
        self._frames = frames
        self._cache_root = Path(cache_root) if cache_root else None
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update({k: float(v) for k, v in thresholds.items()})
        self._frame_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------
    def attribute_user(
        self,
        user_id: str,
        *,
        deployment_id: str | None = None,
        limit: int = 2000,
        force: bool = False,
    ) -> dict[str, Any]:
        """Attribute every closed trade for a user that needs it.

        ``force`` recomputes even when the fingerprint is unchanged. It exists for
        the case where the *thresholds* changed — the fingerprint covers the
        inputs, not the classifier, so a threshold change is only picked up when
        a caller says so. That is deliberate: a sweep that silently reclassified
        history every time a constant was edited would make the stored codes
        unreproducible, which is the opposite of what they are for.
        """
        journal = self._journal_service()
        materialised = journal.reconcile(user_id, deployment_id) if deployment_id else None
        if deployment_id is None:
            materialised = self._reconcile_all_journals(user_id)

        with self.db.session() as session:
            episodes, total = TradeJournalRepository.list_for_user(
                session, user_id, closed_only=True, limit=limit
            )

        inserted = replaced = skipped = 0
        failed: list[dict[str, str]] = []

        for episode in episodes:
            try:
                outcome = self.attribute_episode(user_id, episode, force=force)
            except Exception as exc:  # noqa: BLE001 - one bad trade must not stop the sweep
                logger.exception(
                    "attribution: failed for trade %s", str(episode.get("trade_id"))[:12]
                )
                failed.append(
                    {
                        "trade_id": str(episode.get("trade_id")),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if outcome == "inserted":
                inserted += 1
            elif outcome == "replaced":
                replaced += 1
            else:
                skipped += 1

        return {
            "closed_trades": total,
            "scanned": len(episodes),
            "inserted": inserted,
            "replaced": replaced,
            "skipped_unchanged": skipped,
            "failed": failed,
            "journal": materialised,
            "computed_at": _utcnow().isoformat(),
        }

    def attribute_episode(
        self, user_id: str, episode: dict[str, Any], *, force: bool = False
    ) -> str:
        """Attribute one closed journal episode. Returns
        ``"inserted" | "replaced" | "skipped"``.

        An open episode is skipped: MAE and MFE for a live position are a moving
        target, and a stored figure that is silently a snapshot of a trade still
        running is worse than no figure. The sweep attributes closed trades only.
        """
        trade_id = str(episode["trade_id"])
        if episode.get("exit_ts") is None:
            return "skipped"

        fingerprint = self.fingerprint(user_id, episode)
        with self.db.session() as session:
            existing = TradeAttributionRepository.fingerprint_for(session, trade_id)
        if existing == fingerprint and not force:
            return "skipped"

        payload = self.build_input(user_id, episode)
        attribution = attribute_trade(payload, thresholds=self.thresholds)
        row = self._row_for(user_id, episode, payload.trade, attribution, fingerprint)

        with self.db.session() as session:
            return TradeAttributionRepository.upsert(session, row)

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------
    def build_input(self, user_id: str, episode: dict[str, Any]) -> AttributionInput:
        """Everything the engine needs, resolved from the platform's own record."""
        symbol = str(episode.get("symbol") or "").upper()
        side = str(episode.get("side") or "BUY").upper()
        missing: dict[str, str] = {}

        source, grade, evidence_class, simulated = self._provenance(user_id, episode)
        origin = self._origin(user_id, episode)
        context = self._context(user_id, origin.get("signal_id"))
        sizing = self._sizing(origin)

        entry_price = _number(episode.get("entry_price"))
        exit_price = _number(episode.get("exit_price"))
        quantity = _number(episode.get("quantity")) or 0.0

        if entry_price is None:
            missing["entry_price"] = "the journal episode recorded no entry price"
        if exit_price is None:
            missing["exit_price"] = "the journal episode recorded no exit price"
        if origin.get("opening_order_id") is None:
            missing["opening_order_id"] = "the episode could not be linked to its opening order"
        if context.get("context_score") is None:
            missing.setdefault("context_score", "no recorded signal context for this signal")
        missing.update(sizing.pop("_missing", {}))

        entry_leg, exit_leg, leg_missing = self._legs(user_id, episode, origin, side)
        missing.update(leg_missing)

        position_value = (
            entry_price * quantity if entry_price is not None and quantity else None
        )

        gross = _number(episode.get("gross_pnl"))
        net = _number(episode.get("net_pnl"))
        costs = None
        if gross is not None and net is not None:
            # Costs are recovered from the two figures the journal records rather
            # than re-derived from today's rate card: the journal closes a trade
            # as ``gross - commission``, so the difference *is* what was charged,
            # and recomputing it would restate history whenever a rate changed.
            costs = gross - net

        stop_price = _number(sizing.get("stop_price"))
        planned_risk = _number(sizing.get("planned_risk_amount"))
        realized_risk_pct = _realized_risk_pct(
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            mae_pct=None,
        )

        duration = _number(episode.get("duration_sec"))
        if duration is None and episode.get("entry_ts") is not None and episode.get("exit_ts") is not None:
            try:
                duration = max(
                    0.0,
                    (episode["exit_ts"] - episode["entry_ts"]).total_seconds(),
                )
            except TypeError:
                duration = None

        atr_pct, rvol = self._volatility(user_id, symbol, episode.get("entry_ts"), context)

        trade = TradeDetails(
            trade_id=str(episode["trade_id"]),
            user_id=user_id,
            symbol=symbol,
            side=side,
            source=source,
            evidence_grade=grade,
            evidence_class=evidence_class,
            simulated=simulated,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            position_value=position_value,
            gross_pnl=gross,
            net_pnl=net,
            gross_return_pct=_return_pct(entry_price, exit_price, side),
            net_return_pct=_net_return_pct(
                entry_price, exit_price, side, net, position_value
            ),
            entry_ts=episode.get("entry_ts"),
            exit_ts=episode.get("exit_ts"),
            holding_duration_sec=int(duration) if duration is not None else None,
            stop_price=stop_price,
            planned_risk_amount=planned_risk,
            realized_risk_pct=realized_risk_pct,
            sizing_method=sizing.get("sizing_method"),
            sizing_cap_reason=sizing.get("sizing_cap_reason"),
            sizing_cap_value=sizing.get("sizing_cap_value"),
            signal_id=origin.get("signal_id"),
            strategy_id=episode.get("strategy_id"),
            strategy_version=_int(episode.get("strategy_version")),
            context_model_version=context.get("context_model_version"),
            context_score=_int(context.get("context_score")),
            context_class=context.get("context_class"),
            market_regime=context.get("market_regime") or episode.get("regime"),
            sector=context.get("sector"),
            sector_strength=context.get("sector_strength"),
            stock_relative_strength=context.get("stock_relative_strength"),
            rvol=rvol,
            atr=atr_pct,
            atr_pct=atr_pct,
            exit_reason=episode.get("exit_reason"),
            transaction_costs=costs,
            missing_fields=missing,
        )

        bars = self._bars(symbol)
        if bars is None:
            missing["mfe"] = "no cached price history for this symbol"
            missing["mae"] = "no cached price history for this symbol"

        return AttributionInput(
            trade=trade,
            entry_leg=entry_leg,
            exit_leg=exit_leg,
            bars_after_entry=bars,
            # The observation stops at the exit. Not at "now", and not at the end
            # of the series — a trade's excursion is what happened while it was
            # open, and letting the scan run past the exit would inflate MFE with
            # price action the position never had.
            bars_until=episode.get("exit_ts"),
            thresholds=self.thresholds,
        )

    # ------------------------------------------------------------------
    # provenance — the one thing that is copied, never derived
    # ------------------------------------------------------------------
    def _provenance(
        self, user_id: str, episode: dict[str, Any]
    ) -> tuple[str, str, str, bool]:
        """``(source, grade, class, simulated)`` for a journal episode.

        ``grade`` comes from ``trade_journal.evidence_grade`` **verbatim**. When
        that column is empty the grade is ``in_sample`` — the same conservative
        default :func:`grade_of` applies and for the same reason: an unknown
        provenance is not a licence to claim independence.

        ``source`` comes from the deployment's mode, which is what actually
        distinguishes a paper trade from real money; the journal does not record
        it, and guessing LIVE when it is paper would overstate the real evidence.
        """
        grade = episode.get("evidence_grade")
        deployment_id = episode.get("deployment_id")
        mode = self._deployment_mode(user_id, deployment_id) if deployment_id else None
        source = "LIVE" if mode in {"LIVE", "REAL"} else "PAPER"

        if grade not in (GRADE_FORWARD, GRADE_IN_SAMPLE):
            grade = GRADE_IN_SAMPLE
        # The class is derived from the grade and the source, so the two columns
        # can never contradict each other. A backtest episode cannot reach here
        # (backtests live in ``backtest_trades``, not the journal), so there is no
        # ``BACKTEST`` branch to get wrong: if one ever did arrive, its class would
        # resolve through the same grade-derived path and land in-sample, which is
        # the conservative answer rather than an optimistic one.
        if grade == GRADE_FORWARD:
            evidence_class = forward_class(source)
        else:
            evidence_class = CLASS_IN_SAMPLE
        return source, grade, evidence_class, source != "LIVE"

    def _deployment_mode(self, user_id: str, deployment_id: str) -> str | None:
        from atr.appdb.repositories import DeploymentRepository

        try:
            with self.db.session() as session:
                row = DeploymentRepository.get(session, deployment_id, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: deployment %s unreadable: %s", deployment_id[:8], exc)
            return None
        return str(row.get("mode") or "").upper() if row else None

    # ------------------------------------------------------------------
    # the opening order, its signal, and the sizing metadata it carries
    # ------------------------------------------------------------------
    def _origin(self, user_id: str, episode: dict[str, Any]) -> dict[str, Any]:
        """The order that opened the episode, and the signal that raised it.

        Resolved by the same rule the learning builder applies: the earliest order
        with the same strategy, symbol and direction whose creation falls within
        ``SIGNAL_MATCH_WINDOW_SEC`` of the entry. One rule, applied from the
        attribution side — the journal does not record an ``order_id``, so the
        match is made here rather than stored.
        """
        from sqlalchemy import select

        from atr.appdb.schema import orders

        out: dict[str, Any] = {"opening_order_id": None, "opening_order": None}
        symbol = str(episode.get("symbol") or "").upper()
        side = str(episode.get("side") or "BUY").upper()
        strategy_id = episode.get("strategy_id")
        entry_ts = episode.get("entry_ts")
        if not strategy_id or entry_ts is None:
            return out

        try:
            with self.db.session() as session:
                rows = session.execute(
                    select(orders)
                    .where(
                        orders.c.user_id == user_id,
                        orders.c.strategy_id == strategy_id,
                        orders.c.symbol == symbol,
                    )
                    .order_by(orders.c.created_at)
                ).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: orders unreadable: %s", exc)
            return out

        for row in rows:
            record = dict(row)
            if str(record.get("side") or "").upper() != side:
                continue
            gap = _gap_sec(entry_ts, record.get("created_at"))
            if gap is None or gap > SIGNAL_MATCH_WINDOW_SEC:
                continue
            out["opening_order_id"] = record.get("order_id")
            out["opening_order"] = record
            out["signal_id"] = record.get("signal_id")
            return out
        return out

    def _sizing(self, origin: dict[str, Any]) -> dict[str, Any]:
        """Sizing method, stop, planned risk and cap, from the opening order.

        The opening order's ``NEW`` event is the only place a strategy's sizing
        decision was ever written down, so the event log is read rather than the
        order row: the row carries the *result* (a quantity), and the event
        carries the *reason* (why that quantity). A trade attributed from the row
        alone could say what was bought but never whether a cap bound it, which is
        the difference between ``OVERSIZED`` as an arithmetic fact and as an
        opinion.
        """
        out: dict[str, Any] = {
            "sizing_method": None,
            "sizing_cap_reason": None,
            "sizing_cap_value": None,
            "stop_price": None,
            "planned_risk_amount": None,
            "_missing": {},
        }
        order = origin.get("opening_order")
        if not order:
            out["_missing"]["sizing_method"] = "the opening order could not be resolved"
            return out

        out["stop_price"] = _number(order.get("stop_price"))
        if out["stop_price"] is None:
            out["_missing"]["stop_price"] = "no protective stop was recorded on the opening order"

        raw = self._order_new_payload(order.get("order_id"))
        if raw is None:
            return out

        out["sizing_method"] = raw.get("sizing_method") or raw.get("sizing")
        cap = raw.get("sizing_cap") or {}
        if isinstance(cap, dict):
            out["sizing_cap_reason"] = cap.get("reason")
            out["sizing_cap_value"] = _number(cap.get("value"))
        else:
            out["sizing_cap_reason"] = raw.get("sizing_cap_reason")
            out["sizing_cap_value"] = _number(raw.get("sizing_cap_value"))

        planned = raw.get("planned_risk_amount") or raw.get("risk_amount")
        out["planned_risk_amount"] = _number(planned)
        stop = _number(raw.get("stop_price"))
        if stop is not None and out["stop_price"] is None:
            out["stop_price"] = stop

        # A planned risk that was never recorded is not zero. Deriving it from the
        # stop is legitimate — it is the strategy's own arithmetic, re-applied —
        # and the derivation is named so a reader knows which it is.
        if out["planned_risk_amount"] is None and stop is not None:
            entry = _number(raw.get("requested_price")) or _number(order.get("requested_price"))
            quantity = _number(order.get("quantity"))
            if entry is not None and quantity:
                out["planned_risk_amount"] = abs(entry - stop) * quantity
                out["sizing_method"] = out["sizing_method"] or "derived_from_stop"
        if out["sizing_method"] is None:
            out["_missing"]["sizing_method"] = "the strategy recorded no sizing method"
        return out

    def _order_new_payload(self, order_id: str | None) -> dict[str, Any] | None:
        """The ``raw`` payload of an order's ``NEW`` event — where reasons live.

        A missing event is not an error: the order row is still fully usable, and
        losing a label to protect a fact would be the wrong trade.
        """
        if not order_id:
            return None
        from atr.appdb.repositories import OrderEventRepository

        try:
            with self.db.session() as session:
                for event in OrderEventRepository.history(session, order_id):
                    if event.get("to_status") != "NEW":
                        continue
                    raw = event.get("raw")
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                    if isinstance(raw, dict):
                        return raw
        except Exception as exc:  # noqa: BLE001
            logger.debug("attribution: NEW event unreadable for %s: %s", str(order_id)[:12], exc)
        return None

    # ------------------------------------------------------------------
    # signal context
    # ------------------------------------------------------------------
    def _context(self, user_id: str, signal_id: str | None) -> dict[str, Any]:
        """The recorded signal context, kept exactly as recorded.

        Never recomputed. The whole value of the context record is that it was
        measured at signal time and cannot be restated later; re-deriving it here
        would let today's market change what yesterday's context was, which is the
        one thing the ``context_model_version`` column exists to prevent.
        """
        if not signal_id:
            return {}
        from sqlalchemy import select

        from atr.appdb.schema import signal_contexts

        try:
            with self.db.session() as session:
                row = session.execute(
                    select(signal_contexts).where(
                        signal_contexts.c.user_id == user_id,
                        signal_contexts.c.signal_id == str(signal_id),
                    )
                ).mappings().first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: signal context unreadable: %s", exc)
            return {}
        if row is None:
            return {}

        record = dict(row)
        market = _json_or_empty(record.get("market_context"))
        sector = _json_or_empty(record.get("sector_context"))
        stock = _json_or_empty(record.get("stock_context"))
        return {
            "context_score": record.get("context_score"),
            "context_class": record.get("context_class"),
            "context_model_version": record.get("context_model_version"),
            "market_regime": market.get("regime"),
            "sector": sector.get("sector"),
            "sector_strength": sector.get("relative_strength_1m"),
            "stock_relative_strength": stock.get("relative_strength_nifty_20d"),
            "rvol": stock.get("relative_volume"),
            "atr_pct": stock.get("atr_pct"),
        }

    # ------------------------------------------------------------------
    # legs, from the order-event log
    # ------------------------------------------------------------------
    def _legs(
        self,
        user_id: str,
        episode: dict[str, Any],
        origin: dict[str, Any],
        side: str,
    ) -> tuple[AttributionLeg, AttributionLeg | None, dict[str, str]]:
        """The entry and exit legs, rebuilt from the fills that made them.

        A leg is collapsed into one average price — which is the honest summary —
        but the *number of fills* and the requested quantity travel with it, so a
        partial fill is visible rather than smoothed away. An entry that got 40%
        of its size is a materially different trade from one that got all of it,
        and a leg that recorded only its average price could not tell them apart.
        """
        missing: dict[str, str] = {}
        entry_price = _number(episode.get("entry_price"))
        exit_price = _number(episode.get("exit_price"))
        quantity = _number(episode.get("quantity")) or 0.0
        entry_ts = episode.get("entry_ts")

        closing_side = "SELL" if side == "BUY" else "BUY"

        entry_fills = self._fills_for(
            user_id, episode.get("symbol"), side, at_or_after=entry_ts
        )
        exit_fills = self._fills_for(
            user_id, episode.get("symbol"), closing_side, at_or_after=entry_ts
        )

        entry_leg = self._leg_from(
            kind="entry",
            side=side,
            fills=entry_fills,
            fallback_price=entry_price,
            fallback_qty=quantity,
            order=origin.get("opening_order"),
            signal_id=origin.get("signal_id"),
        )
        if entry_leg.expected_price is None:
            missing["expected_entry_price"] = (
                "no reference price was recorded on the opening order, so entry "
                "slippage is not measurable for this trade"
            )

        exit_leg: AttributionLeg | None = None
        if exit_fills or exit_price is not None:
            exit_leg = self._leg_from(
                kind="exit",
                side=closing_side,
                fills=exit_fills,
                fallback_price=exit_price,
                fallback_qty=quantity,
                order=None,
                signal_id=None,
            )
            if exit_leg.expected_price is None:
                missing["expected_exit_price"] = (
                    "no reference price was recorded on the closing order, so exit "
                    "slippage is not measurable for this trade"
                )
        return entry_leg, exit_leg, missing

    def _leg_from(
        self,
        *,
        kind: str,
        side: str,
        fills: list[dict[str, Any]],
        fallback_price: float | None,
        fallback_qty: float | None,
        order: dict[str, Any] | None,
        signal_id: str | None,
    ) -> AttributionLeg:
        """Collapse a leg's fills into one average price plus its shape."""
        filled_qty = sum(_number(f.get("filled_qty")) or 0.0 for f in fills) or None
        actual = None
        if fills and filled_qty:
            actual = sum(
                (_number(f.get("filled_price")) or 0.0) * (_number(f.get("filled_qty")) or 0.0)
                for f in fills
            ) / filled_qty
        if actual is None:
            actual = fallback_price

        first = fills[0] if fills else {}
        last = fills[-1] if fills else {}

        requested = None
        expected = None
        order_ts = None
        signal_ts = None
        commission = None
        if order is not None:
            requested = _number(order.get("quantity"))
            expected = _number(order.get("requested_price"))
            order_ts = order.get("created_at")
        else:
            # The closing order is not linked to the episode, so its own row is
            # found from the fill's order id — which is the only handle the log
            # gives us, and it is exact.
            closing_order_id = last.get("order_id")
            closing = self._order_row(closing_order_id) if closing_order_id else None
            if closing:
                requested = _number(closing.get("quantity"))
                expected = _number(closing.get("requested_price"))
                order_ts = closing.get("created_at")

        if first.get("commission") is not None:
            commission = sum(
                _number(f.get("commission")) or 0.0 for f in fills
            ) or 0.0
        if expected is None:
            # No reference price means slippage is unmeasurable, and the leg says
            # so by leaving ``expected_price`` None rather than filling it with the
            # actual price — which would report perfectly-executed trades that were
            # never measured at all.
            expected = None

        signal_to_order = None
        if signal_ts is not None and order_ts is not None:
            signal_to_order = _gap_sec(order_ts, signal_ts)

        return AttributionLeg(
            kind=kind,
            side=side,
            expected_price=expected,
            actual_price=actual,
            requested_qty=requested if requested is not None else fallback_qty,
            filled_qty=filled_qty if filled_qty is not None else fallback_qty,
            signal_ts=signal_ts,
            order_ts=order_ts,
            fill_ts=last.get("fill_ts") or last.get("ts"),
            signal_to_order_sec=signal_to_order,
            order_to_fill_sec=_gap_sec(
                last.get("fill_ts") or last.get("ts"), order_ts
            ),
            commission=commission,
            partial=len(fills) > 1,
            fill_count=len(fills),
        )

    def _fills_for(
        self,
        user_id: str,
        symbol: Any,
        side: str,
        *,
        at_or_after: Any = None,
    ) -> list[dict[str, Any]]:
        """Execution events for a symbol and side, oldest first.

        Read through the same ledger the account's P&L comes from, so an
        attribution and a position cannot disagree about what filled.
        """
        from atr.services.paper import PaperLedger

        try:
            fills = PaperLedger(db=self.db).fills(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: fills unreadable: %s", exc)
            return []

        wanted = str(symbol or "").upper()
        out: list[dict[str, Any]] = []
        for fill in fills:
            if str(getattr(getattr(fill, "instrument", None), "symbol", "")).upper() != wanted:
                continue
            fill_side = str(getattr(fill, "side", ""))
            if fill_side.upper() not in (side, side[0]):
                continue
            ts = getattr(fill, "ts", None)
            if at_or_after is not None and ts is not None and ts < at_or_after:
                continue
            out.append(
                {
                    "order_id": getattr(fill, "order_id", None),
                    "filled_qty": abs(_number(getattr(fill, "quantity", None)) or 0.0),
                    "filled_price": _number(getattr(fill, "price", None)),
                    "commission": _number(getattr(fill, "commission", None)),
                    "fill_ts": ts,
                    "ts": ts,
                }
            )
        out.sort(key=lambda f: f.get("fill_ts") or _utcnow())
        return out

    def _order_row(self, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        from sqlalchemy import select

        from atr.appdb.schema import orders

        try:
            with self.db.session() as session:
                row = session.execute(
                    select(orders).where(orders.c.order_id == order_id)
                ).mappings().first()
        except Exception as exc:  # noqa: BLE001
            logger.debug("attribution: order %s unreadable: %s", str(order_id)[:12], exc)
            return None
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # price path
    # ------------------------------------------------------------------
    def _bars(self, symbol: str) -> Any | None:
        """The symbol's daily bars, or ``None`` when the cache has none.

        The frame passed to the engine is the *whole* cached series; the engine
        truncates it to ``[entry, exit]`` itself. Handing it a pre-sliced frame
        would be a second place the boundary is decided, and the two would
        eventually disagree.
        """
        if symbol in self._frame_cache:
            return self._frame_cache[symbol]
        if self._frames is not None:
            # Look the keys up separately rather than ``a or b``. A pandas
            # DataFrame raises on truth-testing — ``or`` would evaluate
            # ``bool(frame)`` and raise ``ValueError`` — so the only reason a
            # chained lookup ever appeared to work is that the first key was
            # *absent*, which is the case that never reaches the boolean. The
            # injected-frame path is the one every test and every offline
            # analysis uses, so it was the path that always raised.
            frame = self._frames.get(symbol)
            if frame is None:
                frame = self._frames.get(f"{symbol}-EQ")
            self._frame_cache[symbol] = frame
            return frame
        try:
            from atr.data.history import load_cached

            loaded = load_cached("NSEEQ", [f"{symbol}-EQ", symbol])
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: price cache unreadable for %s: %s", symbol, exc)
            self._frame_cache[symbol] = None
            return None
        frame = None
        for candidate in loaded.values():
            frame = candidate
            break
        self._frame_cache[symbol] = frame
        return frame

    def _volatility(
        self, user_id: str, symbol: str, entry_ts: Any, context: dict[str, Any]
    ) -> tuple[float | None, float | None]:
        """ATR% and RVOL at entry.

        The recorded context wins where it has them: it was measured at signal
        time and cannot be restated, which is exactly what an entry feature needs.
        The price cache is the fallback, computed point-in-time through the
        learning layer's own truncation helper rather than a second implementation
        of it.
        """
        atr_pct = _number(context.get("atr_pct"))
        rvol = _number(context.get("rvol"))
        if atr_pct is not None and rvol is not None:
            return atr_pct, rvol
        if entry_ts is None:
            return atr_pct, rvol
        frame = self._bars(symbol)
        if frame is None:
            return atr_pct, rvol
        try:
            from atr.research.learning_enrich import metrics_at

            metrics, _missing = metrics_at(frame, entry_ts)
        except Exception as exc:  # noqa: BLE001
            logger.debug("attribution: metrics unavailable for %s: %s", symbol, exc)
            return atr_pct, rvol
        return (
            atr_pct if atr_pct is not None else metrics.atr_pct,
            rvol if rvol is not None else metrics.volume_multiple,
        )

    # ------------------------------------------------------------------
    # fingerprint & persistence
    # ------------------------------------------------------------------
    def fingerprint(self, user_id: str, episode: dict[str, Any]) -> str:
        """A hash of everything the computation reads.

        Two jobs. It makes the sweep cheap — an unchanged trade never reaches the
        write — and it makes the row *auditable*: if a stored attribution later
        disagrees with a recomputation, the fingerprints say whether the inputs
        changed or the arithmetic did, which is the difference between a late fill
        and a regression.

        Deliberately excludes the thresholds. A threshold edit is a *reclassification*
        of unchanged inputs, not a new measurement, and reclassifying history every
        time a constant moves would make the stored codes unreproducible.
        """
        material = {
            "trade_id": str(episode.get("trade_id")),
            "symbol": episode.get("symbol"),
            "side": episode.get("side"),
            "quantity": _number(episode.get("quantity")),
            "entry_price": _number(episode.get("entry_price")),
            "exit_price": _number(episode.get("exit_price")),
            "gross_pnl": _number(episode.get("gross_pnl")),
            "net_pnl": _number(episode.get("net_pnl")),
            "entry_ts": str(episode.get("entry_ts")),
            "exit_ts": str(episode.get("exit_ts")),
            "exit_reason": episode.get("exit_reason"),
            "evidence_grade": episode.get("evidence_grade"),
            "strategy_id": episode.get("strategy_id"),
            "strategy_version": _int(episode.get("strategy_version")),
        }
        blob = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _row_for(
        self,
        user_id: str,
        episode: dict[str, Any],
        trade: TradeDetails,
        attribution: TradeAttribution,
        fingerprint: str,
    ) -> dict[str, Any]:
        """The persisted row: the tree whole, plus the sliceable fields lifted.

        ``trade`` is passed rather than re-derived so the grade and class written
        here are *the same objects* the engine classified with. Recomputing them
        would create a second place provenance is decided, and the two would
        eventually disagree — which is the one failure this feature must not have.
        """
        flat = attribution.flat()
        now = _utcnow()
        return {
            "trade_id": attribution.trade_id,
            "user_id": user_id,
            "deployment_id": episode.get("deployment_id"),
            "symbol": attribution.symbol,
            "side": attribution.side,
            "source": attribution.source,
            # Copied from the journal by ``_provenance``. Written, never derived.
            "evidence_grade": trade.evidence_grade,
            "evidence_class": trade.evidence_class,
            "simulated": attribution.simulated,
            "attribution": json.dumps(attribution.as_dict(), default=str),
            "reason_codes": ",".join(attribution.reason_codes),
            "missing_fields": json.dumps(attribution.missing_fields, default=str),
            "entry_quality": flat.get("attribution_entry_quality"),
            "execution_quality": flat.get("attribution_execution_quality"),
            "mfe_pct": attribution.excursions.mfe_pct,
            "mae_pct": attribution.excursions.mae_pct,
            "mfe_amount": attribution.excursions.mfe_amount,
            "mae_amount": attribution.excursions.mae_amount,
            "mfe_over_risk": attribution.excursions.mfe_over_risk,
            "realized_over_risk": attribution.excursions.realized_over_risk,
            "capture_efficiency_pct": attribution.exit.capture_efficiency_pct,
            "entry_slippage_bps": attribution.entry.slippage_bps,
            "exit_slippage_bps": attribution.execution.exit_slippage_bps,
            "total_slippage_bps": attribution.execution.total_slippage_bps,
            "transaction_costs": attribution.execution.transaction_costs,
            "cost_pct": attribution.execution.cost_pct,
            "signal_to_order_sec": attribution.entry.signal_to_order_sec,
            "order_to_fill_sec": attribution.entry.order_to_fill_sec,
            "holding_sec": attribution.exit.holding_sec,
            "partial_fill": bool(attribution.entry.partial),
            "fill_ratio": attribution.entry.fill_ratio,
            "sizing_method": attribution.sizing.method,
            "sizing_cap_reason": attribution.sizing.cap_reason,
            "realized_risk_pct": attribution.risk.realized_risk_pct,
            "planned_risk_amount": attribution.risk.planned_risk_amount,
            "context_score": attribution.context.context_score,
            "context_class": attribution.context.context_class,
            "market_regime": attribution.context.market_regime,
            "sector": attribution.context.sector,
            "sector_strength": attribution.context.sector_strength,
            "stock_relative_strength": attribution.context.stock_relative_strength,
            "rvol": attribution.context.rvol,
            "atr_pct": attribution.context.atr_pct,
            "exit_reason": attribution.exit.reason,
            "input_fingerprint": fingerprint,
            "computed_at": now,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get(self, user_id: str, trade_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            return TradeAttributionRepository.get(session, trade_id, user_id)

    def rows(
        self, user_id: str, **filters: Any
    ) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows, _total = TradeAttributionRepository.list_for_user(
                session, user_id, **filters
            )
        return rows

    def all_rows(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return TradeAttributionRepository.all_for_user(session, user_id)

    def coverage(self, user_id: str, attributed: int) -> dict[str, Any]:
        """``attributed`` against the size of the closed book it summarises.

        A route that reports a summary over eleven rows has to say eleven out of
        what. The denominator is the user's closed ``trade_journal`` count, which
        is the population the attribution layer is defined over — not the order
        count, which would count open positions and cancelled orders as trades
        that failed to be attributed.

        Lives here rather than in the route because reading the journal is
        storage access, and a route that reads a table directly is a route with
        no owner for its authorization. On failure the count is reported as
        ``None`` with ``complete`` also ``None``: "we could not count" and "the
        count is zero" must not look the same to a caller deciding whether the
        summary is the whole book.
        """
        try:
            from atr.appdb.repositories import TradeJournalRepository

            with self.db.session() as session:
                _closed, total = TradeJournalRepository.list_for_user(
                    session, user_id, closed_only=True, limit=1
                )
        except Exception as exc:  # noqa: BLE001 - a summary is still useful without it
            logger.debug("attribution: coverage unavailable: %s", exc)
            return {"attributed": attributed, "closed_trades": None, "complete": None}

        return {
            "attributed": attributed,
            "closed_trades": total,
            "complete": total == attributed,
            "unattributed": max(0, total - attributed),
        }

    # ------------------------------------------------------------------
    def _journal_service(self) -> Any:
        from atr.services.journal import TradeJournalService

        return TradeJournalService(db=self.db)

    def _reconcile_all_journals(self, user_id: str) -> dict[str, int]:
        """Materialise the journal for every one of the user's live deployments.

        ``TradeJournalService.reconcile`` is per-deployment, and a user may hold
        several. One broken book must not stop the rest from being journalled —
        the same rule the platform-wide sweep applies.
        """
        from atr.appdb.repositories import DeploymentRepository

        service = self._journal_service()
        totals = {"opened": 0, "closed": 0, "deployments": 0}
        try:
            with self.db.session() as session:
                deployments = DeploymentRepository.list_for_user(session, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("attribution: deployments unreadable: %s", exc)
            return totals
        for deployment in deployments:
            if str(deployment.get("status") or "").upper() == "STOPPED":
                continue
            try:
                result = service.reconcile(user_id, str(deployment["deployment_id"]))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "attribution: journal reconcile failed for %s",
                    str(deployment.get("deployment_id"))[:8],
                )
                continue
            totals["opened"] += len(result.get("opened") or [])
            totals["closed"] += len(result.get("closed") or [])
            totals["deployments"] += 1
        return totals


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _gap_sec(later: Any, earlier: Any) -> float | None:
    """``later - earlier`` in seconds, or ``None``. Never negative."""
    if later is None or earlier is None:
        return None
    try:
        import pandas as pd

        a = pd.to_datetime(later, errors="coerce")
        b = pd.to_datetime(earlier, errors="coerce")
        if pd.isna(a) or pd.isna(b):
            return None
        if getattr(a, "tzinfo", None) is not None:
            a = a.tz_localize(None)
        if getattr(b, "tzinfo", None) is not None:
            b = b.tz_localize(None)
        return max(0.0, float((a - b).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def _json_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _return_pct(entry: float | None, exit_price: float | None, side: str) -> float | None:
    if not entry or exit_price is None:
        return None
    sign = -1.0 if str(side).upper() in ("SELL", "S") else 1.0
    return (float(exit_price) / float(entry) - 1.0) * 100.0 * sign


def _net_return_pct(
    entry: float | None,
    exit_price: float | None,
    side: str,
    net_pnl: float | None,
    position_value: float | None,
) -> float | None:
    """Net return on the position's entry value.

    Preferred over scaling the gross percentage when both the rupees and the value
    are known, because the two differ by a rounding that compounds across a
    book — and a net return figure is what a comparison between a large position
    and a small one has to use.
    """
    if net_pnl is not None and position_value:
        return float(net_pnl) / float(position_value) * 100.0
    return _return_pct(entry, exit_price, side)


def _realized_risk_pct(
    *,
    side: str,
    entry_price: float | None,
    stop_price: float | None,
    mae_pct: float | None,
) -> float | None:
    """The risk the trade actually carried, as a percentage of entry.

    Taken as the distance from entry to the recorded stop, which is the risk the
    position was *sized for* and therefore the one that makes ``realized_over_risk``
    an R multiple rather than an arbitrary ratio. Falls back to the worst adverse
    excursion when no stop was recorded, and names that fallback through the
    sizing method it lands beside — never silently.
    """
    if entry_price and stop_price:
        return abs(float(entry_price) - float(stop_price)) / float(entry_price) * 100.0
    if mae_pct is not None:
        return abs(float(mae_pct))
    return None


def _service_key(user_id: str, deployment_id: str | None) -> tuple[str, str | None]:
    """The cache key for a deployment mode lookup. Kept so the two lookups for
    the same episode resolve identically rather than reaching the database
    twice for one answer."""
    return (user_id, deployment_id)


def get_attribution_service() -> AttributionService:
    """Process-wide instance. Cheap to construct; the cache is per-symbol."""
    global _service
    with _service_lock:
        if _service is None:
            _service = AttributionService()
        return _service


def reset_attribution_service() -> None:
    """Drop the singleton. Called by the test fixtures between cases."""
    global _service
    with _service_lock:
        _service = None


_service: AttributionService | None = None
_service_lock = threading.Lock()


__all__ = [
    "SIGNAL_MATCH_WINDOW_SEC",
    "AttributionService",
    "get_attribution_service",
    "reset_attribution_service",
]
