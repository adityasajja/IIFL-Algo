"""The trading- and strategy-tier repositories.

These are the tables that guard money, so the tests here are about the
*invariants* rather than the CRUD:

* ``orders.status`` must equal the newest ``order_events`` row, always.
* A duplicate idempotency key must return the original order and must not create
  a second one — and the check must survive the caller's transaction being
  poisoned by the conflict.
* A strategy version must be immutable, and an identical definition must be
  refused rather than stored as a new version.
* A reconciliation run that found differences must not be able to record itself
  as ``ok``.
* Every read is user-scoped, so a route that forgets to check ownership gets
  ``None`` rather than another account's order.
"""

from __future__ import annotations

import pytest

from atr.appdb.repositories import (
    BacktestRunRepository,
    DeploymentRepository,
    DuplicateDefinition,
    IdempotencyConflict,
    OrderEventRepository,
    OrderIntentRepository,
    OrderRepository,
    ReconciliationRepository,
    ScreenerRepository,
    StrategyRepository,
    TradeJournalRepository,
    UserRepository,
    canonical_definition,
    definition_hash,
)
from atr.execution.oms import idempotency_key_for


@pytest.fixture()
def users(app_db):
    """Two accounts, so every scoping assertion has a second party to test with."""
    with app_db.session() as session:
        alice = UserRepository.create(
            session, email="alice@example.com", username="alice",
            password_hash="x", role="trader",
        )
        bob = UserRepository.create(
            session, email="bob@example.com", username="bob",
            password_hash="x", role="trader",
        )
        return alice["user_id"], bob["user_id"]


def _order(app_db, user_id: str, **overrides):
    kwargs = {
        "user_id": user_id,
        "symbol": "reliance",
        "side": "buy",
        "quantity": 10,
        "mode": "paper",
        "requested_price": 2500.0,
    }
    kwargs.update(overrides)
    with app_db.session() as session:
        return OrderRepository.create(session, **kwargs)


# =========================================================================== orders
def test_creating_an_order_writes_its_opening_event(app_db, users):
    """An order with no events is malformed — the log is what we reconstruct from."""
    alice, _ = users
    order = _order(app_db, alice)

    with app_db.session() as session:
        history = OrderEventRepository.history(session, order["order_id"])

    assert len(history) == 1
    assert history[0]["seq"] == 1
    assert history[0]["from_status"] is None
    assert history[0]["to_status"] == "NEW"
    assert history[0]["source"] == "oms"


def test_order_inputs_are_normalised(app_db, users):
    alice, _ = users
    order = _order(app_db, alice, symbol="reliance", side="buy", mode="paper")
    assert order["symbol"] == "RELIANCE"
    assert order["side"] == "BUY"
    assert order["mode"] == "PAPER"


@pytest.mark.parametrize(
    ("field", "value"),
    [("mode", "TURBO"), ("side", "HOLD"), ("quantity", 0), ("quantity", -5)],
)
def test_invalid_order_inputs_are_refused(app_db, users, field, value):
    """Refused at the repository, so no route can bypass it by forgetting to validate."""
    alice, _ = users
    with pytest.raises(ValueError):
        _order(app_db, alice, **{field: value})


def test_status_is_a_projection_of_the_newest_event(app_db, users):
    """The invariant the whole event-log design rests on."""
    alice, _ = users
    order = _order(app_db, alice)
    order_id = order["order_id"]

    with app_db.session() as session:
        for from_status, to_status in (
            ("NEW", "VALIDATING"),
            ("VALIDATING", "RISK_APPROVED"),
            ("RISK_APPROVED", "SUBMITTED"),
            ("SUBMITTED", "ACKNOWLEDGED"),
            ("ACKNOWLEDGED", "PARTIALLY_FILLED"),
            ("PARTIALLY_FILLED", "FILLED"),
        ):
            OrderEventRepository.append(
                session, order_id=order_id, from_status=from_status, to_status=to_status
            )

    with app_db.session() as session:
        stored = OrderRepository.get(session, order_id, alice)
        folded = OrderEventRepository.current_status(session, order_id)
        assert stored["status"] == folded == "FILLED"


