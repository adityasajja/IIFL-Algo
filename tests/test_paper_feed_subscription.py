"""A running paper deployment must be *on* the live feed, not only reading it.

What this suite protects
------------------------

The paper venue reads its prices from :class:`~atr.api.stream.TickBroadcaster`,
and a symbol only reaches that broadcaster's tick store when somebody
**subscribes** to it. Subscription used to be reachable from one place only —
:meth:`TickBroadcaster.subscribe`, the browser path. So the chain

    deployment RUNNING ──► runner pass ──► latest_price(symbol) ──► None

was the normal state of a deployment nobody was watching: no bridge connection,
no ticks, every price lookup ``None``, and ``default_price_source`` falling
through to the daily cache. The venue reads "no price" as a rejection for a new
order and as "resting" for a resting one, so the deployment either filled at
yesterday's close or never filled at all — while reporting itself as running.

That is the difference between a paper account that trades and one that only
looks like it does, and it is why these tests are about *subscription* rather
than about prices. Each one pins a property that, if it broke, would leave a
deployment silently un-priced and the dashboard showing green.
"""

from __future__ import annotations

import pytest

from atr.api.stream import TickBroadcaster, _normalise_universe

SYMBOL = "RELIANCE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeBridge:
    """A bridge that records what was asked of it."""

    def __init__(self) -> None:
        self.is_connected = True
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def subscribe_feed(self, topics: list[str]) -> None:
        self.subscribed.extend(topics)

    def unsubscribe_feed(self, topics: list[str]) -> None:
        self.unsubscribed.extend(topics)


def _broadcaster(monkeypatch, *, resolve: bool = True) -> tuple[TickBroadcaster, FakeBridge]:
    """A broadcaster with a fake bridge and a deterministic contract resolver.

    The resolver is stubbed rather than left real because resolving a symbol
    against the instrument master reads a 22,899-contract index — the subject
    here is the subscription logic, not the master.
    """
    broadcaster = TickBroadcaster()
    bridge = FakeBridge()
    broadcaster._bridge = bridge

    def stub_resolve(symbol: str, exchange: str = "NSEEQ"):
        if not resolve:
            return None
        topic = f"{exchange.lower()}/{abs(hash(symbol)) % 10_000}"
        broadcaster._symbol_to_topic[symbol.upper()] = topic
        broadcaster._topic_to_symbol[topic] = symbol.upper()
        return topic

    monkeypatch.setattr(broadcaster, "_resolve_symbol", stub_resolve)
    return broadcaster, bridge


@pytest.fixture()
def subscriber_reset():
    """Leave the process-wide subscriber seam as it was found."""
    from atr.services import paper as paper_service

    yield
    paper_service.install_live_subscriber(None)
    paper_service.install_live_source(None)


# ---------------------------------------------------------------------------
# 1. the universe statement itself
# ---------------------------------------------------------------------------
def test_a_flat_universe_is_normalised_to_upper_case_symbols():
    assert _normalise_universe(["reliance", " tcs ", ""], "nseeq") == {
        "NSEEQ": {"RELIANCE", "TCS"}
    }


def test_a_per_exchange_universe_keeps_its_exchanges_apart():
    """A caller with deployments on two exchanges must not have them guessed."""
    assert _normalise_universe({"NSEEQ": ["INFY"], "BSEEQ": ["wipro"]}, "NSEEQ") == {
        "NSEEQ": {"INFY"},
        "BSEEQ": {"WIPRO"},
    }


def test_an_empty_universe_subscribes_nothing(monkeypatch):
    broadcaster, bridge = _broadcaster(monkeypatch)
    report = broadcaster.ensure_symbols([])
    assert report == {"added": [], "removed": [], "unresolved": []}
    assert bridge.subscribed == []


# ---------------------------------------------------------------------------
# 2. a server-side consumer gets the symbol onto the feed
# ---------------------------------------------------------------------------
def test_the_universe_is_subscribed_without_any_browser(monkeypatch):
    """The defect this whole path exists to close.

    No WebSocket is connected and no browser has ever asked for this symbol. It
    still has to reach the exchange, because a paper deployment is trading it.
    """
    broadcaster, bridge = _broadcaster(monkeypatch)

    report = broadcaster.ensure_symbols([SYMBOL], "NSEEQ")

    assert report["added"] == [SYMBOL]
    assert report["unresolved"] == []
    assert len(bridge.subscribed) == 1, "the symbol never reached the exchange"
    assert broadcaster._runner_symbols == {SYMBOL}


