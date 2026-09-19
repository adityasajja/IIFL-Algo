"""The learning API surface, over HTTP.

The service tests prove the payloads are right. These prove they are
*reachable*, that they survive JSON encoding, and — most importantly — that the
surface is **read-only**. That last one is the guarantee the user asked for
explicitly: the learning engine may never modify a live strategy, place an
order, or bypass the risk engine. A write route is precisely where that would be
lost, because a route is reachable without passing through the service's own
guards.

Two things are therefore asserted that a normal API test would not bother with:

1. **No mutating method exists on the router.** Not "no mutating route is
   called" — the method set itself is empty. A future contributor adding
   ``@router.post("/apply")`` finds out here.
2. **The empty database still answers 200.** The live book holds zero trades,
   so the honest response is a well-formed empty one. A 404 or a 500 would mean
   the dashboard shows an error where it should show "nothing to learn from
   yet".
"""

from __future__ import annotations

import json

from atr.api.routers import learning as learning_router

# ---------------------------------------------------------------------------
# the surface is read-only
# ---------------------------------------------------------------------------


def test_the_learning_router_registers_no_mutating_route():
    """The safety constraint, enforced at the surface a client can reach."""
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = [
        (route.path, method)
        for route in learning_router.router.routes
        for method in getattr(route, "methods", set())
        if method in mutating
    ]
    assert offenders == [], f"learning exposes a mutating route: {offenders}"


def test_the_learning_router_is_registered():
    from atr.api.routers import ROUTERS

    assert learning_router.router in ROUTERS


def test_the_learning_routes_are_under_the_versioned_prefix():
    for route in learning_router.router.routes:
        assert route.path.startswith("/api/v1/learning"), route.path


# ---------------------------------------------------------------------------
# the empty database — which is the live state
# ---------------------------------------------------------------------------


