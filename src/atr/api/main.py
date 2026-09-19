"""FastAPI control plane.

Exposes the engine over HTTP so you can drive it from a dashboard, a scheduler,
or a notebook. Live endpoints refuse to act unless ``env`` is paper/live and a
session exists — the API is a convenience layer, not a risk control.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import json
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from atr.config.settings import Settings, get_settings

# Imported at module level (not lazily) because the warmup table is read while a
# response is being built, and ``atr.backtest.config`` pulls in nothing heavy.
from atr.backtest.config import WARMUP_BARS

app = FastAPI(title="ATR — algo trading backend", version="0.1.0")

logger = logging.getLogger("atr.api")

@app.on_event("startup")
async def _on_startup() -> None:
    from atr.alerts.intelligent import get_intelligent_monitor
    from atr.api.stream import get_broadcaster

    get_broadcaster().set_loop(asyncio.get_running_loop())
    await get_intelligent_monitor().start()
    # Price the paper engine off live ticks before the runner starts, so the
    # first fill is priced from the feed rather than yesterday's close.
    from atr.api.price_sources import install_live_price_source

    install_live_price_source()
    _start_paper_runner()
    _warm_breadth_cache()
    _warm_instrument_master()
    asyncio.create_task(_insights_loop())


async def _insights_loop() -> None:
    """Send the day's read once, after the close. Checked every five minutes."""
    from atr.insights.service import get_insights_service

    def run():
        try:
            from atr.services.broker_access import authed_client

            client = authed_client()
        except Exception:  # noqa: BLE001 - no session: the digest uses the saved holdings
            client = None
        return get_insights_service().maybe_send_daily(client=client)

    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(run)
        except Exception:  # noqa: BLE001 - a failed send must not stop the loop
            logger.exception("daily insights send failed")


def _start_paper_runner() -> None:
    """Start the continuous paper-trading loop.

    The runner is what turns the paper engine from something a request drives
    into something that runs: without it, a deployment marked RUNNING evaluates
    nothing and fills nothing, and the dashboard shows a live strategy that is
    actually inert.

    Best-effort, never fatal. A runner that cannot start must not take the API
    down with it — the deployment routes and the health endpoint are how an
    operator finds out what is wrong, and ``GET /api/v1/paper/runner`` reports
    ``running: false`` rather than pretending the loop is up.
    """
    try:
        from atr.services.runner import get_runner

        get_runner().start()
        logger.info("paper runner started")
    except Exception as exc:  # noqa: BLE001 — the API must still serve
        logger.warning("paper runner could not start: %s", exc)


def _warm_instrument_master() -> None:
    """Build the instrument index off the request path.

    A cold build reads 3,000+ parquet footers and takes ~8s. Paying that on the
    first watchlist or search request would look like a hung page, so it is paid
    at startup on a daemon thread — the same pattern as the breadth cache below.
    """
    try:
        from atr.instruments.service import get_instrument_master

        get_instrument_master().warm()
    except Exception as exc:  # noqa: BLE001 — best effort, never fatal
        logger.warning("instrument master warm-up could not start: %s", exc)


def _warm_breadth_cache() -> None:
    """Compute the 5-session breadth series once, off the request path.

    Scoring a universe sample five times takes ~15s. Paying that on the first
    dashboard poll would look like a hung page, so it is paid at startup on a
    daemon thread instead — by the time a browser connects, the answer is
    usually already cached.
    """
    import threading

    def work() -> None:
        try:
            import time as _t

            t0 = _t.monotonic()
            _breadth_trend("NSEEQ")
            logger.info("breadth cache warmed in %.1fs", _t.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 — best effort, never fatal
            logger.warning("breadth warm-up failed: %s", exc)

    threading.Thread(target=work, daemon=True, name="atr-breadth-warm").start()


# Request identity, security headers, CSRF and rate limiting. Registered *before*
# CORS so that CORS ends up outermost — Starlette wraps in reverse registration
# order, and a 429 or 403 produced by our middleware must still carry CORS
# headers or the browser reports a network failure instead of the real reason.
from atr.api.middleware import install_middleware  # noqa: E402

install_middleware(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Versioned API surface. Registered here, before the SPA catch-all further down,
# because FastAPI matches routes in registration order — a catch-all declared
# first would swallow every /api/v1 path.
from atr.api.deps import get_principal, require_permission  # noqa: E402
from atr.auth.models import Principal  # noqa: E402
from atr.auth.rbac import Permission  # noqa: E402
from atr.api.routers import include_routers  # noqa: E402

include_routers(app)


class HealthResponse(BaseModel):
    status: str
    env: str
    database: bool
    session_active: bool
    # The environment banner reads these. They live on /health rather than a
    # separate call so the banner can never disagree with the rest of the
    # header, and so it refreshes on the existing poll.
    execution_mode: str = "paper"
    kill_switch: bool = False


class BacktestRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT"])
    initial_cash: float = 1_000_000.0
    fast: int = 20
    slow: int = 50
    slippage_bps: float = 5.0
    square_off_eod: bool = False
    futures: bool = False
    #: ``synthetic`` generates random walks (fast, but meaningless as evidence);
    #: ``history`` replays the real cached NSE dailies.
    source: str = "synthetic"
    exchange: str = "NSEEQ"


class OrderRequest(BaseModel):
    symbol: str
    exchange: str = "NSEEQ"
    quantity: int  # signed: negative = sell
    order_type: str = "MARKET"
    price: float | None = None
    product: str | None = None
    tag: str | None = None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    database = _db_health()
    session_active = False
    try:
        from atr.brokers.iifl.auth import SessionStore

        session_active = SessionStore(settings.iifl_session_cache).load() is not None
    except Exception as exc:
        logger.warning("session check failed: %s", exc)
        session_active = False

    return HealthResponse(
        status="ok",
        env=settings.env,
        database=database,
        session_active=session_active,
        execution_mode=str(_risk_state().get("execution_mode", "paper")),
        kill_switch=bool(_risk_state().get("kill_switch")),
    )


_DB_CHECK: dict[str, Any] = {"at": 0.0, "ok": False, "probing": False}


def _db_health(ttl: float = 60.0) -> bool:
    """Optional-Postgres check that never blocks the request thread.

    The probe itself is slow when no database is running: ``localhost``
    resolves to both ::1 and 127.0.0.1, so a dead Postgres burns the full
    connect timeout twice (~4s). The dashboard polls ``/health`` every 15s, so
    doing that inline made the whole UI feel broken. The check now runs on a
    daemon thread and callers get the last known answer immediately.
    """
    import threading
    import time as _time

    now = _time.monotonic()
    if now - float(_DB_CHECK["at"]) >= ttl and not _DB_CHECK["probing"]:
        _DB_CHECK["probing"] = True

        def probe() -> None:
            ok = False
            try:
                from atr.data.store import Database

                ok = Database().health()
            except Exception:  # noqa: BLE001 - DB is optional
                ok = False
            _DB_CHECK["ok"] = ok
            _DB_CHECK["at"] = _time.monotonic()
            _DB_CHECK["probing"] = False

        threading.Thread(target=probe, daemon=True, name="atr-db-probe").start()

    return bool(_DB_CHECK["ok"])


@app.post("/backtest", dependencies=[Depends(get_principal)])
def run_backtest(request: BacktestRequest) -> dict[str, Any]:
    """Score one configuration on one dataset.

    In-sample by construction: the parameters you pass were chosen by you,
    usually after seeing a result like this one. Use ``/research`` for the
    number you would actually act on.
    """
    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig, BacktestEngine
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.strategy.strategies import STRATEGIES

    strategy_cls = STRATEGIES.get(request.strategy)
    if strategy_cls is None:
        raise HTTPException(400, f"unknown strategy: {request.strategy}")

    symbols = [s.strip().upper() for s in request.symbols if s.strip()]
    if request.source == "history":
        if not symbols:
            raise HTTPException(400, "pass `symbols` when source=history")
        feed, used = _history_feed(symbols, request.exchange.upper())
    else:
        used = symbols or ["AAPL", "MSFT"]
        feed = SyntheticFeed(
            SyntheticConfig(
                symbols=tuple(used),
                start=datetime(2024, 1, 1, 9, 30),
                end=datetime(2024, 6, 28, 15, 59),
            ),
            futures=request.futures,
        )

    strategy = (
        strategy_cls(fast=request.fast, slow=request.slow)
        if request.strategy == "sma_crossover"
        else strategy_cls()
    )
    config = BacktestConfig(
        initial_cash=request.initial_cash,
        slippage=SlippageModel(bps=request.slippage_bps),
        square_off_eod=request.square_off_eod,
    )
    result = BacktestEngine(feed, strategy, config).run()
    return _clean(
        {
            "strategy": result.strategy_name,
            "source": request.source,
            "symbols": used,
            "metrics": result.metrics.as_dict(),
            "num_fills": int(len(result.fills)),
            "num_trades": int(len(result.trades)),
            "killed": result.killed,
            "kill_reason": result.kill_reason,
            "equity": _equity_points(result.equity),
            "trades": result.trades.head(200).to_dict(orient="records"),
        }
    )


@app.get("/positions")
def positions() -> list[dict[str, Any]]:
    broker = _live_broker()
    return [
        {
            "symbol": p.instrument.symbol,
            "exchange": p.instrument.exchange,
            "quantity": p.quantity,
            "avg_price": p.avg_price,
            "last_price": p.last_price,
            "unrealized_pnl": p.unrealized_pnl,
            "realized_pnl": p.realized_pnl,
        }
        for p in broker.positions()
    ]


@app.post("/orders")
def place_order(request: OrderRequest, http_request: Request) -> dict[str, Any]:
    """Place a manual order.

    Rewired onto the OMS on 2026-09-14. Before this, the route called
    ``broker.place_order()`` directly: no risk check, no idempotency, no event log,
    and — because it built the ``Order`` itself — no single place where an order's
    shape was decided. It now goes through the same
    :class:`~atr.services.execution.ExecutionService` as every other path.

    **Requires a platform account.** This is a deliberate tightening. An order
    needs an owner (``orders.user_id`` is a non-null foreign key), and an
    unattributed order is one nobody can be asked about. Read-only legacy routes
    stay open; the routes that can move money now require the caller to be
    identified, which is also what makes the per-account kill switch meaningful.
    """
    from atr.execution.oms import OrderDraft
    from atr.services.execution import (
        BrokerPortfolio,
        ExecutionService,
        VenueError,
        iifl_venue,
    )
    from atr.services.orders import get_order_service

    principal = _order_principal(http_request)
    _require_live_execution("Manual order")

    broker = _live_broker()
    side = "BUY" if request.quantity > 0 else "SELL"
    draft = OrderDraft(
        user_id=principal.user_id,
        symbol=request.symbol,
        side=side,
        quantity=abs(request.quantity),
        mode="LIVE",
        exchange=request.exchange,
        order_type=request.order_type,
        limit_price=request.price,
        product=request.product,
        requested_price=request.price,
        tag=request.tag,
    )
    service = ExecutionService(
        orders=get_order_service(
            portfolio=BrokerPortfolio(broker), instruments=None
        ),
        venue=iifl_venue(broker),
    )
    try:
        placed = service.place(draft)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(502, str(exc)) from exc

    _append_audit(
        actor=principal.username,
        action="order.place",
        subject=f"{side} {abs(request.quantity)} {request.symbol}",
        detail=f"{request.order_type} @ {request.price or 'MKT'} -> {placed.status}",
    )
    return {
        "order_id": placed.order_id,
        "broker_order_id": placed.broker_order_id,
        "status": placed.status,
        "reject_reason": placed.reject_reason,
    }


@app.post("/risk/kill-switch")
def kill_switch(
    engaged: bool = True,
    reason: str = "",
    principal: Principal = Depends(require_permission(Permission.RISK_CONFIGURE)),
) -> dict[str, Any]:
    """Engage or release the global kill switch. Blocks all new orders until cleared.

    Rewired onto the durable state on 2026-09-14. It used to write an in-process
    dict, which had two consequences: a restart silently re-armed trading, and
    there was no single answer to "is the switch engaged?" because each route read
    the dict itself. The switch is now stored in ``system_state`` and read through
    ``RiskStateService``, which is also where the OMS risk gate gets it — so it
    applies to every order path rather than to the routes that remembered to ask.

    A reason is now required in **both** directions. Releasing is arguably the more
    consequential of the two: it re-enables trading, and a release with no recorded
    reason is indistinguishable from someone clearing it by accident.
    """
    from atr.services.risk import RiskStateError, RiskStateService

    # The actor is who authenticated, never a query parameter: a client-supplied
    # name would let anyone write any identity into the audit trail.
    actor = principal.username

    if not reason.strip():
        raise HTTPException(
            400,
            detail={
                "detail": (
                    "changing the kill switch requires a reason — it is recorded in "
                    "the audit trail"
                ),
                "code": "reason_required",
            },
        )
    try:
        state = RiskStateService().set_kill_switch(engaged, reason=reason, actor=actor)
    except RiskStateError as exc:
        raise HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code}) from exc

    _append_audit(
        actor=actor,
        action="kill_switch.engage" if engaged else "kill_switch.release",
        subject="global",
        detail=reason.strip(),
    )
    return {"kill_switch": state.kill_switch, "reason": reason.strip()}


