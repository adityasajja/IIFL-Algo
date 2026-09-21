"""IIFL's documented per-endpoint limits, applied before every call."""

import pytest

from atr.brokers.iifl.ratelimit import RateLimits, allowed, bucket_for


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def limits():
    clock = Clock()
    return RateLimits(monotonic=clock.now, sleep=clock.sleep), clock


@pytest.mark.parametrize(
    ("method", "path", "bucket", "documented"),
    [
        ("GET", "/holdings", "holdings", 3),
        ("GET", "/positions", "positions", 3),
        ("GET", "/orders", "order_book", 3),
        ("GET", "/orders/240919000000041", "order_history", 10),
        ("POST", "/orders", "place_order", 10),
        ("PUT", "/orders/24", "modify_order", 20),
        ("DELETE", "/orders/24", "cancel_order", 20),
        ("DELETE", "/orders", "cancel_all_orders", 3),
        ("GET", "/trades", "trade_book", 3),
        ("POST", "/marketdata/historicaldata", "historical_data", 10),
        ("POST", "/marketdata/marketquotes", "market_quotes", 10),
        ("POST", "/getusersession", "get_user_session", 3),
        ("GET", "/profile", "profile", 3),
        ("POST", "/profile/logout", "logout", 2),
        ("GET", "/limits", "limits", 10),
        ("GET", "/contractfiles/NSEEQ.json", "contract_NSEEQ", 2),
    ],
)
def test_each_endpoint_counts_against_its_own_documented_limit(method, path, bucket, documented):
    assert bucket_for(method, path) == (bucket, documented)


def test_a_query_string_or_trailing_slash_does_not_change_the_bucket():
    assert bucket_for("GET", "/holdings/?x=1") == ("holdings", 3)


def test_an_endpoint_with_no_documented_figure_gets_the_strictest_common_limit():
    assert bucket_for("GET", "/something/new")[1] == 3


def test_we_stay_at_eighty_percent_and_never_below_one_call():
    assert [allowed(n) for n in (2, 3, 10, 20)] == [1, 2, 8, 16]


def test_calls_under_the_limit_do_not_wait():
    rl, clock = limits()
    for _ in range(8):  # market quotes: 10 documented, 8 allowed
        assert rl.wait("POST", "/marketdata/marketquotes") == 0
    assert clock.slept == []


def test_the_call_over_the_limit_waits_for_the_oldest_to_age_out():
    rl, clock = limits()
    for _ in range(2):  # holdings: 3 documented, 2 allowed
        clock.t += 0.1
        rl.wait("GET", "/holdings")
    waited = rl.wait("GET", "/holdings")
    assert waited == pytest.approx(0.9)  # the first call was at t=0.1, now is 0.2
    assert clock.slept == [pytest.approx(0.9)]


def test_a_full_second_apart_never_waits():
    rl, clock = limits()
    for _ in range(20):
        clock.t += 1.0
        assert rl.wait("GET", "/holdings") == 0


def test_one_endpoint_being_busy_does_not_hold_up_another():
    rl, clock = limits()
    for _ in range(2):
        rl.wait("GET", "/holdings")
    assert rl.wait("GET", "/positions") == 0  # separate row in IIFL's table, separate bucket
    assert rl.wait("GET", "/holdings") > 0  # while holdings itself is full


def test_a_burst_of_many_calls_is_spread_so_no_second_holds_more_than_allowed():
    rl, clock = limits()
    times = []
    for _ in range(30):
        rl.wait("GET", "/holdings")
        times.append(clock.t)
    for start in times:
        in_window = [t for t in times if start <= t < start + 1.0]
        assert len(in_window) <= allowed(3)


def test_contract_files_are_limited_per_segment():
    rl, clock = limits()
    rl.wait("GET", "/contractfiles/NSEEQ.json")
    assert rl.wait("GET", "/contractfiles/BSEEQ.json") == 0
    assert rl.wait("GET", "/contractfiles/NSEEQ.json") > 0  # one allowed a second
