"""Daily profit and loss, for the calendar: one series for paper trades, one for the real book.

Two series, kept apart on purpose because they mean different things:

**Paper** adds up every paper trade the app has closed, across all strategies:
the paper-trading engine's journal, the forward paper ledger, and the Monday gap plan.
The last two record returns rather than rupees, so they are converted at a stated
virtual size (``VIRTUAL_TICKET``) and the calendar says so.

**Real** is the day-by-day change in the value of the holdings, from the local daily
prices. That is what a portfolio screen shows as "today's P&L". Days computed after
the fact from *today's* holdings are marked ``estimated``, because the book may have
looked different then; a day recorded on its own date from the holdings at that time is
not. Profit from buying and selling is a separate record, ``realised_pnl.json``: entered by
hand, or from the broker's tradebook where it can be read. A day's figure on the
calendar is the two added together, and the day view shows them as separate lines.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

#: The rupee size given to a trade that was recorded only as a percentage return.
VIRTUAL_TICKET = 100_000.0
BACKFILL_SESSIONS = 90
#: A day needs at least this share of the holdings priced, or it is left out rather than
#: reported understated.
MIN_COVERAGE = 0.8


@dataclass
class Day:
    date: str
    pnl: float
    trades: int = 0
    wins: int = 0
    losses: int = 0
    estimated: bool = False
    realised: float = 0.0  # profit from buying and selling, included in ``pnl``


# --------------------------------------------------------------------------- paper
def _add(days: dict[str, Day], when: str, pnl: float) -> None:
    d = days.setdefault(when, Day(date=when, pnl=0.0))
    d.pnl += pnl
    d.trades += 1
    d.wins += pnl > 0
    d.losses += pnl < 0


def _journal_trades(db: Any) -> list[tuple[str, float]]:
    """Closed paper-engine trades as ``(exit date, net rupees)``."""
    from sqlalchemy import select

    from atr.appdb.schema import deployments, trade_journal

    stmt = (
        select(trade_journal.c.exit_ts, trade_journal.c.net_pnl, trade_journal.c.gross_pnl)
        .join(deployments, deployments.c.deployment_id == trade_journal.c.deployment_id)
        .where(deployments.c.mode == "PAPER", trade_journal.c.exit_ts.is_not(None))
    )
    with db.session() as session:
        rows = session.execute(stmt).all()
    out = []
    for exit_ts, net, gross in rows:
        value = net if net is not None else gross
        if value is not None:
            out.append((str(exit_ts)[:10], float(value)))
    return out


def paper_days(data_root: Path, db: Any = None) -> dict[str, Day]:
    """Everything paper-traded and closed, by the day it closed."""
    days: dict[str, Day] = {}
    try:
        if db is None:
            from atr.appdb.engine import get_app_db

            db = get_app_db()
        for when, pnl in _journal_trades(db):
            _add(days, when, pnl)
    except Exception as exc:  # noqa: BLE001 - the calendar must load without the journal
        logger.info("paper journal unavailable for the calendar: {}", exc)

    # The forward paper ledger settles a week at a time: an equal-weight return on the
    # picks, dated when its exit window closed.
    ledger = Path(data_root) / "paper_momentum" / "settlements.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf8").splitlines():
            try:
                row = json.loads(line)
                _add(days, row["exit_window_end"], float(row["portfolio_return"]) * VIRTUAL_TICKET)
            except (ValueError, KeyError, TypeError):
                continue

    # The Monday gap plan's live trades. Replayed weeks are history re-run after the fact,
    # not trades the app took, so they stay out of the calendar.
    plan = Path(data_root) / "research" / "gap_plan.json"
    if plan.exists():
        try:
            for t in json.loads(plan.read_text(encoding="utf8")).get("trades", []):
                if t.get("source") == "live" and t.get("market_ok") and t.get("status") == "closed":
                    _add(days, t["exit_date"], float(t["net_pct"]) / 100 * VIRTUAL_TICKET)
        except (OSError, ValueError, KeyError):
            pass
    return days


# --------------------------------------------------------------------------- real
def store_path(data_root: Path) -> Path:
    return Path(data_root) / "portfolio" / "pnl_daily.json"


def realised_path(data_root: Path) -> Path:
    return Path(data_root) / "portfolio" / "realised_pnl.json"


def realised_entries(data_root: Path) -> dict[str, dict[str, Any]]:
    """Trading profit recorded per day: ``{date: {amount, source, note, updated}}``."""
    try:
        return json.loads(realised_path(data_root).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def set_realised(data_root: Path, day: str, amount: float, *, note: str = "", source: str = "manual") -> None:
    """Record (or replace) the profit made by trading on ``day``."""
    date.fromisoformat(day)  # a real date, or this raises
    if not math.isfinite(amount):
        raise ValueError("amount must be a finite number")
    entries = realised_entries(data_root)
    entries[day] = {"amount": round(float(amount), 2), "source": source, "note": note.strip()[:200],
                    "updated": datetime.now().isoformat(timespec="seconds")}
    _write_json(realised_path(data_root), entries)


def clear_realised(data_root: Path, day: str) -> bool:
    entries = realised_entries(data_root)
    if day not in entries:
        return False
    del entries[day]
    _write_json(realised_path(data_root), entries)
    return True


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf8")
    tmp.replace(path)  # a crash mid-write never leaves half a file


def _stored_days(data_root: Path) -> dict[str, Day]:
    """The holdings series exactly as stored, with no trading profit mixed in."""
    try:
        saved = json.loads(store_path(data_root).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}
    keep = ("date", "pnl", "trades", "wins", "losses", "estimated")
    return {k: Day(**{f: v[f] for f in keep if f in v}) for k, v in saved.get("days", {}).items()}


def real_days(data_root: Path) -> dict[str, Day]:
    """Each day's holdings change plus any trading profit recorded for it."""
    days = _stored_days(data_root)
    for when, entry in realised_entries(data_root).items():
        amount = float(entry["amount"])
        d = days.get(when)
        if d is None:  # a trading day with no holdings reading still belongs on the calendar
            days[when] = Day(date=when, pnl=amount, trades=0, realised=amount)
        else:
            d.pnl, d.realised = round(d.pnl + amount, 2), amount
    return days


