"""The Signal Explorer API surface.

Read-only projections of what the engine recorded. The tests pin the contract
the frontend depends on (envelope shape, filters, 404/400/401 behaviour) rather
than re-asserting the scoring, which belongs to ``test_signal_context``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.appdb.repositories import UserRepository
from atr.signal_context.service import get_signal_context_service

BASE = "/api/v1/signal-context"


def _frame(seed: int, periods: int = 300, up: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(up, 0.012, periods).cumsum()
    close = 100.0 * np.exp(steps)
    high = close * (1 + rng.uniform(0.001, 0.02, periods))
    low = close * (1 - rng.uniform(0.001, 0.02, periods))
    open_ = close * (1 + rng.normal(0, 0.004, periods))
    volume = rng.integers(100_000, 900_000, periods).astype(float)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=periods, freq="B"),
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
        }
    )


def _seed_context(app_db, user_id: str, signal_id: str = "TCS:2024-06-03") -> None:
    service = get_signal_context_service()
    stock = _frame(2, up=0.002)
    bench = _frame(3)
    when = stock["ts"].iloc[250]
    market = service.engine.build_market_snapshot(
        bench, when, breadth_above_ema50_pct=62.0, regime="BULLISH_TREND"
    )
    sector = service.engine.build_sector_snapshot("IT", when, relative_strength_1m=2.0)
    context = service.engine.enrich(
        signal_id=signal_id,
        symbol="TCS",
        action="BUY",
        signal_source="PAPER",
        signal_ts=str(when),
        strategy_id="strat-1",
        strategy_version=1,
        stock_frame=stock,
        bench_frame=bench,
        market_hint=market,
        sector_hint=sector,
        when=when,
    )
    with app_db.session() as session:
        service.record(session, context, user_id=user_id)


def _owner_id(app_db) -> str:
    with app_db.session() as session:
        user = UserRepository.get_by_identifier(session, "owner@example.com")
    assert user is not None
    return user["user_id"]


def test_model_endpoint_publishes_the_scoring_model(auth_client):
    response = auth_client.get(f"{BASE}/model")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["version"]
    assert data["criteria"]


def test_signals_list_is_empty_before_any_signal(auth_client):
    response = auth_client.get(f"{BASE}/signals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["total"] == 0
    assert body["data"]["items"] == []


def test_signals_list_and_detail_expose_the_recorded_context(auth_client, app_db):
    user_id = _owner_id(app_db)
    _seed_context(app_db, user_id)

    listing = auth_client.get(f"{BASE}/signals").json()["data"]
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["symbol"] == "TCS"
    assert item["market_context"]["regime"] == "BULLISH_TREND"

    detail = auth_client.get(f"{BASE}/signals/TCS:2024-06-03")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["score_breakdown"]


def test_unknown_signal_is_a_404(auth_client):
    assert auth_client.get(f"{BASE}/signals/does-not-exist").status_code == 404


def test_analytics_rejects_an_unknown_dimension(auth_client):
    response = auth_client.get(f"{BASE}/analytics", params={"dimension": "nonsense"})
    assert response.status_code == 400


def test_analytics_returns_buckets(auth_client, app_db):
    user_id = _owner_id(app_db)
    _seed_context(app_db, user_id)

    response = auth_client.get(f"{BASE}/analytics", params={"dimension": "context_class"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dimension"] == "context_class"
    assert data["min_forward_n"] >= 1
    assert sum(bucket["n"] for bucket in data["buckets"]) == 1


def test_endpoints_require_authentication(client):
    assert client.get(f"{BASE}/model").status_code in (401, 403)


def test_effectiveness_reports_the_analysis_shape(auth_client, app_db):
    """The dashboard's backing payload: score verdict, axes, in-sample split."""
    user_id = _owner_id(app_db)
    _seed_context(app_db, user_id)

    response = auth_client.get(f"{BASE}/effectiveness")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["metric"] == "return_pct"
    assert data["min_sample"] == 10
    # A paper context with no resolved outcome yet: forward by provenance,
    # with no metric to analyse.
    assert data["forward_n"] == 1
    assert data["forward_with_metric"] == 0
    assert data["model_version"]
    verdict = data["score_verdict"]
    assert set(verdict) >= {
        "bands",
        "means",
        "monotonic_high_is_better",
        "high_band",
        "low_band",
        "may_claim",
        "statement",
    }
    assert verdict["may_claim"] is False
    assert data["axes"] and data["in_sample_axes"]
    assert all(
        b["significance"] == "in_sample_not_a_claim"
        for axis in data["in_sample_axes"]
        for b in axis["buckets"]
    )
    assert data["caveats"]


def test_effectiveness_rejects_an_unknown_metric(auth_client):
    response = auth_client.get(f"{BASE}/effectiveness", params={"metric": "sharpe"})
    assert response.status_code == 400


def test_effectiveness_rejects_an_unknown_source(auth_client):
    response = auth_client.get(f"{BASE}/effectiveness", params={"source": "FUTURE"})
    assert response.status_code == 400
