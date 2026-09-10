"""FastAPI control plane.

Exposes the engine over HTTP so you can drive it from a dashboard, a scheduler,
or a notebook. Live endpoints refuse to act unless ``env`` is paper/live and a
session exists — the API is a convenience layer, not a risk control.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from atr.config.settings import Settings, get_settings

app = FastAPI(title="ATR — algo trading backend", version="0.1.0")

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


class HealthResponse(BaseModel):
    status: str
    env: str
    database: bool
    session_active: bool


class BacktestRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT"])
    initial_cash: float = 1_000_000.0
    fast: int = 20
    slow: int = 50
    slippage_bps: float = 5.0
    square_off_eod: bool = False
    futures: bool = False


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
    database = False
    try:
        from atr.data.store import Database

        database = Database().health()
    except Exception:  # noqa: BLE001 - DB is optional
        pass

    session_active = False
    try:
        from atr.brokers.iifl.auth import SessionStore

        session_active = SessionStore(settings.iifl_session_cache).load() is not None
    except Exception:  # noqa: BLE001
        pass

    return HealthResponse(
        status="ok", env=settings.env, database=database, session_active=session_active
    )


@app.post("/backtest")
def run_backtest(request: BacktestRequest) -> dict[str, Any]:
    from datetime import datetime

    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig, BacktestEngine
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.strategy.strategies import STRATEGIES

    strategy_cls = STRATEGIES.get(request.strategy)
    if strategy_cls is None:
        raise HTTPException(400, f"unknown strategy: {request.strategy}")

    feed = SyntheticFeed(
        SyntheticConfig(
            symbols=tuple(s.upper() for s in request.symbols),
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
    return {
        "strategy": result.strategy_name,
        "metrics": result.metrics.as_dict(),
        "num_fills": int(len(result.fills)),
        "num_trades": int(len(result.trades)),
        "killed": result.killed,
        "kill_reason": result.kill_reason,
    }


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
def place_order(request: OrderRequest) -> dict[str, Any]:
    from atr.core.enums import OrderType, Side
    from atr.core.models import Order

    broker = _live_broker()
    instrument = broker.master.find(request.symbol, request.exchange)
    side = Side.BUY if request.quantity > 0 else Side.SELL
    order = Order(
        instrument=instrument,
        side=side,
        quantity=abs(request.quantity),
        order_type=OrderType(request.order_type.upper()),
        limit_price=request.price,
        tag=request.tag,
        broker_params={"product": request.product} if request.product else {},
    )
    submitted = broker.place_order(order)
    return {
        "order_id": submitted.order_id,
        "broker_order_id": submitted.broker_order_id,
        "status": submitted.status.value,
        "reject_reason": submitted.reject_reason,
    }


@app.post("/risk/kill-switch")
def kill_switch(engaged: bool = True) -> dict[str, bool]:
    """Engage the global kill switch. Blocks all new orders until cleared."""
    store = _risk_state()
    store["kill_switch"] = engaged
    return {"kill_switch": engaged}


@app.get("/risk/status")
def risk_status() -> dict[str, Any]:
    return dict(_risk_state())


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


@app.post("/alerts/rules")
def alert_create(body: AlertRuleIn) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    return _alert_store().upsert(AlertRule(**body.model_dump())).model_dump(mode="json")


@app.patch("/alerts/rules/{rule_id}")
def alert_arm(rule_id: str, armed: bool = True) -> dict[str, Any]:
    from atr.alerts.models import AlertRule

    store = _alert_store()
    for rule in store.rules():
        if rule.id == rule_id:
            updated = AlertRule(**{**rule.model_dump(), "armed": armed})
            return store.upsert(updated).model_dump(mode="json")
    raise HTTPException(404, f"no such rule: {rule_id}")


@app.delete("/alerts/rules/{rule_id}")
def alert_delete(rule_id: str) -> dict[str, bool]:
    if not _alert_store().remove(rule_id):
        raise HTTPException(404, f"no such rule: {rule_id}")
    return {"deleted": True}


@app.get("/alerts/events")
def alert_events(limit: int = 50) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _alert_store().events(limit)]


@app.post("/alerts/check")
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


@app.post("/alerts/test")
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


class BriefingIn(BaseModel):
    top_n: int = 8
    avoid_n: int = 5
    min_price: float = 50.0
    min_day_value_lakh: float = 50.0
    min_bars: int = 60
    universe: str = "all"
    watchlist: list[str] = []
    send_enabled: bool = True


@app.get("/briefing/config")
def briefing_config() -> dict[str, Any]:
    from atr.briefing import last_sent, load_config

    return {"config": load_config().model_dump(), "last_sent": last_sent()}


@app.put("/briefing/config")
def briefing_save(body: BriefingIn) -> dict[str, Any]:
    from atr.briefing import BriefingConfig, save_config

    data = body.model_dump()
    if not data["watchlist"]:
        from atr.scanner import UNIVERSE
        data["watchlist"] = list(UNIVERSE)
    return save_config(BriefingConfig(**data)).model_dump()


@app.post("/briefing/preview")
def briefing_preview() -> dict[str, Any]:
    from atr.briefing import build_brief, load_config

    message, stats = build_brief(load_config())
    return {"message": message, "stats": stats}


@app.post("/briefing/send")
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
    global _STATE
    return _STATE


_STATE: dict[str, Any] = {"kill_switch": False}


def _authed_client():
    """IIFL client with a restored session. Works in any env — market data
    and other read APIs don't need paper/live mode (only order placement
    goes through the `_live_broker` gate below)."""
    from atr.brokers.iifl.client import IiflClient

    settings: Settings = get_settings()
    client = IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
    )
    if client.restore_session() is None:
        raise HTTPException(401, "no active IIFL session — run `atr login`")
    return client


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


@app.get("/scan-all")
def scan_all(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Full-market scan over the local history cache (`atr history sync`).

    No broker session needed and no API calls — scores 2000+ names in
    seconds. Refresh the cache nightly for fresh numbers.
    """
    from datetime import date

    import pandas as pd

    from atr.data.history import load_cached
    from atr.scanner import score_frame

    frames = load_cached(exchange.upper())
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
    return {
        "as_of": date.today().isoformat(),
        "universe": len(frames),
        "scored": len(scan),
        "breadth_up": up,
        "rows": scan.to_dict(orient="records"),
    }