def _closes(data_root: Path, symbol: str) -> pd.Series | None:
    """Daily closes for a holding, trying both spellings the price folder uses."""
    from atr.data.gap_repair import HELD_FOLDER

    root = Path(data_root) / "iifl_daily"
    for folder in (root / "NSEEQ", root / HELD_FOLDER):
        for name in (symbol, symbol[:-3] if symbol.endswith("-EQ") else symbol):
            path = folder / f"{name}.parquet"
            if path.exists():
                df = pd.read_parquet(path, columns=["ts", "close"]).dropna()
                df = df[df["close"] > 0].drop_duplicates("ts")
                return pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(df["ts"]).normalize())
    return None


def _daily_change(holdings: list[dict[str, Any]], closes: dict[str, pd.Series]) -> pd.DataFrame:
    """Each holding's price change from the previous *session* to each day.

    A holding is priced on a day only if it has a bar both that day and on the session
    before it. One with a missing bar is left out (NaN) for that day and the next, rather
    than being compared with an older close, which would credit a multi-day move to one
    day. The calendar and its day view both read this, so they cannot disagree.
    """
    series = {h["symbol"]: closes[h["symbol"]] for h in holdings if h["symbol"] in closes}
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1).sort_index().diff()


def compute_real(
    holdings: list[dict[str, Any]], closes: dict[str, pd.Series], *, sessions: int = BACKFILL_SESSIONS
) -> dict[str, float]:
    """Day-by-day change in the value of ``holdings``, for the last ``sessions`` sessions."""
    change = _daily_change(holdings, closes)
    if change.empty:
        return {}
    qty = pd.Series({h["symbol"]: h["qty"] for h in holdings})
    out: dict[str, float] = {}
    for when, row in change.tail(sessions).iterrows():
        priced = row.dropna()
        # Measured against the holdings that have any price history at all: a stock with none
        # (a trade-to-trade "BE" share, say) can never be priced, so it must not count against
        # every day. What it leaves out is reported separately as unpriced.
        if len(priced) / change.shape[1] < MIN_COVERAGE:
            continue  # too few of the priceable holdings have a price that day: a partial number would mislead
        out[when.date().isoformat()] = float((priced * qty[priced.index]).sum())
    return out