def test_restating_the_same_universe_does_not_resubscribe(monkeypatch):
    """The runner states its universe on every pass — once a second.

    A statement that re-sent the subscription each time would be a message a
    second per symbol for no gain.
    """
    broadcaster, bridge = _broadcaster(monkeypatch)
    broadcaster.ensure_symbols([SYMBOL])
    before = len(bridge.subscribed)

    for _ in range(5):
        report = broadcaster.ensure_symbols([SYMBOL])
        assert report["added"] == []

    assert len(bridge.subscribed) == before


def test_a_symbol_the_browser_already_carries_is_not_subscribed_twice(monkeypatch):
    """Two consumers, one exchange subscription."""
    broadcaster, bridge = _broadcaster(monkeypatch)
    # A browser is already watching it.
    broadcaster._subscribed_symbols[SYMBOL] = 1
    broadcaster._symbol_to_topic[SYMBOL] = "nseeq/2885"
    broadcaster._topic_to_symbol["nseeq/2885"] = SYMBOL

    report = broadcaster.ensure_symbols([SYMBOL])

    assert report["added"] == [SYMBOL]
    assert bridge.subscribed == [], "a duplicate subscription was sent to the exchange"


def test_a_multi_exchange_universe_is_stated_in_one_call(monkeypatch):
    broadcaster, bridge = _broadcaster(monkeypatch)
    report = broadcaster.ensure_symbols({"NSEEQ": ["INFY"], "BSEEQ": ["WIPRO"]})
    assert sorted(report["added"]) == ["INFY", "WIPRO"]
    assert len(bridge.subscribed) == 2


# ---------------------------------------------------------------------------
# 3. the two kinds of consumer cannot unsubscribe each other
# ---------------------------------------------------------------------------
def test_a_browser_disconnect_does_not_drop_a_deployments_symbol(monkeypatch):
    """Closing a dashboard tab must not stop a paper deployment being priced.

    This is the failure that would be hardest to notice: the chart stops
    updating, which nobody is there to see, and the deployment goes back to
    yesterday's close.
    """
    broadcaster, bridge = _broadcaster(monkeypatch)
    broadcaster._symbol_to_topic[SYMBOL] = "nseeq/2885"

    # A browser subscribes and then goes away.
    broadcaster._subscribed_symbols[SYMBOL] = 1
    broadcaster.ensure_symbols([SYMBOL])

    class FakeWs:
        pass

    ws = FakeWs()
    broadcaster._client_subscriptions[ws] = {SYMBOL}
    broadcaster.disconnect(ws)

    assert bridge.unsubscribed == [], (
        "the deployment's universe was unsubscribed by a browser leaving"
    )
    assert SYMBOL not in broadcaster._subscribed_symbols
    assert broadcaster._runner_symbols == {SYMBOL}


def test_a_browser_unsubscribe_does_not_drop_a_deployments_symbol(monkeypatch):
    broadcaster, bridge = _broadcaster(monkeypatch)
    broadcaster._symbol_to_topic[SYMBOL] = "nseeq/2885"

    class FakeWs:
        pass

    ws = FakeWs()
    broadcaster._client_subscriptions[ws] = {SYMBOL}
    broadcaster._subscribed_symbols[SYMBOL] = 1
    broadcaster.ensure_symbols([SYMBOL])

    import asyncio

    asyncio.run(broadcaster.unsubscribe(ws, [SYMBOL]))
    assert bridge.unsubscribed == []


def test_a_symbol_no_longer_wanted_is_taken_off_the_feed(monkeypatch):
    """A stopped deployment should stop consuming feed bandwidth."""
    broadcaster, bridge = _broadcaster(monkeypatch)
    broadcaster.ensure_symbols([SYMBOL])
    bridge.unsubscribed.clear()

    report = broadcaster.ensure_symbols([])

    assert report["removed"] == [SYMBOL]
    assert len(bridge.unsubscribed) == 1
    assert broadcaster._runner_symbols == set()


