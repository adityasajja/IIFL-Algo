"""The track record: what a paper deployment actually did, day by day.

The paper ledger is event-sourced: there is no table of daily equity, only the fills that
produced the account. So a track record is a *replay*. For each trading day we apply every
fill up to the close of that day and value what is still held at that day's close.

Three things are kept honest, because this is the page a person judges the product on:

* **Nothing is invented.** A position with no close on or before a day is valued at its cost
  and the day is flagged ``priced = False``. The response says how many days were affected
  rather than smoothing them over.
* **Costs are inside the number.** Commission is part of the fill and so of the cash; the
  curve is net of the costs the paper venue charged.
* **It says what it is.** ``provenance`` states that fills are *simulated* by the paper
  venue, where prices came from, and how recent they are. A simulated curve is evidence about
  a strategy's logic, not a promise about real fills.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from atr.backtest.portfolio import Portfolio
from atr.core.models import Fill

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DAYS = 90
MAX_DAYS = 365

#: ``(symbol, exchange, day) -> the last close on or before that day``, or None.
CloseOn = Callable[[str, str, date], float | None]


@dataclass(frozen=True)
class DayPoint:
    day: date
    equity: float
    #: False when a held position had to be valued at cost for lack of a close.
    priced: bool


def fill_day(fill: Fill) -> date:
    """The IST trading date a fill belongs to. A naive timestamp is taken as UTC."""
    ts = fill.ts if fill.ts.tzinfo else fill.ts.replace(tzinfo=UTC)
    return ts.astimezone(IST).date()


def replay_equity(
    fills: list[Fill], initial_cash: float, days: list[date], close_on: CloseOn
) -> tuple[list[DayPoint], dict[str, Any]]:
    """Equity at the close of each of ``days``, replaying ``fills`` oldest first.

    Also returns the closed-trade statistics from the same replay. A trade is an *episode*: it opens
    when a position leaves flat and closes when it returns to flat, and its result is the P&L those
    fills realised minus the commission they paid. That is the rule the trade journal uses, so the
    two cannot disagree about what a trade is.
    """
    ordered = sorted(fills, key=lambda f: f.ts)
    portfolio = Portfolio(initial_cash=initial_cash)
    last_mark: dict[str, float] = {}
    out: list[DayPoint] = []
    closed = wins = losses = 0
    gross_win = gross_loss = 0.0
    episode: dict[str, list[float]] = {}  # symbol -> [realised, commission] since it left flat
    i = 0
    for day in days:
        while i < len(ordered) and fill_day(ordered[i]) <= day:
            fill = ordered[i]
            symbol = fill.instrument.symbol
            held = portfolio.positions.get(symbol)
            was_flat = held is None or held.is_flat
            realised = portfolio.apply_fill(fill)
            acc = episode.setdefault(symbol, [0.0, 0.0])
            acc[0] += realised
            acc[1] += abs(fill.commission)
            if not was_flat and portfolio.positions[symbol].is_flat:
                net = acc[0] - acc[1]
                closed += 1
                if net > 0:
                    wins += 1
                    gross_win += net
                elif net < 0:
                    losses += 1
                    gross_loss += -net
                episode.pop(symbol)
            i += 1
        priced = True
        value = portfolio.cash
        for position in portfolio.positions.values():
            if position.is_flat:
                continue
            symbol = position.instrument.symbol
            price = close_on(symbol, position.instrument.exchange, day)
            if price is None:
                price = last_mark.get(symbol)
            if price is None:
                price, priced = position.avg_price, False
            else:
                last_mark[symbol] = price
            value += position.quantity * price * position.instrument.multiplier
        out.append(DayPoint(day=day, equity=value, priced=priced))
    stats = {
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "commission_paid": portfolio.commission_paid,
    }
    return out, stats


def _payoff(wins: int, losses: int, gross_win: float, gross_loss: float) -> dict[str, Any]:
    """Average win, average loss and profit factor, each None when it cannot be measured.

    A win rate alone says nothing: 53% of trades can win and the book still lose money. The
    payoff is what turns it into an expectancy. Profit factor needs a loss to divide by.
    """
    return {
        "win_count": wins,
        "loss_count": losses,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "avg_win": gross_win / wins if wins else None,
        "avg_loss": -gross_loss / losses if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
    }


def max_drawdown_pct(values: list[float]) -> float:
    """The deepest peak-to-trough fall, as a negative percent (0.0 when it never fell)."""
    peak = worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v / peak - 1.0) * 100.0)
    return worst


def current_drawdown_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = max(values)
    return (values[-1] / peak - 1.0) * 100.0 if peak > 0 else 0.0


def build_record(
    *,
    fills: list[Fill],
    initial_cash: float,
    calendar: list[date],
    close_on: CloseOn,
    benchmark: dict[date, float] | None,
    window_days: int,
    today: date,
) -> dict[str, Any]:
    """Equity, benchmark and summary for one account, over the last ``window_days``."""
    if not calendar or initial_cash <= 0:
        return {"has_data": False}
    points, stats = replay_equity(fills, initial_cash, calendar, close_on)

    cutoff = today - timedelta(days=window_days)
    shown = [p for p in points if p.day >= cutoff] or points[-1:]
    # Return is measured from the capital when the whole history fits the window, and from the
    # first shown day when older history was cut off, so the figure matches the chart.
    base = initial_cash if shown[0].day == points[0].day else shown[0].equity
    values = [p.equity for p in shown]

    bench_rows: list[float | None] = []
    bench_base: float | None = None
    if benchmark:
        bench_base = benchmark.get(shown[0].day)
        for p in shown:
            b = benchmark.get(p.day)
            bench_rows.append(None if b is None or not bench_base else base * b / bench_base)
    else:
        bench_rows = [None] * len(shown)

    ret = (values[-1] / base - 1.0) * 100.0 if base else 0.0
    bench_vals = [b for b in bench_rows if b is not None]
    bench_ret = (bench_vals[-1] / base - 1.0) * 100.0 if bench_vals and base else None
    closed = stats["closed_trades"]
    return {
        "has_data": True,
        "capital": initial_cash,
        "base": base,
        "start": shown[0].day.isoformat(),
        "end": shown[-1].day.isoformat(),
        "trading_days": len(shown),
        "points": [
            {"d": p.day.isoformat(), "equity": round(p.equity, 2), "benchmark": None if b is None else round(b, 2)}
            for p, b in zip(shown, bench_rows, strict=True)
        ],
        "summary": {
            "return_pct": ret,
            "benchmark_return_pct": bench_ret,
            "excess_pct": None if bench_ret is None else ret - bench_ret,
            "max_drawdown_pct": max_drawdown_pct([base, *values]),
            "current_drawdown_pct": current_drawdown_pct([base, *values]),
            "net_pnl": values[-1] - base,
            "closed_trades": closed,
            "win_rate_pct": (stats["wins"] / closed * 100.0) if closed else None,
            "commission_paid": stats["commission_paid"],
            **_payoff(stats["wins"], stats["losses"], stats["gross_win"], stats["gross_loss"]),
        },
        "unpriced_days": sum(1 for p in shown if not p.priced),
    }


def combine(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Add several accounts into one: the whole paper book as a single curve."""
    live = [r for r in records if r.get("has_data")]
    if not live:
        return {"has_data": False}
    if len(live) == 1:
        return live[0]
    days = sorted({p["d"] for r in live for p in r["points"]})
    by_rec = [{p["d"]: p for p in r["points"]} for r in live]
    carry: list[dict[str, Any] | None] = [None] * len(live)
    points: list[dict[str, Any]] = []
    for d in days:
        eq = 0.0
        bench: float | None = 0.0
        for k, rec in enumerate(by_rec):
            if d in rec:
                carry[k] = rec[d]
            row = carry[k]
            # Before an account's first shown day it was still just its starting capital.
            eq += row["equity"] if row else live[k]["base"]
            if row is None:
                bench = bench + live[k]["base"] if bench is not None else None
            elif row["benchmark"] is None:
                bench = None
            elif bench is not None:
                bench += row["benchmark"]
        points.append({"d": d, "equity": round(eq, 2), "benchmark": None if bench is None else round(bench, 2)})
    base = sum(r["base"] for r in live)
    values = [p["equity"] for p in points]
    bench_vals = [p["benchmark"] for p in points if p["benchmark"] is not None]
    ret = (values[-1] / base - 1.0) * 100.0
    bench_ret = (bench_vals[-1] / base - 1.0) * 100.0 if bench_vals else None
    closed = sum(r["summary"]["closed_trades"] for r in live)
    wins = sum(r["summary"]["win_count"] for r in live)
    losses = sum(r["summary"]["loss_count"] for r in live)
    gross_win = sum(r["summary"]["gross_win"] for r in live)
    gross_loss = sum(r["summary"]["gross_loss"] for r in live)
    return {
        "has_data": True,
        "capital": sum(r["capital"] for r in live),
        "base": base,
        "start": days[0],
        "end": days[-1],
        "trading_days": len(days),
        "points": points,
        "summary": {
            "return_pct": ret,
            "benchmark_return_pct": bench_ret,
            "excess_pct": None if bench_ret is None else ret - bench_ret,
            "max_drawdown_pct": max_drawdown_pct([base, *values]),
            "current_drawdown_pct": current_drawdown_pct([base, *values]),
            "net_pnl": values[-1] - base,
            "closed_trades": closed,
            "win_rate_pct": (wins / closed * 100.0) if closed else None,
            "commission_paid": sum(r["summary"]["commission_paid"] for r in live),
            **_payoff(wins, losses, gross_win, gross_loss),
        },
        "unpriced_days": max(r["unpriced_days"] for r in live),
    }


