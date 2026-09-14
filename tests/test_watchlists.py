"""Watchlists: persistence, ownership, column validation, and quote assembly.

The two properties that would be expensive to get wrong:

* **Ownership is enforced by the query, not by a check.** A watchlist id that
  belongs to another user must behave exactly like one that does not exist.
* **A column with no data returns null and says why**, rather than a zero. The
  brief asks for market cap, OI and IV; this repo has neither a fundamentals feed
  nor an option chain, and a zero would look like a measurement.
"""

from __future__ import annotations

import pytest

from atr.appdb.repositories import UserRepository
from atr.services.watchlists import (
    COLUMN_INDEX,
    COMPUTABLE_COLUMNS,
    DEFAULT_COLUMNS,
    WatchlistError,
    WatchlistService,
)


@pytest.fixture()
def users(app_db) -> tuple[str, str]:
    """Two accounts, so ownership can be tested rather than assumed."""
    with app_db.session() as session:
        first = UserRepository.create(
            session, email="a@example.com", username="a", password_hash="x", role="trader"
        )
        second = UserRepository.create(
            session, email="b@example.com", username="b", password_hash="x", role="trader"
        )
    return first["user_id"], second["user_id"]


@pytest.fixture()
def service(app_db, master) -> WatchlistService:
    return WatchlistService(app_db, master)


# ─── the column registry ──────────────────────────────────────────────────────
def test_registry_declares_unavailable_columns_instead_of_faking_them():
    for key in ("market_cap", "oi", "iv", "delta"):
        spec = COLUMN_INDEX[key]
        assert spec.available is False
        assert spec.requires, f"{key} must name what it needs"
        assert "not collected yet" in spec.description
    # And the ones that do work are marked as such.
    for key in ("ltp", "rsi14", "ema20", "volume_ratio", "atr_pct"):
        assert COLUMN_INDEX[key].available is True
    assert "market_cap" not in COMPUTABLE_COLUMNS
    assert set(DEFAULT_COLUMNS) <= set(COMPUTABLE_COLUMNS)


def test_default_columns_are_all_computable():
    """A default that cannot render is a default that shows a column of dashes."""
    assert all(COLUMN_INDEX[key].available for key in DEFAULT_COLUMNS)


def test_unknown_columns_are_dropped_not_stored(service):
    assert service.validate_columns(["ltp", "not_a_column", "ltp", ""]) == ["ltp"]