def test_a_symbol_a_browser_still_watches_is_not_taken_off(monkeypatch):
    broadcaster, bridge = _broadcaster(monkeypatch)
    broadcaster._symbol_to_topic[SYMBOL] = "nseeq/2885"
    broadcaster.ensure_symbols([SYMBOL])
    # A browser picks it up, then the deployment stops.
    broadcaster._subscribed_symbols[SYMBOL] = 1
    bridge.unsubscribed.clear()

    broadcaster.ensure_symbols([])

    assert bridge.unsubscribed == [], "the browser's subscription was cancelled"


# ---------------------------------------------------------------------------
# 4. failure is retried, not remembered as done
# ---------------------------------------------------------------------------
def test_a_statement_made_while_the_bridge_is_down_is_retried(monkeypatch):
    """The subtle one: "no session yet" must not be recorded as "subscribed".

    A deployment started before the broker session exists would otherwise be
    permanently absent from the feed while looking perfectly healthy.
    """
    broadcaster, bridge = _broadcaster(monkeypatch)
    bridge.is_connected = False
    # No session: `_ensure_bridge` cannot build one either.
    monkeypatch.setattr(broadcaster, "_ensure_bridge", lambda: False)

    first = broadcaster.ensure_symbols([SYMBOL])
    assert first["added"] == [SYMBOL]
    assert bridge.subscribed == [], "nothing can have reached the exchange"

    # The session arrives.
    bridge.is_connected = True
    monkeypatch.setattr(broadcaster, "_ensure_bridge", lambda: True)

    second = broadcaster.ensure_symbols([SYMBOL])
    assert second["added"] == [SYMBOL], "the outstanding symbol was not retried"
    assert len(bridge.subscribed) == 1


def test_a_broker_error_during_subscribe_is_retried(monkeypatch):
    """A bridge that throws on subscribe must not consume the statement.

    The symbol is still wanted and still absent, so the next statement has to
    send it again — otherwise one transient MQTT error leaves a deployment off
    the feed for the rest of its life.
    """
    broadcaster, bridge = _broadcaster(monkeypatch)

    def explode(topics):
        raise RuntimeError("bridge is unhappy")

    monkeypatch.setattr(bridge, "subscribe_feed", explode)
    assert broadcaster.ensure_symbols([SYMBOL])["added"] == [SYMBOL]
    assert broadcaster._runner_acked == set()

    monkeypatch.setattr(bridge, "subscribe_feed", bridge.subscribed.extend)
    broadcaster.ensure_symbols([SYMBOL])
    assert len(bridge.subscribed) == 1, "the outstanding symbol was not retried"
    assert broadcaster._runner_acked == {SYMBOL}


def test_a_symbol_with_no_contract_is_reported_and_not_retried_every_pass(monkeypatch):
    """A symbol with no conid can never be priced from live ticks.

    It is reported once — a fact an operator can act on — rather than
    re-resolved and re-warned about on every pass forever.
    """
    broadcaster, _bridge = _broadcaster(monkeypatch, resolve=False)

    first = broadcaster.ensure_symbols([SYMBOL])
    assert first["unresolved"] == [SYMBOL]
    assert first["added"] == []

    second = broadcaster.ensure_symbols([SYMBOL])
    assert second["unresolved"] == [], "the unresolvable symbol was retried"
    assert second["added"] == []


def test_an_unresolvable_symbol_is_retried_once_it_leaves_and_returns(monkeypatch):
    """A corrected symbol list gets a fresh attempt."""
    broadcaster, _bridge = _broadcaster(monkeypatch, resolve=False)
    broadcaster.ensure_symbols([SYMBOL])

    broadcaster.ensure_symbols([])
    monkeypatch.setattr(
        broadcaster,
        "_resolve_symbol",
        lambda s, e="NSEEQ": broadcaster._symbol_to_topic.setdefault(s, "nseeq/1"),
    )
    assert broadcaster.ensure_symbols([SYMBOL])["added"] == [SYMBOL]


