"""Alert evaluation: bulk quotes in, firings out.

One `marketquotes` call covers every armed rule, so a 50-symbol watchlist
costs a single API round-trip. Indicator rules (RSI/crosses) read the local
daily cache with today's live price appended as the forming bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

from atr.alerts.channels import Channel
from atr.alerts.models import AlertEvent, AlertRule
from atr.alerts.store import AlertStore
from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.data.history import CACHE_ROOT
from atr.strategy.indicators import crossover, rsi, sma

IST = timezone(timedelta(hours=5, minutes=30))


def market_open_now(now: datetime | None = None) -> bool:
    now = (now or datetime.now(tz=IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    return now.hour * 60 + now.minute >= 9 * 60 + 15 and now.hour * 60 + now.minute < 15 * 60 + 30


def fetch_quotes(client: IiflClient, symbols: list[tuple[str, str]]) -> dict[str, dict]:
    """{(symbol): {ltp, day_chg, open, high, low, prev_close}} via one call."""
    master = InstrumentMaster(client)
    master.load_cached(sorted({ex for _, ex in symbols}))
    legs, keys = [], []
    for symbol, exchange in symbols:
        try:
            conid = getattr(master.find(symbol, exchange), "conid", None)
        except KeyError:
            continue
        legs.append((exchange, str(conid)))
        keys.append(symbol)
    if not legs:
        return {}
    out: dict[str, dict] = {}
    raw = client.market_quotes(legs)
    rows = raw.get("result", []) if isinstance(raw, dict) else raw
    for row in rows:
        ltp = float(row.get("ltp") or 0)
        prev = float(row.get("close") or 0)
        sym = keys[legs.index((row.get("exchange"), str(row.get("instrumentId"))))]
        out[sym] = {
            "ltp": ltp,
            "prev_close": prev,
            "day_chg": (ltp / prev - 1) * 100 if prev else 0.0,
            "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
        }
    return out


def context_frame(symbol: str, ltp: float) -> pd.DataFrame | None:
    """Cached dailies + live price as the forming bar (for RSI/cross rules)."""
    for path in Path(CACHE_ROOT).glob(f"*/{symbol}.parquet"):
        try:
            df = pd.read_parquet(path).sort_values("ts").reset_index(drop=True)
            if not df.empty:
                df = pd.concat([df, pd.DataFrame([{
                    "ts": pd.Timestamp.now(), "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp,
                    "volume": float(df["volume"].iloc[-1] or 0),
                }])], ignore_index=True)
                return df
        except Exception:  # noqa: BLE001
            continue
    return None


def evaluate(rule: AlertRule, quote: dict, frame: pd.DataFrame | None) -> tuple[bool, str]:
    ltp = quote["ltp"]
    if rule.kind == "price_above":
        return ltp >= rule.threshold, f"{rule.symbol} at {ltp} (≥ {rule.threshold})"
    if rule.kind == "price_below":
        return ltp <= rule.threshold, f"{rule.symbol} at {ltp} (≤ {rule.threshold})"
    if rule.kind == "day_drop_pct":
        return quote["day_chg"] <= -abs(rule.threshold), \
            f"{rule.symbol} down {quote['day_chg']:.1f}% today @ {ltp}"
    if rule.kind == "day_gain_pct":
        return quote["day_chg"] >= abs(rule.threshold), \
            f"{rule.symbol} up {quote['day_chg']:.1f}% today @ {ltp}"
    if frame is None or len(frame) < 30:
        return False, "no daily context"
    c = frame["close"]
    if rule.kind == "rsi_below":
        v = float(rsi(c).iloc[-1])
        return v <= rule.threshold, f"{rule.symbol} RSI {v:.0f} (≤ {rule.threshold}) @ {ltp}"
    if rule.kind == "rsi_above":
        v = float(rsi(c).iloc[-1])
        return v >= rule.threshold, f"{rule.symbol} RSI {v:.0f} (≥ {rule.threshold}) @ {ltp}"
    s20, s50 = sma(c, 20), sma(c, 50)
    if rule.kind == "sma_cross_up":
        hit = bool(crossover(s20, s50).tail(5).any())
        return hit, f"{rule.symbol} golden cross near {ltp}"
    if rule.kind == "sma_cross_down":
        from atr.strategy.indicators import crossunder
        hit = bool(crossunder(s20, s50).tail(5).any())
        return hit, f"{rule.symbol} death cross near {ltp}"
    return False, "unknown rule kind"


def check(store: AlertStore, client: IiflClient, channels: list[Channel],
          now: datetime | None = None) -> list[AlertEvent]:
    """Evaluate all armed rules. Returns the events fired (possibly empty)."""
    now = now or datetime.now()
    rules = [r for r in store.rules() if r.armed]
    if not rules:
        return []
    quotes = fetch_quotes(client, [(r.symbol, r.exchange) for r in rules])
    fired: list[AlertEvent] = []
    for rule in rules:
        if rule.last_fired_at and now - rule.last_fired_at < timedelta(minutes=rule.cooldown_min):
            continue
        quote = quotes.get(rule.symbol)
        if not quote or not quote["ltp"]:
            continue
        frame = context_frame(rule.symbol, quote["ltp"]) \
            if rule.kind.startswith(("rsi", "sma")) else None
        hit, message = evaluate(rule, quote, frame)
        if not hit:
            continue
        sent_on = "none"
        ok = False
        for ch in channels:
            if ch.send(f"ATR alert: {rule.display}", message):
                sent_on, ok = ch.name, True
                break
        event = AlertEvent(rule_id=rule.id, rule=rule.display,
                           message=message, channel=sent_on, ok=ok)
        store.log(event)
        rule.last_fired_at = now
        store.upsert(rule)
        logger.warning("alert fired [{}] {}", sent_on, message)
        fired.append(event)
    return fired