@app.get("/risk/execution-mode")
def get_execution_mode() -> dict[str, Any]:
    """Whether orders reach the broker, or are only recorded.

    `paper` is the safe default: signals are generated and graded exactly as in
    live, the order path is exercised up to the broker boundary, but nothing is
    transmitted. This is enforced in the execution service, not merely shown here
    — a toggle that only changes a label is worse than none, because it invites
    you to trust it.
    """
    return _risk_state()


@app.post("/risk/execution-mode")
def set_execution_mode(
    mode: str,
    reason: str = "",
    principal: Principal = Depends(require_permission(Permission.EXECUTION_MODE_CHANGE)),
) -> dict[str, Any]:
    """Switch between `paper` and `live`.

    Going *live* requires an explicit reason. That is deliberate friction: the
    transition that can lose real money should cost a sentence, and the
    sentence is what shows up in the audit trail later. Coming back to paper does
    not require one — reducing risk must never be harder than taking it on.
    """
    from atr.services.risk import RiskStateError, RiskStateService

    actor = principal.username

    if mode not in {"paper", "live"}:
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    if mode == "live" and not reason.strip():
        raise HTTPException(
            400,
            detail={
                "detail": (
                    "Switching to live requires a reason — it is recorded in the "
                    "audit trail"
                ),
                "code": "reason_required",
            },
        )

    previous = _risk_state()["mode"]
    try:
        RiskStateService().set_execution_mode(mode, reason=reason, actor=actor)
    except RiskStateError as exc:
        raise HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code}) from exc

    _append_audit(
        actor=actor,
        action="execution_mode.change",
        subject=f"{previous} -> {mode}",
        detail=reason.strip() or None,
    )
    logger.warning(
        "execution mode %s -> %s by %s (%s)", previous, mode, actor, reason.strip() or "no reason"
    )
    return _risk_state()


@app.get("/risk/status")
def risk_status() -> dict[str, Any]:
    """Kill switch plus the limits that actually gate order placement.

    Reads the *live* settings rather than restating a config default, so the
    panel cannot drift from what the engine enforces — a risk panel showing a
    stale limit is worse than no panel.
    """
    from atr.trade_signals import load_settings

    state = _risk_state()
    settings = get_settings()
    ts = load_settings()

    # Broker-reported margin is best-effort: a dead session must not blank the
    # panel, because the kill switch is exactly what you reach for when things
    # are broken.
    margin: dict[str, Any] = {}
    margin_error: str | None = None
    try:
        client = _authed_client()
        try:
            row = _broker_rows(client.limits())
            row = row[0] if row else {}
            for key in (
                "availableMargin", "marginUtilized", "collateralValue",
                "openingCashLimit", "intradayPayinAmount", "creditForSellAmount",
                "blockedForPayoutAmount", "utilizedAmount", "net",
            ):
                if key in row:
                    margin[key] = row[key]
        finally:
            with contextlib.suppress(Exception):
                client.close()
    except Exception as exc:  # noqa: BLE001 — report, don't fail the panel
        margin_error = str(exc)

    return {
        "kill_switch": bool(state.get("kill_switch")),
        "execution_mode": str(state.get("execution_mode", "paper")),
        "env": settings.env,
        "live_orders_allowed": settings.env in {"paper", "live"},
        "limits": {
            "capital": ts.capital,
            "risk_per_trade_pct": ts.risk_per_trade_pct,
            "max_active": ts.max_active,
            "rr_ratio": ts.rr_ratio,
            "stop_method": ts.stop_method,
            "stop_atr_mult": ts.stop_atr_mult,
            "stop_pct": ts.stop_pct,
            "product": ts.product,
        },
        "margin": margin,
        "margin_error": margin_error,
    }


# ---------------------------------------------------------------------------
# Semi-automatic trade signals
# ---------------------------------------------------------------------------
# Self-learning quantitative engine
# ---------------------------------------------------------------------------

@app.get("/self-learning/status")
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


@app.post("/self-learning/train", dependencies=[Depends(get_principal)])
def self_learning_train() -> dict[str, Any]:
    """Triggers an online learning cycle across historical data."""
    from atr.research.self_learning import get_self_learning_engine
    engine = get_self_learning_engine()
    return engine.train_on_history()


@app.get("/trade-signals/settings")
def trade_signals_settings_get() -> dict[str, Any]:
    """Return current position sizing + stop-loss configuration."""
    from atr.trade_signals import load_settings
    return load_settings().model_dump()


@app.put(
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


@app.get("/trade-signals")
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


@app.post("/trade-signals/scan", dependencies=[Depends(get_principal)])
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
        from atr.api.main import _alert_store
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


@app.post("/trade-signals/{signal_id}/execute")
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


@app.post("/trade-signals/{signal_id}/skip", dependencies=[Depends(get_principal)])
def trade_signals_skip(signal_id: str) -> dict[str, Any]:
    """Dismiss a pending signal without trading."""
    from atr.trade_signals import get_queue
    q = get_queue()
    if not q.skip(signal_id):
        raise HTTPException(404, f"Signal {signal_id} not found")
    return {"skipped": signal_id}


class AlertRuleIn(BaseModel):
    name: str = ""
    symbol: str
    exchange: str = "NSEEQ"
    kind: str = "price_below"
    threshold: float = 0.0
    cooldown_min: int = 60
    armed: bool = True


def _alert_store():
    from atr.alerts.store import AlertStore

    return AlertStore()


@app.get("/alerts/rules")
def alert_rules() -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in _alert_store().rules()]


@app.post("/alerts/rules", dependencies=[Depends(get_principal)])
def alert_create(body: AlertRuleIn) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    return _alert_store().upsert(AlertRule(**body.model_dump())).model_dump(mode="json")


@app.patch("/alerts/rules/{rule_id}", dependencies=[Depends(get_principal)])
def alert_arm(rule_id: str, armed: bool = True) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    store = _alert_store()
    for rule in store.rules():
        if rule.id == rule_id:
            updated = AlertRule(**{**rule.model_dump(), "armed": armed})
            return store.upsert(updated).model_dump(mode="json")
    raise HTTPException(404, f"no such rule: {rule_id}")


@app.delete("/alerts/rules/{rule_id}", dependencies=[Depends(get_principal)])
def alert_delete(rule_id: str) -> dict[str, bool]:
    if not _alert_store().remove(rule_id):
        raise HTTPException(404, f"no such rule: {rule_id}")
    return {"deleted": True}


@app.get("/alerts/events")
def alert_events(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _alert_store().events(limit)]


@app.post("/alerts/check", dependencies=[Depends(get_principal)])
def alert_check() -> dict[str, Any]:
    """Evaluate all armed rules right now. Returns what fired."""
    from atr.alerts.channels import channels_from_settings
    from atr.alerts.engine import check, market_open_now

    settings = get_settings()
    events = check(_alert_store(), _authed_client(), channels_from_settings(settings))
    return {
        "market_open": market_open_now(),
        "fired": [e.model_dump(mode="json") for e in events],
    }


@app.post("/alerts/test", dependencies=[Depends(get_principal)])
def alert_test() -> dict[str, Any]:
    """Send 'ATR test' down the channel chain. Verifies Telegram/SMS setup."""
    from atr.alerts.channels import channels_from_settings

    settings = get_settings()
    sent_on = "none"
    for ch in channels_from_settings(settings):
        if ch.send("ATR test", "alerts are wired — you will get firing rules here."):
            sent_on = ch.name
            break
    return {"sent_on": sent_on}


# ----------------------------------------------------------------------
# Intelligent Automated Buy & Sell Alerts
# ----------------------------------------------------------------------
@app.get("/alerts/intelligent/config")
def get_intelligent_alert_config() -> dict[str, Any]:
    from atr.alerts.intelligent import get_intelligent_monitor, load_intelligent_config
    cfg = load_intelligent_config()
    status = get_intelligent_monitor().get_status()
    return {
        "config": cfg.model_dump(mode="json"),
        "status": status,
    }


@app.post("/alerts/intelligent/config", dependencies=[Depends(get_principal)])
def update_intelligent_alert_config(body: dict[str, Any]) -> dict[str, Any]:
    from atr.alerts.intelligent import (
        IntelligentAlertConfig,
        get_intelligent_monitor,
        load_intelligent_config,
        save_intelligent_config,
    )
    current = load_intelligent_config().model_dump()
    current.update(body)
    new_cfg = save_intelligent_config(IntelligentAlertConfig(**current))
    return {
        "config": new_cfg.model_dump(mode="json"),
        "status": get_intelligent_monitor().get_status(),
    }


@app.post("/alerts/intelligent/evaluate", dependencies=[Depends(get_principal)])
async def evaluate_intelligent_alerts_now() -> dict[str, Any]:
    """Force an immediate evaluation cycle across target stocks right now."""
    from atr.alerts.intelligent import get_intelligent_monitor
    monitor = get_intelligent_monitor()
    signals = await monitor.run_evaluation_cycle(force=True)
    return {
        "signals": [s.model_dump(mode="json") for s in signals],
        "count": len(signals),
        "as_of": datetime.now().isoformat(),
        "status": monitor.get_status(),
    }


class BriefingIn(BaseModel):
    top_n: int = 8
    avoid_n: int = 5
    min_price: float = 50.0
    min_day_value_lakh: float = 50.0
    min_atr_pct: float = 0.5
    min_bars: int = 60
    universe: str = "all"
    watchlist: list[str] = []
    ranking: str = "vs_high"
    send_enabled: bool = True


@app.get("/briefing/config")
def briefing_config() -> dict[str, Any]:
    from atr.briefing import last_sent, load_config

    return {"config": load_config().model_dump(), "last_sent": last_sent()}


@app.put("/briefing/config", dependencies=[Depends(get_principal)])
def briefing_save(body: BriefingIn) -> dict[str, Any]:
    from atr.briefing import BriefingConfig, save_config

    data = body.model_dump()
    if not data["watchlist"]:
        from atr.scanner import UNIVERSE
        data["watchlist"] = list(UNIVERSE)
    return save_config(BriefingConfig(**data)).model_dump()