# ---------------------------------------------------------------------------
# 5. the runner is the caller, and it states the running universe
# ---------------------------------------------------------------------------
def test_the_runner_states_the_union_of_its_running_deployments(app_db, monkeypatch):
    """The runner is the only thing that knows the current universe."""
    from datetime import datetime as _dt

    from atr.appdb.repositories import DeploymentRepository
    from atr.appdb.schema import users
    from atr.services import paper as paper_service
    from atr.services.runner import PaperRunner

    monkeypatch.setattr(
        "atr.signals.engine.load_daily", lambda symbol, exchange="NSEEQ", **kw: None
    )

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1", email="f@example.com", username="f", display_name="F",
                password_hash="x", role="owner", is_active=True, mfa_enabled=False,
                failed_logins=0, created_at=_dt(2026, 1, 1), updated_at=_dt(2026, 1, 1),
            )
        )
        DeploymentRepository.create(
            session, user_id="u1", strategy_id="s", strategy_version=1, mode="PAPER",
            capital=100_000.0, status="RUNNING",
            config={"symbols": ["RELIANCE", "TCS"], "exchange": "NSEEQ"},
        )
        DeploymentRepository.create(
            session, user_id="u1", strategy_id="s", strategy_version=1, mode="PAPER",
            capital=100_000.0, status="RUNNING",
            config={"symbols": ["INFY"], "exchange": "NSEEQ"},
        )

    calls: list[object] = []
    paper_service.install_live_subscriber(
        lambda symbols, exchange="NSEEQ": calls.append(symbols) or {
            "added": [], "removed": [], "unresolved": []
        }
    )

    runner = PaperRunner(db=app_db, price_source=lambda s, e: 100.0)
    runner.pass_once()

    assert calls, "the runner never told the feed what it needs"
    stated = calls[-1]
    assert set(stated["NSEEQ"]) == {"RELIANCE", "TCS", "INFY"}


def test_the_runner_does_not_state_a_universe_when_nothing_is_running(app_db, monkeypatch):
    from atr.services import paper as paper_service
    from atr.services.runner import PaperRunner

    calls: list[object] = []
    paper_service.install_live_subscriber(lambda symbols, exchange="NSEEQ": calls.append(symbols))
    PaperRunner(db=app_db, price_source=lambda s, e: 100.0).pass_once()
    assert calls == []


def test_a_failing_subscriber_does_not_stop_the_pass(app_db, monkeypatch):
    """A feed that cannot be subscribed must not take the trading loop down."""
    from atr.services import paper as paper_service
    from atr.services.runner import PaperRunner

    def explode(symbols, exchange="NSEEQ"):
        raise RuntimeError("no feed today")

    paper_service.install_live_subscriber(explode)
    tick = PaperRunner(db=app_db, price_source=lambda s, e: 100.0).pass_once()
    assert tick.deployments == 0  # nothing running; the point is that it returned


# ---------------------------------------------------------------------------
# 6. the seam itself
# ---------------------------------------------------------------------------
def test_no_subscriber_installed_is_not_an_error(subscriber_reset):
    """A CLI run, a test, or a stream that failed to start.

    Absence degrades to the daily cache — the documented behaviour — rather than
    raising into the trading loop.
    """
    from atr.services.paper import ensure_live_symbols

    assert ensure_live_symbols(["RELIANCE"]) is None


def test_the_api_layer_installs_both_seams(monkeypatch, subscriber_reset):
    """Reading a price is useless without being on the feed.

    Pinned as one wiring because the two are installed in one place and it is
    exactly the omission of the second that produced the defect.
    """
    from atr.api import price_sources
    from atr.api.stream import TickBroadcaster
    from atr.services import paper as paper_service

    broadcaster = TickBroadcaster()
    monkeypatch.setattr("atr.api.stream.get_broadcaster", lambda: broadcaster)

    price_sources.install_live_price_source()

    assert paper_service._LIVE_SOURCE[0] is not None, "no price source was installed"
    assert paper_service._LIVE_SUBSCRIBER[0] is not None, "no subscriber was installed"

    price_sources.clear_live_price_source()
    assert paper_service._LIVE_SOURCE[0] is None
    assert paper_service._LIVE_SUBSCRIBER[0] is None
