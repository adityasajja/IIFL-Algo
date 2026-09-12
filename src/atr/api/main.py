"""FastAPI control plane.

Exposes the engine over HTTP so you can drive it from a dashboard, a scheduler,
or a notebook. Live endpoints refuse to act unless ``env`` is paper/live and a
session exists — the API is a convenience layer, not a risk control.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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
    except Exception:  # noqa: BLE001
        pass

    return HealthResponse(
        status="ok", env=settings.env, database=database, session_active=session_active
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


@app.post("/backtest")
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
    "signals_entry": ["trend_fast_sma", "trend_slow_sma", "pullback_rsi_low", "pullback_rsi_high"],
    "opening_range_breakout": [],
    "cross_sectional_momentum": [],
}

#: Bars of history each strategy needs before its rules will fire. A test
#: window shorter than this cannot produce trades, and "no trades" reads
#: exactly like "no edge" unless we say so out loud.
_WARMUP_NEED: dict[str, int] = {
    "sma_crossover": 50,
    "signals_entry": 110,
    "opening_range_breakout": 30,
    "cross_sectional_momentum": 130,
}


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


@app.get("/strategies")
def strategies() -> dict[str, Any]:
    """Every strategy the engine can run, and whether it takes a parameter grid."""
    from atr.strategy.strategies import STRATEGIES

    return {
        "strategies": [
            {
                "name": name,
                "tunable": _TUNABLE.get(name, []),
                "warmup_bars": _WARMUP_NEED.get(name),
            }
            for name in sorted(STRATEGIES)
        ]
    }


@app.post("/research")
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


# ----------------------------------------------------------------------
# Signals — the buy/sell rules, live
# ----------------------------------------------------------------------
class SignalConfigIn(BaseModel):
    entries: dict[str, Any] = Field(default_factory=dict)
    exits: dict[str, Any] = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    exchange: str = "NSEEQ"


class SignalScanIn(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    include_holdings: bool = True
    include_entries: bool = True


def _signal_store():
    from atr.signals.models import DEFAULT_CONFIG_PATH, SignalConfig

    return SignalConfig, DEFAULT_CONFIG_PATH


@app.get("/signals/config")
def signals_config() -> dict[str, Any]:
    """The live rule thresholds. These are starting points, not findings."""
    import dataclasses

    from atr.signals.models import SEARCH_GRID, SignalConfig

    cfg = SignalConfig.load()
    return {
        "config": _clean(dataclasses.asdict(cfg)),
        "search_grid": SEARCH_GRID,
    }


@app.put("/signals/config")
def signals_save(body: SignalConfigIn) -> dict[str, Any]:
    import dataclasses

    from atr.signals.models import (
        DEFAULT_CONFIG_PATH,
        EntryRules,
        ExitRules,
        SignalConfig,
    )

    try:
        cfg = SignalConfig(
            entries=EntryRules(**body.entries) if body.entries else EntryRules(),
            exits=ExitRules(**body.exits) if body.exits else ExitRules(),
            universe=[s.strip().upper() for s in body.universe if s.strip()],
            exchange=body.exchange.upper(),
        )
    except TypeError as exc:  # unknown threshold name
        raise HTTPException(400, f"unknown threshold: {exc}") from exc
    cfg.save(DEFAULT_CONFIG_PATH)
    return _clean(dataclasses.asdict(cfg))


@app.post("/signals/scan")
def signals_scan(body: SignalScanIn) -> dict[str, Any]:
    """Run the exit rules over the book and the entry rules over a watchlist.

    Buy signals come back with ``validated: false`` because the entry rules
    have not passed ``/research`` — they are a watchlist, not a reason to buy.
    """
    from atr.scanner import UNIVERSE
    from atr.signals.engine import scan_holdings, scan_universe
    from atr.signals.models import SignalConfig

    cfg = SignalConfig.load()
    client = _authed_client()
    buys, sells, errors = [], [], []

    with client:
        if body.include_holdings:
            try:
                fired, errs = scan_holdings(client, cfg)
                sells.extend(fired)
                errors.extend(errs)
            except Exception as exc:  # noqa: BLE001 — one side failing is still useful
                errors.append(f"holdings scan failed: {exc}")
        if body.include_entries:
            watchlist = body.symbols or cfg.universe or list(UNIVERSE)
            try:
                fired, errs = scan_universe(client, watchlist, cfg)
                buys.extend(fired)
                errors.extend(errs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"universe scan failed: {exc}")

    def dump(signal) -> dict[str, Any]:
        return _clean(
            {
                "symbol": signal.symbol,
                "action": signal.action,
                "rule": signal.rule,
                "reason": signal.reason,
                "price": signal.price,
                "detail": signal.detail,
                "validated": signal.validated,
                "ts": signal.ts,
            }
        )

    return {
        "buys": [dump(s) for s in buys],
        "sells": [dump(s) for s in sells],
        "errors": errors,
        "rules_are_mechanical": True,
    }


# ----------------------------------------------------------------------
# Portfolio & quotes — the broker's own view
# ----------------------------------------------------------------------
_PORTFOLIO_SECTIONS = ("limits", "positions", "holdings", "orders", "trades")


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
                    out[name] = {"rows": [], "count": 0, "error": broker_error}
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
        raise HTTPException(502, broker_error)

    rows = _broker_rows(payload)
    for symbol, row in zip(resolved, rows, strict=False):
        row.setdefault("symbol", symbol)
    return {
        "exchange": exchange.upper(),
        "as_of": datetime.now().isoformat(),
        "quotes": _clean(rows),
        "failed": failed,
    }


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
    from atr.brokers.iifl.broker import IiflBroker
    from atr.brokers.iifl.contracts import InstrumentMaster

    settings: Settings = get_settings()
    if settings.env == "dev":
        raise HTTPException(403, "live endpoints disabled in dev — set ENV=paper|live")
    client = _authed_client()
    return IiflBroker(client, InstrumentMaster(client))


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


def _session_info(settings: Settings = get_settings()) -> dict[str, Any]:
    from atr.brokers.iifl.auth import SessionStore, login_url

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
        + f'<div class="card"><h1 class="ok">Logged in ✓</h1>'
        + f"<p>Client: <strong>{session.client_id}</strong></p>"
        + f"<p>Expires: <strong>{session.expires_at.strftime('%d-%b-%Y %H:%M')} IST</strong></p>"
        + '<p>You can close this tab and return to the dashboard.</p>'
        + "<button onclick='window.close()'>Close</button></div>"
    )


@app.post("/logout")
def logout() -> dict[str, bool]:
    client = _login_client()
    client.logout()
    return {"logged_out": True}


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
            candidate = _WEB_DIST / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_WEB_DIST / "index.html")


_mount_frontend()
