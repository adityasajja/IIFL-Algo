"""Portfolio policy and gate, at the unit layer.

Pinned here because each would silently change what trades if it drifted:

* **Policy validation fails closed.** Unknown fields are refused, percentage
  whole-numbers are refused, and a missing reason is refused — a limit the
  caller believes they set must never silently vanish.
* **No policy means no gate.** The pre-policy behaviour is pass-through, so
  every existing order path is untouched until an operator configures one.
* **Exits are never trapped.** An order that strictly reduces its own arm's
  position bypasses exposure and conflict checks; the strategy gate still
  applies (it runs first, elsewhere).
* **Conflicts never auto-resolve.** ``reject`` fails closed, ``priority``
  needs a *strict* rank win (ties fail closed), and ``net`` still counts
  gross exposure against the size limits.
* **Unpriceable is not zero.** When a finite notional limit is configured and
  the order cannot be priced, the notional checks are skipped and said to be
  skipped — never evaluated at zero.
"""

from __future__ import annotations

import pytest

from atr.execution.oms import OrderDraft
from atr.services.portfolio import (
    BookPosition,
    DeploymentSlice,
    PolicyError,
    PortfolioBook,
    PortfolioService,
    default_policy,
    evaluate_order,
    policy_from_dict,
)


def _draft(**over: object) -> OrderDraft:
    base = {
        "user_id": "u1",
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10.0,
        "deployment_id": "dep-a",
        "strategy_id": "strat-a",
    }
    base.update(over)
    return OrderDraft(**base)  # type: ignore[arg-type]


def _position(symbol: str, qty: float, price: float, dep: str = "dep-a") -> BookPosition:
    return BookPosition(
        symbol=symbol, qty=qty, avg_price=price, last_price=price,
        deployment_id=dep, strategy_id="strat-a",
    )


def _book(*deps: DeploymentSlice, capital: float = 500_000.0) -> PortfolioBook:
    return PortfolioBook(deployments=list(deps), total_capital=capital)


def _dep(
    dep_id: str,
    *positions: BookPosition,
    strategy: str = "strat-a",
    capital: float = 250_000.0,
) -> DeploymentSlice:
    by_symbol = {p.symbol: p for p in positions}
    return DeploymentSlice(
        deployment_id=dep_id,
        strategy_id=strategy,
        strategy_version=1,
        mode="PAPER",
        status="RUNNING",
        capital=capital,
        positions=by_symbol,
    )


def _sectors(symbol: str) -> str:
    return {"RELIANCE": "Energy", "TCS": "IT"}.get(symbol, "Unknown")


# ---------------------------------------------------------------------------
# policy validation
# ---------------------------------------------------------------------------


def test_unknown_policy_fields_are_refused_not_dropped():
    with pytest.raises(PolicyError):
        policy_from_dict({"max_totl_exposure": 100.0})


def test_whole_number_percentages_are_refused():
    with pytest.raises(PolicyError):
        policy_from_dict({"max_sector_exposure_pct": 40})


def test_conflict_mode_is_closed_vocabulary():
    with pytest.raises(PolicyError):
        policy_from_dict({"conflict_mode": "coin_flip"})
    assert policy_from_dict({"conflict_mode": "NET"}).conflict_mode == "net"


def test_default_policy_is_pass_through_with_reject_conflicts():
    policy = default_policy()
    assert policy.max_total_exposure is None
    assert policy.conflict_mode == "reject"
    decision = evaluate_order(
        _draft(), book=_book(), policy=policy, price=100.0,
    )
    assert decision.allowed is True


def _user(app_db) -> str:
    from atr.appdb.repositories import UserRepository

    with app_db.session() as session:
        user = UserRepository.create(
            session,
            email="owner-pp@example.com",
            username="owner-pp",
            password_hash="x",
            role="owner",
        )
    return user["user_id"]


def test_setting_policy_without_a_reason_is_refused(app_db):
    user_id = _user(app_db)
    service = PortfolioService(db=app_db)
    with pytest.raises(PolicyError):
        service.set_policy(
            user_id, {"max_total_exposure": 1.0}, actor="tester", reason="  "
        )


