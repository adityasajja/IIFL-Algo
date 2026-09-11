"""Signal scanning: holdings out, alerts in.

Daily history is refreshed from the API for the symbols actually being scanned
rather than read from the cache alone. The cache lags by at least a session, and
an SMA or RSI computed across a gap is quietly wrong — the sort of error that
looks like a signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from atr.alerts.engine import IST
from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.data.history import CACHE_ROOT
from atr.signals.models import ScanResult, Signal, SignalConfig
from atr.signals.rules import append_live_bar, eval_entry, eval_exit, primary_exit

OHLCV = ["open", "high", "low", "close", "volume"]
#: Calendar days of daily candles to request — comfortably over a year of sessions.
_LOOKBACK_DAYS = 420


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict)]
        if isinstance(result, dict):
            return [result]
        return []
    return [r for r in (payload or []) if isinstance(r, dict)]


def _candles_to_frame(payload) -> pd.DataFrame:
    """Turn a historical-data response into a daily OHLCV frame."""
    from atr.brokers.iifl.feeds import _candle_row, _candles

    rows = []
    for raw in _candles(payload):
        row = _candle_row(raw)
        if row is None:
            continue
        rows.append(
            {
                "ts": pd.to_datetime(row["ts"]),
                "open": float(row["open"] or 0),
                "high": float(row["high"] or 0),
                "low": float(row["low"] or 0),
                "close": float(row["close"] or 0),
                "volume": float(row["volume"] or 0),
            }
        )
    if not rows:
        return pd.DataFrame(columns=OHLCV)
    return (
        pd.DataFrame(rows)
        .sort_values("ts")
        .drop_duplicates("ts", keep="last")
        .reset_index(drop=True)
    )


def load_daily(
    symbol: str,
    exchange: str,
    client: IiflClient | None,
    conid,
    lookback_days: int = _LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Fresh daily candles, falling back to the local cache.

    ``lookback_days`` is raised for validation, which needs years rather than
    months — a year of dailies cannot be carved into folds that also leave room
    for indicator warmup.
    """
    if client is not None and conid is not None:
        to_date = datetime.now(IST)
        from_date = to_date - timedelta(days=lookback_days)
        try:
            payload = client.historical_data(
                exchange=exchange,
                instrument_id=str(conid),
                interval="1d",
                from_date=from_date,
                to_date=to_date,
            )
            frame = _candles_to_frame(payload)
            if len(frame) > 30:
                return frame
            logger.debug("{}: only {} fresh daily bars, using cache", symbol, len(frame))
        except Exception as exc:  # noqa: BLE001 - fall back to cache
            logger.debug("fresh dailies failed for {}: {}", symbol, exc)

    path = CACHE_ROOT / exchange.upper() / f"{symbol}.parquet"
    if path.exists():
        try:
            frame = pd.read_parquet(path).sort_values("ts").reset_index(drop=True)
            if not frame.empty:
                return frame[OHLCV]
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache read failed for {}: {}", symbol, exc)
    return pd.DataFrame(columns=OHLCV)