def refresh_real(data_root: Path, holdings: list[dict[str, Any]], *, today: date | None = None) -> int:
    """Record the holdings' daily change. Only the day being recorded on its own date is exact.

    Everything computed for earlier sessions uses today's holdings, so it is flagged
    ``estimated``. A day already recorded exactly is never overwritten by an estimate.
    """
    if not holdings:
        return 0
    closes = {h["symbol"]: s for h in holdings if (s := _closes(data_root, h["symbol"])) is not None}
    computed = compute_real(holdings, closes)
    if not computed:
        return 0
    days = _stored_days(data_root)  # never the merged view: writing that back would count trading profit twice
    last_session = max(computed)
    today_iso = (today or date.today()).isoformat()
    changed = 0
    for when, pnl in computed.items():
        exact = when == last_session == today_iso  # the book is known as of the day it was recorded
        old = days.get(when)
        if old is not None and not old.estimated:
            continue
        days[when] = Day(date=when, pnl=round(pnl, 2), trades=len(holdings), estimated=not exact)
        changed += 1
    _write_json(
        store_path(data_root),
        {"updated": datetime.now().isoformat(timespec="seconds"), "days": {k: asdict(v) for k, v in sorted(days.items())}},
    )
    return changed


def unpriced_holdings(data_root: Path, holdings: list[dict[str, Any]]) -> list[str]:
    """Holdings with no local price history, which the real series therefore leaves out."""
    return [h["symbol"] for h in holdings if _closes(data_root, h["symbol"]) is None]


# --------------------------------------------------------------------------- one day
def _journal_rows(db: Any, day: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from atr.appdb.schema import deployments, strategies, trade_journal

    stmt = (
        select(
            trade_journal.c.symbol, trade_journal.c.side, trade_journal.c.quantity,
            trade_journal.c.entry_price, trade_journal.c.exit_price, trade_journal.c.exit_ts,
            trade_journal.c.net_pnl, trade_journal.c.gross_pnl, trade_journal.c.exit_reason,
            strategies.c.name,
        )
        .select_from(
            trade_journal.join(deployments, deployments.c.deployment_id == trade_journal.c.deployment_id).outerjoin(
                strategies, strategies.c.strategy_id == trade_journal.c.strategy_id
            )
        )
        .where(deployments.c.mode == "PAPER", trade_journal.c.exit_ts.is_not(None))
    )
    with db.session() as session:
        rows = session.execute(stmt).all()
    out = []
    for symbol, side, qty, entry, exit_, exit_ts, net, gross, reason, name in rows:
        pnl = net if net is not None else gross
        if pnl is None or str(exit_ts)[:10] != day:
            continue
        out.append(
            {
                "source": name or "Paper strategy", "symbol": symbol, "side": side, "quantity": qty,
                "entry": entry, "exit": exit_, "pnl": round(float(pnl), 2),
                "pnl_pct": round(100 * float(pnl) / (entry * qty), 2) if entry and qty else None,
                "note": reason,
            }
        )
    return out


def paper_day_detail(data_root: Path, day: str, db: Any = None) -> list[dict[str, Any]]:
    """Every paper trade that closed on ``day``, from each of the three sources."""
    rows: list[dict[str, Any]] = []
    try:
        if db is None:
            from atr.appdb.engine import get_app_db

            db = get_app_db()
        rows += _journal_rows(db, day)
    except Exception as exc:  # noqa: BLE001 - the day view must load without the journal
        logger.info("paper journal unavailable for the day view: {}", exc)

    ledger = Path(data_root) / "paper_momentum" / "settlements.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf8").splitlines():
            try:
                week = json.loads(line)
                if week["exit_window_end"] != day:
                    continue
                share = VIRTUAL_TICKET / max(len(week["returns"]), 1)  # equal weight across the picks
                for symbol, ret in sorted(week["returns"].items(), key=lambda kv: -kv[1]):
                    rows.append(
                        {
                            "source": "Weekly momentum picks", "symbol": symbol, "side": "BUY", "quantity": None,
                            "entry": None, "exit": None, "pnl": round(float(ret) * share, 2),
                            "pnl_pct": round(100 * float(ret), 2), "note": f"week ending {day}",
                        }
                    )
            except (ValueError, KeyError, TypeError):
                continue

    plan = Path(data_root) / "research" / "gap_plan.json"
    if plan.exists():
        try:
            for t in json.loads(plan.read_text(encoding="utf8")).get("trades", []):
                if t.get("source") == "live" and t.get("market_ok") and t.get("status") == "closed" and t.get("exit_date") == day:
                    rows.append(
                        {
                            "source": "Monday gap plan", "symbol": t["symbol"], "side": "BUY", "quantity": None,
                            "entry": t["entry"], "exit": t["exit_price"],
                            "pnl": round(float(t["net_pct"]) / 100 * VIRTUAL_TICKET, 2), "pnl_pct": t["net_pct"],
                            "note": {"target": "hit target", "stop": "stopped out", "friday": "sold Friday"}.get(t["exit_reason"], ""),
                        }
                    )
        except (OSError, ValueError, KeyError):
            pass
    return sorted(rows, key=lambda r: -abs(r["pnl"]))