@app.post("/briefing/preview", dependencies=[Depends(get_principal)])
def briefing_preview() -> dict[str, Any]:
    from atr.briefing import build_brief, load_config

    message, stats = build_brief(load_config())
    return {"message": message, "stats": stats}


@app.post("/briefing/send", dependencies=[Depends(get_principal)])
def briefing_send() -> dict[str, Any]:
    from atr.alerts.channels import channels_from_settings
    from atr.briefing import build_brief, load_config, record_sent

    cfg = load_config()
    message, stats = build_brief(cfg)
    if not cfg.send_enabled:
        return {"sent_on": "disabled", "stats": stats}
    for ch in channels_from_settings(get_settings()):
        if ch.send("ATR morning brief", message):
            record_sent(message, ch.name)
            return {"sent_on": ch.name, "stats": stats}
    return {"sent_on": "none", "stats": stats}


# ----------------------------------------------------------------------
def _risk_state() -> dict[str, Any]:
    """The kill switch and execution mode, read from the durable store.

    This used to return a module-level dict. Two things were wrong with that: a
    restart silently re-armed trading and dropped the platform back to paper, and
    there was no single answer to "is the kill switch engaged?" because every
    route read the dict itself — the manual order route never asked at all.

    It is now a *view* over ``atr.services.risk.RiskStateService``, which is the
    same state the OMS risk gate reads. One source of truth, so the switch applies
    everywhere rather than to the routes that remembered.

    A read failure returns the safe answer (paper, switch engaged = False is
    deliberately *not* the fallback for the switch — see below).
    """
    from atr.services.risk import RiskStateService

    try:
        snapshot = RiskStateService().snapshot()
    except Exception:  # noqa: BLE001 - an unreadable store must not 500 the dashboard
        logger.exception("could not read the risk state; reporting the safe default")
        # Paper, because an unreadable mode must never read as "live". The kill
        # switch defaults to False because `_require_live_execution` already
        # refuses to transmit in paper mode, so nothing can be sent anyway — and
        # reporting it as engaged would misrepresent what an operator did.
        return {"mode": "paper", "live": False, "paper": True,
                "changed_at": None, "changed_by": None, "reason": None}
    return {
        "mode": snapshot.execution_mode,
        "live": snapshot.live,
        "paper": not snapshot.live,
        "changed_at": snapshot.changed_at,
        "changed_by": snapshot.changed_by,
        "reason": snapshot.reason,
        # Legacy key names kept so existing readers and the dashboard do not break.
        "execution_mode": snapshot.execution_mode,
        "kill_switch": snapshot.kill_switch,
        "mode_changed_at": snapshot.changed_at,
        "mode_changed_by": snapshot.changed_by,
        "mode_reason": snapshot.reason,
    }


def _kill_switch_engaged() -> bool:
    """Whether the global kill switch is on. Read through the durable store."""
    return bool(_risk_state().get("kill_switch", False))

# Append-only audit log. Every action that changes what the system will do to
# real money lands here, with who and why. Kept as a file rather than in-memory
# state so a restart cannot erase an inconvenient decision.
_AUDIT_PATH = Path("data/audit/audit.jsonl")


