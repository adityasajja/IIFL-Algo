"""Dashboard summary, market breadth and the portfolio view."""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException


from atr.api.legacy.common import _authed_client, _broker_error, _broker_rows, _clean, _empty_state, _utcnow_iso, _with_ip_hint

logger = logging.getLogger("atr.api")

router = APIRouter()


# ----------------------------------------------------------------------
# Portfolio & quotes — the broker's own view
# ----------------------------------------------------------------------
_PORTFOLIO_SECTIONS = ("limits", "positions", "holdings", "orders", "trades")


@router.get("/dashboard/summary")
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


def _attach_last_price(client: Any, rows: list[dict[str, Any]]) -> None:
    """Put each holding's last traded price on its row, from one batch quote.

    The holdings call carries the average price and the previous close but no current
    price. Without it the page valued a stock from the live tick stream, priced a stock
    with no tick at nothing, and disagreed with the broker's own app. A failed quote
    leaves the rows as they were: no price is better than a wrong one.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for r in rows:  # a stock held in two lots is two rows with one instrument
        if r.get("nseInstrumentId"):
            by_id.setdefault(str(r["nseInstrumentId"]), []).append(r)
    if not by_id:
        return
    try:
        quotes = _broker_rows(client.market_quotes([("NSEEQ", i) for i in by_id]))
    except Exception:  # noqa: BLE001 - a quote failure must not blank the holdings
        return
    for quote in quotes:
        try:
            price = float(quote.get("ltp") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            for row in by_id.get(str(quote.get("instrumentId")), []):
                row["ltp"] = price


@router.get("/portfolio")
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
                if name == "holdings":
                    _attach_last_price(client, rows)
                out[name] = {"rows": _clean(rows), "count": len(rows)}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"rows": [], "count": 0, "error": str(exc)[:300]}
    return {"sections": out, "as_of": datetime.now().isoformat()}