# ───────────────────────────── data access ─────────────────────────────


def _series(path: Path) -> dict[date, float] | None:
    """Daily closes from a parquet file as ``{date: close}``, or None if unusable."""
    try:
        frame = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - an unreadable file is "no prices"
        logger.warning("track record: could not read {}", path)
        return None
    if frame.empty or "close" not in frame.columns or "ts" not in frame.columns:
        return None
    frame = frame[frame["close"].notna() & (frame["close"] > 0)]
    days = pd.to_datetime(frame["ts"]).dt.date
    return dict(zip(days, frame["close"].astype(float), strict=True))


class _LastOnOrBefore:
    """``{date: close}`` with an as-of lookup: the last close on or before a day."""

    def __init__(self, closes: dict[date, float]) -> None:
        self._days = sorted(closes)
        self._closes = closes

    def __call__(self, day: date) -> float | None:
        import bisect

        i = bisect.bisect_right(self._days, day)
        return self._closes[self._days[i - 1]] if i else None

    @property
    def last_day(self) -> date | None:
        return self._days[-1] if self._days else None


@dataclass
class TrackRecordService:
    """Builds track records from the paper ledger and the local daily price cache."""

    ledger: Any = None
    db: Any = None
    data_root: Path | None = None
    today: Callable[[], date] | None = None

    def __post_init__(self) -> None:
        from atr.appdb.engine import get_app_db
        from atr.market_intel.service import DATA_ROOT
        from atr.services.paper import PaperLedger

        self.db = self.db or get_app_db()
        self.ledger = self.ledger or PaperLedger(db=self.db)
        self.data_root = Path(self.data_root) if self.data_root else Path(DATA_ROOT)

    # -- prices ---------------------------------------------------------
    def _stock_closes(self) -> tuple[CloseOn, Callable[[], date | None]]:
        from atr.instruments.service import CACHE_ROOT, get_instrument_master

        cache: dict[tuple[str, str], _LastOnOrBefore | None] = {}
        latest: list[date] = []

        def lookup(symbol: str, exchange: str) -> _LastOnOrBefore | None:
            key = (symbol, exchange)
            if key not in cache:
                series = None
                try:
                    record = get_instrument_master().get(symbol, exchange)
                    cache_file = getattr(record, "cache_file", None)
                    path = Path(CACHE_ROOT) / cache_file if cache_file else None
                    closes = _series(path) if path and path.exists() else None
                    series = _LastOnOrBefore(closes) if closes else None
                except Exception:  # noqa: BLE001 - an unknown symbol is "no price"
                    series = None
                if series and series.last_day:
                    latest.append(series.last_day)
                cache[key] = series
            return cache[key]

        def close_on(symbol: str, exchange: str, day: date) -> float | None:
            series = lookup(symbol, exchange)
            return series(day) if series else None

        return close_on, lambda: max(latest) if latest else None

    def _is_demo(self) -> bool:
        from atr.services.data_status import is_demo

        return is_demo(self.data_root)

    def _benchmark(self) -> tuple[dict[date, float] | None, dict[str, Any]]:
        from atr.data.indices import NIFTY_50, index_path

        path = index_path(self.data_root, NIFTY_50)
        closes = _series(path) if path.exists() else None
        if not closes:
            return None, {"name": NIFTY_50, "source": None, "as_of": None}
        try:
            source = path.with_suffix(".source").read_text(encoding="utf8").strip() or None
        except OSError:
            source = None
        return closes, {"name": NIFTY_50, "source": source, "as_of": max(closes).isoformat()}

    # -- records --------------------------------------------------------
    def _record_for(
        self, row: dict[str, Any], user_id: str, window_days: int, close_on: CloseOn,
        benchmark: dict[date, float] | None, calendar: list[date], today: date,
    ) -> dict[str, Any]:
        fills = self.ledger.fills(user_id, deployment_id=row["deployment_id"])
        capital = float(row["capital"])
        first = min([fill_day(f) for f in fills], default=None)
        if first is None:
            return {"has_data": False}
        days = [d for d in calendar if d >= first]
        return build_record(
            fills=fills, initial_cash=capital, calendar=days, close_on=close_on,
            benchmark=benchmark, window_days=window_days, today=today,
        )

    def _calendar(self, benchmark: dict[date, float] | None, since: date, today: date) -> list[date]:
        """Trading days from ``since``: the index's own days, else weekdays."""
        # The first fill's own day is always a point, so a deployment that began today (before
        # any close exists for it) still has a curve of one rather than none.
        if benchmark:
            return sorted({d for d in benchmark if since <= d <= today} | {since})
        return [since + timedelta(days=n) for n in range((today - since).days + 1)
                if (since + timedelta(days=n)).weekday() < 5 or n == 0]

    def journal_status(self, user_id: str) -> dict[str, Any]:
        """Whether the trade journal agrees with the ledger about how many trades have closed.

        Performance pages read the journal; the track record replays the ledger. They are meant to
        be the same arithmetic (the journal is a projection of the position fold), but the journal
        is written by a reconcile step, so it can lag. This says whether it does.
        """
        from atr.appdb.repositories import DeploymentRepository, TradeJournalRepository

        ledger_closed = journal_closed = 0
        with self.db.session() as session:
            rows = [r for r in DeploymentRepository.list_for_user(session, user_id)
                    if str(r.get("mode", "")).upper() == "PAPER" and r.get("status") != "STOPPED"]
        for row in rows:
            fills = self.ledger.fills(user_id, deployment_id=row["deployment_id"])
            if not fills:
                continue
            _, stats = replay_equity(fills, float(row["capital"]), [fill_day(fills[-1])], lambda *_: None)
            ledger_closed += stats["closed_trades"]
            with self.db.session() as session:
                _, total = TradeJournalRepository.list_for_user(
                    session, user_id, deployment_id=row["deployment_id"], closed_only=True, limit=1
                )
            journal_closed += int(total)
        return {
            "ledger_closed": ledger_closed,
            "journal_closed": journal_closed,
            "in_sync": ledger_closed == journal_closed,
        }

    def build(
        self, user_id: str, *, deployment_id: str | None = None, days: int = DEFAULT_DAYS
    ) -> dict[str, Any]:
        from atr.appdb.repositories import DeploymentRepository

        days = max(1, min(int(days), MAX_DAYS))
        today = self.today() if self.today else datetime.now(IST).date()
        with self.db.session() as session:
            if deployment_id:
                row = DeploymentRepository.get(session, deployment_id, user_id)
                rows = [row] if row else []
            else:
                rows = [r for r in DeploymentRepository.list_for_user(session, user_id)
                        if str(r.get("mode", "")).upper() == "PAPER" and r.get("started_at")]
        close_on, stocks_latest = self._stock_closes()
        benchmark, bench_meta = self._benchmark()

        records: list[dict[str, Any]] = []
        for row in rows:
            fills_first = self.ledger.fills(user_id, deployment_id=row["deployment_id"])
            if not fills_first:
                continue
            since = min(fill_day(f) for f in fills_first)
            calendar = self._calendar(benchmark, since, today)
            records.append(self._record_for(row, user_id, days, close_on, benchmark, calendar, today))
        record = combine(records)

        stocks_as_of = stocks_latest()
        return {
            "scope": "deployment" if deployment_id else "all",
            "deployment_id": deployment_id,
            "window_days": days,
            "deployments": len(records),
            **record,
            "provenance": {
                "simulated": True,
                "demo": self._is_demo(),
                "fills": "Simulated by the paper venue against recorded prices, with modelled slippage and brokerage. No real order was sent.",
                "prices": "Daily closes from the local cache (broker history, topped up from public bars).",
                "prices_as_of": stocks_as_of.isoformat() if stocks_as_of else None,
                "benchmark": bench_meta,
                "complete": record.get("unpriced_days", 0) == 0 if record.get("has_data") else True,
                "unpriced_days": record.get("unpriced_days", 0),
            },
        }