def test_policy_round_trips_through_the_store(app_db):
    user_id = _user(app_db)
    service = PortfolioService(db=app_db)
    assert service.get_policy(user_id) is None
    stored = service.set_policy(
        user_id,
        {"max_total_exposure": 100_000.0, "conflict_mode": "priority",
         "strategy_priorities": {"strat-a": 5}},
        actor="tester",
        reason="acceptance setup",
    )
    assert stored["policy"]["max_total_exposure"] == 100_000.0
    reloaded = service.get_policy(user_id)
    assert reloaded is not None
    assert reloaded.max_total_exposure == 100_000.0
    assert reloaded.strategy_priorities == {"strat-a": 5.0}


# ---------------------------------------------------------------------------
# exits are exempt, openers are measured
# ---------------------------------------------------------------------------


def test_an_exit_passes_whatever_the_limits_say():
    policy = policy_from_dict({"max_total_exposure": 1.0})
    book = _book(_dep("dep-a", _position("RELIANCE", 10.0, 100.0)))
    decision = evaluate_order(
        _draft(side="SELL", quantity=10.0), book=book, policy=policy, price=100.0,
    )
    assert decision.allowed is True
    assert any(c.check == "exit_exempt" for c in decision.checks)


def test_a_flip_past_flat_is_an_opener_not_an_exit():
    # Selling 25 against a 10-long arm flips to short 15: the exit exemption
    # does not apply, so the order meets the conflict check — and loses to the
    # arm's own long book under the default reject mode.
    policy = policy_from_dict({"max_total_exposure": 10**12})
    book = _book(_dep("dep-a", _position("RELIANCE", 10.0, 100.0)))
    decision = evaluate_order(
        _draft(side="SELL", quantity=25.0), book=book, policy=policy, price=100.0,
    )
    assert decision.allowed is False
    assert decision.code == "portfolio_conflict"


def test_total_exposure_blocks_before_the_oms():
    policy = policy_from_dict({"max_total_exposure": 15_000.0})
    book = _book(_dep("dep-a", _position("RELIANCE", 100.0, 100.0)))
    decision = evaluate_order(
        _draft(quantity=100.0), book=book, policy=policy, price=100.0,
    )
    assert decision.allowed is False
    assert decision.code == "portfolio_total_exposure"
    assert "20,000" in (decision.reason or "")


def test_unpriceable_orders_skip_notional_checks_said_so():
    policy = policy_from_dict({"max_total_exposure": 15_000.0})
    book = _book()
    decision = evaluate_order(
        _draft(), book=book, policy=policy, price=None,
    )
    assert decision.allowed is True
    skipped = [c for c in decision.checks if c.check == "total_exposure"]
    assert skipped and "skipped" in skipped[0].detail


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def _opposed_book() -> PortfolioBook:
    return _book(
        _dep("dep-a", _position("RELIANCE", 100.0, 100.0), strategy="strat-a"),
        _dep("dep-b", _position("RELIANCE", -40.0, 100.0), strategy="strat-b"),
    )


def test_same_direction_aggregates_without_conflict():
    policy = policy_from_dict({})
    decision = evaluate_order(
        _draft(deployment_id="dep-c", strategy_id="strat-c"),
        book=_opposed_book(), policy=policy, price=100.0,
    )
    assert decision.allowed is True
    assert any(c.check == "conflict" and c.allowed for c in decision.checks)


def test_opposite_direction_rejects_by_default():
    decision = evaluate_order(
        _draft(deployment_id="dep-c", strategy_id="strat-c", side="SELL", quantity=10.0),
        book=_opposed_book(), policy=default_policy(), price=100.0,
    )
    assert decision.allowed is False
    assert decision.code == "portfolio_conflict"