def test_the_lifecycle_is_reconstructable_from_events_alone(app_db, users):
    """Every transition is recoverable, in order, without reading ``orders``."""
    alice, _ = users
    order = _order(app_db, alice)
    order_id = order["order_id"]
    transitions = [
        ("NEW", "VALIDATING"),
        ("VALIDATING", "RISK_APPROVED"),
        ("RISK_APPROVED", "SUBMITTED"),
        ("SUBMITTED", "ACKNOWLEDGED"),
        ("ACKNOWLEDGED", "PARTIALLY_FILLED"),
        ("PARTIALLY_FILLED", "FILLED"),
    ]
    with app_db.session() as session:
        for from_status, to_status in transitions:
            OrderEventRepository.append(
                session, order_id=order_id, from_status=from_status, to_status=to_status
            )

    with app_db.session() as session:
        history = OrderEventRepository.history(session, order_id)

    assert [e["seq"] for e in history] == list(range(1, len(transitions) + 2))
    assert [(e["from_status"], e["to_status"]) for e in history[1:]] == transitions
    assert history[-1]["to_status"] == "FILLED"


def test_events_sharing_a_timestamp_still_order_by_sequence(app_db, users):
    """Timestamps are not a total order; ``seq`` is. This is why both exist."""
    alice, _ = users
    order = _order(app_db, alice)
    order_id = order["order_id"]

    with app_db.session() as session:
        first = OrderEventRepository.append(
            session, order_id=order_id, from_status="NEW", to_status="VALIDATING"
        )
        # Same instant, written second.
        second = OrderEventRepository.append(
            session,
            order_id=order_id,
            from_status="VALIDATING",
            to_status="RISK_APPROVED",
            ts=first["ts"],
        )

    assert second["seq"] == first["seq"] + 1
    with app_db.session() as session:
        assert OrderEventRepository.current_status(session, order_id) == "RISK_APPROVED"


def test_a_non_fill_event_does_not_erase_the_requested_price(app_db, users):
    """Otherwise every slippage figure becomes unmeasurable the moment an order is acked."""
    alice, _ = users
    order = _order(app_db, alice, requested_price=2500.0)
    order_id = order["order_id"]

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=order_id, from_status="RISK_APPROVED", to_status="SUBMITTED"
        )
        OrderEventRepository.append(
            session,
            order_id=order_id,
            from_status="SUBMITTED",
            to_status="ACKNOWLEDGED",
            broker_order_id="BR-77",
        )

    with app_db.session() as session:
        stored = OrderRepository.get(session, order_id, alice)

    assert stored["requested_price"] == 2500.0
    assert stored["broker_order_id"] == "BR-77"
    assert stored["submitted_at"] is not None
    assert stored["completed_at"] is None


def test_filled_quantity_is_cumulative_and_the_projection_copies_it(app_db, users):
    """The convention matches the broker's own reporting, so raw and parsed agree."""
    alice, _ = users
    order = _order(app_db, alice, quantity=10)
    order_id = order["order_id"]

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=order_id, from_status="ACKNOWLEDGED",
            to_status="PARTIALLY_FILLED", filled_qty=4.0, filled_price=2501.5,
        )
        OrderEventRepository.append(
            session, order_id=order_id, from_status="PARTIALLY_FILLED",
            to_status="FILLED", filled_qty=10.0, filled_price=2502.0,
        )

    with app_db.session() as session:
        stored = OrderRepository.get(session, order_id, alice)

    assert stored["filled_quantity"] == 10.0
    assert stored["avg_fill_price"] == 2502.0
    assert stored["completed_at"] is not None


def test_fills_are_events_not_a_separate_table(app_db, users):
    alice, _ = users
    order = _order(app_db, alice)
    order_id = order["order_id"]

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=order_id, from_status="ACKNOWLEDGED",
            to_status="PARTIALLY_FILLED", filled_qty=4.0, filled_price=2501.5,
        )
        OrderEventRepository.append(
            session, order_id=order_id, from_status="PARTIALLY_FILLED",
            to_status="FILLED", filled_qty=10.0, filled_price=2502.0,
        )

    with app_db.session() as session:
        fills = OrderEventRepository.fills(session, order_id)

    assert [f["to_status"] for f in fills] == ["PARTIALLY_FILLED", "FILLED"]
    assert [f["filled_qty"] for f in fills] == [4.0, 10.0]


def test_the_raw_broker_payload_is_kept_verbatim(app_db, users):
    alice, _ = users
    order = _order(app_db, alice)

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=order["order_id"], from_status="SUBMITTED",
            to_status="ACKNOWLEDGED", raw={"status": "OPEN", "filledQty": 0},
        )
        event = OrderEventRepository.latest(session, order["order_id"])

    assert '"filledQty": 0' in event["raw"]


