"""Fill an EMPTY data directory with clearly marked synthetic demo data, so the app can be run,
screenshotted and demonstrated with something in it.

    python scripts/seed_demo.py            # writes to <repo>/data
    atr serve                              # then browse

What it makes: ten synthetic stocks and a Nifty-like series, a worked-example strategy, two paper
deployments, and about three months of backdated paper fills written through the real order tables.
A ``.demo`` file marks the directory, and the API reports ``demo: true``, so the interface says
"Demo data" wherever the numbers appear. Nothing here resembles a real result.

It REFUSES to run if the data directory already holds anything. It never mixes demo data into a
real one: use a fresh clone or an empty ``data/``.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SYMBOLS = {
    "RELIANCE": "Oil Gas & Consumable Fuels", "TCS": "Information Technology", "INFY": "Information Technology",
    "HDFCBANK": "Financial Services", "ICICIBANK": "Financial Services", "SBIN": "Financial Services",
    "LT": "Construction", "ITC": "Fast Moving Consumer Goods", "AXISBANK": "Financial Services",
    "MARUTI": "Automobile and Auto Components",
}
SESSIONS = 320  # of price history
TRADE_SESSIONS = 75  # of paper trading


def business_days(end: date, n: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=pd.Timestamp(end), periods=n)]


def write_prices(root: Path, days: list[date], rng: np.random.Generator) -> dict[str, pd.Series]:
    daily = root / "iifl_daily" / "NSEEQ"
    daily.mkdir(parents=True)
    closes: dict[str, pd.Series] = {}
    ts = [datetime(d.year, d.month, d.day, 9, 15) for d in days]  # the broker stamps a daily bar at 09:15
    for i, sym in enumerate(SYMBOLS):
        steps = rng.normal(0.0006 + 0.0001 * (i % 3), 0.013, len(days)).cumsum()
        close = (400 + 300 * i) * np.exp(steps)
        high = close * (1 + rng.uniform(0.001, 0.015, len(days)))
        low = close * (1 - rng.uniform(0.001, 0.015, len(days)))
        open_ = close * (1 + rng.normal(0, 0.004, len(days)))
        frame = pd.DataFrame({
            "ts": pd.to_datetime(ts), "open": open_, "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]), "close": close,
            "volume": rng.integers(200_000, 3_000_000, len(days)).astype(float),
        })
        frame.to_parquet(daily / f"{sym}-EQ.parquet")
        closes[sym] = pd.Series(close, index=days)
    # a Nifty-like series: the average of the stocks' log returns, with its own noise
    ret = np.mean([np.log(s).diff().fillna(0).to_numpy() for s in closes.values()], axis=0)
    level = 24_000 * np.exp((ret * 0.8 + rng.normal(0, 0.002, len(days))).cumsum())
    idx_dir = root / "iifl_daily" / "INDICES"
    idx_dir.mkdir(parents=True)
    pd.DataFrame({"ts": pd.to_datetime(ts), "open": level, "high": level * 1.003, "low": level * 0.997,
                  "close": level, "volume": 0.0}).to_parquet(idx_dir / "NIFTY50.parquet")
    (idx_dir / "NIFTY50.source").write_text("Synthetic demo series", encoding="utf8")
    uni = root / "universe"
    uni.mkdir()
    (uni / "n50.txt").write_text(",".join(SYMBOLS), encoding="utf8")
    rows = "".join(f"{s.title()} Ltd.,{ind},{s},EQ,INE{i:09d}\n" for i, (s, ind) in enumerate(SYMBOLS.items()))
    (uni / "ind_nifty50list.csv").write_text("Company Name,Industry,Symbol,Series,ISIN Code\n" + rows, encoding="utf8")
    return closes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", default="DemoPassw0rd!", help="password for the demo owner account")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    root = ROOT / "data"
    if root.exists() and any(root.iterdir()):
        print(f"refusing: {root} is not empty. Demo data is never mixed into a real data directory.", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)

    from atr.market_calendar import IST, NSEMarketCalendar
    from atr.services.data_status import expected_session

    rng = np.random.default_rng(args.seed)
    last = expected_session(datetime.now(IST), NSEMarketCalendar())
    days = business_days(last, SESSIONS)
    closes = write_prices(root, days, rng)
    (root / ".demo").write_text("synthetic demo data: nothing here is a real result\n", encoding="utf8")

    from fastapi.testclient import TestClient
    from sqlalchemy import update

    from atr.api.main import app
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import OrderEventRepository, OrderRepository
    from atr.appdb.schema import deployments, orders

    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post("/api/v1/auth/bootstrap", json={"email": "demo@example.com", "username": "demo",
                    "password": args.password, "display_name": "Demo"})
    assert r.status_code == 201, r.text
    client.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    user_id = r.json()["user"]["user_id"] if "user" in r.json() else None
    seeded = client.post("/api/v1/strategies/seed")
    assert seeded.status_code in (200, 201), seeded.text
    strategy_id = seeded.json()["strategy"]["strategy_id"]
    version = seeded.json()["version"]["version"]

    db = get_app_db()
    trade_days = days[-TRADE_SESSIONS:]
    py = random.Random(args.seed)
    total_fills = 0
    for capital in (500_000.0, 300_000.0):
        d = client.post("/api/v1/paper/deployments", json={"strategy_id": strategy_id, "strategy_version": version, "capital": capital})
        assert d.status_code == 201, d.text
        dep = d.json()
        user_id = dep["user_id"]
        i = py.randint(0, 3)
        while i < len(trade_days) - 6:
            sym = py.choice(list(SYMBOLS))
            hold = py.randint(4, 12)
            j = min(i + hold, len(trade_days) - 1)
            entry_day, exit_day = trade_days[i], trade_days[j]
            px_in, px_out = float(closes[sym][entry_day]), float(closes[sym][exit_day])
            qty = max(1, int(capital * 0.12 / px_in))
            move = (px_out / px_in - 1.0) * 100.0
            reasons = {
                "BUY": f"breakout: closed above its 20-day high of {px_in * 0.99:,.2f}",
                "SELL": (
                    f"take_profit: up {move:.1f}% against an average of {px_in:,.2f}" if move >= 4
                    else f"stop_loss: down {move:.1f}% against an average of {px_in:,.2f}" if move <= -3
                    else "trend_exit: closed below its 10-day average"
                ),
            }
            for side, day, px in (("BUY", entry_day, px_in * 1.0005), ("SELL", exit_day, px_out * 0.9995)):
                at = datetime(day.year, day.month, day.day, 5, 0)  # 10:30 IST, naive UTC like the app
                commission = round(px * qty * 0.0004, 2)
                with db.session() as session:
                    o = OrderRepository.create(session, user_id=user_id, symbol=f"{sym}-EQ", side=side, quantity=qty,
                                               deployment_id=dep["deployment_id"], strategy_id=strategy_id,
                                               strategy_version=version, requested_price=px, tag="DEMO")
                    # The rule's own words ride on the NEW event, exactly as the paper runner writes them.
                    OrderEventRepository.append(session, order_id=o["order_id"], to_status="NEW", ts=at,
                                                raw={"reason": reasons[side]}, source="demo")
                    OrderEventRepository.append(session, order_id=o["order_id"], from_status="NEW", to_status="FILLED",
                                                ts=at, fill_ts=at, filled_qty=float(qty), filled_price=round(px, 2),
                                                commission=commission, requested_price=px, source="demo")
                    session.execute(update(orders).where(orders.c.order_id == o["order_id"]).values(created_at=at, updated_at=at))
                total_fills += 1
            i = j + py.randint(1, 4)
        with db.session() as session:
            session.execute(update(deployments).where(deployments.c.deployment_id == dep["deployment_id"]).values(
                status="RUNNING", started_at=datetime(trade_days[0].year, trade_days[0].month, trade_days[0].day, 4, 0)))
    # Bring the trade journal (which Performance reads) up to date with the ledger, as the app does.
    synced = client.post("/api/v1/paper/sync-journal")
    assert synced.status_code == 200, synced.text
    assert synced.json()["in_sync"], synced.json()
    print(f"seeded {len(SYMBOLS)} stocks to {last}, a strategy, 2 paper deployments and {total_fills} fills")
    print(f"log in as: demo / {args.password}   (then: atr serve)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
