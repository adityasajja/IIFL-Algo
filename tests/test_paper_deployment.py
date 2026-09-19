"""The paper deployment's strategy version is its subject, not a label.

What this suite is protecting
-----------------------------

A paper deployment stores an immutable ``(strategy_id, strategy_version)`` pair
and stamps that version onto every order it places. That stamp is a claim: *this
trade came from that definition*. The claim is only true if the runner actually
reads the version and evaluates its rules.

It did not. ``DeploymentLoop._rules()`` resolved rules from
``STRATEGIES[strategy_id]`` — treating the *strategy id* as a registry key — and
never opened the version at all. Worse, no class in that registry defines
``signal_rules``, so the lookup always missed and fell through to
``EntryRules()``/``ExitRules()`` defaults. Every paper deployment in this
codebase's history has therefore been trading a generic default ruleset while
reporting itself, in the orders table, as the strategy the operator chose.

That is the worst category of bug in this system: nothing raised, the loop
traded, and the artefacts were internally consistent — just attributed to rules
that never ran. The tests below pin the correction:

* the version's stored rules are what execute,
* two versions of one strategy produce different behaviour,
* an unresolvable version is *refused*, never defaulted,
* ``blocked_reason`` says why, so the monitoring screen can distinguish
  "quiet market" from "not actually trading".
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from atr.services.runner import DeploymentLoop, PaperRunner

#: A Monday, 10:00 IST — inside the cash session.
SESSION = datetime(2026, 9, 14, 10, 0)
SYMBOL = "RELIANCE"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _daily(symbol: str, n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    """A flat daily frame long enough for every indicator to warm up."""
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "ts": index,
            "open": [start] * n,
            "high": [start * 1.01] * n,
            "low": [start * 0.99] * n,
            "close": [start] * n,
            "volume": [100_000.0] * n,
        }
    )


@pytest.fixture()
def user(app_db):
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1",
                email="t@example.com",
                username="tester",
                display_name="T",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    return "u1"


def _make_strategy(app_db, *, name: str, definition: dict) -> tuple[str, int]:
    """Create a saved strategy with one version. Returns (strategy_id, version)."""
    from atr.appdb.repositories import StrategyRepository

    with app_db.session() as session:
        strategy = StrategyRepository.create(
            session,
            user_id="u1",
            name=name,
            kind="rules",
            description="seeded by the deployment test",
            engine_key="sma_crossover",
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id="u1",
            definition=definition,
        )
    return strategy["strategy_id"], int(version["version"])


def _deploy(app_db, *, strategy_id: str, version: int, symbols=(SYMBOL,)) -> dict:
    from atr.appdb.repositories import DeploymentRepository

    with app_db.session() as session:
        return DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id=strategy_id,
            strategy_version=version,
            mode="PAPER",
            capital=200_000.0,
            status="RUNNING",
            config={"symbols": list(symbols), "exchange": "NSEEQ"},
        )


@pytest.fixture()
def runner(app_db, user, monkeypatch):
    """A PaperRunner with stubbed history, so nothing touches a real cache."""
    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(symbol),
    )
    return PaperRunner(db=app_db, price_source=lambda s, e: 1000.0)


# ---------------------------------------------------------------------------
# the version is the subject
# ---------------------------------------------------------------------------
def test_the_version_stored_rules_are_what_execute(app_db, runner):
    """The rules that run must be the version's, not the registry's defaults.

    The version below disables the trend exit (``trend_sma=0``) and sets a stop
    of exactly 7.5%. If the resolver were ignored and the class defaults used,
    the stop would read 15.0 — a number that would also make this test pass if
    it only asserted "some stop exists". Asserting the exact value is what
    distinguishes "the version ran" from "something ran".
    """
    strategy_id, version = _make_strategy(
        app_db,
        name="Rule-bearing version",
        definition={
            "engine_key": "sma_crossover",
            "params": {"fast": 8, "slow": 21},
            "rules": {
                "entry": {"trend_fast_sma": 8, "trend_slow_sma": 21, "min_history_bars": 30},
                "exit": {
                    "stop_loss_pct": 7.5,
                    "take_profit_pct": 12.0,
                    "trailing_stop_pct": None,
                    "trend_sma": 0,
                    "rsi_overbought": None,
                },
            },
        },
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    resolved = loop._rules()
    assert resolved is not None, loop.blocked_reason
    entry, exit_rules = resolved

    assert exit_rules.stop_loss_pct == 7.5
    assert exit_rules.take_profit_pct == 12.0
    assert exit_rules.trend_sma == 0
    assert entry.trend_fast_sma == 8
    assert entry.trend_slow_sma == 21


def test_an_unknown_strategy_is_refused_rather_than_defaulted(app_db, runner):
    """A version that cannot be read must stop the deployment, not paper over it.

    This is the regression that matters. ``sma_pullback`` is not a strategy in
    this codebase, and no version exists for it. The old code ran generic
    defaults and placed orders; the artefact then said the trades came from
    ``sma_pullback``. Refusing is the only answer that does not lie.
    """
    row = _deploy(app_db, strategy_id="sma_pullback", version=1)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    assert loop._rules() is None
    assert loop.blocked_reason, "a refusal must explain itself for the status surface"
    assert "sma_pullback" in loop.blocked_reason


def test_a_blocked_deployment_places_no_orders(app_db, runner):
    """Refusal has to mean no orders, not just a message.

    A ``blocked_reason`` that still trades would be worse than the bug it
    replaced: it would document the wrong rules while running them.
    """
    row = _deploy(app_db, strategy_id="not_a_real_strategy", version=3)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    acted = loop.evaluate()
    assert acted == []
    assert loop.last_pass.get("blocked"), "the pass must record why it did nothing"


@pytest.mark.parametrize("version", [7, 99])
def test_a_missing_version_number_is_refused(app_db, runner, version):
    """A version that does not exist is not "the latest one"."""
    strategy_id, real_version = _make_strategy(
        app_db,
        name="One version only",
        definition={
            "rules": {
                "entry": {"trend_fast_sma": 5},
                "exit": {"stop_loss_pct": 4.0},
            }
        },
    )
    assert version != real_version
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    assert loop._rules() is None
    assert loop.blocked_reason


def test_two_versions_of_one_strategy_differ(app_db, runner):
    """The point of versioning: v1 and v2 are different subjects.

    If both resolved to the same rules, "select immutable strategy version" would
    be a control that changes nothing — and a backtest of v1 would not predict
    what a deployment of v1 does.
    """
    strategy_id, v1 = _make_strategy(
        app_db,
        name="Tight",
        definition={"rules": {"exit": {"stop_loss_pct": 3.0}, "entry": {"trend_fast_sma": 5}}},
    )
    from atr.appdb.repositories import StrategyRepository

    with app_db.session() as session:
        v2row = StrategyRepository.create_version(
            session,
            strategy_id=strategy_id,
            author_user_id="u1",
            definition={
                "rules": {"exit": {"stop_loss_pct": 18.0}, "entry": {"trend_fast_sma": 40}}
            },
            change_note="widen the stop",
        )
    v2 = int(v2row["version"])
    assert v2 != v1

    stops = {}
    for version in (v1, v2):
        row = _deploy(app_db, strategy_id=strategy_id, version=version)
        runner.sync_loops()
        loop = runner._loops[row["deployment_id"]]
        resolved = loop._rules()
        assert resolved is not None, loop.blocked_reason
        stops[version] = resolved[1].stop_loss_pct

    assert stops[v1] == 3.0
    assert stops[v2] == 18.0
    assert stops[v1] != stops[v2]


def test_a_flat_definition_without_a_rules_block_is_refused(app_db, runner):
    """A definition whose rules are not expressible must not silently default.

    A version can legitimately be a code strategy carrying nothing but an
    ``engine_key`` whose class has no ``signal_rules``. There are no rules to
    run, and inventing them is the failure being guarded against.
    """
    strategy_id, version = _make_strategy(
        app_db,
        name="Key only",
        definition={"engine_key": "a_strategy_without_signal_rules"},
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    assert loop._rules() is None
    assert loop.blocked_reason


def test_a_malformed_rule_block_is_refused_not_crashed(app_db, runner):
    """Stored JSON is untrusted input: a bad key must refuse, not raise.

    A raised exception inside the loop would be caught by the pass, but the
    deployment would retry the same broken read every second forever. A refusal
    is cached and reported once.
    """
    strategy_id, version = _make_strategy(
        app_db,
        name="Malformed",
        definition={"rules": {"exit": {"stop_loss_pct": "not-a-number"}}},
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    assert loop._rules() is None
    assert loop.blocked_reason


def test_the_nested_rules_shape_is_accepted(app_db, runner):
    """``definition["rules"]["entry"|"exit"]`` is the documented shape.

    ``DATA_MODEL.md`` calls the column "canonical JSON of rules / params / code",
    so both a top-level and a nested placement are plausible; both must work, or
    which one a caller chose decides whether the deployment trades.
    """
    strategy_id, version = _make_strategy(
        app_db,
        name="Nested",
        definition={
            "rules": {
                "entry": {"breakout_lookback": 20},
                "exit": {"stop_loss_pct": 6.25, "trailing_stop_pct": 9.0},
            }
        },
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    resolved = loop._rules()
    assert resolved is not None, loop.blocked_reason
    entry, exit_rules = resolved
    assert exit_rules.stop_loss_pct == 6.25
    assert exit_rules.trailing_stop_pct == 9.0
    assert entry.breakout_lookback == 20


def test_the_version_is_read_once_per_version_not_once_per_pass(app_db, runner):
    """A version is immutable, so re-reading it every second is pure cost.

    The runner evaluates every second; the store read is the only I/O on the
    resolution path. Caching is safe precisely because the row can never change.
    """
    strategy_id, version = _make_strategy(
        app_db,
        name="Cached",
        definition={"rules": {"exit": {"stop_loss_pct": 5.0}}},
    )
    row = _deploy(app_db, strategy_id=strategy_id, version=version)
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]

    calls = {"n": 0}
    original = runner._read_version_rules

    def counting(key):
        calls["n"] += 1
        return original(key)

    runner._read_version_rules = counting  # type: ignore[method-assign]

    for _ in range(5):
        assert loop._rules() is not None
    assert calls["n"] == 1, f"version read {calls['n']} times; expected once"


def test_a_deployment_with_no_version_refuses(app_db, runner):
    """A deployment must name a version. Without one there is no subject."""
    from atr.appdb.repositories import DeploymentRepository

    with app_db.session() as session:
        row = DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id="sma_crossover",
            strategy_version=0,
            mode="PAPER",
            capital=100_000.0,
            status="RUNNING",
            config={"symbols": [SYMBOL], "exchange": "NSEEQ"},
        )
    runner.sync_loops()
    loop = runner._loops[row["deployment_id"]]
    assert loop._rules() is None
    assert loop.blocked_reason