def test_open_orders_excludes_every_terminal_status(app_db, users):
    """Derived from the terminal set, so a new status cannot silently drop out."""
    alice, _ = users
    live = _order(app_db, alice)
    done = _order(app_db, alice)
    killed = _order(app_db, alice)

    with app_db.session() as session:
        OrderEventRepository.append(
            session, order_id=done["order_id"], from_status="ACKNOWLEDGED",
            to_status="FILLED", filled_qty=10.0, filled_price=2502.0,
        )
        OrderEventRepository.append(
            session, order_id=killed["order_id"], from_status="VALIDATING",
            to_status="REJECTED", reject_reason="kill switch engaged",
        )

    with app_db.session() as session:
        open_ids = {o["order_id"] for o in OrderRepository.open_orders(session, alice)}

    assert open_ids == {live["order_id"]}


def test_order_reads_are_user_scoped(app_db, users):
    alice, bob = users
    order = _order(app_db, alice)

    with app_db.session() as session:
        assert OrderRepository.get(session, order["order_id"], bob) is None
        assert OrderRepository.list_for_user(session, bob)[1] == 0
        assert OrderRepository.list_for_user(session, alice)[1] == 1


def test_order_listing_filters(app_db, users):
    alice, _ = users
    _order(app_db, alice, symbol="TCS")
    _order(app_db, alice, symbol="INFY")

    with app_db.session() as session:
        rows, total = OrderRepository.list_for_user(session, alice, symbol="tcs")
        by_correlation, _ = OrderRepository.list_for_user(session, alice)

    assert total == 1
    assert rows[0]["symbol"] == "TCS"
    assert len(by_correlation) == 2


# ============================================================================ idempotency
def _key(user_id: str, *, signal_id: str = "sig-1") -> str:
    return idempotency_key_for(
        user_id=user_id, strategy_id="s1", strategy_version=1,
        signal_id=signal_id, symbol="TCS", side="BUY",
    )


def test_the_key_is_stable_and_intent_specific(app_db, users):
    alice, bob = users
    base = _key(alice)

    assert base == _key(alice)
    assert base != _key(alice, signal_id="sig-2")
    # A shared registry strategy must not let two accounts collide: the second
    # account would be handed the first account's order id.
    assert base != _key(bob)


def test_a_duplicate_claim_returns_the_original_order(app_db, users):
    """A retry is the case this exists for, so it is not an error."""
    alice, _ = users
    key = _key(alice)
    first = _order(app_db, alice, symbol="TCS", side="BUY", quantity=5)

    with app_db.session() as session:
        order_id, created = OrderIntentRepository.create_if_absent(
            session, idempotency_key=key, order_id=first["order_id"], user_id=alice,
            strategy_id="s1", strategy_version=1, signal_id="sig-1",
        )
    assert created is True
    assert order_id == first["order_id"]

    # The duplicate arrives as a *different* order row, as it would if the
    # caller had already inserted before checking.
    second = _order(app_db, alice, symbol="TCS", side="BUY", quantity=5)
    with app_db.session() as session:
        order_id, created = OrderIntentRepository.create_if_absent(
            session, idempotency_key=key, order_id=second["order_id"], user_id=alice,
            strategy_id="s1", strategy_version=1, signal_id="sig-1",
        )
        assert created is False
        assert order_id == first["order_id"]
        assert OrderIntentRepository.for_order(session, second["order_id"]) is None


def test_the_session_survives_the_conflict(app_db, users):
    """The savepoint is what keeps the caller's transaction usable.

    Without it the failed INSERT poisons the transaction and the read that has to
    follow — "which order already owns this key?" — is impossible.
    """
    alice, _ = users
    key = _key(alice)
    first = _order(app_db, alice, symbol="TCS", side="BUY", quantity=5)
    second = _order(app_db, alice, symbol="TCS", side="BUY", quantity=5)

    with app_db.session() as session:
        OrderIntentRepository.create_if_absent(
            session, idempotency_key=key, order_id=first["order_id"], user_id=alice
        )
        OrderIntentRepository.create_if_absent(
            session, idempotency_key=key, order_id=second["order_id"], user_id=alice
        )
        # Still usable, and the second order is still there.
        assert OrderRepository.get(session, second["order_id"], alice) is not None