def _utcnow_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _append_audit(*, actor: str, action: str, subject: str, detail: str | None = None) -> dict[str, Any]:
    """Append one immutable record. Never rewrites or deletes existing lines."""
    entry = {
        "ts": _utcnow_iso(),
        "actor": actor,
        "action": action,
        "subject": subject,
        "detail": detail,
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        logger.warning("audit append failed: %s", exc)
    return entry


def _read_audit(limit: int = 200) -> list[dict[str, Any]]:
    if not _AUDIT_PATH.exists():
        return []
    limit = max(1, limit)  # `lines[-0:]` is the whole file, not zero lines
    try:
        with _AUDIT_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # Read only the tail: the trail is append-only and grows forever, so
            # reading it all per request is O(history). ~1 KiB/entry is generous.
            fh.seek(max(0, size - limit * 1024 - 1024))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


@app.get("/audit")
def get_audit(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    """The immutable trail, newest first."""
    return {"entries": _read_audit(limit), "path": str(_AUDIT_PATH)}



def _authed_client():
    """IIFL client with a restored session. Works in any env — market data
    and other read APIs don't need paper/live mode (only order placement
    goes through the `_live_broker` gate below).

    Delegates to `atr.services.broker_access.authed_client`, which is the one place
    that knows how to build and authenticate a client.
    """
    from atr.services.broker_access import BrokerUnavailable, authed_client

    try:
        return authed_client()
    except BrokerUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.get("/scan")
def scan(symbols: str | None = None) -> dict[str, Any]:
    """Momentum scan over liquid NSE names (real IIFL daily candles).

    Pass `?symbols=RELIANCE-EQ,INFY-EQ` to override the default universe.
    Takes ~30-60s for the full universe — the dashboard shows a spinner.
    """
    from datetime import date

    from atr.scanner import UNIVERSE, run_scan

    client = _authed_client()
    wanted = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else UNIVERSE
    )
    rows, errors = run_scan(client, wanted)
    return {"as_of": date.today().isoformat(), "rows": rows, "errors": errors}


_SCAN_CACHE: dict[str, Any] = {"data": None, "as_of": 0.0, "exchange": ""}


@app.get("/scan-all")
def scan_all(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Full-market scan over the local history cache (`atr history sync`).

    No broker session needed and no API calls — scores 2000+ names in
    seconds. Caches results in-memory for 120 seconds to make UI tab
    switches instant.
    """
    import time
    from datetime import date

    import pandas as pd

    from atr.data.history import load_cached
    from atr.scanner import score_frame

    now = time.monotonic()
    ex = exchange.upper()
    if (
        _SCAN_CACHE["data"] is not None
        and _SCAN_CACHE["exchange"] == ex
        and now - float(_SCAN_CACHE["as_of"]) < 120.0
    ):
        return _SCAN_CACHE["data"]

    frames = load_cached(ex)
    if not frames:
        raise HTTPException(503, "history cache is empty — run `atr history sync`")
    rows = []
    for symbol, df in frames.items():
        try:
            rows.append(score_frame(symbol, df))
        except Exception:  # noqa: BLE001 — thin/odd histories just don't rank
            continue
    scan = pd.DataFrame(rows).sort_values("score", ascending=False)
    up = int((scan["trend"] == "UP").sum())
    result = {
        "as_of": date.today().isoformat(),
        "universe": len(frames),
        "scored": len(scan),
        "breadth_up": up,
        "rows": scan.to_dict(orient="records"),
    }
    _SCAN_CACHE["data"] = result
    _SCAN_CACHE["as_of"] = now
    _SCAN_CACHE["exchange"] = ex
    return result


# ---------------------------------------------------------------------------
# Custom condition-based scanner
# ---------------------------------------------------------------------------

class CustomCondition(BaseModel):
    indicator: str                      # "rsi", "sma", "close", etc.
    period: int | None = None           # period for sma/ema/rsi/atr/bb_*
    op: str                             # ">", "<", ">=", "<=", "=", "crosses_above", "crosses_below"
    rhs_type: str = "value"             # "value" | "indicator"
    rhs_value: float = 0.0             # used when rhs_type == "value"
    rhs_indicator: str | None = None   # used when rhs_type == "indicator"
    rhs_period: int | None = None      # used when rhs_type == "indicator"


class CustomScanRequest(BaseModel):
    conditions: list[CustomCondition] = Field(default_factory=list)
    combine: str = "AND"               # "AND" | "OR"
    exchange: str = "NSEEQ"
    universe: str = "all"              # "all" | "watchlist"
    watchlist: list[str] = Field(default_factory=list)


@app.post("/scanner/custom", dependencies=[Depends(get_principal)])
def scanner_custom(body: CustomScanRequest) -> dict[str, Any]:
    """Evaluate user-defined indicator conditions over the local Parquet cache.

    No broker session required — runs entirely against the cached dailies.
    Returns matching symbols with standard score metrics + condition values.
    """
    import time
    from datetime import date

    from atr.data.history import load_cached
    from atr.scanner import UNIVERSE
    from atr.scanner_custom import run_custom_scan

    if not body.conditions:
        raise HTTPException(400, "provide at least one condition")

    ex = body.exchange.upper()
    t0 = time.monotonic()

    # Load frames — for "watchlist" mode only load the requested symbols
    if body.universe == "watchlist":
        syms = body.watchlist or list(UNIVERSE)
        frames = load_cached(ex, symbols=syms)
    else:
        frames = load_cached(ex)

    if not frames:
        raise HTTPException(503, "history cache empty — run `atr history sync`")

    conds = [c.model_dump() for c in body.conditions]
    results = run_custom_scan(frames, conds, combine=body.combine)

    elapsed = round(time.monotonic() - t0, 2)
    return {
        "as_of": date.today().isoformat(),
        "universe_size": len(frames),
        "matched": len(results),
        "elapsed_s": elapsed,
        "rows": results,
    }


# Saved scans — stored in data/scans/custom.json
_SCANS_PATH = Path("data/scans/custom.json")

_DEFAULT_SCANS: list[dict] = [
    {
        "id": "oversold_uptrend",
        "name": "Oversold in Uptrend",
        "combine": "AND",
        "conditions": [
            {"indicator": "rsi", "period": 14, "op": "<", "rhs_type": "value", "rhs_value": 35},
            {"indicator": "close", "op": ">", "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 50},
        ],
    },
    {
        "id": "fresh_breakout",
        "name": "Fresh Breakout",
        "combine": "AND",
        "conditions": [
            {"indicator": "vs_high", "op": ">", "rhs_type": "value", "rhs_value": -3},
            {"indicator": "vol_x", "op": ">", "rhs_type": "value", "rhs_value": 2.0},
        ],
    },
    {
        "id": "golden_cross",
        "name": "Golden Cross (recent)",
        "combine": "AND",
        "conditions": [
            {"indicator": "sma", "period": 20, "op": "crosses_above",
             "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 50},
        ],
    },
    {
        "id": "rsi_momentum",
        "name": "RSI Momentum Zone",
        "combine": "AND",
        "conditions": [
            {"indicator": "rsi", "period": 14, "op": ">=", "rhs_type": "value", "rhs_value": 55},
            {"indicator": "rsi", "period": 14, "op": "<=", "rhs_type": "value", "rhs_value": 70},
            {"indicator": "close", "op": ">", "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 20},
        ],
    },
    {
        "id": "near_52w_high",
        "name": "Near 52-week High",
        "combine": "AND",
        "conditions": [
            {"indicator": "vs_high", "op": ">", "rhs_type": "value", "rhs_value": -5},
            {"indicator": "vol_x", "op": ">", "rhs_type": "value", "rhs_value": 1.5},
        ],
    },
]


def _load_scans() -> list[dict]:
    try:
        import json
        _SCANS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _SCANS_PATH.exists():
            return json.loads(_SCANS_PATH.read_text())
    except Exception:  # noqa: BLE001
        pass
    return list(_DEFAULT_SCANS)


def _save_scans(scans: list[dict]) -> None:
    import json
    _SCANS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SCANS_PATH.write_text(json.dumps(scans, indent=2))


@app.get("/scanner/saved")
def scanner_saved_list() -> dict[str, Any]:
    """List all saved custom scans (includes built-in presets on first run)."""
    scans = _load_scans()
    if not _SCANS_PATH.exists():
        _save_scans(scans)
    return {"scans": scans}


class SavedScanUpsert(BaseModel):
    id: str
    name: str
    combine: str = "AND"
    conditions: list[dict[str, Any]] = Field(default_factory=list)


@app.put("/scanner/saved/{scan_id}", dependencies=[Depends(get_principal)])
def scanner_saved_upsert(scan_id: str, body: SavedScanUpsert) -> dict[str, Any]:
    """Create or overwrite a saved scan."""
    scans = _load_scans()
    entry = body.model_dump()
    entry["id"] = scan_id
    scans = [s for s in scans if s["id"] != scan_id]
    scans.append(entry)
    _save_scans(scans)
    return {"saved": entry}


@app.delete("/scanner/saved/{scan_id}", dependencies=[Depends(get_principal)])
def scanner_saved_delete(scan_id: str) -> dict[str, Any]:
    """Delete a saved scan by id."""
    scans = [s for s in _load_scans() if s["id"] != scan_id]
    _save_scans(scans)
    return {"deleted": scan_id}



@app.get("/candles")
def candles(
    symbol: str = "RELIANCE-EQ",
    exchange: str = "NSEEQ",
    interval: str = "1d",
    from_date: str = "01-Mar-2026",
    to_date: str | None = None,
) -> dict[str, Any]:
    """Raw OHLCV candles for charting. Interval accepts 1m/5m/15m/30m/60m/1d."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    client = _authed_client()
    master = InstrumentMaster(client)
    master.load_cached([exchange.upper()])
    conid = resolve_conid(master, symbol.upper(), exchange.upper())
    raw = client.historical_data(exchange.upper(), conid, interval, from_date,
                                 to_date or date.today().strftime("%d-%b-%Y"))
    out = [
        {"ts": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in raw["result"][0]["candles"]
    ]
    return {"symbol": symbol.upper(), "exchange": exchange.upper(),
            "interval": interval, "candles": out}


_SYMBOL_MASTERS: dict[str, tuple[float, Any]] = {}
_SYMBOL_MASTER_TTL_S = 300.0


def _symbol_master(exchange: str) -> Any:
    """The instrument master for symbol search, loaded once per few minutes.

    Search fires on every keystroke; rebuilding a ~10k-row master from disk each
    time made the box lag. A five-minute TTL still picks up a fresh sync.
    """
    from atr.brokers.iifl.contracts import InstrumentMaster

    now = time.monotonic()
    hit = _SYMBOL_MASTERS.get(exchange)
    if hit and now - hit[0] < _SYMBOL_MASTER_TTL_S:
        return hit[1]
    master = InstrumentMaster()
    try:
        master.load_cached([exchange])
    except Exception as exc:  # noqa: BLE001 — cold cache, no client to refresh with
        raise HTTPException(503, f"instrument cache is cold — run `atr instruments sync` ({exc})")
    _SYMBOL_MASTERS[exchange] = (now, master)
    return master


@app.get("/symbols")
def symbols(query: str = "", exchange: str = "NSEEQ", limit: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    """Symbol search for the chart header. Served from the cached instrument
    master — no broker session needed."""
    master = _symbol_master(exchange.upper())
    hits = master.search(query.upper() or "EQ", exchange=exchange.upper(), limit=limit)
    return {
        "results": [
            {"symbol": r.symbol, "exchange": r.exchange, "conid": str(r.conid)}
            for r in hits.itertuples()
        ]
    }


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """Make numpy/pandas output JSON-safe.

    ``NaN`` and ``inf`` are not valid JSON, and the metrics dataclasses emit
    both (profit factor with no losses is ``inf``, an untraded fold is ``nan``).
    Starlette would happily write the literal ``NaN`` token, which ``JSON.parse``
    rejects — so every payload crossing this boundary goes through here.
    """
    import math

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar (np.int64 is not an int)
        return _clean(value.item())
    return value


def _equity_points(series: pd.Series | None, max_points: int = 900) -> list[dict[str, Any]]:
    """Downsample an equity curve for the wire without changing its shape."""
    if series is None or len(series) == 0:
        return []
    stride = max(1, len(series) // max_points)
    return [
        {"ts": pd.Timestamp(ts).isoformat(), "value": float(v)}
        for ts, v in zip(series.index[::stride], series.to_numpy()[::stride], strict=True)
    ]


def _broker_rows(payload: Any) -> list[dict[str, Any]]:
    """IIFL wraps list responses in ``{"result": [...]}``; tolerate either.

    Reused from the CLI rather than reimplemented so the dashboard and the
    terminal cannot disagree about what a portfolio section contains.
    """
    from atr.cli import _rows

    return _rows(payload)


#: IIFL reports "there is nothing here" as an error too — e.g. EC926
#: "No Trade's are found for this user". Those are empty results, not failures.
_EMPTY_STATES = ("no trade", "no holding", "no position", "no order", "no record")

_EGRESS: dict[str, Any] = {"ip": None, "at": 0.0}


def _egress_ip(ttl: float = 600.0) -> str | None:
    """The public IPv4 this host egresses as, for diagnosing the IP whitelist.

    Fetched lazily and cached — it only runs once IIFL has already rejected us
    for an IP reason, so it costs nothing on the happy path.

    Uses a **dual-stack** endpoint with the same IPv4 pinning as `IiflClient`.
    An IPv4-only service would cheerfully report the IPv4 while the request
    itself left over IPv6, which is the exact confusion that hid this bug the
    first time round.
    """
    import time as _time

    now = _time.monotonic()
    if _EGRESS["ip"] and now - float(_EGRESS["at"]) < ttl:
        return _EGRESS["ip"]
    try:
        import httpx

        with httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"), timeout=8
        ) as http:
            ip = http.get("https://api64.ipify.org").text.strip()
        if ip:
            _EGRESS["ip"] = ip
            _EGRESS["at"] = now
    except Exception:  # noqa: BLE001 - a diagnostic must never mask the real error
        pass
    return _EGRESS["ip"]


def _with_ip_hint(message: str) -> str:
    """Turn IIFL's opaque IP rejection into something you can act on.

    The whitelisted address is a SEBI requirement and the connection here is a
    dynamic consumer line, so this recurs whenever the ISP re-leases. Saying
    which address to register beats re-deriving it every time.
    """
    if "ip address not authorized" not in message.lower():
        return message
    ip = _egress_ip()
    if not ip:
        return message
    return (
        f"{message} This machine currently egresses as {ip} — register that "
        f"address at developers.iiflcapital.com (My Apps → View All Details → "
        f"Primary Static IP)."
    )


def _empty_state(node: Any) -> bool:
    """True for IIFL's 'nothing here' rows, which arrive carrying an error status."""
    if not isinstance(node, dict):
        return False
    message = str(node.get("message") or "").lower()
    return any(hint in message for hint in _EMPTY_STATES)


def _broker_error(payload: Any) -> str | None:
    """IIFL signals failure with a status field, it does not raise.

    The catch is that failures are nested. A rejected call comes back as::

        {"status": "Ok", "message": "Success",
         "result": [{"status": "EC500",
                     "message": "Error : IP address not authorized for trading."}]}

    The outer envelope reports Ok no matter what happened, so checking only the
    top level misses every real failure — which is exactly what happened here,
    and made a wall of failing calls look like a healthy book. Check both.
    """
    if not isinstance(payload, dict):
        return None

    def failure(node: Any) -> str | None:
        if not isinstance(node, dict):
            return None
        status = node.get("status")
        if isinstance(status, str) and status.strip().lower() not in ("ok", "success"):
            message = str(node.get("message") or status).strip()
            if any(hint in message.lower() for hint in _EMPTY_STATES):
                return None
            return message
        return None

    outer = failure(payload)
    if outer:
        return outer

    result = payload.get("result")
    for row in result if isinstance(result, list) else [result]:
        inner = failure(row)
        if inner:
            return inner
    return None


def _history_feed(symbols: list[str], exchange: str):
    """A ListFeed over the local parquet cache, limited to ``symbols``.

    Reads the cache instead of the API so a validation run needs no broker
    session and costs nothing to repeat.
    """
    from atr.core.enums import Timeframe
    from atr.core.models import Instrument
    from atr.data.base import ListFeed, pivot_to_snapshots
    from atr.data.history import CACHE_ROOT, load_cached
    from atr.scanner import UNIVERSE

    wanted = [s.upper() for s in symbols] or list(UNIVERSE)
    frames = load_cached(exchange, wanted)
    if not frames:
        if not (CACHE_ROOT / exchange).is_dir():
            raise HTTPException(
                503,
                f"history cache is empty for {exchange} — run "
                f"`atr history sync --exchange {exchange}`",
            )
        raise HTTPException(
            404,
            f"none of {wanted} are in the {exchange} cache — run "
            f"`atr history sync --exchange {exchange}`",
        )
    picked = {s: frames[s] for s in wanted if s in frames}
    if not picked:
        raise HTTPException(
            404,
            f"none of {wanted} are in the {exchange} cache "
            f"({len(frames)} symbols cached)",
        )
    combined = pd.concat(
        [df.assign(symbol=s) for s, df in picked.items()], ignore_index=True
    )
    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {s: Instrument(symbol=s, exchange=exchange) for s in picked}
    return ListFeed(snapshots, instruments), sorted(picked)


# ----------------------------------------------------------------------
# Research — walk-forward validation
# ----------------------------------------------------------------------
class ResearchRequest(BaseModel):
    strategy: str = "signals_entry"
    #: ``cache``  — local parquet dailies (fast, no session, ~1 year)
    #: ``fetch``  — pull deep history from IIFL (needs a session, years of data)
    #: ``synthetic`` — random walks; only useful for smoke-testing the harness
    source: str = "cache"
    symbols: list[str] = Field(default_factory=list)
    exchange: str = "NSEEQ"
    cash: float = 1_000_000.0
    train: int | None = None
    test: int | None = None
    step: int | None = None
    warmup: int | None = None
    lookback_days: int = 2200
    fast: list[int] = Field(default_factory=lambda: [10, 20, 30])
    slow: list[int] = Field(default_factory=lambda: [50, 100])
    search: bool = False
    min_trades: int = 20
    min_folds: int = 3
    confidence: float = 0.95
    slippage_bps: float = 5.0


#: Which parameters a strategy's grid actually varies. A grid the strategy
#: ignores would inflate the trial count and therefore the Sharpe hurdle,
#: making the test stricter for no reason.
_TUNABLE: dict[str, list[str]] = {
    "sma_crossover": ["fast", "slow"],
    "momentum_breakout": ["lookahead", "volume_multiple", "consecutive_breakout"],
    "signals_entry": ["trend_fast_sma", "trend_slow_sma", "pullback_rsi_low", "pullback_rsi_high"],
    "opening_range_breakout": [],
    "cross_sectional_momentum": [],
    # Paper models: min_confidence is a filter, not a fitted parameter, so
    # varying it would inflate the trial count without learning anything.
    "paper_jegadeesh_titman": [],
    "paper_avellaneda_lee": [],
    "paper_volatility_breakout": [],
    "paper_multi_factor_composite": [],
    "paper_iima_nse_momentum": [],
    "paper_nism_52w_high": [],
    "paper_sehgal_low_vol": [],
}

#: Bars of history each strategy needs before its rules will fire. A test
#: window shorter than this cannot produce trades, and "no trades" reads
#: exactly like "no edge" unless we say so out loud.
#:
#: The table itself now lives in ``atr.backtest.config`` because the runner
#: needs it too, and the backtest layer may not import this module. Kept as a
#: name here so existing callers and the ``/strategies`` response are unchanged.
_WARMUP_NEED: dict[str, int] = WARMUP_BARS


def _fetch_feed(symbols: list[str], exchange: str, lookback_days: int):
    """Deep daily history pulled from IIFL, one symbol at a time.

    Needed because the local cache holds about a year, which cannot be carved
    into folds that also leave room for indicator warmup.
    """
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.core.enums import Timeframe
    from atr.core.models import Instrument
    from atr.data.base import ListFeed, pivot_to_snapshots
    from atr.scanner import UNIVERSE, resolve_conid
    from atr.signals.engine import load_daily

    client = _authed_client()
    wanted = [s.upper() for s in symbols] or list(UNIVERSE)
    master = InstrumentMaster(client)
    master.load_cached([exchange])

    frames, used = [], []
    with client:
        for symbol in wanted:
            try:
                conid = resolve_conid(master, symbol, exchange)
            except KeyError:
                continue
            frame = load_daily(symbol, exchange, client, conid, lookback_days=lookback_days)
            if frame.empty or "ts" not in frame.columns:
                continue
            frames.append(frame.assign(symbol=symbol))
            used.append(symbol)

    if not frames:
        raise HTTPException(
            503,
            "no daily history returned for any requested symbol — check the "
            "session and that the symbols exist on this exchange",
        )
    combined = pd.concat(frames, ignore_index=True).sort_values("ts", kind="mergesort")
    snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
    instruments = {s: Instrument(symbol=s, exchange=exchange) for s in used}
    return ListFeed(snapshots, instruments), sorted(used)


#: Prior results that live in the project record rather than the current
#: ``paper_validation.json``. Reporting ``signals_entry`` as merely "untested"
#: would overstate it: it was tested and lost. Losing is a result.
_PRIOR_RESULTS: dict[str, dict[str, Any]] = {
    "signals_entry": {
        "state": "fail",
        "oos_return_pct": -5.05,
        "benchmark_return_pct": 57.39,
        "deflated_sharpe": 0.926,
        "note": "Walk-forward on 19 large caps, 2020-2026. Below the 0.95 bar and behind buy-and-hold.",
    },
    "cross_sectional_momentum": {
        "state": "fail",
        "deflated_sharpe": 0.976,
        "z_vs_control": -0.71,
        "note": "Cleared deflated Sharpe but sat at the 25th percentile of random selection — significance without usefulness.",
    },
}

_UNTESTED = {"state": "untested"}


@app.get("/strategies")
def strategies() -> dict[str, Any]:
    """Every strategy the engine can run, with its validation status.

    ``validation`` is attached from the last walk-forward run rather than left
    to the caller. A registry that lists an unvalidated strategy next to a
    validated one, with nothing to tell them apart, invites exactly the mistake
    this project is built to avoid.
    """
    from atr.strategy.strategies import STRATEGIES

    measured: dict[str, dict[str, Any]] = {}
    if _PAPER_VALIDATION_PATH.exists():
        try:
            payload = json.loads(_PAPER_VALIDATION_PATH.read_text(encoding="utf8"))
            for r in payload.get("results", []):
                measured[r.get("strategy", "")] = r
        except (OSError, ValueError):
            measured = {}

    rows = []
    for name in sorted(STRATEGIES):
        m = measured.get(name)
        rows.append(
            {
                "name": name,
                "tunable": _TUNABLE.get(name, []),
                "warmup_bars": _WARMUP_NEED.get(name),
                "validation": (
                    {
                        "state": "pass" if m.get("passed") else "fail",
                        "oos_sharpe": m.get("oos_sharpe"),
                        "oos_return_pct": m.get("oos_return_pct"),
                        "benchmark_sharpe": m.get("benchmark_sharpe"),
                        "deflated_sharpe": m.get("deflated_sharpe"),
                        "measured_win_rate": m.get("measured_win_rate"),
                        "measured_trades": m.get("measured_trades"),
                        "expectancy_r": m.get("measured_expectancy_r"),
                        "z_vs_control": m.get("sharpe_z_vs_control"),
                        "as_of": payload.get("generated_at") if measured else None,
                    }
                    if m
                    # A known loss is a result. Fall back to the project record
                    # before calling something untested.
                    else _PRIOR_RESULTS.get(name, _UNTESTED)
                ),
            }
        )

    return {
        "strategies": rows,
        "validation_as_of": payload.get("generated_at") if measured else None,
        "control_sharpe": (
            payload.get("control", {}).get("sharpe_mean") if measured else None
        ),
    }


@app.post("/research", dependencies=[Depends(get_principal)])
def research(request: ResearchRequest) -> dict[str, Any]:
    """Walk a strategy forward and report only the out-of-sample evidence.

    This is the endpoint to trust. ``/backtest`` scores one configuration on
    data it may already have seen; this one picks parameters on a training
    window, scores them once on the following unseen window, and then asks
    whether the winner clears a Sharpe hurdle scaled to how many
    combinations were tried.
    """
    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.execution.risk import RiskLimits
    from atr.research.validate import (
        ValidationConfig,
        WalkForwardConfig,
        buy_and_hold_equity,
        walk_forward,
    )
    from atr.strategy.strategies import STRATEGIES

    strategy_cls = STRATEGIES.get(request.strategy)
    if strategy_cls is None:
        raise HTTPException(
            400, f"unknown strategy: {request.strategy} (have {sorted(STRATEGIES)})"
        )

    source = {"history": "cache"}.get(request.source, request.source)
    if source not in {"cache", "fetch", "synthetic"}:
        raise HTTPException(400, f"unknown source: {request.source}")

    symbols = [s.strip().upper() for s in request.symbols if s.strip()]
    if source == "synthetic":
        used = symbols or ["AAPL", "MSFT"]
        feed = SyntheticFeed(
            SyntheticConfig(
                symbols=tuple(used),
                start=datetime(2024, 1, 1, 9, 30),
                end=datetime(2024, 6, 28, 15, 59),
            )
        )
    elif source == "fetch":
        feed, used = _fetch_feed(symbols, request.exchange.upper(), request.lookback_days)
    else:
        feed, used = _history_feed(symbols, request.exchange.upper())

    snapshots = feed.load()
    total = len(snapshots)

    # Size the windows against the data actually in hand. The CLI defaults
    # (5000/1250) are tuned for intraday bars and would refuse to run on a
    # year of dailies, which is what the cache holds.
    folds_wanted = max(1, request.min_folds)
    test = request.test or max(2, total // (folds_wanted + 1))
    train = request.train or max(2, total - folds_wanted * test)
    step = request.step or test
    if total < train + test:
        raise HTTPException(
            422,
            f"{total} bars is too few for a {train}/{test} train/test split — "
            f"sync more history or use smaller windows",
        )

    warnings: list[str] = []
    need = _WARMUP_NEED.get(request.strategy, 0)
    if need and test < need:
        warnings.append(
            f"test window is {test} bars but {request.strategy} needs about {need} "
            f"bars of history before its rules fire — expect near-zero trades, "
            f"which is not evidence of no edge. Use source=fetch for a longer history."
        )
    if train < need:
        warnings.append(
            f"training window is {train} bars, below the ~{need} bars this strategy "
            f"needs to warm up; parameters are being chosen from a window where "
            f"the strategy can barely trade."
        )

    if request.strategy == "sma_crossover":
        grid: dict[str, list] = {"fast": request.fast, "slow": request.slow}
    elif request.strategy == "signals_entry" and request.search:
        from atr.signals.models import SEARCH_GRID

        grid = {k: list(v) for k, v in SEARCH_GRID.items()}
    else:
        grid = {}

    # Warmup is drawn from bars before the test window, which in practice
    # means the training window — asking for more than that is silently
    # clamped by the harness, so clamp it here and report the real number.
    warmup = max(0, min(request.warmup if request.warmup is not None else 130, train))

    try:
        result = walk_forward(
            feed,
            strategy_cls,
            grid,
            config=WalkForwardConfig(
                train_bars=train,
                test_bars=test,
                step_bars=step,
                warmup_bars=warmup,
            ),
            backtest=BacktestConfig(
                initial_cash=request.cash,
                slippage=SlippageModel(bps=request.slippage_bps),
                risk=RiskLimits(max_daily_loss=request.cash * 0.10),
            ),
            validation=ValidationConfig(
                min_folds=request.min_folds,
                min_trades=request.min_trades,
                min_confidence=request.confidence,
            ),
        )
    except ValueError as exc:  # window sizing / empty folds
        raise HTTPException(422, str(exc)) from exc

    if result.oos_metrics.num_trades == 0:
        warnings.append(
            "no trades were taken out of sample — the verdict below is a "
            "statement about the windows, not about the strategy."
        )

    # The benchmark is whatever you would otherwise have done: hold the same
    # names over the same out-of-sample window. A strategy that cannot beat
    # doing nothing is just paying commission.
    first, last = result.folds[0].test_start, result.folds[-1].test_end
    window = [s for s in snapshots if first <= s.ts <= last]
    benchmark_equity = buy_and_hold_equity(window, request.cash)

    return _clean(
        {
            "strategy": request.strategy,
            "source": source,
            "symbols": used,
            "bars": total,
            "train_bars": train,
            "test_bars": test,
            "step_bars": step,
            "warmup_bars": warmup,
            "n_trials": result.n_trials,
            "required_sharpe": result.required_sharpe,
            "deflated_sharpe": result.deflated_sharpe,
            "verdict": {
                "passed": result.verdict.passed,
                "checks": [
                    {"name": name, "ok": ok, "detail": detail}
                    for name, ok, detail in result.verdict.checks
                ],
            },
            "warnings": warnings,
            "oos": result.oos_metrics.as_dict(),
            "benchmark": result.benchmark_metrics.as_dict(),
            "folds": result.to_frame().to_dict(orient="records"),
            "equity": _equity_points(result.oos_equity),
            "benchmark_equity": _equity_points(benchmark_equity),
        }
    )


_PAPER_VALIDATION_PATH = Path("data/self_learning/paper_validation.json")


@app.get("/validation")
def validation_report() -> dict[str, Any]:
    """The last out-of-sample run over the academic paper strategies.

    Serves ``scripts/validate_paper_strategies.py`` output so the dashboard can
    show measured numbers instead of the constants that used to be hardcoded in
    ``atr.research.papers``. Includes the random-selection control: a strategy
    that does not clear it has demonstrated nothing.
    """
    if not _PAPER_VALIDATION_PATH.exists():
        return {
            "available": False,
            "hint": "run: .venv/Scripts/python.exe scripts/validate_paper_strategies.py",
        }
    try:
        payload = json.loads(_PAPER_VALIDATION_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    payload["available"] = True
    return payload


_EPISODIC_VALIDATION_PATH = Path("data/self_learning/episodic_pivot_validation.json")
_EPISODIC_PREMISE_PATH = Path("data/self_learning/episodic_pivot_premise.json")


@app.get("/validation/episodic-pivot")
def episodic_pivot_report() -> dict[str, Any]:
    """Pradeep Bonde's Episodic Pivot, measured rather than quoted.

    Serves two scripts together because either alone invites the wrong
    conclusion. ``episodic_pivot_premise.json`` measures whether the playbook's
    raw material — routine 20–40% gaps and 100–300% repricings — exists on the
    universe at all; ``episodic_pivot_validation.json`` scores the rules out of
    sample. A "no trades" verdict means something entirely different depending
    on which of the two came up empty, so the panel shows both.
    """
    if not _EPISODIC_VALIDATION_PATH.exists():
        return {
            "available": False,
            "hint": (
                "run: .venv/Scripts/python.exe scripts/research_episodic_pivot.py "
                "&& .venv/Scripts/python.exe scripts/validate_episodic_pivot.py"
            ),
        }
    try:
        payload = json.loads(_EPISODIC_VALIDATION_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    if _EPISODIC_PREMISE_PATH.exists():
        try:
            payload["premise"] = json.loads(
                _EPISODIC_PREMISE_PATH.read_text(encoding="utf8")
            )
        except (OSError, ValueError):
            pass

    payload["available"] = True
    return payload


_ALPHA_HUNT_PATHS = {
    "nifty50": Path("data/self_learning/alpha_hunt_nifty50.json"),
    "midcap150": Path("data/self_learning/alpha_hunt_midcap150.json"),
    "smallcap250": Path("data/self_learning/alpha_hunt_smallcap250.json"),
}


@app.get("/validation/alpha-hunt")
def alpha_hunt_report() -> dict[str, Any]:
    """Pre-registered candidate edges, each against a null that removes only its signal.

    One JSON per universe because the runs are long enough to want running in
    parallel; merged here so the dashboard reads one payload. The
    ``pre_registered`` block is carried through deliberately — a candidate list
    fixed *after* seeing results is not a test, and the reader should be able to
    see that the priors and grids were written down first.
    """
    present = {u: p for u, p in _ALPHA_HUNT_PATHS.items() if p.exists()}
    if not present:
        return {
            "available": False,
            "hint": "run: .venv/Scripts/python.exe scripts/research_alpha_hunt.py",
        }

    merged: dict[str, Any] = {"available": True, "universes": {}, "pre_registered": {},
                              "config": {}, "generated_at": None}
    for _universe, path in present.items():
        try:
            payload = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError) as exc:
            merged.setdefault("errors", {})[str(path)] = str(exc)
            continue
        merged["universes"].update(payload.get("universes", {}))
        merged["pre_registered"].update(payload.get("pre_registered", {}))
        merged["config"].update(payload.get("config", {}))
        generated = payload.get("generated_at")
        if generated and (merged["generated_at"] is None or generated > merged["generated_at"]):
            merged["generated_at"] = generated
    return merged


#: Findings from the factor research programme, written by
#: ``scripts/research_honest_verdicts.py``. Each entry is an ``Evidence`` record
#: rendered with its credibility verdict attached.
_EVIDENCE_PATH = Path("data/signals/evidence.json")


@app.get("/evidence")
def evidence_report() -> dict[str, Any]:
    """Measured findings, each with the credibility verdict that reads it.

    This endpoint exists because a backtest number on its own is not evidence.
    The results served here were each individually correct and collectively
    misleading when first produced: a +480% index that was really +124% once
    universe selection was removed, and a "factor" that was beta in disguise.

    Nothing here should be presented to a user as an opportunity without the
    verdict travelling with it, so the payload keeps them in one record.
    """
    if not _EVIDENCE_PATH.exists():
        return {
            "available": False,
            "hint": (
                "run: .venv/Scripts/python.exe scripts/research_honest_verdicts.py"
            ),
        }
    try:
        payload = json.loads(_EVIDENCE_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        return {"available": False, "error": str(exc)}

    findings = payload.get("findings", [])
    payload["available"] = True
    payload["credible_count"] = sum(
        1 for f in findings if f.get("verdict", {}).get("credible")
    )
    payload["total"] = len(findings)
    return payload


# ----------------------------------------------------------------------
# Portfolio & quotes — the broker's own view
# ----------------------------------------------------------------------
_PORTFOLIO_SECTIONS = ("limits", "positions", "holdings", "orders", "trades")


@app.get("/dashboard/summary")
def dashboard_summary(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Everything the dashboard KPI strip needs, in one call.

    Four numbers, each with the daily series behind it so the UI can draw a
    sparkline without a second round trip:

      day_pnl      — change in open positions since yesterday's close
      win_rate     — fraction of the last 30 *closed* trades that made money
      drawdown     — current equity vs the running peak of the trade history
      breadth      — share of the universe in an uptrend, last 5 sessions

    Broker calls are memoised for a few seconds: this is polled every 30s and
    two IIFL round trips per poll is pure waste. The TTL is short enough that
    the number still moves visibly during a session.
    """
    global _SUMMARY_CACHE

    now = time.monotonic()
    if (
        _SUMMARY_CACHE.get("data") is not None
        and _SUMMARY_CACHE.get("exchange") == exchange.upper()
        and now - float(_SUMMARY_CACHE.get("at") or 0) < _SUMMARY_TTL
    ):
        return _SUMMARY_CACHE["data"]

    out: dict[str, Any] = {"as_of": _utcnow_iso(), "exchange": exchange.upper()}

    # ── positions → day P&L ───────────────────────────────────────────────
    positions: list[dict[str, Any]] = []
    try:
        client = _authed_client()
        try:
            payload = client.positions()
            err = _broker_error(payload)
            if not err:
                positions = [_clean(r) for r in _broker_rows(payload) if not _empty_state(r)]
        finally:
            with contextlib.suppress(Exception):
                client.close()

        # Resolve prior closes once, up front: the broker omits them, so fall
        # back to the local daily cache. Doing it before the loop keeps the
        # per-position work trivial and avoids a cache read per row.
        held = [
            str(_pick(p, "tradingSymbol", "symbol", "Symbol",
                      "trading_symbol", default=""))
            for p in positions
            if float(_pick(p, "netQuantity", "NetQuantity", "quantity",
                           "qty", "Quantity", default=0) or 0) != 0
        ]
        held_symbols = [s for s in held if s]
        cache_closes = _prior_closes(held_symbols, exchange) if held_symbols else {}

        total = day = invested = 0.0
        counted = no_prev = from_cache = 0
        for p in positions:
            qty = float(_pick(p, "netQuantity", "NetQuantity", "quantity",
                              "qty", "Quantity", default=0) or 0)
            if qty == 0:
                continue          # mirror IiflBroker.positions(): flat is not a position
            last = float(_pick(p, "ltp", "LTP", "lastTradedPrice", "lastPrice",
                               "last_price", default=0) or 0)
            avg = float(_pick(p, "averagePrice", "AveragePrice", "avgPrice",
                              "avg_price", default=0) or 0)
            # Prefer whatever the broker sent; otherwise use the cached prior
            # close. Never fall back to the entry price — that reports "no
            # change today" for a book that may be moving hard.
            sym = str(_pick(p, "tradingSymbol", "symbol", "Symbol",
                            "trading_symbol", default=""))
            prev = _pick(p, "close", "prev_close", "previous_close",
                         "previousClose", "prevClose", default=None)
            if prev is None:
                prev = cache_closes.get(sym.upper())
                if prev is not None:
                    from_cache += 1
            invested += qty * avg
            total += qty * last
            counted += 1
            try:
                prev_f = float(prev)
            except (TypeError, ValueError):
                prev_f = None
            # Sanity-bound the prior close. A price feed and a daily cache can
            # disagree wildly (different symbol in the master, unadjusted vs
            # adjusted series, a stale file), and a mismatch turns Day P&L into
            # fiction — a fixture test produced "+52.99% in one day" this way.
            # A real session rarely moves a large cap past ±35%; beyond that,
            # treat the baseline as unusable rather than reporting it.
            if prev_f is not None and last > 0 and prev_f > 0:
                move = abs(last - prev_f) / prev_f
                if move > 0.35:
                    logger.warning(
                        "discarding prior close for %s: cached %.2f vs live %.2f "
                        "is a %.1f%% gap, which is more likely a bad baseline "
                        "than a real move",
                        sym or "(unnamed)", prev_f, last, move * 100.0,
                    )
                    prev_f = None
            if prev_f is not None:
                day += qty * (last - prev_f)
            else:
                no_prev += 1

        out["positions"] = {
            "count": counted,
            "value": round(total, 2),
            "invested": round(invested, 2),
            "day_pnl": round(day, 2),
            # True only when there is a book AND every counted position had a
            # usable prior close (broker-sent or cache-resolved). An empty book
            # reports True, not False: with nothing held, "0 change today" is a
            # complete and correct answer, whereas False would imply data is
            # missing. Callers branch on `count` first.
            "day_pnl_complete": counted == 0 or no_prev == 0,
            # How many prior closes had to come from the local cache rather
            # than the broker. Non-zero explains why Day P&L is present at all;
            # equal to `count` means every baseline is ours, not IIFL's.
            "day_pnl_from_cache": from_cache,
            "day_pnl_pct": round((day / invested * 100.0) if invested else 0.0, 3),
            "unrealized_pnl": round(total - invested, 2),
            "last_flat_at": _last_flat_at(positions),
        }
    except Exception as exc:  # noqa: BLE001
        out["positions"] = {"count": 0, "error": str(exc)[:200]}

    # ── trade book → win rate + drawdown ──────────────────────────────────
    try:
        client = _authed_client()
        try:
            payload = client.trades()
            err = _broker_error(payload)
            trades = [] if err else [_clean(r) for r in _broker_rows(payload)]
        finally:
            with contextlib.suppress(Exception):
                client.close()
        out["performance"] = _performance_from_trades(trades)
    except Exception as exc:  # noqa: BLE001
        out["performance"] = {"error": str(exc)[:200], "trades": 0}

    # ── breadth trend over the last 5 sessions ────────────────────────────
    # `load_cached` reads 2,672 parquets (~6s). It must only be paid on a
    # genuine cache miss, so the cache check happens *inside* the helper and
    # the frames are loaded there rather than here.
    try:
        out["breadth"] = _breadth_trend(exchange.upper())
    except Exception as exc:  # noqa: BLE001
        out["breadth"] = {"error": str(exc)[:200], "series": []}

    _SUMMARY_CACHE = {"data": out, "at": now, "exchange": exchange.upper()}
    return out


_SUMMARY_CACHE: dict[str, Any] = {"data": None, "at": 0.0, "exchange": ""}
_SUMMARY_TTL = 20.0   # seconds; shorter than the UI's 30s poll, so it still moves


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present, non-empty value among `keys`.

    Mirrors the identical helper in `atr.brokers.iifl.broker` so the two
    layers resolve IIFL's polymorphic field names the same way.
    """
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _last_flat_at(positions: list[dict[str, Any]]) -> str | None:
    """When the book was last empty, if the broker tells us.

    Not every IIFL payload carries a timestamp on a flat book, so this is
    best-effort — the UI falls back to "no record" rather than inventing one.
    """
    for p in positions:
        for key in ("last_flat_at", "flat_at", "as_of", "updated_at"):
            if p.get(key):
                return str(p[key])
    return None


_PNL_KEYS = ("realized_pnl", "realised_pnl", "pnl", "net_pnl", "profit")
_TRADE_TS_KEYS = ("ts", "time", "trade_time", "fill_time", "order_time", "date")


def _trade_day(trade: dict[str, Any]) -> str | None:
    """Calendar date (YYYY-MM-DD) of a closed trade, or None if unreadable.

    IIFL nests the fill timestamp under several field names. Return None
    rather than today's date on failure: silently bucketing an undated trade
    into "now" would make a stale book look freshly active.
    """
    raw = None
    for key in _TRADE_TS_KEYS:
        if trade.get(key):
            raw = trade[key]
            break
    if raw is None:
        return None
    text = str(raw)
    # Accept ISO ("2026-09-11T09:32:00+05:30") and epoch seconds alike.
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    with contextlib.suppress(TypeError, ValueError, OSError):
        ts = float(text)
        if ts > 1e11:          # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return None


def _performance_from_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Win rate over the last 30 closed trades, plus peak-to-now drawdown.

    Trades arrive newest-first from IIFL. Realised P&L is read from whichever
    of the several plausible field names is present; a trade with none is
    skipped rather than counted as a loss, because counting an unknown as a
    loss would understate the win rate and mislead in the opposite direction.
    """
    realised: list[float] = []
    for t in trades:
        for key in _PNL_KEYS:
            if t.get(key) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    realised.append(float(t[key]))
                break

    window = realised[:30]
    wins = sum(1 for p in window if p > 0)
    losses = sum(1 for p in window if p < 0)

    # Equity path from realised results, oldest → newest, to find the peak.
    equity, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    for p in reversed(realised):
        equity += p
        peak = max(peak, equity)
        dd = (equity - peak) / abs(peak) * 100.0 if peak else 0.0
        max_dd = min(max_dd, dd)
        curve.append(round(equity, 2))
    current_dd = (equity - peak) / abs(peak) * 100.0 if peak else 0.0

    # Sparkline: cumulative realised P&L sampled over the last 7 *sessions*,
    # not the last 7 fills. Slicing `realised[:7]` would draw a line through
    # seven trades in one afternoon and label it a week. Bucket by calendar
    # date and carry the running total forward so a quiet session reads flat
    # rather than collapsing the window. Oldest → newest, matching the curve.
    by_day: dict[str, float] = {}
    for t in trades:
        day = _trade_day(t)
        if day is None:
            continue
        for key in _PNL_KEYS:
            if t.get(key) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    by_day[day] = by_day.get(day, 0.0) + float(t[key])
                break
    recent: list[float] = []
    if by_day:
        run = 0.0
        for day in sorted(by_day)[-7:]:
            run += by_day[day]
            recent.append(round(run, 2))

    return {
        "trades": len(realised),
        "win_rate": round(wins / len(window) * 100.0, 1) if window else None,
        "wins": wins,
        "losses": losses,
        "sample": len(window),
        "realized_pnl": round(sum(realised), 2),
        "current_drawdown_pct": round(current_dd, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sparkline": recent,
        "equity_curve": curve[-40:],
    }


def _prior_closes(symbols: list[str], exchange: str = "NSEEQ") -> dict[str, float]:
    """Previous session's close per symbol, from the local daily cache.

    IIFL's position rows carry no prior close, so Day P&L would otherwise be
    unavailable for every real book. The daily parquets already hold it.

    Two guards, because a stale prior close produces a confidently wrong
    number rather than a missing one:

    * The cached frame's last date must be the *most recent completed* trading
      session, not merely "recent". If the cache is more than 4 days behind
      the newest frame we can see, we return nothing and let the caller report
      Day P&L as unknown. A number that is silently three weeks old is worse
      than `n/a`.
    * Only the named symbols are read, so this stays cheap.
    """
    if not symbols:
        return {}
    import datetime as _dt

    from atr.data.history import load_cached

    wanted = [s.upper() for s in symbols]
    try:
        frames = load_cached(exchange=exchange, symbols=wanted)
    except Exception as exc:  # noqa: BLE001 — a cache miss must not break the page
        logger.warning("prior-close lookup failed for %d symbols: %s", len(wanted), exc)
        return {}

    latest: dict[str, tuple[str, float]] = {}
    for sym, df in frames.items():
        day = _frame_last_date(df)
        if day is None or getattr(df, "empty", True):
            continue
        try:
            close = float(df["close"].iloc[-1])
        except (KeyError, TypeError, ValueError):
            continue
        if close != close or close <= 0:   # NaN or nonsense
            continue
        latest[sym.upper()] = (day, close)

    if not latest:
        return {}

    newest = max(day for day, _ in latest.values())
    try:
        newest_d = _dt.date.fromisoformat(newest)
    except ValueError:
        return {}
    today = _dt.datetime.now(_dt.UTC).date()
    # > 4 calendar days behind = a weekend plus a holiday, or a stale cache.
    if (today - newest_d).days > 4:
        logger.warning(
            "daily cache last session is %s (%d days old) — reporting Day P&L "
            "as unknown rather than serving a stale baseline",
            newest, (today - newest_d).days,
        )
        return {}

    return {sym: close for sym, (day, close) in latest.items() if day == newest}


def _frame_last_date(df: Any) -> str | None:
    """Last calendar date in a cached history frame.

    `load_cached` returns a RangeIndex, with the timestamps in a `ts` column —
    so this reads the column, not the index. Returns None (not "") when there
    is genuinely no timestamp, so the caller can tell the two apart.
    """
    try:
        if "ts" in getattr(df, "columns", []):
            ts = df["ts"].iloc[-1]
        else:
            ts = df.index[-1]
        import pandas as pd

        return str(pd.Timestamp(ts).date())
    except Exception:  # noqa: BLE001 — a missing date must not break breadth
        return None


def _breadth_trend(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Share of the universe above its trend, for each of the last 5 sessions.

    Measured the same way the scanner measures it — same frames, same
    `score_frame` — so this cannot drift from the number on the scanner page.

    Three things make it fast enough to sit on a 30s dashboard poll:
      * results are cached for `_BREADTH_TTL`, because breadth only changes
        when a new session closes;
      * the cache check happens **before** `load_cached`, so the 6s parquet
        read is only paid on a genuine miss;
      * it samples the universe rather than scoring all 2,672 names. Scoring
        every symbol five times costs ~55s, which is not a dashboard call.
        A 600-name sample puts the standard error near 2pp — fine for a
        sparkline whose whole job is "expanding or contracting".
    """
    import time as _time

    now = _time.monotonic()
    cached = _BREADTH_CACHE.get("data")
    if (
        cached is not None
        and _BREADTH_CACHE.get("exchange") == exchange.upper()
        and now - float(_BREADTH_CACHE.get("as_of") or 0) < _BREADTH_TTL
    ):
        return cached

    from atr.data.history import load_cached
    from atr.scanner import score_frame

    frames = load_cached(exchange.upper())
    if not frames:
        raise ValueError("history cache is empty — run `atr history sync`")

    series: list[float] = []
    dates: list[str] = []
    sample = sorted(frames.items())[:_BREADTH_SAMPLE]

    for back in range(4, -1, -1):
        up = total = 0
        as_of: str | None = None
        for _symbol, df in sample:
            if df is None or len(df) <= back + 60:
                continue
            window = df.iloc[: len(df) - back] if back else df
            try:
                row = score_frame("_", window)
            except Exception:  # noqa: BLE001 — thin/odd histories just don't count
                continue
            total += 1
            if row.get("trend") == "UP":
                up += 1
            if as_of is None:
                # The frame index is a RangeIndex; the real timestamp lives in
                # the `ts` column. Reading `.index[-1].date()` silently returns
                # "" (RangeIndex holds ints) — a plausible-looking empty string
                # rather than an error.
                as_of = _frame_last_date(window)
        if total:
            series.append(round(up / total * 100.0, 1))
            dates.append(as_of or "")

    delta = (series[-1] - series[0]) if len(series) >= 2 else None
    result = {
        "series": series,
        "dates": dates,
        "current": series[-1] if series else None,
        "delta_5d": round(delta, 2) if delta is not None else None,
        "expanding": None if delta is None else delta > 0,
        "sampled": min(len(sample), _BREADTH_SAMPLE),
        "universe": len(frames),
    }
    _BREADTH_CACHE["data"] = result
    _BREADTH_CACHE["as_of"] = now
    _BREADTH_CACHE["exchange"] = exchange.upper()
    return result


_BREADTH_CACHE: dict[str, Any] = {"data": None, "as_of": 0.0, "exchange": ""}
_BREADTH_TTL = 900.0      # 15 min — breadth moves once a session
_BREADTH_SAMPLE = 600     # ~2pp standard error on a 50% proportion


@app.get("/portfolio")
def portfolio(sections: str | None = None) -> dict[str, Any]:
    """Every broker section in one call: limits, positions, holdings, orders, trades.

    A section that errors is reported as an error rather than failing the whole
    response — one dead endpoint should not blank the dashboard.
    """
    client = _authed_client()
    wanted = (
        [s.strip() for s in sections.split(",") if s.strip()]
        if sections
        else list(_PORTFOLIO_SECTIONS)
    )
    unknown = [s for s in wanted if s not in _PORTFOLIO_SECTIONS]
    if unknown:
        raise HTTPException(400, f"unknown section(s): {unknown}")

    fetchers = {
        "limits": client.limits,
        "positions": client.positions,
        "holdings": client.holdings,
        "orders": client.order_book,
        "trades": client.trades,
    }

    out: dict[str, Any] = {}
    with client:
        for name in wanted:
            try:
                payload = fetchers[name]()
                broker_error = _broker_error(payload)
                if broker_error:
                    out[name] = {"rows": [], "count": 0, "error": _with_ip_hint(broker_error)}
                    continue
                rows = [r for r in _broker_rows(payload) if not _empty_state(r)]
                out[name] = {"rows": _clean(rows), "count": len(rows)}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"rows": [], "count": 0, "error": str(exc)[:300]}
    return {"sections": out, "as_of": datetime.now().isoformat()}


@app.get("/quote")
def quote(symbols: str, exchange: str = "NSEEQ") -> dict[str, Any]:
    """Live quotes for a comma-separated symbol list, e.g. ``?symbols=RELIANCE-EQ,INFY-EQ``."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(400, "pass ?symbols=RELIANCE-EQ,INFY-EQ")
    if len(wanted) > 50:
        raise HTTPException(400, "at most 50 symbols per request")

    client = _authed_client()
    master = InstrumentMaster(client)
    try:
        master.load_cached([exchange.upper()])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            503,
            f"no cached instrument master for {exchange} — run "
            f"`atr instruments sync --exchanges {exchange}` ({exc})",
        ) from exc

    legs, resolved, failed = [], [], []
    for symbol in wanted:
        try:
            legs.append((exchange.upper(), resolve_conid(master, symbol, exchange.upper())))
            resolved.append(symbol)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the call
            failed.append({"symbol": symbol, "error": str(exc)[:160]})
    if not legs:
        raise HTTPException(404, f"could not resolve any of {wanted} on {exchange}")

    try:
        payload = client.market_quotes(legs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"quote request failed: {exc}") from exc

    broker_error = _broker_error(payload)
    if broker_error:
        raise HTTPException(502, _with_ip_hint(broker_error))

    rows = _broker_rows(payload)
    for symbol, row in zip(resolved, rows, strict=False):
        row.setdefault("symbol", symbol)
    return {
        "exchange": exchange.upper(),
        "as_of": datetime.now().isoformat(),
        "quotes": _clean(rows),
        "failed": failed,
    }


def _watchlist_live_quotes(
    symbols: list[str], exchange: str = "NSEEQ"
) -> dict[str, dict[str, Any]]:
    """Batch quote overlay for the watchlist table.

    Returns ``{}`` on any failure — no broker session, a rejected IP, a broker
    error — so the table degrades to cached closes per symbol instead of
    blanking. A watchlist is a read-only view; losing live prices must not make
    it unusable, and every affected row is marked ``stale`` so the reader knows.
    """
    try:
        from atr.brokers.iifl.contracts import InstrumentMaster
        from atr.scanner import resolve_conid

        client = _authed_client()
        master = InstrumentMaster(client)
        master.load_cached([exchange.upper()])

        legs: list[tuple[str, Any]] = []
        resolved: list[str] = []
        for symbol in symbols[:50]:
            try:
                legs.append((exchange.upper(), resolve_conid(master, symbol, exchange.upper())))
                resolved.append(symbol)
            except Exception:  # noqa: BLE001 - one unresolved symbol is not fatal
                continue
        if not legs:
            return {}

        payload = client.market_quotes(legs)
        if _broker_error(payload):
            return {}
        rows = _broker_rows(payload)
        return {
            symbol: dict(row)
            for symbol, row in zip(resolved, rows, strict=False)
            if isinstance(row, dict)
        }
    except Exception as exc:  # noqa: BLE001 - live data is optional
        logger.debug("live quote overlay unavailable: %s", exc)
        return {}


# Consumed by the watchlist router through ``request.app.state`` rather than an
# import, because the router is imported *by* this module and importing back
# would be circular.
app.state.live_quote_provider = _watchlist_live_quotes


# ----------------------------------------------------------------------
# Local caches — what the engine has to work with offline
# ----------------------------------------------------------------------
@app.get("/history/status")
def history_status() -> dict[str, Any]:
    """Coverage of the local daily cache. No broker session required."""
    from atr.data.history import CACHE_ROOT

    exchanges = []
    if CACHE_ROOT.is_dir():
        for folder in sorted(p for p in CACHE_ROOT.iterdir() if p.is_dir()):
            files = list(folder.glob("*.parquet"))
            if not files:
                continue
            stats = [f.stat() for f in files]
            exchanges.append(
                {
                    "exchange": folder.name,
                    "symbols": len(files),
                    "size_mb": round(sum(s.st_size for s in stats) / 1e6, 1),
                    "last_sync": datetime.fromtimestamp(
                        max(s.st_mtime for s in stats)
                    ).isoformat(),
                }
            )
    return {
        "cache_root": str(CACHE_ROOT),
        "exchanges": exchanges,
        "total_symbols": sum(e["symbols"] for e in exchanges),
    }


@app.get("/instruments/status")
def instruments_status() -> dict[str, Any]:
    """Cached contract-file coverage per segment. No broker session required."""
    from atr.brokers.iifl.contracts import CACHE_DIR

    segments = []
    if CACHE_DIR.is_dir():
        for path in sorted(CACHE_DIR.glob("*.json")):
            stat = path.stat()
            count: int | None = None
            try:
                import json

                with path.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, list):
                    count = len(payload)
                elif isinstance(payload, dict):
                    for key in ("result", "data", "contracts"):
                        if isinstance(payload.get(key), list):
                            count = len(payload[key])
                            break
            except Exception:  # noqa: BLE001 — size alone is still informative
                count = None
            segments.append(
                {
                    "segment": path.stem,
                    "contracts": count,
                    "size_mb": round(stat.st_size / 1e6, 2),
                    "synced_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return {
        "cache_dir": str(CACHE_DIR),
        "segments": segments,
        "total_contracts": sum(s["contracts"] or 0 for s in segments),
    }


def _live_broker():
    """The broker that may place orders, via the shared access service.

    Delegates rather than building the client here: the construction of an
    authenticated IIFL client used to live in this module, which meant the
    reconciler — a service — would have had to reach into the transport layer for
    one. `atr.services.broker_access` owns it now, and it owns the paper/live gate
    with it, so "can this transmit?" has exactly one answer.
    """
    from atr.services.broker_access import BrokerUnavailable, live_broker

    try:
        return live_broker()
    except BrokerUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _order_principal(request: Request):
    """The account that owns an order placed through a legacy route.

    The legacy order routes predate accounts. An order without an owner is not
    something this platform can store — ``orders.user_id`` is a non-null foreign
    key — and it is not something it *should* store: an unattributed order is one
    nobody can be asked about, and the per-account kill switch has nothing to
    switch.

    So placing an order requires a platform account while read-only legacy routes
    stay open. This is a deliberate tightening of behaviour, recorded in
    ``docs/NEXT_STAGE_GAP_REPORT.md`` §4.2: the previous shape let an
    unauthenticated request reach the exchange with no risk check.
    """
    from atr.api.deps import optional_principal

    principal = optional_principal(request)
    if principal is None or not getattr(principal, "user_id", None):
        raise HTTPException(
            401,
            detail={
                "detail": (
                    "placing an order requires a platform account — sign in first. "
                    "Read-only routes remain available without one."
                ),
                "code": "authentication_required_for_orders",
            },
        )
    return principal


def _require_live_execution(action: str = "order") -> None:
    """Refuse to transmit orders unless the operator has switched to live.

    Called by every endpoint that can reach the broker with an order. Read-only
    paths deliberately skip this — the whole point of paper mode is that you can
    still see the market, the signals, and the queue.
    """
    store = _risk_state()
    if str(store.get("execution_mode", "paper")) != "live":
        raise HTTPException(
            403,
            f"{action} blocked: execution mode is 'paper'. "
            "Switch to live in Trading → Execution mode (a reason is required).",
        )


# ----------------------------------------------------------------------
# Session / login
# ----------------------------------------------------------------------
class LoginIn(BaseModel):
    client_id: str
    auth_code: str


def _login_client():
    from atr.brokers.iifl.auth import SessionStore
    from atr.brokers.iifl.client import IiflClient

    settings: Settings = get_settings()
    return IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
        session_store=SessionStore(settings.iifl_session_cache),
    )


def _session_info(settings: Settings | None = None) -> dict[str, Any]:
    from atr.brokers.iifl.auth import SessionStore, login_url

    settings = settings or get_settings()
    store = SessionStore(settings.iifl_session_cache)
    session = store.load()
    return {
        "session_active": session is not None,
        "client_id": session.client_id if session else None,
        "expires_at": session.expires_at.isoformat() if session else None,
        "login_url": login_url(settings.iifl_app_key, settings.iifl_redirect_url),
    }


@app.get("/login/status")
def login_status() -> dict[str, Any]:
    return _session_info()


@app.post("/login")
def login_submit(body: LoginIn) -> dict[str, Any]:
    """Manual login: exchange clientId + authCode for a session in one call."""
    client = _login_client()
    try:
        session = client.create_session(body.client_id.strip(), body.auth_code.strip())
    except Exception as exc:  # noqa: BLE001 - surface IIFL error text
        raise HTTPException(400, f"login failed: {exc}") from exc
    return {
        "session_active": True,
        "client_id": session.client_id,
        "expires_at": session.expires_at.isoformat(),
    }


_LOGIN_CALLBACK_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>ATR — login</title>
<style>
  body { font-family: -apple-system, Segoe UI, Inter, sans-serif; background: #0f1319; color: #e6e9ef;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }
  .card { background: #161b22; border: 1px solid #26d3; border-radius: 12px; padding: 28px 36px; max-width: 420px; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  p { color: #9aa4b2; margin: 6px 0; }
  .ok { color: #26a69a; }
  .err { color: #ef5350; }
  button { margin-top: 14px; padding: 8px 16px; border: 0; border-radius: 8px; background: #635bff; color: white; cursor: pointer; }
</style>
<script>setTimeout(function() {{ if (window.opener) window.close(); }}, 1200);</script>
</head>
<body>
"""


@app.get("/login/callback")
def login_callback(
    # IIFL actually sends lowercase, unseparated names:
    #   /login/callback?authcode=...&clientid=...
    # The camelCase spellings are what the README claimed and what the docs
    # suggest, and the snake_case ones are ours. Accepting all of them costs
    # nothing and stops a documentation guess from breaking the only path the
    # broker controls.
    authcode: str | None = Query(None),
    clientid: str | None = Query(None),
    authCode: str | None = Query(None),  # noqa: N803 - IIFL's documented casing
    clientId: str | None = Query(None),  # noqa: N803 - IIFL's documented casing
    auth_code: str | None = Query(None),
    client_id: str | None = Query(None),
) -> HTMLResponse:
    """Landing page for the IIFL redirect. Exchanges the code for a session.

    Declaring these as required `client_id`/`auth_code` meant every real
    redirect came back 422 "Field required" as raw JSON, and no session was
    ever created — the login could not complete by any route through the
    browser.
    """
    auth = authcode or authCode or auth_code
    cid = clientid or clientId or client_id

    if not auth or not cid:
        return HTMLResponse(
            _LOGIN_CALLBACK_HTML
            + '<div class="card"><h1>Login failed</h1>'
            + '<p class="err">The redirect did not carry a client id and auth code.</p>'
            + "<p>Expected <code>?authcode=...&amp;clientid=...</code>, which is what "
            + "markets.iiflcapital.com sends. If you opened this URL by hand, check "
            + "the parameter names.</p></div>",
            status_code=400,
        )

    client = _login_client()
    try:
        session = client.create_session(cid, auth)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            _LOGIN_CALLBACK_HTML
            + f'<div class="card"><h1>Login failed</h1><p class="err">{exc}</p>'
            + "<p>Close this tab and try again from the dashboard.</p></div>",
            status_code=400,
        )
    return HTMLResponse(
        _LOGIN_CALLBACK_HTML
        + '<div class="card"><h1 class="ok">Logged in ✓</h1>'
        + f"<p>Client: <strong>{session.client_id}</strong></p>"
        + f"<p>Expires: <strong>{session.expires_at.strftime('%d-%b-%Y %H:%M')} IST</strong></p>"
        + '<p>You can close this tab and return to the dashboard.</p>'
        + "<button onclick='window.close()'>Close</button></div>"
    )


@app.websocket("/ws/ticks")
async def ws_ticks(websocket: WebSocket) -> None:
    """Real-time market ticks stream over WebSocket."""
    import json

    from atr.api.stream import get_broadcaster

    # Browsers apply no CORS to websockets, so without this any page the operator
    # visits could open ws://localhost:8000/ws/ticks and drive the broker feed.
    from urllib.parse import urlparse

    from atr.api.middleware import _ALLOWED_ORIGINS

    origin = websocket.headers.get("origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        host = (websocket.headers.get("host") or "").lower()
        if urlparse(origin).netloc.lower() != host:
            await websocket.close(code=1008)
            return

    broadcaster = get_broadcaster()
    try:
        broadcaster.set_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass

    await broadcaster.connect(websocket)
    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
                action = msg.get("action")
                symbols = msg.get("symbols", [])
                exchange = msg.get("exchange", "NSEEQ")
                if action == "subscribe" and isinstance(symbols, list):
                    await broadcaster.subscribe(websocket, symbols, exchange)
                elif action == "unsubscribe" and isinstance(symbols, list):
                    await broadcaster.unsubscribe(websocket, symbols)
            except Exception:
                pass
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
    except Exception:
        broadcaster.disconnect(websocket)


@app.get("/ticks/history")
def get_tick_history(symbol: str = Query(..., description="Stock symbol"), limit: int = Query(100, ge=1, le=5000)) -> list[dict[str, Any]]:
    """Fetch raw buffered ticks from the in-memory time-series ring buffer."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_tick_history(symbol, limit=limit)


@app.get("/ticks/vwap")
def get_tick_vwap(symbol: str = Query(..., description="Stock symbol"), window: int = Query(900, ge=1, le=86400)) -> dict[str, Any]:
    """Compute instant rolling VWAP over `window` seconds from in-memory ring buffer."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_rolling_vwap(symbol, window_seconds=window)


@app.get("/ticks/candles")
def get_tick_candles(symbol: str = Query(..., description="Stock symbol"), interval: int = 5) -> list[dict[str, Any]]:
    """Resample buffered ticks into sub-minute OHLCV candles via Polars."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_tick_candles(symbol, interval_seconds=interval)


# ----------------------------------------------------------------------
# Frontend (built React app) — API routes above take precedence.
# ----------------------------------------------------------------------
_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


def _mount_frontend() -> None:
    """Mount the built SPA if present. Called at module import."""
    assets = _WEB_DIST / "assets"
    if assets.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    if (_WEB_DIST / "index.html").exists():
        from fastapi.responses import FileResponse

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse | JSONResponse:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            # Resolve before serving: `%2e%2e` decodes to `..` and would otherwise
            # walk out of the dist folder to `.env` or the broker session file.
            root = _WEB_DIST.resolve()
            candidate = (root / path).resolve()
            if path and candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_WEB_DIST / "index.html")


_mount_frontend()