def real_day_detail(data_root: Path, holdings: list[dict[str, Any]], day: str) -> dict[str, Any]:
    """Which holdings made or lost the money on ``day``, on the same rule as the calendar."""
    closes = {h["symbol"]: s for h in holdings if (s := _closes(data_root, h["symbol"])) is not None}
    change = _daily_change(holdings, closes)
    when = pd.Timestamp(day)
    rows: list[dict[str, Any]] = []
    priced: set[str] = set()
    if not change.empty and when in change.index:
        i = change.index.get_loc(when)
        for h in holdings:
            sym = h["symbol"]
            delta = change[sym].iloc[i] if sym in change.columns else float("nan")
            if pd.isna(delta):
                continue
            series = closes[sym]
            close = float(series.loc[when])
            prev = close - float(delta)
            priced.add(sym)
            rows.append(
                {
                    "symbol": sym, "quantity": h["qty"], "previous_close": round(prev, 2), "close": round(close, 2),
                    "change_pct": round(100 * float(delta) / prev, 2), "pnl": round(h["qty"] * float(delta), 2),
                }
            )
    rows.sort(key=lambda r: -abs(r["pnl"]))
    holdings_total = round(sum(r["pnl"] for r in rows), 2)
    trading = realised_entries(data_root).get(day)
    return {
        "rows": rows,
        "holdings_total": holdings_total,
        "realised": trading,  # None when nothing has been recorded for the day
        "total": round(holdings_total + (trading["amount"] if trading else 0.0), 2),
        "gainers": sum(1 for r in rows if r["pnl"] > 0),
        "losers": sum(1 for r in rows if r["pnl"] < 0),
        "unpriced": [h["symbol"] for h in holdings if h["symbol"] not in priced],
    }


# --------------------------------------------------------------------------- the view
def _runs(days: list[Day]) -> dict[str, int]:
    """Consecutive traded days that finished up (green) or down (red), across all history."""
    best_green = worst_red = green = red = 0
    for d in days:
        green = green + 1 if d.pnl > 0 else 0
        red = red + 1 if d.pnl < 0 else 0
        best_green, worst_red = max(best_green, green), max(worst_red, red)
    return {"current_green": green, "best_green": best_green, "worst_red": worst_red}


def month_view(days: dict[str, Day], month: str) -> dict[str, Any]:
    """One month of the calendar, with the headline figures the page shows above it."""
    ordered = [days[k] for k in sorted(days)]
    in_month = [d for d in ordered if d.date.startswith(month)]
    if in_month:
        best = max(in_month, key=lambda d: d.pnl)
        worst = min(in_month, key=lambda d: d.pnl)
    else:
        best = worst = None
    return {
        "month": month,
        "days": [asdict(d) for d in in_month],
        "total": round(sum(d.pnl for d in in_month), 2),
        "traded_days": len(in_month),
        "green_days": sum(1 for d in in_month if d.pnl > 0),
        "red_days": sum(1 for d in in_month if d.pnl < 0),
        "best_day": {"date": best.date, "pnl": round(best.pnl, 2)} if best else None,
        "worst_day": {"date": worst.date, "pnl": round(worst.pnl, 2)} if worst else None,
        **_runs(ordered),
        "months_with_data": sorted({d.date[:7] for d in ordered}),
        "any_estimated": any(d.estimated for d in in_month),
    }
