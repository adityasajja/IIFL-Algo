"""Trade signals and the self-learning status."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from atr.api.deps import get_principal, require_permission
from atr.auth.rbac import Permission

from atr.api.legacy.common import _append_audit
from atr.api.legacy.risk import _live_broker, _order_principal, _require_live_execution

logger = logging.getLogger("atr.api")

router = APIRouter()


@router.get("/self-learning/status")
def self_learning_status() -> dict[str, Any]:
    """Returns the market regime, dynamic strategy weights, and training metrics."""
    from dataclasses import asdict

    from atr.research.self_learning import get_self_learning_engine

    engine = get_self_learning_engine()
    return {
        "market_regime": asdict(engine.state.market_regime),
        "strategies": {k: asdict(v) for k, v in engine.state.strategies.items()},
        "total_cycles_trained": engine.state.total_cycles_trained,
        "last_trained_at": engine.state.last_trained_at,
        "model_version": engine.state.model_version,
    }


@router.post("/self-learning/train", dependencies=[Depends(get_principal)])
def self_learning_train() -> dict[str, Any]:
    """Triggers an online learning cycle across historical data."""
    from atr.research.self_learning import get_self_learning_engine
    engine = get_self_learning_engine()
    return engine.train_on_history()


@router.get("/trade-signals/settings")
def trade_signals_settings_get() -> dict[str, Any]:
    """Return current position sizing + stop-loss configuration."""
    from atr.trade_signals import load_settings
    return load_settings().model_dump()


@router.put(
    "/trade-signals/settings",
    dependencies=[Depends(require_permission(Permission.RISK_CONFIGURE))],
)
def trade_signals_settings_put(body: dict[str, Any]) -> dict[str, Any]:
    """Update position sizing + stop-loss configuration."""
    from atr.trade_signals import TradeSignalSettings, load_settings, save_settings
    current = load_settings()
    merged = TradeSignalSettings(**{**current.model_dump(), **body})
    save_settings(merged)
    return merged.model_dump()


@router.get("/trade-signals")
def trade_signals_list(status: str | None = None) -> dict[str, Any]:
    """Return the signal queue. Pass ?status=PENDING|ACTIVE|DONE|SKIPPED to filter."""
    from atr.trade_signals import get_queue
    q = get_queue()
    signals = q.all()
    if status:
        signals = [s for s in signals if s.status == status.upper()]
    return {
        "signals": [s.model_dump(mode="json") for s in signals[:100]],
        "pending": len(q.pending()),
        "active": len(q.active()),
    }


@router.post("/trade-signals/scan", dependencies=[Depends(get_principal)])
def trade_signals_scan() -> dict[str, Any]:
    """Trigger an immediate signal scan combining intelligent rules and quantitative research papers."""

    from atr.alerts.channels import channels_from_settings
    from atr.alerts.intelligent import evaluate_stock_signals, load_intelligent_config
    from atr.config.settings import get_settings
    from atr.data.history import load_cached
    from atr.research.self_learning import get_self_learning_engine
    from atr.scanner import UNIVERSE
    from atr.trade_signals import (
        build_signal_from_intelligent,
        format_telegram_preview,
        get_queue,
        load_settings,
    )

    cfg = load_intelligent_config()
    ts_settings = load_settings()
    q = get_queue()
    frames = load_cached("NSEEQ")
    channels = channels_from_settings(get_settings())
    sl_engine = get_self_learning_engine()

    # 1. First add highest-conviction quantitative research paper signals
    new_signals = []
    try:
        quant_signals = sl_engine.scan_for_quant_signals(max_candidates=10)
        for qs in quant_signals:
            if q.active_count() >= ts_settings.max_active:
                break
            existing = [s for s in q.pending() if s.symbol == qs.symbol and s.action == qs.action]
            if existing:
                continue
            df_sym = frames.get(qs.symbol)
            if df_sym is None or len(df_sym) < 20:
                continue

            trade_sig = build_signal_from_intelligent(
                symbol=qs.symbol,
                action=qs.action,
                setup=qs.setup,
                reason=qs.thesis,
                entry_price=qs.price,
                df=df_sym,
                settings=ts_settings,
                paper_citation=qs.paper_citation,
                thesis=qs.thesis,
                confidence_score=qs.confidence_score,
                expected_value=qs.expected_value,
                regime_fit=qs.regime_fit,
            )
            q.add(trade_sig)
            new_signals.append(trade_sig.model_dump(mode="json"))
    except Exception as e:
        logger.warning("Quant alpha scan failed: %s", e)

    # 2. Add scanner universe & alert rule candidates
    syms = set(UNIVERSE)
    try:
        from atr.api.legacy.alerts import _alert_store
        store = _alert_store()
        for r in store.rules():
            syms.add(r.symbol)
    except Exception as exc:
        logger.warning("alert rules load failed: %s", exc)

    for sym in syms:
        if q.active_count() >= ts_settings.max_active:
            break
        df = frames.get(sym)
        if df is None or len(df) < 30:
            continue
        try:
            found = evaluate_stock_signals(sym, df, cfg)
            for sig_raw in found:
                existing = [s for s in q.pending() if s.symbol == sym and s.action == sig_raw.action]
                if existing:
                    continue
                if q.active_count() >= ts_settings.max_active:
                    continue

                sig = build_signal_from_intelligent(
                    symbol=sym,
                    action=sig_raw.action,
                    setup=sig_raw.metric,
                    reason=sig_raw.reason,
                    entry_price=sig_raw.price,
                    df=df,
                    settings=ts_settings,
                    rsi_val=sig_raw.rsi,
                )
                q.add(sig)
                new_signals.append(sig.model_dump(mode="json"))

                # Send Telegram preview
                header, body = format_telegram_preview(sig)
                for ch in channels:
                    try:
                        if ch.send(header, body):
                            break
                    except Exception as exc:
                        logger.warning("Telegram channel send failed: %s", exc)
                else:
                    if channels:
                        logger.warning("no Telegram channel sent preview for %s", sig.id)
        except Exception as e:
            logger.warning("trade scan failed for %s: %s", sym, e)

    return {"scanned": len(syms) + len(frames), "new_signals": len(new_signals), "signals": new_signals}


@router.post("/trade-signals/{signal_id}/execute")
def trade_signals_execute(signal_id: str, http_request: Request) -> dict[str, Any]:
    """Approve a pending signal — places the entry, the stop-loss and the target.

    Rewired onto the OMS and the execution service on 2026-09-14. Three things
    were wrong with the previous version, and all three are why it now shares one
    order path with everything else:

    * ``OrderType.SL_MARKET`` **did not exist**. The route placed the entry order
      at the exchange and *then* raised ``AttributeError`` on the stop-loss leg,
      returning a 500 and leaving a live position with no stop and no target —
      the single most dangerous state this platform can produce.
    * Even had the name existed, that leg set ``limit_price`` and a
      ``broker_params["triggerPrice"]`` that nothing reads. ``IiflBroker`` takes
      the trigger from ``order.stop_price``, so the stop would have reached the
      exchange with no trigger price.
    * There was no idempotency guard, so clicking Execute twice placed the bracket
      twice.

    Each leg now carries a key derived from ``(signal_id, leg_index)``, so a retry
    — whether a double click, a timeout the browser retried, or an operator
    re-sending — returns the order that already exists instead of placing a
    second bracket.
    """
    from atr.alerts.channels import channels_from_settings
    from atr.config.settings import get_settings
    from atr.execution.oms import OrderDraft, idempotency_key_for
    from atr.services.execution import (
        BrokerPortfolio,
        ExecutionService,
        VenueError,
        iifl_venue,
    )
    from atr.services.orders import get_order_service
    from atr.trade_signals import format_telegram_confirm, get_queue

    principal = _order_principal(http_request)

    q = get_queue()
    sig = q.get(signal_id)
    if not sig:
        raise HTTPException(404, f"Signal {signal_id} not found")
    if sig.status != "PENDING":
        raise HTTPException(400, f"Signal is {sig.status}, not PENDING")

    # Paper mode check — last gate before anything is transmitted. The kill switch
    # is no longer checked here: it lives in the OMS risk gate, so it applies to
    # this route and to every other one without each having to remember.
    _require_live_execution("Signal execution")

    broker = _live_broker()
    service = ExecutionService(
        orders=get_order_service(portfolio=BrokerPortfolio(broker)),
        venue=iifl_venue(broker),
    )

    entry_side = "BUY" if sig.action == "BUY" else "SELL"
    exit_side = "SELL" if sig.action == "BUY" else "BUY"
    version = getattr(sig, "strategy_version", None)
    strategy_id = getattr(sig, "strategy_id", None)

    # (leg_index, side, order_type, limit_price, stop_price, tag)
    legs = (
        (0, entry_side, "MARKET", None, None, "ATR-SEMI"),
        (1, exit_side, "SL-M", None, float(sig.stop_loss), "ATR-SL"),
        (2, exit_side, "LIMIT", float(sig.target), None, "ATR-TGT"),
    )

    placed: dict[str, Any] = {}
    for leg_index, side, order_type, limit_price, stop_price, tag in legs:
        draft = OrderDraft(
            user_id=principal.user_id,
            symbol=sig.symbol,
            side=side,
            quantity=float(sig.quantity),
            mode="LIVE",
            exchange="NSEEQ",
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            product="CNC",
            requested_price=float(sig.entry_price) if sig.entry_price else None,
            signal_id=signal_id,
            strategy_id=strategy_id,
            strategy_version=version,
            tag=tag,
        )
        key = idempotency_key_for(
            user_id=principal.user_id,
            strategy_id=strategy_id,
            strategy_version=version,
            signal_id=signal_id,
            symbol=sig.symbol,
            side=side,
            leg_index=leg_index,
        )
        try:
            result = service.place(draft, idempotency_key=key)
        except ValueError as exc:
            raise HTTPException(400, f"leg {leg_index} ({tag}): {exc}") from exc
        except VenueError as exc:
            # The leg that failed is named, and the ones already placed are
            # returned, so an operator knows exactly what is live and what is not
            # rather than being told only that something went wrong.
            raise HTTPException(
                502,
                detail={
                    "detail": f"leg {leg_index} ({tag}) failed: {exc}",
                    "code": "venue_failed",
                    "placed": placed,
                },
            ) from exc
        placed[tag] = {
            "order_id": result.order_id,
            "status": result.status,
            "broker_order_id": result.broker_order_id,
            "reject_reason": result.reject_reason,
            "duplicate": result.duplicate,
        }

    _append_audit(
        actor=principal.username,
        action="signal.execute",
        subject=f"{sig.action} {sig.quantity} {sig.symbol}",
        detail=f"signal {signal_id} approved from the trade queue",
    )

    q.mark_active(
        signal_id,
        placed["ATR-SEMI"]["broker_order_id"] or "",
        placed["ATR-SL"]["broker_order_id"] or "",
        placed["ATR-TGT"]["broker_order_id"] or "",
    )

    channels = channels_from_settings(get_settings())
    header, body = format_telegram_confirm(sig)
    for ch in channels:
        try:
            if ch.send(header, body):
                break
        except Exception:  # noqa: BLE001 - a notification must not undo a placement
            logger.debug("telegram confirm failed for signal %s", signal_id)

    return {
        "signal_id": signal_id,
        "entry_order": placed["ATR-SEMI"]["broker_order_id"],
        "sl_order": placed["ATR-SL"]["broker_order_id"],
        "target_order": placed["ATR-TGT"]["broker_order_id"],
        "orders": placed,
    }


@router.post("/trade-signals/{signal_id}/skip", dependencies=[Depends(get_principal)])
def trade_signals_skip(signal_id: str) -> dict[str, Any]:
    """Dismiss a pending signal without trading."""
    from atr.trade_signals import get_queue
    q = get_queue()
    if not q.skip(signal_id):
        raise HTTPException(404, f"Signal {signal_id} not found")
    return {"skipped": signal_id}
