from atr.core.models import Instrument
from atr.services.paper import PaperVenue


class _Draft:
    symbol, exchange, side, order_type = "XYZ", "NSEEQ", "BUY", "MARKET"
    limit_price = stop_price = None

    def __init__(self, qty):
        self.quantity = qty


def _venue(adv):
    v = PaperVenue(prices=lambda s, e: 100.0, adv=(lambda s, e: adv))
    v.max_tick_age_seconds = None
    return v


def test_small_order_barely_moves_price_and_big_order_costs_more_and_is_capped():
    small = _venue(1_000_000).submit(_Draft(100))
    big = _venue(1_000_000).submit(_Draft(200_000))  # 20% of a day's volume
    assert small.filled_qty == 100 and big.filled_qty == 50_000  # capped at 5%
    assert big.raw["capped_from"] == 200_000
    assert big.raw["impact_bps"] > small.raw["impact_bps"]
    assert big.filled_price > small.filled_price > 100.0


def test_no_liquidity_data_keeps_flat_slippage():
    out = PaperVenue(prices=lambda s, e: 100.0).submit(_Draft(500_000))
    assert out.filled_qty == 500_000 and "impact_bps" not in out.raw


def test_a_partly_filled_order_does_not_take_another_slice_the_same_day():
    from datetime import datetime

    v = PaperVenue(prices=lambda s, e: 100.0, adv=lambda s, e: 1_000_000.0, clock=lambda: datetime(2026, 9, 14, 10, 0))
    v.max_tick_age_seconds = None
    order = {
        "order_id": "o1", "symbol": "XYZ", "exchange": "NSEEQ", "side": "BUY", "order_type": "MARKET",
        "quantity": 200_000, "filled_quantity": 50_000, "updated_at": datetime(2026, 9, 14, 9, 30),
    }
    assert v.match(order).filled_qty == 0.0  # same day: allowance used up
    order["updated_at"] = datetime(2026, 9, 11, 9, 30)
    assert v.match(order).filled_qty > 50_000  # next day: another slice


def test_market_orders_cross_the_real_spread_when_the_feed_has_a_quote():
    from atr.services.paper import TickPrice

    tick = TickPrice(price=100.0, bid=99.9, ask=100.2, open=98.0)
    v = PaperVenue(prices=lambda s, e: tick)
    buy = v.submit(_Draft(10))
    assert buy.filled_price == 100.2 and buy.raw["fill_basis"] == "quote"

    class _Sell(_Draft):
        side = "SELL"

    assert v.submit(_Sell(10)).filled_price == 99.9


def test_no_quote_falls_back_to_flat_slippage():
    out = PaperVenue(prices=lambda s, e: 100.0).submit(_Draft(10))
    assert out.raw["fill_basis"] == "last_plus_flat_slippage" and out.filled_price > 100.0


def test_live_tick_source_carries_open_bid_ask():
    from types import SimpleNamespace

    from atr.services.paper import live_tick_source

    class B:
        def latest_price_and_time(self, s):
            return 100.0, None

        def latest_tick(self, s):
            return SimpleNamespace(open=98.5, best_bid=99.9, best_ask=100.1)

    t = live_tick_source(broadcaster=B())("X", "NSEEQ")
    assert (t.open, t.bid, t.ask) == (98.5, 99.9, 100.1)