def test_status_answers_on_an_empty_database(auth_client):
    response = auth_client.get("/api/v1/learning/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["trades_available"] == 0
    assert body["sufficient_for_analysis"] is False


def test_status_separates_analysable_from_claimable(auth_client):
    """Two flags, because one number cannot answer both questions.

    "Is there enough to analyse?" and "may anything be claimed?" are different,
    and a client that renders the first next to a finding is showing a number
    that does not mean what the reader will take it to mean.
    """
    body = auth_client.get("/api/v1/learning/status").json()
    assert body["forward_available"] == 0
    assert body["sufficient_for_analysis"] is False
    assert body["sufficient_for_a_claim"] is False


def test_status_names_the_features_that_do_not_exist(auth_client):
    """A client needs to know VWAP is unavailable so it can say why rather than
    render an empty cell that looks like a bug."""
    body = auth_client.get("/api/v1/learning/status").json()
    assert "vwap_relationship" in body["missing_features"]
    assert "india_vix" in body["missing_features"]


def test_every_read_route_answers_200_with_no_trades(auth_client):
    for path in (
        "/api/v1/learning/dataset",
        "/api/v1/learning/performance",
        "/api/v1/learning/report",
        "/api/v1/learning/drift",
        "/api/v1/learning/overview",
        "/api/v1/learning/axes",
        "/api/v1/learning/readiness",
    ):
        response = auth_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"


def test_the_overview_says_the_dataset_is_empty_and_why(auth_client):
    body = auth_client.get("/api/v1/learning/overview").json()
    assert body["empty"] is True
    assert body["limited"] is True
    assert body["limitations"], "an empty book must carry a stated reason"
    assert "correctly absent rather than zero" in " ".join(body["limitations"])


def test_every_payload_survives_json_encoding(auth_client):
    """The payloads hold floats that came from a division; a NaN or an Infinity
    would raise on encode or ship invalid JSON a browser cannot parse."""
    for path in (
        "/api/v1/learning/overview",
        "/api/v1/learning/report",
        "/api/v1/learning/performance",
        "/api/v1/learning/drift",
        "/api/v1/learning/readiness",
    ):
        body = auth_client.get(path).json()
        text = json.dumps(body)
        assert "NaN" not in text
        assert "Infinity" not in text


# ---------------------------------------------------------------------------
# the advisory contract
# ---------------------------------------------------------------------------


def test_the_overview_marks_itself_advisory(auth_client):
    body = auth_client.get("/api/v1/learning/overview").json()
    assert body["advisory"] is True
    assert body["applies_changes"] is False


def test_the_report_marks_itself_advisory(auth_client):
    body = auth_client.get("/api/v1/learning/report").json()
    assert body["advisory"] is True
    assert body["applies_changes"] is False


def test_the_drift_payload_marks_itself_advisory(auth_client):
    body = auth_client.get("/api/v1/learning/drift").json()
    assert body["advisory"] is True
    assert body["applies_changes"] is False


def test_the_readiness_payload_marks_itself_advisory(auth_client):
    """Research states are labels: the payload says nothing is applied."""
    body = auth_client.get("/api/v1/learning/readiness").json()
    assert body["advisory_only"] is True
    assert body["applies_changes"] is False
    assert body["gates"] == [10, 30, 50]


def test_the_headline_never_claims_stability_from_an_empty_comparison(auth_client):
    """The single most dangerous string this API could return."""
    body = auth_client.get("/api/v1/learning/drift").json()
    headline = body["headline"].lower()
    assert "no drift" not in headline
    assert "stable" not in headline


# ---------------------------------------------------------------------------
# the axes surface
# ---------------------------------------------------------------------------


def test_axes_lists_what_can_be_sliced_and_what_cannot(auth_client):
    body = auth_client.get("/api/v1/learning/axes").json()
    assert "market_regime" in body["available"]
    assert "rvol_bucket" in body["available"]
    assert body["default"], "the default axis set must not be empty"
    assert "sector_strength" in body["unavailable"]


def test_an_unknown_axis_is_refused_rather_than_silently_ignored(auth_client):
    """Analysing a default axis set when the caller asked for something else
    answers a different question than the one that was asked."""
    response = auth_client.get("/api/v1/learning/performance", params={"axes": "not_an_axis"})
    assert response.status_code in (400, 422)


def test_an_empty_roles_parameter_means_all_not_none(auth_client):
    """``?roles=`` must not filter every row out — an empty page would be a lie
    about a database that is merely empty for other reasons."""
    body = auth_client.get(
        "/api/v1/learning/performance", params={"roles": ""}
    ).json()
    assert body["n"] == 0
    assert "absent rather than zero" in " ".join(body["caveats"])


def test_the_dataset_row_page_reports_missing_features_per_row(auth_client):
    body = auth_client.get("/api/v1/learning/dataset/rows").json()
    assert body["total"] == 0
    assert body["returned"] == 0
    assert body["columns"], "the column list is part of the contract"


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------


def test_the_learning_routes_require_authentication(client):
    """``client`` has no token, so every read must be refused."""
    for path in (
        "/api/v1/learning/status",
        "/api/v1/learning/overview",
        "/api/v1/learning/report",
    ):
        response = client.get(path)
        assert response.status_code in (401, 403), f"{path} was reachable unauthenticated"


# ---------------------------------------------------------------------------
# the paper ledger reaches the surface
# ---------------------------------------------------------------------------


def _write_paper_ledger() -> None:
    """Write a one-week forward ledger into the test's own data root.

    ``fresh_env`` points ``ATR_DATA_ROOT`` at ``tmp_path/data``, so this lands
    in the test's directory and never in the operator's real record.
    """
    import os
    from pathlib import Path

    root = Path(os.environ["ATR_DATA_ROOT"]) / "paper_momentum"
    root.mkdir(parents=True, exist_ok=True)
    (root / "picks.jsonl").write_text(
        json.dumps(
            {
                "week": "2026-06-05",
                "filter": "top_decile_ret_26w",
                "n_picks": 2,
                "picks": [{"symbol": "RELIANCE", "entry": 500.0}],
                "backtest_expectation": {
                    "mean_weekly_pct": 0.88,
                    "source": "weekly_stock_picks.json filters.top_decile_ret_26w",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "settlements.jsonl").write_text(
        json.dumps(
            {
                "week": "2026-06-05",
                "exit_window_end": "2026-06-12",
                "returns": {"RELIANCE": 0.03},
                "n_settled": 1,
                "missing": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_status_counts_the_paper_ledger(auth_client):
    """The first screen an operator checks must not report zero while the book
    holds paper trades."""
    _write_paper_ledger()
    body = auth_client.get("/api/v1/learning/status").json()
    assert body["counts"]["paper_ledger_trades"] == 1
    assert body["counts"]["paper_ledger_forward_trades"] == 1
    assert body["trades_available"] == 1
    assert body["paper_ledger"]["present"] is True


def test_the_overview_reports_grades_and_metric_coverage(auth_client):
    """The two fields that stop a client misreading the book: which rows are
    forward evidence, and which outcome column is actually populated."""
    _write_paper_ledger()
    body = auth_client.get("/api/v1/learning/overview").json()
    assert body["empty"] is False
    assert body["dataset"]["evidence_grades"] == {"forward": 1, "in_sample": 0}
    assert body["dataset"]["metric_coverage"]["net_pnl"] == 0
    assert body["dataset"]["metric_coverage"]["return_pct"] == 1
    assert body["dataset"]["sources"]["PAPER"] == 1


def test_the_overview_names_the_metric_it_ran_on(auth_client):
    """A returns-only book must not be reported as an empty rupee table."""
    _write_paper_ledger()
    body = auth_client.get("/api/v1/learning/overview").json()
    assert body["analysis"]["metric"] == "return_pct"
    assert body["analysis"]["metric_note"]
    assert body["report"]["metric"] == "return_pct"
    assert body["report"]["aggregate"] == "mean"
    assert body["report"]["net_pnl_today"] is None


def test_a_paper_ledger_does_not_make_the_engine_mutate_anything(auth_client, app_db):
    """The read-only guarantee, re-asserted over HTTP with a non-empty book."""
    from sqlalchemy import func, select

    from atr.appdb.schema import orders, strategies

    def fingerprint() -> tuple[int, int]:
        with app_db.session() as session:
            return (
                int(session.execute(select(func.count()).select_from(strategies)).scalar() or 0),
                int(session.execute(select(func.count()).select_from(orders)).scalar() or 0),
            )

    _write_paper_ledger()
    before = fingerprint()
    for path in (
        "/api/v1/learning/overview",
        "/api/v1/learning/dataset",
        "/api/v1/learning/performance",
        "/api/v1/learning/report",
        "/api/v1/learning/drift",
    ):
        assert auth_client.get(path).status_code == 200, path
    assert fingerprint() == before