def test_a_key_may_not_be_reused_across_accounts(app_db, users):
    """The one failure mode a duplicate guard must never have."""
    alice, bob = users
    key = _key(alice)
    first = _order(app_db, alice, symbol="TCS", side="BUY", quantity=5)

    with app_db.session() as session:
        OrderIntentRepository.create_if_absent(
            session, idempotency_key=key, order_id=first["order_id"], user_id=alice
        )

    with pytest.raises(IdempotencyConflict), app_db.session() as session:
        OrderIntentRepository.create_if_absent(
            session, idempotency_key=key,
            order_id=first["order_id"], user_id=bob,
        )


# ============================================================================ deployments
def test_a_deployment_requires_a_reason_to_stop(app_db, users):
    """"Why did this stop trading?" is the first question asked when it is wrong."""
    alice, _ = users
    with app_db.session() as session:
        deployment = DeploymentRepository.create(
            session, user_id=alice, strategy_id="s1", strategy_version=1,
            capital=100_000.0,
        )
        deployment_id = deployment["deployment_id"]
        DeploymentRepository.start(session, deployment_id, alice)

    with pytest.raises(ValueError), app_db.session() as session:
        DeploymentRepository.stop(session, deployment_id, alice, reason="   ")

    with app_db.session() as session:
        assert DeploymentRepository.stop(
            session, deployment_id, alice, reason="daily loss limit"
        ) == 1
        stored = DeploymentRepository.get(session, deployment_id, alice)
        assert stored["stop_reason"] == "daily loss limit"
        # Idempotent: a second stop does not rewrite the reason.
        assert DeploymentRepository.stop(
            session, deployment_id, alice, reason="again"
        ) == 0
        assert DeploymentRepository.get(session, deployment_id, alice)["stop_reason"] == (
            "daily loss limit"
        )


def test_a_deployment_is_not_readable_by_another_account(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        deployment = DeploymentRepository.create(
            session, user_id=alice, strategy_id="s1", strategy_version=1,
            capital=100_000.0,
        )
        assert DeploymentRepository.get(session, deployment["deployment_id"], bob) is None


def test_only_running_deployments_appear_in_running(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        started = DeploymentRepository.create(
            session, user_id=alice, strategy_id="s1", strategy_version=1,
            capital=50_000.0,
        )
        DeploymentRepository.create(
            session, user_id=alice, strategy_id="s1", strategy_version=1,
            capital=50_000.0,
        )
        DeploymentRepository.start(session, started["deployment_id"], alice)
        running = DeploymentRepository.running(session, alice)

    assert [d["deployment_id"] for d in running] == [started["deployment_id"]]


# ============================================================================ reconciliation
def test_a_clean_run_records_ok(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        run = ReconciliationRepository.record(
            session, user_id=alice, scope="all", mismatches=[], severity="ok",
            internal_count=3, broker_count=3, duration_ms=42,
        )
        assert run["severity"] == "ok"
        assert run["mismatches"] == 0
        assert ReconciliationRepository.worst_unacknowledged(session, alice) is None


def test_a_run_with_mismatches_cannot_call_itself_ok(app_db, users):
    """A reconciliation that found differences and recorded 'ok' is worse than none."""
    alice, _ = users
    with pytest.raises(ValueError), app_db.session() as session:
        ReconciliationRepository.record(
            session, user_id=alice, scope="orders",
            mismatches=[{"symbol": "TCS", "internal": 5, "broker": 0}],
            severity="ok",
        )


def test_an_errored_run_cannot_call_itself_ok(app_db, users):
    alice, _ = users
    with pytest.raises(ValueError), app_db.session() as session:
        ReconciliationRepository.record(
            session, user_id=alice, scope="all", severity="ok", error="broker timeout"
        )


def test_a_critical_run_raises_a_banner_and_a_later_clean_run_clears_it(app_db, users):
    """The banner clears when the problem is fixed, without anyone dismissing it."""
    alice, _ = users
    with app_db.session() as session:
        bad = ReconciliationRepository.record(
            session, user_id=alice, scope="positions",
            mismatches=[{"symbol": "INFY", "internal": 0, "broker": 10}],
            severity="critical",
        )
        assert bad["mismatches"] == 1
        assert ReconciliationRepository.worst_unacknowledged(session, alice) is not None

    with app_db.session() as session:
        ReconciliationRepository.record(session, user_id=alice, scope="all", severity="ok")
        assert ReconciliationRepository.worst_unacknowledged(session, alice) is None


def test_reconciliation_runs_are_user_scoped(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        ReconciliationRepository.record(session, user_id=alice, scope="all", severity="ok")
        assert ReconciliationRepository.latest(session, bob) is None
        assert ReconciliationRepository.list_for_user(session, bob)[1] == 0


def test_an_unknown_severity_is_refused(app_db, users):
    alice, _ = users
    with pytest.raises(ValueError), app_db.session() as session:
        ReconciliationRepository.record(
            session, user_id=alice, scope="all", severity="catastrophic"
        )


# ============================================================================ strategies
@pytest.fixture()
def strategy(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        return StrategyRepository.create(
            session, user_id=alice, name="EMA Cross", kind="rules",
            description="fast/slow crossover",
        )


def test_a_strategy_needs_a_known_kind(app_db, users):
    alice, _ = users
    with pytest.raises(ValueError), app_db.session() as session:
        StrategyRepository.create(session, user_id=alice, name="X", kind="magic")


def test_the_first_version_is_one(app_db, strategy, users):
    alice, _ = users
    with app_db.session() as session:
        version = StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=alice,
            definition={"fast": 9, "slow": 21}, change_note="initial",
        )
    assert version["version"] == 1
    assert version["is_deployed"] is False


def test_key_order_does_not_make_a_new_version(app_db, strategy, users):
    """Two definitions that differ only in key order are the same strategy."""
    alice, _ = users
    with app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=alice,
            definition={"fast": 9, "slow": 21, "exit": {"type": "atr", "mult": 2.0}},
        )

    with pytest.raises(DuplicateDefinition), app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=alice,
            definition={"slow": 21, "fast": 9, "exit": {"mult": 2.0, "type": "atr"}},
        )


def test_a_duplicate_names_the_version_that_already_holds_it(app_db, strategy, users):
    """The useful half of a 409: which version is it."""
    alice, _ = users
    definition = {"fast": 9, "slow": 21}
    with app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=alice,
            definition=definition,
        )

    with pytest.raises(DuplicateDefinition) as caught, app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=alice,
            definition=definition,
        )
    assert caught.value.existing_version == 1


