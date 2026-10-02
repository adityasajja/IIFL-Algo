"""The track record: a replay of the paper ledger, valued at each day's close.

The replay is pure, so most of these tests need no database. They pin the properties a
person would otherwise have to take on trust: costs are inside the number, an unpriced day is
flagged rather than smoothed, drawdown is measured from the running peak, and several
accounts add up to one curve without double counting.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from atr.core.models import Fill, Instrument, Side
from atr.services.track_record import (
    build_record,
    combine,
    current_drawdown_pct,
    fill_day,
    max_drawdown_pct,
    replay_equity,
)

TCS = Instrument(symbol="TCS-EQ", exchange="NSEEQ", currency="INR")
D0 = date(2026, 7, 6)  # a Monday


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def fill(side: Side, qty: float, price: float, on: date, commission: float = 0.0, symbol: str = "TCS-EQ") -> Fill:
    # 05:00 UTC is 10:30 IST: the same calendar day in both zones.
    ts = datetime(on.year, on.month, on.day, 5, 0, tzinfo=UTC)
    inst = TCS if symbol == "TCS-EQ" else Instrument(symbol=symbol, exchange="NSEEQ", currency="INR")
    return Fill(order_id="o", instrument=inst, side=side, quantity=qty, price=price, ts=ts, commission=commission)


def closes(table: dict[date, float]):
    def close_on(symbol: str, exchange: str, d: date) -> float | None:
        known = [k for k in table if k <= d]
        return table[max(known)] if known else None

    return close_on


CAL = [day(n) for n in range(5)]


class TestReplay:
    def test_flat_account_is_just_the_capital(self):
        points, stats = replay_equity([], 100_000.0, CAL, closes({}))
        assert [p.equity for p in points] == [100_000.0] * 5
        assert all(p.priced for p in points)
        assert stats["closed_trades"] == 0

    def test_open_position_moves_with_the_close(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        table = {day(0): 100.0, day(1): 110.0, day(2): 90.0, day(3): 90.0, day(4): 95.0}
        points, _ = replay_equity(fills, 10_000.0, CAL, closes(table))
        assert [p.equity for p in points] == [10_000.0, 10_100.0, 9_900.0, 9_900.0, 9_950.0]

    def test_costs_are_inside_the_curve(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0), commission=25.0)]
        points, stats = replay_equity(fills, 10_000.0, CAL, closes({day(0): 100.0}))
        assert points[0].equity == pytest.approx(10_000.0 - 25.0)
        assert stats["commission_paid"] == 25.0

    def test_a_day_with_no_close_is_flagged_and_valued_at_cost(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        points, _ = replay_equity(fills, 10_000.0, CAL, closes({}))
        assert all(not p.priced for p in points)
        assert all(p.equity == pytest.approx(10_000.0) for p in points)  # cost, not zero, not a guess up

    def test_a_missing_day_carries_the_last_known_close_and_stays_priced(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        points, _ = replay_equity(fills, 10_000.0, CAL, closes({day(0): 100.0, day(1): 120.0}))
        assert points[-1].equity == pytest.approx(10_200.0)
        assert all(p.priced for p in points)

    def test_closed_trades_and_wins_are_counted_from_realised_pnl(self):
        fills = [
            fill(Side.BUY, 10, 100.0, day(0)), fill(Side.SELL, 10, 110.0, day(1)),   # win
            fill(Side.BUY, 10, 100.0, day(2)), fill(Side.SELL, 10, 90.0, day(3)),    # loss
        ]
        points, stats = replay_equity(fills, 10_000.0, CAL, closes({day(0): 100.0}))
        assert stats["closed_trades"] == 2 and stats["wins"] == 1
        assert points[-1].equity == pytest.approx(10_000.0)  # +100 and -100

    def test_a_fill_belongs_to_its_ist_date(self):
        late = Fill(order_id="o", instrument=TCS, side=Side.BUY, quantity=1, price=1,
                    ts=datetime(2026, 7, 6, 20, 0, tzinfo=UTC))  # 01:30 IST the next day
        assert fill_day(late) == date(2026, 7, 7)


class TestDrawdown:
    def test_measured_from_the_running_peak(self):
        assert max_drawdown_pct([100, 120, 90, 110]) == pytest.approx(-25.0)

    def test_never_fell(self):
        assert max_drawdown_pct([100, 101, 102]) == 0.0

    def test_current_is_against_the_peak(self):
        assert current_drawdown_pct([100, 120, 108]) == pytest.approx(-10.0)
        assert current_drawdown_pct([]) == 0.0


def _record(fills, table, benchmark=None, window=90, today=None):
    return build_record(
        fills=fills, initial_cash=10_000.0, calendar=CAL, close_on=closes(table),
        benchmark=benchmark, window_days=window, today=today or day(4),
    )


class TestRecord:
    def test_summary_matches_the_curve(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        r = _record(fills, {day(0): 100.0, day(4): 120.0})
        assert r["has_data"] and r["trading_days"] == 5
        assert r["summary"]["return_pct"] == pytest.approx(2.0)
        assert r["summary"]["net_pnl"] == pytest.approx(200.0)
        assert r["points"][-1]["equity"] == 10_200.0

    def test_benchmark_is_rebased_to_the_same_start(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        bench = {day(n): 20_000.0 * (1 + 0.01 * n) for n in range(5)}  # +4% over the window
        r = _record(fills, {day(0): 100.0, day(4): 120.0}, benchmark=bench)
        assert r["points"][0]["benchmark"] == pytest.approx(10_000.0)
        assert r["summary"]["benchmark_return_pct"] == pytest.approx(4.0)
        assert r["summary"]["excess_pct"] == pytest.approx(2.0 - 4.0)

    def test_no_benchmark_is_null_not_zero(self):
        r = _record([fill(Side.BUY, 1, 100.0, day(0))], {day(0): 100.0})
        assert r["summary"]["benchmark_return_pct"] is None
        assert all(p["benchmark"] is None for p in r["points"])

    def test_win_rate_is_null_with_no_closed_trade(self):
        r = _record([fill(Side.BUY, 1, 100.0, day(0))], {day(0): 100.0})
        assert r["summary"]["win_rate_pct"] is None

    def test_window_cuts_old_days_and_rebases_the_return(self):
        fills = [fill(Side.BUY, 10, 100.0, day(0))]
        table = {day(0): 100.0, day(1): 100.0, day(2): 100.0, day(3): 110.0, day(4): 121.0}
        r = _record(fills, table, window=1, today=day(4))  # only day(3) and day(4) are inside
        assert r["start"] == day(3).isoformat()
        assert r["base"] == pytest.approx(10_100.0)
        assert r["summary"]["return_pct"] == pytest.approx((10_210.0 / 10_100.0 - 1) * 100)

    def test_unpriced_days_are_reported(self):
        r = _record([fill(Side.BUY, 1, 100.0, day(0))], {})
        assert r["unpriced_days"] == 5

    def test_no_calendar_or_capital_means_no_data(self):
        assert build_record(fills=[], initial_cash=0, calendar=CAL, close_on=closes({}), benchmark=None,
                            window_days=90, today=day(4)) == {"has_data": False}


class TestCombine:
    def _rec(self, fills, table, cal=CAL):
        return build_record(fills=fills, initial_cash=10_000.0, calendar=cal, close_on=closes(table),
                            benchmark=None, window_days=90, today=day(4))

    def test_one_record_passes_through(self):
        r = self._rec([fill(Side.BUY, 1, 100.0, day(0))], {day(0): 100.0})
        assert combine([r, {"has_data": False}]) is r

    def test_nothing_is_no_data(self):
        assert combine([]) == {"has_data": False}
        assert combine([{"has_data": False}]) == {"has_data": False}

    def test_two_accounts_add_and_do_not_double_count_capital(self):
        a = self._rec([fill(Side.BUY, 10, 100.0, day(0))], {day(0): 100.0, day(4): 120.0})
        b = self._rec([fill(Side.BUY, 10, 100.0, day(0), symbol="INFY-EQ")], {day(0): 100.0, day(4): 80.0})
        both = combine([a, b])
        assert both["base"] == 20_000.0 and both["capital"] == 20_000.0
        assert both["points"][-1]["equity"] == pytest.approx(10_200.0 + 9_800.0)
        assert both["summary"]["return_pct"] == pytest.approx(0.0)
        assert both["summary"]["closed_trades"] == 0

    def test_an_account_that_started_later_counts_as_cash_before_it_began(self):
        early = self._rec([fill(Side.BUY, 10, 100.0, day(0))], {day(0): 100.0, day(4): 110.0})
        late = self._rec([fill(Side.BUY, 10, 100.0, day(3))], {day(3): 100.0, day(4): 100.0}, cal=CAL[3:])
        both = combine([early, late])
        assert both["points"][0]["equity"] == pytest.approx(10_000.0 + 10_000.0)  # late still just capital
        assert len(both["points"]) == 5


# ───────────────────────── through the real ledger and HTTP ─────────────────────────
from fastapi.testclient import TestClient  # noqa: E402

from atr.api.main import app  # noqa: E402
from atr.services import track_record as tr  # noqa: E402

LOOPBACK = ("127.0.0.1", 51234)


@pytest.fixture()
def prices(monkeypatch):
    from atr.services import paper as paper_service
    from atr.services.paper import fixed_price_source

    table = {"RELIANCE": 2500.0}
    monkeypatch.setattr(paper_service, "default_price_source", lambda: fixed_price_source(table))
    return table


@pytest.fixture()
def owner(fresh_env, master, prices) -> TestClient:
    client = TestClient(app, client=LOOPBACK)
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"email": "owner@example.com", "username": "owner", "password": "Str0ngPassw0rd",
              "display_name": "Owner"},
    )
    assert response.status_code == 201, response.text
    client.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
    return client


@pytest.fixture()
def closes_and_index(monkeypatch):
    """Daily closes for RELIANCE and a Nifty series, without reading the operator's cache."""
    today = datetime.now(tr.IST).date()
    days = [today - timedelta(days=n) for n in range(6, -1, -1)]
    stock = {d: 2500.0 + 10 * i for i, d in enumerate(days)}
    index = {d: 20_000.0 + 50 * i for i, d in enumerate(days)}

    def stocks(self):
        def close_on(symbol, exchange, d):
            known = [k for k in stock if k <= d]
            return stock[max(known)] if known else None
        return close_on, lambda: max(stock)

    monkeypatch.setattr(tr.TrackRecordService, "_stock_closes", stocks)
    monkeypatch.setattr(
        tr.TrackRecordService, "_benchmark",
        lambda self: (index, {"name": "NIFTY 50", "source": "test", "as_of": max(index).isoformat()}),
    )
    return stock, index