def test_priority_needs_a_strict_win_ties_fail_closed():
    policy = policy_from_dict({
        "conflict_mode": "priority",
        "strategy_priorities": {"strat-a": 5, "strat-c": 5},
    })
    tied = evaluate_order(
        _draft(deployment_id="dep-c", strategy_id="strat-c", side="SELL", quantity=1.0),
        book=_book(_dep("dep-a", _position("RELIANCE", 100.0, 100.0), strategy="strat-a")),
        policy=policy, price=100.0,
    )
    assert tied.allowed is False
    assert tied.code == "portfolio_conflict_priority"

    winning = evaluate_order(
        _draft(deployment_id="dep-c", strategy_id="strat-c", side="SELL", quantity=1.0),
        book=_book(_dep("dep-a", _position("RELIANCE", 100.0, 100.0), strategy="strat-a")),
        policy=policy_from_dict({
            "conflict_mode": "priority",
            "strategy_priorities": {"strat-a": 5, "strat-c": 9},
        }),
        price=100.0,
    )
    assert winning.allowed is True


def test_net_mode_allows_but_still_counts_gross_size():
    policy = policy_from_dict({
        "conflict_mode": "net", "max_total_exposure": 1.0,
    })
    decision = evaluate_order(
        _draft(deployment_id="dep-c", strategy_id="strat-c", side="SELL", quantity=1.0),
        book=_book(_dep("dep-a", _position("RELIANCE", 100.0, 100.0), strategy="strat-a")),
        policy=policy, price=100.0,
    )
    # Allowed past the conflict — then stopped by the gross size limit, which
    # netting does not switch off.
    assert decision.allowed is False
    assert decision.code == "portfolio_total_exposure"


# ---------------------------------------------------------------------------
# concentration, counts, loss
# ---------------------------------------------------------------------------


def test_stock_sector_and_correlated_limits():
    policy = policy_from_dict({
        "max_stock_exposure": 15_000.0,
        "max_sector_exposure_pct": 0.05,
        "max_correlated_exposure_pct": 0.05,
    })
    book = _book(_dep("dep-a", _position("RELIANCE", 100.0, 100.0)), capital=500_000.0)
    assert evaluate_order(
        _draft(quantity=100.0), book=book, policy=policy, price=100.0,
        sector_of=_sectors,
    ).code == "portfolio_stock_exposure"

    stock_ok = policy_from_dict({
        "max_sector_exposure_pct": 0.05, "max_correlated_exposure_pct": 0.50,
    })
    # 10,000 held + 20,000 incoming = 30,000 of Energy against a 25,000 cap.
    assert evaluate_order(
        _draft(quantity=200.0), book=book, policy=stock_ok, price=100.0,
        sector_of=_sectors,
    ).code == "portfolio_sector_exposure"

    correlated = policy_from_dict({
        "max_correlated_exposure_pct": 0.05,
        "correlation_groups": [["RELIANCE", "ONGC"]],
    })
    # 10,000 held in the shared cluster + 20,000 incoming = 30,000 against
    # a 25,000 cap.
    decision = evaluate_order(
        _draft(symbol="ONGC", quantity=200.0), book=book, policy=correlated,
        price=100.0, sector_of=_sectors,
    )
    assert decision.code == "portfolio_correlated_exposure"
    assert "group:" in (decision.reason or "")


def test_open_positions_counts_distinct_symbols_after_the_order():
    policy = policy_from_dict({"max_open_positions": 1})
    book = _book(_dep("dep-a", _position("RELIANCE", 10.0, 100.0)))
    decision = evaluate_order(
        _draft(symbol="TCS", quantity=5.0), book=book, policy=policy, price=100.0,
    )
    assert decision.allowed is False
    assert decision.code == "portfolio_open_positions"


def test_a_breached_daily_loss_refuses_new_risk_not_exits():
    policy = policy_from_dict({"max_daily_loss": 1_000.0})
    book = _book(_dep("dep-a", _position("RELIANCE", 10.0, 100.0)))
    opener = evaluate_order(
        _draft(symbol="TCS", quantity=1.0), book=book, policy=policy,
        price=100.0, daily_pnl=-1_500.0,
    )
    assert opener.allowed is False
    assert opener.code == "portfolio_daily_loss"


def test_bad_quantities_are_refused_not_measured():
    policy = default_policy()
    assert evaluate_order(
        _draft(quantity=0), book=_book(), policy=policy, price=100.0
    ).code == "portfolio_bad_order"