def test_a_changed_rule_creates_the_next_version(app_db, strategy, users):
    alice, _ = users
    sid = strategy["strategy_id"]
    with app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 9}
        )
        second = StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 12}
        )
        assert second["version"] == 2
        assert StrategyRepository.latest_version(session, sid)["version"] == 2
        assert StrategyRepository.version_count(session, sid) == 2
        assert [v["version"] for v in StrategyRepository.versions(session, sid)] == [2, 1]


def test_a_refused_version_does_not_consume_a_number(app_db, strategy, users):
    """Numbering is max+1, so the history has no gaps left by rejected attempts."""
    alice, _ = users
    sid = strategy["strategy_id"]
    with app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 9}
        )
    for _ in range(3):
        with pytest.raises(DuplicateDefinition), app_db.session() as session:
            StrategyRepository.create_version(
                session, strategy_id=sid, author_user_id=alice, definition={"fast": 9}
            )
    with app_db.session() as session:
        nxt = StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 15}
        )
    assert nxt["version"] == 2


def test_another_account_cannot_add_a_version(app_db, strategy, users):
    """The FK proves the strategy exists, not that this caller may add to it."""
    _, bob = users
    with pytest.raises(PermissionError), app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=strategy["strategy_id"], author_user_id=bob,
            definition={"fast": 9},
        )


def test_a_version_for_an_unknown_strategy_is_a_lookup_error(app_db, users):
    alice, _ = users
    with pytest.raises(LookupError), app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id="nope", author_user_id=alice, definition={"fast": 9}
        )


def test_renaming_a_strategy_does_not_touch_its_definition(app_db, strategy, users):
    """A version is immutable; metadata is not part of what the strategy *does*."""
    alice, _ = users
    sid = strategy["strategy_id"]
    with app_db.session() as session:
        created = StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 9}
        )
        assert StrategyRepository.update_meta(session, sid, alice, name="EMA 9/21") == 1
        stored = StrategyRepository.version(session, sid, 1)
        assert stored["definition"] == created["definition"]
        assert stored["definition_hash"] == created["definition_hash"]


def test_there_is_no_way_to_update_a_version_definition(app_db):
    """Immutability is enforced by the absence of a path, not by a convention."""
    assert not hasattr(StrategyRepository, "update_version")
    assert not hasattr(StrategyRepository, "delete_version")


