"""IIFL's documented per-endpoint limits, applied before every call."""

import pytest

import multiprocessing
import time

import httpx

from atr.brokers.iifl.client import IiflApiError, IiflClient, _is_retryable
from atr.brokers.iifl.ratelimit import MemoryWindow, RateLimits, SharedWindow, allowed, bucket_for


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
    return RateLimits(MemoryWindow(), now=clock.now, sleep=clock.sleep), clock


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


# -- the allowance belongs to the session, not to one object -----------------------------------
def test_two_limiters_on_one_file_share_one_allowance(tmp_path):
    clock = Clock()
    a = RateLimits(SharedWindow(tmp_path / "rl.sqlite"), now=clock.now, sleep=clock.sleep)
    b = RateLimits(SharedWindow(tmp_path / "rl.sqlite"), now=clock.now, sleep=clock.sleep)
    a.wait("GET", "/holdings", scope="C1")
    b.wait("GET", "/holdings", scope="C1")  # holdings allows 2 a second, between them
    assert a.wait("GET", "/holdings", scope="C1") > 0
    assert clock.slept


def test_a_different_session_has_its_own_allowance(tmp_path):
    clock = Clock()
    rl = RateLimits(SharedWindow(tmp_path / "rl.sqlite"), now=clock.now, sleep=clock.sleep)
    for _ in range(2):
        rl.wait("GET", "/holdings", scope="C1")
    assert rl.wait("GET", "/holdings", scope="C2") == 0


def _hammer(path, calls, out):
    rl = RateLimits(SharedWindow(path))
    for _ in range(calls):
        rl.wait("GET", "/holdings", scope="C1")
        out.append(time.time())


def test_separate_processes_together_stay_within_the_limit(tmp_path):
    """The real case: the server and a script running at the same time."""
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        stamps = manager.list()
        procs = [ctx.Process(target=_hammer, args=(tmp_path / "rl.sqlite", 4, stamps)) for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        times = sorted(stamps)
    assert len(times) == 12
    # What matters is IIFL's own limit (3 a second) measured on when calls actually went out. The
    # 80% headroom exists for exactly this: a call goes out a few milliseconds after it is counted.
    for start in times:
        in_window = [t for t in times if start <= t < start + 1.0]
        assert len(in_window) <= 3, "the processes together went over IIFL's documented limit"
    # And the counts themselves never exceed what we allow ourselves.
    import sqlite3

    rows = [r[0] for r in sqlite3.connect(tmp_path / "rl.sqlite").execute("SELECT ts FROM calls")]
    for start in rows:
        assert len([t for t in rows if start <= t < start + 1.0]) <= allowed(3)


def test_a_broken_shared_file_falls_back_to_this_process_and_never_blocks_a_call(tmp_path):
    class Broken:
        def try_take(self, key, limit, clock):
            raise OSError("disk full")

    clock = Clock()
    rl = RateLimits(Broken(), now=clock.now, sleep=clock.sleep)
    for _ in range(2):
        assert rl.wait("GET", "/holdings") == 0
    assert rl.wait("GET", "/holdings") > 0  # still limited, by the in-process count


# -- a 429 is retried with backoff, other client errors are not ---------------------------------
def test_429_is_retryable_but_other_client_errors_are_not():
    assert _is_retryable(IiflApiError("x", status_code=429))
    assert _is_retryable(IiflApiError("x", status_code=503))
    assert not _is_retryable(IiflApiError("x", status_code=400))
    assert not _is_retryable(IiflApiError("x", status_code=401))


def test_the_client_waits_its_turn_and_retries_a_429(tmp_path, monkeypatch):
    from atr.brokers.iifl.auth import Session, SessionStore
    from datetime import datetime

    answers = [httpx.Response(429), httpx.Response(200, json={"status": "Ok", "result": []})]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return answers.pop(0)

    store = SessionStore(tmp_path / "session.json")
    client = IiflClient(session_store=store)
    client._http = httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler))
    client.set_session(Session("tok", "C1", datetime.now().astimezone()))
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip the backoff wait
    assert client.holdings() == {"status": "Ok", "result": []}
    assert calls == ["/v1/holdings", "/v1/holdings"]
    assert (tmp_path / "iifl_ratelimit.sqlite").exists()  # counted in the shared file