def _quotes(client: IiflClient, legs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """One bulk quote call, keyed by (exchange, instrumentId).

    Keyed rather than positional: sending the same instrument twice (two lots of
    the same stock) otherwise returns two rows and any positional lookup
    misattributes them.
    """
    unique = list(dict.fromkeys((ex.upper(), str(i)) for ex, i in legs if i))
    if not unique:
        return {}
    raw = client.market_quotes(unique)
    out: dict[tuple[str, str], dict] = {}
    for row in _rows(raw):
        key = (str(row.get("exchange", "")).upper(), str(row.get("instrumentId", "")))
        ltp = float(row.get("ltp") or 0)
        prev = float(row.get("close") or 0)
        out[key] = {
            "ltp": ltp,
            "prev_close": prev,
            "day_chg": (ltp / prev - 1) * 100 if prev else 0.0,
            "volume": float(row.get("tradedVolume") or 0),
        }
    return out


def _resolve(client: IiflClient, symbols: list[str], exchange: str) -> dict[str, str]:
    """symbol -> instrumentId, skipping anything that will not resolve."""
    master = InstrumentMaster(client)
    master.load_cached([exchange])
    found: dict[str, str] = {}
    for symbol in symbols:
        try:
            instrument = master.find(symbol, exchange)
        except KeyError:
            logger.debug("{} not found on {}", symbol, exchange)
            continue
        if instrument.conid is not None:
            found[symbol] = str(instrument.conid)
    return found


def scan_holdings(
    client: IiflClient,
    config: SignalConfig | None = None,
) -> tuple[list[Signal], list[str]]:
    """Run the exit rules over the positions actually held."""
    cfg = config or SignalConfig()
    holdings = _rows(client.holdings())
    if not holdings:
        return [], ["no holdings returned"]

    # Collapse multiple lots of the same symbol into one economic position.
    positions: dict[str, dict] = {}
    errors: list[str] = []
    for row in holdings:
        symbol = str(row.get("nseTradingSymbol") or row.get("bseTradingSymbol") or "").strip()
        if not symbol:
            continue
        qty = float(row.get("totalQuantity") or 0)
        avg = float(row.get("averageTradedPrice") or 0)
        if qty <= 0:
            continue
        entry = positions.setdefault(
            symbol, {"qty": 0.0, "cost": 0.0, "conid": row.get("nseInstrumentId")}
        )
        entry["qty"] += qty
        entry["cost"] += qty * avg
        if entry["conid"] is None:
            entry["conid"] = row.get("nseInstrumentId")

    legs = [(cfg.exchange, str(p["conid"])) for p in positions.values() if p["conid"]]
    quotes = _quotes(client, legs)

    signals: list[Signal] = []
    for symbol, position in positions.items():
        avg_price = position["cost"] / position["qty"] if position["qty"] else 0.0
        quote = quotes.get((cfg.exchange.upper(), str(position["conid"])))
        if quote is None or quote["ltp"] <= 0:
            errors.append(f"{symbol}: no quote")
            continue
        frame = load_daily(symbol, cfg.exchange, client, position["conid"])
        if frame.empty:
            errors.append(f"{symbol}: no daily history")
            continue
        frame = append_live_bar(frame, quote["ltp"], quote.get("volume"))
        fired = eval_exit(symbol, frame, avg_price, cfg.exits, quantity=position["qty"])
        best = primary_exit(fired)
        if best is not None:
            signals.append(best)
    return signals, errors


def scan_universe(
    client: IiflClient,
    symbols: list[str],
    config: SignalConfig | None = None,
) -> tuple[list[Signal], list[str]]:
    """Run the entry rules over a watchlist."""
    cfg = config or SignalConfig()
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return [], ["no symbols given"]

    resolved = _resolve(client, symbols, cfg.exchange)
    missing = [s for s in symbols if s not in resolved]
    errors = [f"{s}: not in the instrument master" for s in missing]

    quotes = _quotes(client, [(cfg.exchange, cid) for cid in resolved.values()])
    signals: list[Signal] = []
    for symbol, conid in resolved.items():
        quote = quotes.get((cfg.exchange.upper(), str(conid)))
        if quote is None or quote["ltp"] <= 0:
            errors.append(f"{symbol}: no quote")
            continue
        frame = load_daily(symbol, cfg.exchange, client, conid)
        if frame.empty:
            errors.append(f"{symbol}: no daily history")
            continue
        frame = append_live_bar(frame, quote["ltp"], quote.get("volume"))
        signals.extend(eval_entry(symbol, frame, cfg.entries))
    return signals, errors


def format_report(result: ScanResult, *, validated: bool = False) -> tuple[str, str]:
    """(title, body) for Telegram or the console."""
    title = f"ATR signals — {datetime.now(IST):%d %b %H:%M}"
    lines: list[str] = []

    if result.sells:
        lines.append(f"SELL ({len(result.sells)})")
        for signal in result.sells:
            detail = signal.detail
            lines.append(
                f"  {signal.symbol} @ {signal.price:,.2f} | {signal.rule}: {signal.reason}"
                f" | P&L {detail.get('pnl_pct', 0):+.1f}%"
            )
        lines.append("")

    if result.buys:
        lines.append(f"BUY ({len(result.buys)})")
        for signal in result.buys:
            lines.append(f"  {signal.symbol} @ {signal.price:,.2f} | {signal.rule}: {signal.reason}")
        if not validated:
            lines.append("")
            lines.append(
                "These entry rules have NOT passed out-of-sample validation. "
                "Treat as a watchlist, not a reason to buy. Run: atr signals validate"
            )
        lines.append("")

    if not result.empty:
        lines.append("Rules are mechanical; position sizing and risk limits are yours.")

    body = "\n".join(lines).strip() or "No signals."
    return title, body