# ─── CRUD ─────────────────────────────────────────────────────────────────────
def test_create_list_get_and_default(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    assert created["name"] == "Core"
    assert created["is_default"] is True  # the first list becomes the default
    assert created["items"] == []
    assert created["columns"] == list(DEFAULT_COLUMNS)

    second = service.create(first_user, name="Momentum", columns=["ltp", "rsi14"])
    assert second["is_default"] is False
    assert second["columns"] == ["ltp", "rsi14"]

    listed = service.list_watchlists(first_user)
    assert [w["name"] for w in listed] == ["Core", "Momentum"]
    assert listed[0]["item_count"] == 0

    assert service.set_default(first_user, second["watchlist_id"]) is not None
    assert service.get(first_user, second["watchlist_id"])["is_default"] is True
    assert service.get(first_user, created["watchlist_id"])["is_default"] is False


def test_duplicate_names_are_refused(service, users):
    first_user, _ = users
    service.create(first_user, name="Core")
    with pytest.raises(WatchlistError) as exc:
        service.create(first_user, name="Core")
    assert exc.value.status == 409
    assert exc.value.code == "duplicate_name"


def test_blank_name_is_refused(service, users):
    first_user, _ = users
    with pytest.raises(WatchlistError) as exc:
        service.create(first_user, name="   ")
    assert exc.value.code == "missing_name"


def test_rename_and_delete(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    renamed = service.update(first_user, created["watchlist_id"], name="Core Holdings")
    assert renamed is not None and renamed["name"] == "Core Holdings"

    assert service.delete(first_user, created["watchlist_id"]) is True
    assert service.get(first_user, created["watchlist_id"]) is None
    assert service.delete(first_user, created["watchlist_id"]) is False


def test_another_users_watchlist_looks_like_it_does_not_exist(service, users):
    first_user, second_user = users
    created = service.create(first_user, name="Private")

    assert service.get(second_user, created["watchlist_id"]) is None
    assert service.update(second_user, created["watchlist_id"], name="Hijacked") is None
    assert service.delete(second_user, created["watchlist_id"]) is False
    assert service.set_default(second_user, created["watchlist_id"]) is None
    # The owner's copy is untouched.
    assert service.get(first_user, created["watchlist_id"])["name"] == "Private"

    with pytest.raises(WatchlistError) as exc:
        service.add_items(second_user, created["watchlist_id"], ["INFY"])
    assert exc.value.status == 404


def test_deleting_a_watchlist_removes_its_items(service, users, app_db):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    service.add_items(first_user, created["watchlist_id"], ["INFY"])
    service.delete(first_user, created["watchlist_id"])

    from sqlalchemy import func, select

    from atr.appdb.schema import watchlist_items

    with app_db.session() as session:
        remaining = session.execute(select(func.count()).select_from(watchlist_items)).scalar()
    assert remaining == 0


# ─── items ────────────────────────────────────────────────────────────────────
def test_add_items_validates_against_the_master(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    result = service.add_items(
        first_user, created["watchlist_id"], ["reliance", "RELIANCE-EQ", "TCS", "NOPE", "INFY"]
    )
    # Canonicalised, deduplicated, and the unknown one reported rather than dropped.
    assert result["added"] == ["RELIANCE", "TCS", "INFY"]
    assert result["skipped"] == ["RELIANCE"]
    assert result["unknown"] == ["NOPE"]
    assert service.get(first_user, created["watchlist_id"])["items"] == [
        "RELIANCE",
        "TCS",
        "INFY",
    ]


def test_add_items_with_only_unknown_symbols_adds_nothing(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    result = service.add_items(first_user, created["watchlist_id"], ["NOPE", "ALSOFAKE"])
    assert result["added"] == []
    assert result["unknown"] == ["NOPE", "ALSOFAKE"]


def test_remove_item(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    service.add_items(first_user, created["watchlist_id"], ["INFY", "TCS"])
    assert service.remove_item(first_user, created["watchlist_id"], "infy") is True
    assert service.get(first_user, created["watchlist_id"])["items"] == ["TCS"]
    assert service.remove_item(first_user, created["watchlist_id"], "INFY") is False


def test_reorder_is_idempotent_and_keeps_unlisted_symbols(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    service.add_items(first_user, created["watchlist_id"], ["RELIANCE", "TCS", "INFY"])
    wid = created["watchlist_id"]

    first = service.reorder(first_user, wid, ["INFY", "TCS"])
    # A partial list must not drop rows: RELIANCE stays, after the ones named.
    assert first["items"] == ["INFY", "TCS", "RELIANCE"]
    # INFY moved 2→0 and RELIANCE 0→2; TCS did not move.
    assert first["moved"] == 2

    # Idempotent: repeating the resulting order moves nothing.
    again = service.reorder(first_user, wid, ["INFY", "TCS", "RELIANCE"])
    assert again["moved"] == 0
    assert again["items"] == ["INFY", "TCS", "RELIANCE"]

    # And a genuine swap does move.
    swapped = service.reorder(first_user, wid, ["TCS", "INFY"])
    assert swapped["items"] == ["TCS", "INFY", "RELIANCE"]
    assert swapped["moved"] == 2


def test_reorder_rejects_an_unknown_watchlist(service, users):
    _, second_user = users
    assert service.reorder(second_user, "nope", ["INFY"]) is None


# ─── columns ──────────────────────────────────────────────────────────────────
def test_set_columns_reports_what_it_rejected(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    result = service.set_columns(
        first_user, created["watchlist_id"], ["ltp", "ema20", "bogus_column"]
    )
    assert result["columns"] == ["ltp", "ema20"]
    assert result["rejected_columns"] == ["bogus_column"]


def test_set_columns_with_nothing_valid_is_refused(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core")
    with pytest.raises(WatchlistError) as exc:
        service.set_columns(first_user, created["watchlist_id"], ["nope"])
    assert exc.value.code == "no_valid_columns"


# ─── quotes ───────────────────────────────────────────────────────────────────
def test_quotes_computes_every_configured_column(service, users):
    first_user, _ = users
    created = service.create(
        first_user,
        name="Core",
        columns=[
            "ltp",
            "prev_close",
            "change",
            "change_pct",
            "volume",
            "volume_ratio",
            "rsi14",
            "ema20",
            "atr_pct",
        ],
    )
    service.add_items(first_user, created["watchlist_id"], ["RELIANCE", "TCS"])

    payload = service.quotes(first_user, created["watchlist_id"])
    assert payload["source"] == "local_cache"
    assert payload["count"] == 2
    assert [row["symbol"] for row in payload["rows"]] == ["RELIANCE", "TCS"]

    row = payload["rows"][0]
    assert row["ltp"] > 0
    assert row["stale"] is True  # no broker overlay was supplied
    assert row["change"] == pytest.approx(row["ltp"] - row["prev_close"], abs=0.01)
    assert 0 <= row["rsi14"] <= 100
    assert row["ema20"] > 0
    assert row["atr_pct"] > 0
    assert row["volume"] > 0
    assert row["volume_ratio"] > 0


def test_quotes_return_null_for_columns_with_no_data_source(service, users):
    """A column with no feed must be null, never a zero."""
    first_user, _ = users
    created = service.create(
        first_user, name="Core", columns=["ltp", "market_cap", "oi", "iv", "delta"]
    )
    service.add_items(first_user, created["watchlist_id"], ["INFY"])

    row = service.quotes(first_user, created["watchlist_id"])["rows"][0]
    assert row["ltp"] > 0
    assert row["market_cap"] is None
    assert row["oi"] is None
    assert row["iv"] is None
    assert row["delta"] is None


def test_quotes_reports_the_column_definitions_alongside_the_rows(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core", columns=["ltp", "rsi14"])
    columns = service.quotes(first_user, created["watchlist_id"])["columns"]
    assert [c["key"] for c in columns] == ["ltp", "rsi14"]
    assert columns[1]["label"] == "RSI 14"


def test_a_live_overlay_updates_price_and_clears_stale(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core", columns=["ltp", "change_pct", "volume"])
    service.add_items(first_user, created["watchlist_id"], ["RELIANCE"])

    overlay = {"RELIANCE": {"ltp": 2000.0, "prevClose": 1900.0, "volume": 12345}}
    payload = service.quotes(first_user, created["watchlist_id"], live=overlay)
    row = payload["rows"][0]
    assert row["stale"] is False
    assert row["ltp"] == 2000.0
    assert row["change"] == pytest.approx(100.0)
    assert row["change_pct"] == pytest.approx(5.2632, abs=0.001)
    assert row["volume"] == 12345
    assert payload["source"] == "broker+cache"
    assert payload["live_symbols"] == ["RELIANCE"]


def test_a_broker_payload_using_camel_case_is_understood(service, users):
    """The repo has read `quantity` where the broker sent `netQuantity` before."""
    first_user, _ = users
    created = service.create(first_user, name="Core", columns=["ltp"])
    service.add_items(first_user, created["watchlist_id"], ["TCS"])
    payload = service.quotes(
        first_user, created["watchlist_id"], live={"TCS": {"lastPrice": 4100.5}}
    )
    assert payload["rows"][0]["ltp"] == 4100.5


def test_a_nonsense_overlay_falls_back_to_the_cache(service, users):
    first_user, _ = users
    created = service.create(first_user, name="Core", columns=["ltp"])
    service.add_items(first_user, created["watchlist_id"], ["TCS"])
    payload = service.quotes(
        first_user, created["watchlist_id"], live={"TCS": {"ltp": "not-a-number"}}
    )
    assert payload["rows"][0]["stale"] is True
    assert payload["rows"][0]["ltp"] > 0


def test_quotes_for_another_users_watchlist_is_none(service, users):
    first_user, second_user = users
    created = service.create(first_user, name="Core")
    assert service.quotes(second_user, created["watchlist_id"]) is None