def test_archiving_hides_a_strategy_without_deleting_its_versions(app_db, strategy, users):
    alice, _ = users
    sid = strategy["strategy_id"]
    with app_db.session() as session:
        StrategyRepository.create_version(
            session, strategy_id=sid, author_user_id=alice, definition={"fast": 9}
        )
        assert StrategyRepository.archive(session, sid, alice) == 1
        assert all(
            s["strategy_id"] != sid for s in StrategyRepository.list_for_user(session, alice)
        )
        listed = StrategyRepository.list_for_user(session, alice, include_archived=True)
        assert listed[0]["latest_version"] == 1
        assert listed[0]["version_count"] == 1
        assert StrategyRepository.version_count(session, sid) == 1


def test_strategy_names_are_unique_per_account(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        StrategyRepository.create(session, user_id=alice, name="EMA Cross", kind="rules")
        assert StrategyRepository.name_taken(session, alice, "ema cross")
        # Bob may use the same name — the constraint is (user_id, name).
        assert not StrategyRepository.name_taken(session, bob, "EMA Cross")
        StrategyRepository.create(session, user_id=bob, name="EMA Cross", kind="rules")


def test_canonical_definition_is_order_independent():
    assert canonical_definition({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert canonical_definition({"a": [1, 2], "b": 1}) == canonical_definition({"b": 1, "a": [1, 2]})
    # A string in the same rules must hash the same, or the dedupe is bypassable.
    assert definition_hash(canonical_definition('{"a": [1, 2], "b": 1}')) == (
        definition_hash(canonical_definition({"a": [1, 2], "b": 1}))
    )


# ============================================================================ backtest runs
def test_a_run_needs_a_target(app_db, users):
    """Either a registry key or a (strategy, version) pair — never neither."""
    alice, _ = users
    with pytest.raises(ValueError), app_db.session() as session:
        BacktestRunRepository.create(session, user_id=alice, config={})


def test_a_run_starts_queued(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="ema_cross",
            config={"start": "2015-01-01"},
        )
    assert run["status"] == "QUEUED"
    assert run["progress"] == 0.0
    assert run["started_at"] is None


@pytest.mark.parametrize(("given", "expected"), [(4.5, 1.0), (-2.0, 0.0), (0.4, 0.4)])
def test_progress_is_clamped(app_db, users, given, expected):
    alice, _ = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="k", config={}
        )
        BacktestRunRepository.mark_running(session, run["run_id"], alice)
        BacktestRunRepository.set_progress(session, run["run_id"], alice, given)
        assert BacktestRunRepository.get(session, run["run_id"], alice)["progress"] == expected


def test_a_completed_run_cannot_be_reopened_or_overwritten(app_db, users):
    """A late straggler from the worker must not rewrite a finished run's metrics."""
    alice, _ = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="k", config={}
        )
        run_id = run["run_id"]
        BacktestRunRepository.mark_running(session, run_id, alice)
        assert BacktestRunRepository.complete(
            session, run_id, alice, metrics={"sharpe": 1.83}, data_fingerprint="abc123"
        ) == 1

    with app_db.session() as session:
        assert BacktestRunRepository.fail(session, run_id, alice, error="late") == 0
        assert BacktestRunRepository.mark_running(session, run_id, alice) == 0
        assert BacktestRunRepository.cancel(session, run_id, alice) == 0
        stored = BacktestRunRepository.get(session, run_id, alice)
        assert stored["status"] == "COMPLETED"
        assert stored["progress"] == 1.0
        assert '"sharpe": 1.83' in stored["metrics"]
        assert stored["data_fingerprint"] == "abc123"


def test_a_failed_run_records_the_error(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="k", config={}
        )
        BacktestRunRepository.mark_running(session, run["run_id"], alice)
        assert BacktestRunRepository.fail(
            session, run["run_id"], alice, error="no data for RELIANCE"
        ) == 1
        stored = BacktestRunRepository.get(session, run["run_id"], alice)
        assert stored["status"] == "FAILED"
        assert stored["error"] == "no data for RELIANCE"
        assert stored["finished_at"] is not None


def test_a_cancelled_run_is_terminal(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="k", config={}
        )
        assert BacktestRunRepository.cancel(session, run["run_id"], alice) == 1
        assert BacktestRunRepository.mark_running(session, run["run_id"], alice) == 0


def test_backtest_runs_are_user_scoped(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session, user_id=alice, engine_key="k", config={}
        )
        assert BacktestRunRepository.get(session, run["run_id"], bob) is None
        assert BacktestRunRepository.list_for_user(session, bob)[1] == 0


# ============================================================================ screener
def test_a_saved_screen_round_trips(app_db, users):
    alice, _ = users
    definition = {
        "op": "AND",
        "children": [
            {"field": "ret_26w", "op": "gt", "value": 0.1},
            {"op": "OR", "children": [{"field": "volume_ratio", "op": "gt", "value": 1.5}]},
        ],
    }
    with app_db.session() as session:
        scan = ScreenerRepository.create(
            session, user_id=alice, name="Momentum leaders", definition=definition
        )
        assert ScreenerRepository.update(session, scan["scan_id"], alice, name="Momentum") == 1
        stored = ScreenerRepository.get(session, scan["scan_id"], alice)
    assert stored["name"] == "Momentum"
    assert '"ret_26w"' in stored["definition"]


def test_screen_names_are_unique_per_account(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        ScreenerRepository.create(session, user_id=alice, name="Leaders", definition={})
        assert ScreenerRepository.name_taken(session, alice, "leaders")
        assert not ScreenerRepository.name_taken(session, bob, "Leaders")


def test_a_screen_is_not_readable_or_deletable_by_another_account(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        scan = ScreenerRepository.create(
            session, user_id=alice, name="Leaders", definition={}
        )
        assert ScreenerRepository.get(session, scan["scan_id"], bob) is None
        assert ScreenerRepository.delete(session, scan["scan_id"], bob) == 0
        assert ScreenerRepository.get(session, scan["scan_id"], alice) is not None


# ============================================================================ trade journal
def test_closing_a_trade_derives_the_duration(app_db, users):
    """Derived rather than passed in, so the two cannot disagree."""
    alice, _ = users
    with app_db.session() as session:
        trade = TradeJournalRepository.open_trade(
            session, user_id=alice, symbol="tcs", side="BUY", quantity=10,
            entry_price=3000.0, signal_reason="26w momentum top decile",
        )
        assert trade["exit_ts"] is None
        assert trade["gross_pnl"] is None
        assert trade["symbol"] == "TCS"

    with app_db.session() as session:
        assert TradeJournalRepository.close_trade(
            session, trade["trade_id"], alice, exit_price=3060.0,
            gross_pnl=600.0, net_pnl=552.0, mfe=0.031, mae=-0.008,
            slippage_bps=5.0, regime="risk_on",
        ) == 1
        stored = TradeJournalRepository.get(session, trade["trade_id"], alice)

    assert stored["net_pnl"] == 552.0
    assert stored["duration_sec"] is not None
    assert stored["regime"] == "risk_on"
    assert stored["mfe"] == 0.031


def test_closing_a_trade_twice_is_a_no_op(app_db, users):
    alice, _ = users
    with app_db.session() as session:
        trade = TradeJournalRepository.open_trade(
            session, user_id=alice, symbol="TCS", side="BUY", quantity=10, entry_price=3000.0
        )
        TradeJournalRepository.close_trade(
            session, trade["trade_id"], alice, exit_price=3060.0, gross_pnl=600.0
        )
        assert TradeJournalRepository.close_trade(
            session, trade["trade_id"], alice, exit_price=1.0, gross_pnl=1.0
        ) == 0


def test_the_journal_is_user_scoped_and_filterable(app_db, users):
    alice, bob = users
    with app_db.session() as session:
        open_trade = TradeJournalRepository.open_trade(
            session, user_id=alice, symbol="TCS", side="BUY", quantity=10,
            entry_price=3000.0, strategy_id="s1",
        )
        closed = TradeJournalRepository.open_trade(
            session, user_id=alice, symbol="INFY", side="BUY", quantity=10,
            entry_price=1500.0, strategy_id="s2",
        )
        TradeJournalRepository.close_trade(
            session, closed["trade_id"], alice, exit_price=1520.0, gross_pnl=200.0
        )

        assert TradeJournalRepository.get(session, open_trade["trade_id"], bob) is None
        assert TradeJournalRepository.list_for_user(session, bob)[1] == 0
        assert TradeJournalRepository.list_for_user(session, alice, closed_only=True)[1] == 1
        assert TradeJournalRepository.list_for_user(session, alice, strategy_id="s1")[1] == 1
        assert TradeJournalRepository.list_for_user(session, alice)[1] == 2