def _deploy(client, capital=250_000.0):
    r = client.post("/api/v1/paper/deployments", json={"strategy_id": "s1", "strategy_version": 1, "capital": capital})
    assert r.status_code == 201, r.text
    return r.json()["deployment_id"]


def _buy(client, deployment_id, qty=10):
    r = client.post(f"/api/v1/paper/deployments/{deployment_id}/orders",
                    json={"symbol": "RELIANCE", "side": "BUY", "quantity": qty,
                          "requested_price": 2500.0, "limit_price": 2500.0})
    assert r.status_code in (200, 201), r.text


def test_track_record_needs_authentication(fresh_env, master):
    client = TestClient(app, client=LOOPBACK)
    assert client.get("/api/v1/paper/track-record").status_code == 401


def test_no_trades_means_no_curve_not_a_flat_line(owner, closes_and_index):
    _deploy(owner)
    body = owner.get("/api/v1/paper/track-record").json()
    assert body["has_data"] is False
    assert body["provenance"]["simulated"] is True
    assert "points" not in body


def test_a_traded_deployment_has_a_curve_with_provenance(owner, closes_and_index):
    dep = _deploy(owner)
    owner.post(f"/api/v1/paper/deployments/{dep}/start")
    _buy(owner, dep)
    body = owner.get(f"/api/v1/paper/deployments/{dep}/track-record").json()
    assert body["has_data"] is True
    assert body["scope"] == "deployment" and body["capital"] == 250_000.0
    assert len(body["points"]) >= 1
    prov = body["provenance"]
    assert prov["simulated"] is True
    assert prov["benchmark"]["name"] == "NIFTY 50" and prov["benchmark"]["as_of"]
    assert prov["prices_as_of"]
    # The point is net of costs and valued at the cached close, not at zero.
    assert body["points"][-1]["equity"] > 240_000.0


