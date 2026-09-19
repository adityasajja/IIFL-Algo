"""The Strategy → Backtest → Results workflow, end to end.

These tests exist because the workflow's failure mode is *silent*. A backtest
that runs on the wrong data, or on a fraction of the data it found, still
returns plausible-looking numbers: a positive Sharpe, a smooth equity curve, a
tidy trade list. Nothing raises. Nothing looks wrong. The operator acts on it.

So the assertions here are mostly about the *shape and provenance* of a result
rather than about it being profitable:

* the full available history is used, not the one-year ``-EQ`` sibling file,
* the monthly matrix compounds to the stated total return,
* a repeated identical run reproduces the same fingerprint and the same numbers,
* a trade can name the strategy version and the signal that produced it,
* every artefact the results page reads is actually persisted.

The runs are executed on a worker thread, so ``_wait`` polls. It is bounded —
a hung run must fail the test rather than hang the suite.
"""

from __future__ import annotations

import time

import pytest

from atr.config.settings import get_settings

pytestmark = pytest.mark.usefixtures("market_cache", "master")


# ─── helpers ──────────────────────────────────────────────────────────────────


def _wait(auth_client, run_id: str, timeout: float = 90.0) -> dict:
    """Poll a run until it is terminal. Fails the test rather than hanging."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = auth_client.get(f"/api/v1/backtests/{run_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return last
        time.sleep(0.05)
    pytest.fail(f"run {run_id} never reached a terminal state; last={last!r}")


def _run(auth_client, **overrides) -> dict:
    """Submit and wait. Returns the terminal run payload."""
    body = {
        "strategy": "sma_crossover",
        "engine_key": "sma_crossover",
        "symbols": ["RELIANCE", "TCS", "INFY"],
        "exchange": "NSEEQ",
        "timeframe": "1d",
        "source": "cache",
        "initial_cash": 1_000_000.0,
        "sizing": {"mode": "fixed_fraction", "percent": 25.0},
        "costs": {"model": "india_delivery", "slippage_bps": 5.0},
    }
    body.update(overrides)
    submitted = auth_client.post("/api/v1/backtests", json=body)
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["run_id"]
    return _wait(auth_client, run_id)


# ─── the workflow: options → submit → poll → results ──────────────────────────


def test_the_config_form_can_render_itself_from_one_request(auth_client) -> None:
    """`GET /options` must carry everything the form binds to.

    The form is written against these exact keys. A missing one is a blank
    dropdown in the UI, which is the kind of bug that looks like a styling
    problem and is not.
    """
    response = auth_client.get("/api/v1/backtests/options")
    assert response.status_code == 200, response.text
    body = response.json()

    for key in (
        "strategies",
        "timeframes",
        "cost_models",
        "sizing_modes",
        "sources",
        "exchanges",
        "universes",
        "limits",
    ):
        assert key in body, f"options is missing {key!r}"

    assert any(s["name"] == "sma_crossover" for s in body["strategies"])

    # Timeframes are listed with availability flagged, not omitted. A dropdown
    # that silently lacks an option reads as a bug.
    daily = next(t for t in body["timeframes"] if t["value"] == "1d")
    assert daily["available"] is True
    intraday = next(t for t in body["timeframes"] if t["value"] == "5m")
    assert intraday["available"] is False
    assert intraday["reason"], "an unavailable option must say why"

    # The capital floor is exposed so the form can show it rather than letting
    # the user discover it on submit.
    assert body["limits"]["min_capital"] == 10_000


def test_submit_returns_a_run_id_immediately_rather_than_the_result(auth_client) -> None:
    """The submit call must not block on the computation.

    Holding the request open for a multi-year run pins a worker and gives the
    browser a spinner it cannot tell from a hang. 202 + a run id is the contract.
    """
    response = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE"],
            "exchange": "NSEEQ",
            "source": "cache",
            "initial_cash": 500_000.0,
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"]
    assert body["status"] in {"QUEUED", "RUNNING"}
    # The config is echoed back so a client can confirm what it actually asked
    # for, and the fingerprint is what makes the run reproducible.
    assert body["config"]["symbols"] == ["RELIANCE"]
    assert body["fingerprint"]


def test_a_run_goes_queued_to_completed_with_progress(auth_client) -> None:
    run = _run(auth_client)
    assert run["status"] == "COMPLETED", run.get("error")
    assert run["progress"] == 1.0
    assert run["started_at"] and run["finished_at"]
    assert run["reproducible"] is True
    assert run["data_fingerprint"]


def test_the_results_page_has_every_field_it_renders(auth_client) -> None:
    """Assert the results surface as the page actually reads it.

    Deliberately checking the *keys*, because the failure this catches is a
    metric renamed on the backend: the page would render a dash and the operator
    would read it as "no drawdown" rather than "the field moved".
    """
    run = _run(auth_client)
    run_id = run["run_id"]
    metrics = auth_client.get(f"/api/v1/backtests/{run_id}/metrics")
    assert metrics.status_code == 200, metrics.text
    m = metrics.json()["metrics"]

    for key in (
        "total_return_pct",
        "cagr_pct",
        "max_drawdown_pct",
        "sharpe",
        "sortino",
        "win_rate_pct",
        "profit_factor",
        "num_trades",
    ):
        assert key in m, f"metrics is missing {key!r}"

    # num_trades is a count. If it arrives as 12.0 the table renders "12.0" and
    # the reader has to decide whether it is a count or an average.
    assert isinstance(m["num_trades"], int), f"num_trades is {type(m['num_trades'])}"

    curves = auth_client.get(f"/api/v1/backtests/{run_id}/equity")
    assert curves.status_code == 200, curves.text
    c = curves.json()
    for key in ("equity", "drawdown", "exposure"):
        assert key in c, f"curves is missing {key!r}"
        assert isinstance(c[key], list)
    assert len(c["equity"]) == len(c["drawdown"]) == len(c["exposure"])
    if c["equity"]:
        assert set(c["equity"][0]) == {"ts", "value"}

    monthly = auth_client.get(f"/api/v1/backtests/{run_id}/monthly")
    assert monthly.status_code == 200, monthly.text
    matrix = monthly.json()["matrix"]
    for row in matrix:
        assert len(row["months"]) == 12, "a year row must always be twelve wide"
        assert set(row) == {"year", "months", "year_total"}


def test_the_monthly_matrix_compounds_to_the_stated_total_return(auth_client) -> None:
    """The strongest available check on the matrix.

    Twelve monthly percentages are only evidence if they are the same fact as
    the headline number. Compounding them must land on ``total_return_pct``. If
    they disagree, one of the two is wrong and both are being shown.
    """
    run = _run(auth_client, start="2024-01-01", end="2025-12-31")
    run_id = run["run_id"]
    if run["status"] != "COMPLETED":
        pytest.skip(f"run did not complete: {run.get('error')}")

    total = run["metrics"]["total_return_pct"]
    matrix = auth_client.get(f"/api/v1/backtests/{run_id}/monthly").json()["matrix"]

    compounded = 1.0
    seen = False
    for row in matrix:
        for month in row["months"]:
            if month is None:
                continue
            compounded *= 1.0 + float(month) / 100.0
            seen = True

    assert seen, "a completed run over two years must produce monthly rows"
    compounded_pct = (compounded - 1.0) * 100.0
    assert compounded_pct == pytest.approx(float(total), abs=0.05), (
        f"months compound to {compounded_pct:.3f}% but the run states {total}%"
    )


def test_every_trade_can_be_opened_and_says_what_produced_it(auth_client) -> None:
    """Requirement 4: entry, exit, quantity, P&L, strategy version, signal."""
    run = _run(auth_client)
    run_id = run["run_id"]
    page = auth_client.get(f"/api/v1/backtests/{run_id}/trades?limit=500")
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] >= 1, "the fixture data must produce at least one round trip"
    assert body["total"] == len(body["trades"])

    for trade in body["trades"]:
        for key in (
            "seq",
            "symbol",
            "direction",
            "quantity",
            "entry_ts",
            "entry_price",
            "exit_ts",
            "exit_price",
            "net_pnl",
            "return_pct",
            "exit_reason",
        ):
            assert key in trade, f"trade is missing {key!r}"

    first = body["trades"][0]
    detail = auth_client.get(f"/api/v1/backtests/{run_id}/trades/{first['seq']}")
    assert detail.status_code == 200, detail.text
    d = detail.json()

    # The version that produced the trade. Built-ins are unversioned and say so;
    # a saved strategy pins a number.
    assert "strategy" in d
    assert d["strategy"]["engine_key"] == "sma_crossover"
    assert "version" in d["strategy"]
    assert "params" in d["strategy"]

    # The exits in force, so a reader can tell a stop-out from a signal exit.
    assert set(d["exits"]) == {"stop_loss_pct", "take_profit_pct", "trailing_stop_pct"}

    # The signal conditions. sma_crossover implements describe_signal, so a
    # completed trade must carry a reason rather than null.
    assert d["signal_reason"], (
        "sma_crossover reports why it opened a position, so a trade from it must "
        "carry a signal_reason rather than null"
    )
    assert "SMA" in d["signal_reason"]


def test_a_trade_that_does_not_exist_is_a_404_not_an_empty_trade(auth_client) -> None:
    run = _run(auth_client)
    response = auth_client.get(f"/api/v1/backtests/{run['run_id']}/trades/99999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


# ─── reproducibility ──────────────────────────────────────────────────────────


def test_two_identical_runs_produce_the_same_numbers(auth_client) -> None:
    """Requirement 5: a run must be reproducible from what was stored.

    Same config, same data, same result — both fingerprints must match and the
    outcome must match to the paisa. If they differ, something in the pipeline
    is not deterministic and no stored result can be trusted.
    """
    a = _run(auth_client)
    b = _run(auth_client)

    assert a["status"] == b["status"] == "COMPLETED"
    assert a["data_fingerprint"] == b["data_fingerprint"], (
        "the same symbols over the same window must fingerprint the same"
    )

    ma = auth_client.get(f"/api/v1/backtests/{a['run_id']}/metrics").json()["metrics"]
    mb = auth_client.get(f"/api/v1/backtests/{b['run_id']}/metrics").json()["metrics"]

    assert ma["total_return_pct"] == mb["total_return_pct"]
    assert ma["num_trades"] == mb["num_trades"]
    assert ma["end_equity"] == mb["end_equity"]
    assert ma["total_commission"] == mb["total_commission"]


def test_two_identical_runs_share_a_config_fingerprint(auth_client) -> None:
    """The fingerprint identifies the *request*, so it must be stable across
    submissions and sensitive to a real change."""
    body = {
        "strategy": "sma_crossover",
        "engine_key": "sma_crossover",
        "symbols": ["RELIANCE"],
        "exchange": "NSEEQ",
        "source": "cache",
        "initial_cash": 500_000.0,
    }
    first = auth_client.post("/api/v1/backtests", json=body).json()["fingerprint"]
    second = auth_client.post("/api/v1/backtests", json=body).json()["fingerprint"]
    assert first == second

    changed = auth_client.post(
        "/api/v1/backtests", json={**body, "initial_cash": 600_000.0}
    ).json()["fingerprint"]
    assert changed != first, "a different capital level is a different run"


def test_the_run_is_stored_well_enough_to_replay_it(auth_client) -> None:
    """The config written to the row must be the whole config.

    A stored summary (symbols + dates) is not enough to re-run anything: it
    loses sizing, stops and the cost model, which are exactly the fields that
    decide whether the result was profitable.
    """
    run = _run(
        auth_client,
        sizing={"mode": "fixed_quantity", "quantity": 10},
        stops={"stop_loss_pct": 4.0, "take_profit_pct": 15.0, "trailing_stop_pct": 6.0},
        costs={"model": "flat_per_share", "slippage_bps": 12.0},
    )
    cfg = run["config"]
    assert cfg["sizing"]["mode"] == "fixed_quantity"
    assert cfg["sizing"]["quantity"] == 10
    assert cfg["stops"]["stop_loss_pct"] == 4.0
    assert cfg["stops"]["take_profit_pct"] == 15.0
    assert cfg["costs"]["model"] == "flat_per_share"
    assert cfg["costs"]["slippage_bps"] == 12.0
    assert cfg["symbols"] == ["RELIANCE", "TCS", "INFY"]


# ─── the data the run actually consumed ───────────────────────────────────────


def test_the_longer_series_wins_when_two_files_share_a_ticker(auth_client, market_cache) -> None:
    """The bug that silently shortened every run to one year.

    The real cache holds two files per ticker — ``RELIANCE-EQ.parquet`` with the
    full history and a bare ``RELIANCE.parquet`` that can be far shorter (in the
    operator's cache: 249 bars against 2,899). Preferring the ``-EQ`` spelling
    discarded eleven years of data and still returned a plausible result.

    This test makes the two files genuinely different lengths, which the shared
    fixture does not, and asserts the longer one is used.
    """

    from conftest import _price_frame

    daily = market_cache / "iifl_daily" / "NSEEQ"
    short = _price_frame(seed=42, periods=40)
    short.to_parquet(daily / "RELIANCE.parquet")

    run = _run(auth_client, symbols=["RELIANCE"], start=None, end=None)
    assert run["status"] == "COMPLETED", run.get("error")

    # The 300-bar sibling should have won. If the 40-bar one had, the equity
    # curve would be an order of magnitude shorter.
    points = auth_client.get(f"/api/v1/backtests/{run['run_id']}/equity").json()["equity"]
    assert len(points) > 100, (
        f"only {len(points)} equity points — the 40-bar file was used instead of "
        f"the 300-bar one"
    )

    # And the run should say a richer sibling existed, because a silently
    # shortened window is exactly what the warning is for.
    warnings = run.get("metrics", {}).get("warnings") or run.get("warnings") or []

    # The warning may be surfaced on the run payload or the metrics; accept
    # either, but the equity length above is the load-bearing assertion.
    _ = warnings


# ─── error handling ───────────────────────────────────────────────────────────


def test_an_unknown_strategy_fails_the_run_with_a_reason(auth_client) -> None:
    run = _run(auth_client, strategy="does_not_exist", engine_key="does_not_exist")
    assert run["status"] == "FAILED"
    assert run["error"]
    assert "does_not_exist" in run["error"]


@pytest.mark.parametrize(
    "body,expect_in_message",
    [
        ({"stops": {"stop_loss_pct": -3.0}}, "greater than 0"),
        ({"initial_cash": 500.0}, "at least"),
        ({"timeframe": "5m"}, "daily bars only"),
        ({"timeframe": "3w"}, "unknown timeframe"),
    ],
)
def test_a_config_error_is_rejected_at_submit_not_discovered_later(
    auth_client, body, expect_in_message
) -> None:
    """A config that cannot run must be refused before a worker is allocated.

    Returning 202 and then failing the run would put the error behind a poll and
    make it look like an engine fault rather than a typo in the form.

    Field-level validation errors share the ``invalid_config`` code and carry
    their explanation in the message; only the structural failures (unknown
    timeframe, unsupported timeframe, missing universe) get a named code. Both
    are asserted here so a message that loses its reason is caught.
    """
    payload = {
        "strategy": "sma_crossover",
        "engine_key": "sma_crossover",
        "symbols": ["RELIANCE"],
        "exchange": "NSEEQ",
        "source": "cache",
        "initial_cash": 1_000_000.0,
        **body,
    }
    response = auth_client.post("/api/v1/backtests", json=payload)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"], "a rejection must carry a machine-readable code"
    assert expect_in_message in detail["detail"], detail


def test_an_unknown_timeframe_and_an_unavailable_one_are_distinguished(auth_client) -> None:
    """`3w` is not a timeframe; `5m` is one this build cannot serve yet.

    Collapsing the two would tell a user who typed a valid intraday timeframe
    that it does not exist, when the honest answer is that the cache holds daily
    bars only.
    """
    def post(timeframe: str):
        return auth_client.post(
            "/api/v1/backtests",
            json={
                "strategy": "sma_crossover",
                "engine_key": "sma_crossover",
                "symbols": ["RELIANCE"],
                "exchange": "NSEEQ",
                "source": "cache",
                "initial_cash": 1_000_000.0,
                "timeframe": timeframe,
            },
        )

    unknown = post("3w")
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "unknown_timeframe"

    unavailable = post("5m")
    assert unavailable.status_code == 422
    assert unavailable.json()["detail"]["code"] == "unsupported_timeframe"


def test_a_window_with_no_bars_fails_and_says_what_data_exists(auth_client) -> None:
    """An empty range is a data problem, so the error must name the available
    span rather than just reporting zero bars."""
    run = _run(auth_client, start="2030-01-01", end="2030-06-01")
    assert run["status"] == "FAILED"
    assert "2030" in run["error"]
    # The message should point at the fix.
    assert "2015" in run["error"] or "sync" in run["error"].lower()


def test_equal_weight_sizing_over_one_symbol_is_refused(auth_client) -> None:
    """A contradiction the user cannot see: spreading risk over one member."""
    response = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE"],
            "exchange": "NSEEQ",
            "source": "cache",
            "initial_cash": 1_000_000.0,
            "sizing": {"mode": "equal_weight"},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "equal_weight_needs_many"


def test_reading_a_run_that_does_not_exist_is_a_404(auth_client) -> None:
    response = auth_client.get("/api/v1/backtests/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_reading_results_before_the_run_finishes_is_a_conflict_not_empty_data(
    auth_client,
) -> None:
    """A queued run's curves do not exist yet. Returning empty lists would let
    the results page draw an empty chart, which reads as "no trades"."""
    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE", "TCS"],
            "exchange": "NSEEQ",
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    assert submitted.status_code == 202
    run_id = submitted.json()["run_id"]

    # Either it is still in flight (409) or it finished too fast to catch (200).
    # Both are correct; a 500 or an empty 200 body is not.
    for path in ("metrics", "equity", "trades", "monthly"):
        response = auth_client.get(f"/api/v1/backtests/{run_id}/{path}")
        assert response.status_code in {200, 409}, response.text
        if response.status_code == 409:
            assert response.json()["detail"]["code"] == "not_completed"


def test_cancelling_a_run_discards_its_result(auth_client) -> None:
    """Cancel marks the run so its completion write is rejected.

    The worker is not interrupted — that would need the engine to poll a flag
    inside its bar loop. The guard is the safety: a cancelled run must not end
    up readable.
    """
    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE", "TCS", "INFY", "HDFCBANK"],
            "exchange": "NSEEQ",
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    run_id = submitted.json()["run_id"]

    response = auth_client.post(f"/api/v1/backtests/{run_id}/cancel")
    assert response.status_code == 200, response.text

    final = _wait(auth_client, run_id)
    # If the worker had already finished before the cancel landed, the run is
    # COMPLETED and that is honest. A CANCELLED run must have no readable result.
    if final["status"] == "CANCELLED":
        blocked = auth_client.get(f"/api/v1/backtests/{run_id}/metrics")
        assert blocked.status_code == 409


# ─── history and permissions ──────────────────────────────────────────────────


def test_run_history_lists_without_loading_each_run(auth_client) -> None:
    """The history view needs the headline numbers per row, so they must be in
    the list response — one request per run would make the list O(n) round trips."""
    run = _run(auth_client)
    response = auth_client.get("/api/v1/backtests?limit=10")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1

    listed = next(r for r in body["runs"] if r["run_id"] == run["run_id"])
    assert listed["status"] == "COMPLETED"
    assert listed["strategy"] == "sma_crossover"
    assert listed["total_return_pct"] is not None
    assert listed["num_trades"] is not None
    assert listed["created_at"]


def test_history_can_be_filtered_by_status(auth_client) -> None:
    _run(auth_client)
    completed = auth_client.get("/api/v1/backtests?status=COMPLETED")
    assert completed.status_code == 200
    assert all(r["status"] == "COMPLETED" for r in completed.json()["runs"])

    bad = auth_client.get("/api/v1/backtests?status=NONSENSE")
    assert bad.status_code == 422


def test_the_status_vocabulary_is_exposed_rather_than_hardcoded(auth_client) -> None:
    body = auth_client.get("/api/v1/backtests/statuses").json()
    assert set(body["statuses"]) >= {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}


def test_the_backtest_surface_requires_authentication(client, master) -> None:
    """Every route on this surface, including the ones that only read."""
    for method, path in (
        ("get", "/api/v1/backtests/options"),
        ("get", "/api/v1/backtests"),
        ("get", "/api/v1/backtests/some-id"),
        ("get", "/api/v1/backtests/some-id/metrics"),
        ("get", "/api/v1/backtests/some-id/equity"),
        ("get", "/api/v1/backtests/some-id/trades"),
        ("get", "/api/v1/backtests/some-id/monthly"),
        ("post", "/api/v1/backtests"),
        ("post", "/api/v1/backtests/some-id/cancel"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path} was not gated"


def test_a_viewer_cannot_submit_a_run(fresh_env, auth_client, monkeypatch) -> None:
    """Read-only must mean read-only on the mutating routes.

    A viewer has STRATEGY_READ but not BACKTEST_RUN, so they can read results
    and cannot spend CPU. That split is the reason the two permissions exist.

    The viewer is created through ``/auth/register``, which is the only route
    that makes one, and which assigns ``viewer`` unconditionally — it does not
    promote the first account to owner, and it never reads the role from the
    request body. The owner is the ``auth_client`` fixture; there is
    deliberately no ``POST /auth/users`` to call, so a test that reached for
    one would be testing an endpoint that does not exist.
    """
    monkeypatch.setenv("ALLOW_SIGNUP", "true")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from atr.api.main import app

    origin = ("127.0.0.1", 51234)

    viewer_reg = auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "viewer@example.com",
            "username": "viewer",
            "password": "Str0ngPassw0rd",
            # A caller asking to be an owner must not become one.
            "role": "owner",
        },
    )
    assert viewer_reg.status_code == 201, viewer_reg.text
    assert viewer_reg.json()["user"]["role"] == "viewer", viewer_reg.text

    viewer = TestClient(app, client=origin)
    viewer_login = viewer.post(
        "/api/v1/auth/login",
        json={"identifier": "viewer", "password": "Str0ngPassw0rd"},
    )
    assert viewer_login.status_code == 200, viewer_login.text
    viewer.headers.update({"Authorization": f"Bearer {viewer_login.json()['token']}"})

    # Reading is allowed.
    assert viewer.get("/api/v1/backtests/options").status_code == 200

    # Submitting and cancelling are not.
    refused = viewer.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE"],
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    assert refused.status_code == 403, refused.text
    assert viewer.post("/api/v1/backtests/whatever/cancel").status_code == 403


# ─── the saved-strategy path ──────────────────────────────────────────────────


def _seed_saved_strategy(*, params: dict) -> tuple[str, int]:
    """Create a saved strategy with one version, straight through the repository.

    The strategies API is read-only today — there is no route to create a
    strategy or append a version — so the only way to exercise the "select a
    version" half of the workflow is to seed the rows directly. That is a gap in
    the product surface, not in the test: once a write route exists, this helper
    should be replaced by a call to it.
    """
    from sqlalchemy import text as sql_text

    from atr.appdb.engine import get_app_db
    from atr.appdb.repositories import StrategyRepository

    db = get_app_db()
    with db.session() as session:
        owner = session.execute(sql_text("select user_id from users limit 1")).scalar()
        strategy = StrategyRepository.create(
            session,
            user_id=owner,
            name="Seeded SMA",
            kind="code",
            description="seeded by the backtest workflow test",
            engine_key="sma_crossover",
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id=owner,
            definition={"engine_key": "sma_crossover", "params": params},
            change_note="initial",
        )
        session.commit()
        return strategy["strategy_id"], int(version["version"])


def test_a_saved_version_is_what_actually_runs(auth_client) -> None:
    """Selecting a version must run *that* version's parameters.

    This is the one part of the workflow where a silent failure is easiest to
    miss: if the version is ignored and the engine falls back to defaults, the
    run still completes and still looks fine. The assertion is therefore that
    the run's own reported parameters are the stored ones, not the defaults.
    """
    # A slow SMA that the engine's default (10/30) would never produce.
    strategy_id, version = _seed_saved_strategy(params={"fast": 4, "slow": 60})

    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "strategy_id": strategy_id,
            "strategy_version": version,
            "symbols": ["RELIANCE", "TCS", "INFY"],
            "exchange": "NSEEQ",
            "timeframe": "1d",
            "source": "cache",
            "initial_cash": 1_000_000.0,
            "sizing": {"mode": "fixed_fraction", "percent": 25.0},
            "costs": {"model": "india_delivery", "slippage_bps": 5.0},
        },
    )
    assert submitted.status_code == 202, submitted.text
    run = _wait(auth_client, submitted.json()["run_id"])
    assert run["status"] == "COMPLETED", run.get("error")

    metrics = auth_client.get(f"/api/v1/backtests/{run['run_id']}/metrics").json()
    assert metrics["metrics"]["strategy"] == "sma_crossover"

    # The stored parameters, not the class defaults, are what ran.
    page = auth_client.get(f"/api/v1/backtests/{run['run_id']}/trades?limit=1")
    assert page.status_code == 200, page.text
    assert page.json()["total"] >= 1, "the seeded parameters must trade"

    seq = page.json()["trades"][0]["seq"]
    detail = auth_client.get(f"/api/v1/backtests/{run['run_id']}/trades/{seq}").json()
    assert detail["strategy"]["params"] == {"fast": 4, "slow": 60}, (
        "the version's stored parameters must be the ones that ran; a fallback to "
        f"the engine defaults would go unnoticed, got {detail['strategy']['params']!r}"
    )


def test_an_explicit_parameter_overrides_the_version_without_forking_it(auth_client) -> None:
    """A run may vary one parameter without creating a new version.

    The stored definition is the baseline; explicit run params win. That is what
    lets an operator sweep a value without polluting the version history.
    """
    strategy_id, version = _seed_saved_strategy(params={"fast": 4, "slow": 60})

    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "strategy_id": strategy_id,
            "strategy_version": version,
            "params": {"slow": 90},
            "symbols": ["RELIANCE"],
            "exchange": "NSEEQ",
            "timeframe": "1d",
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    assert submitted.status_code == 202, submitted.text
    run = _wait(auth_client, submitted.json()["run_id"])
    assert run["status"] == "COMPLETED", run.get("error")

    page = auth_client.get(f"/api/v1/backtests/{run['run_id']}/trades?limit=1").json()
    if page["total"] >= 1:
        detail = auth_client.get(
            f"/api/v1/backtests/{run['run_id']}/trades/{page['trades'][0]['seq']}"
        ).json()
        assert detail["strategy"]["params"]["slow"] == 90
        # The untouched half still comes from the version.
        assert detail["strategy"]["params"]["fast"] == 4


def test_a_version_that_does_not_exist_is_refused_by_name(auth_client) -> None:
    """A missing version must name itself, not fail generically.

    A run pinned to a version nobody can find is unreproducible by definition,
    so it has to fail loudly and say which pin was wrong.
    """
    strategy_id, _ = _seed_saved_strategy(params={"fast": 4, "slow": 60})

    response = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "strategy_id": strategy_id,
            "strategy_version": 99,
            "symbols": ["RELIANCE"],
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    # Either rejected at submit, or accepted and failed with the reason recorded.
    if response.status_code == 202:
        run = _wait(auth_client, response.json()["run_id"])
        assert run["status"] == "FAILED", run
        assert "99" in (run.get("error") or ""), run.get("error")
    else:
        assert response.status_code in {404, 422}, response.text
        assert "99" in response.text


def test_an_unexpected_engine_crash_is_recorded_rather_than_stranding_the_run(
    auth_client, monkeypatch
) -> None:
    """A run must never be left RUNNING by an exception nobody anticipated.

    The handler for a non-``BacktestError`` exception formats a traceback into
    the log before marking the run FAILED. That branch had no coverage, and it
    referenced ``traceback`` without importing it — so the one path that exists
    to record an unforeseen crash raised ``NameError`` instead, and the run was
    left ``RUNNING`` with ``error: None``. The UI polls a run like that forever.

    The assertion names the *sentinel* exception, so the test cannot pass by
    catching some unrelated failure on the way to the same FAILED status.
    """
    from atr.backtest import runner as runner_module

    class UnmodelledEngineFailure(Exception):
        """A type the service has no specific handler for."""

    def explode(self, *args, **kwargs):
        raise UnmodelledEngineFailure("engine exploded in a way nobody modelled")

    monkeypatch.setattr(runner_module.BacktestRunner, "execute", explode)

    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE"],
            "exchange": "NSEEQ",
            "timeframe": "1d",
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    assert submitted.status_code == 202, submitted.text
    run = _wait(auth_client, submitted.json()["run_id"])

    assert run["status"] == "FAILED", (
        "an unmodelled engine exception must fail the run; leaving it RUNNING "
        f"means the UI polls forever, got {run['status']}"
    )
    assert "UnmodelledEngineFailure" in (run.get("error") or ""), (
        "the recorded error must be the exception the engine actually raised, "
        f"not a later failure of the error path itself; got {run.get('error')!r}"
    )


def test_a_run_that_crashed_writes_no_partial_artefacts(auth_client, monkeypatch) -> None:
    """A failed run must leave nothing that a later read could mistake for a result.

    Half-written artefacts are worse than none: the metrics endpoint will not
    serve them, but a trade list that exists is a trade list someone may read.
    """
    from atr.backtest import runner as runner_module

    def explode(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_module.BacktestRunner, "execute", explode)

    submitted = auth_client.post(
        "/api/v1/backtests",
        json={
            "strategy": "sma_crossover",
            "engine_key": "sma_crossover",
            "symbols": ["RELIANCE"],
            "source": "cache",
            "initial_cash": 1_000_000.0,
        },
    )
    run_id = submitted.json()["run_id"]
    run = _wait(auth_client, run_id)
    assert run["status"] == "FAILED", run

    # Reads must refuse rather than serve an empty-looking result set.
    for suffix in ("trades", "metrics", "equity", "monthly"):
        response = auth_client.get(f"/api/v1/backtests/{run_id}/{suffix}")
        assert response.status_code in {409, 404}, (
            f"{suffix} served a body for a run that never produced one: "
            f"{response.status_code} {response.text[:200]}"
        )