@app.get("/candles")
def candles(
    symbol: str = "RELIANCE-EQ",
    exchange: str = "NSEEQ",
    interval: str = "1d",
    from_date: str = "01-Mar-2026",
    to_date: str = "08-Sep-2026",
) -> dict[str, Any]:
    """Raw OHLCV candles for charting. Interval accepts 1m/5m/15m/30m/60m/1d."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    client = _authed_client()
    master = InstrumentMaster(client)
    master.load_cached([exchange.upper()])
    conid = resolve_conid(master, symbol.upper(), exchange.upper())
    raw = client.historical_data(exchange.upper(), conid, interval, from_date, to_date)
    out = [
        {"ts": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in raw["result"][0]["candles"]
    ]
    return {"symbol": symbol.upper(), "exchange": exchange.upper(),
            "interval": interval, "candles": out}


@app.get("/symbols")
def symbols(query: str = "", exchange: str = "NSEEQ", limit: int = 25) -> dict[str, Any]:
    """Symbol search for the chart header. Served from the cached instrument
    master — no broker session needed."""
    from atr.brokers.iifl.contracts import InstrumentMaster

    master = InstrumentMaster()
    try:
        master.load_cached([exchange.upper()])
    except Exception as exc:  # noqa: BLE001 — cold cache, no client to refresh with
        raise HTTPException(503, f"instrument cache is cold — run `atr instruments sync` ({exc})")
    hits = master.search(query.upper() or "EQ", exchange=exchange.upper(), limit=limit)
    return {
        "results": [
            {"symbol": r.symbol, "exchange": r.exchange, "conid": str(r.conid)}
            for r in hits.itertuples()
        ]
    }


def _live_broker():
    from atr.brokers.iifl.broker import IiflBroker
    from atr.brokers.iifl.contracts import InstrumentMaster

    settings: Settings = get_settings()
    if settings.env == "dev":
        raise HTTPException(403, "live endpoints disabled in dev — set ENV=paper|live")
    client = _authed_client()
    return IiflBroker(client, InstrumentMaster(client))