def test_the_whole_book_view_matches_a_single_deployment(owner, closes_and_index):
    dep = _deploy(owner)
    owner.post(f"/api/v1/paper/deployments/{dep}/start")
    _buy(owner, dep)
    one = owner.get(f"/api/v1/paper/deployments/{dep}/track-record").json()
    allbook = owner.get("/api/v1/paper/track-record").json()
    assert allbook["scope"] == "all" and allbook["deployments"] == 1
    assert allbook["points"] == one["points"]


def test_another_accounts_deployment_track_record_is_a_404(owner, closes_and_index):
    dep = _deploy(owner)
    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import UserRepository
    from atr.auth.passwords import hash_password

    with get_app_db().session() as session:
        UserRepository.create(session, email="v@example.com", username="viewer",
                              password_hash=hash_password("Str0ngPassw0rd"), role="viewer")
    other = TestClient(app, client=LOOPBACK)
    token = other.post("/api/v1/auth/login", json={"identifier": "viewer", "password": "Str0ngPassw0rd"}).json()["token"]
    other.headers.update({"Authorization": f"Bearer {token}"})
    assert other.get(f"/api/v1/paper/deployments/{dep}/track-record").status_code == 404


def test_days_is_bounded(owner):
    assert owner.get("/api/v1/paper/track-record", params={"days": 0}).status_code == 422
    assert owner.get("/api/v1/paper/track-record", params={"days": 9999}).status_code == 422
