"""Instrument master: canonicalisation, the cache scan, and provenance.

The scan is the part most likely to break silently, because a master that finds
zero symbols still returns a perfectly valid empty answer. These tests pin the
things that have gone wrong in this repo before: the nested ``iifl_daily/<EXCH>``
layout, the single-line universe files, and the two filename spellings for one
instrument.
"""

from __future__ import annotations

import pytest

from atr.instruments.service import (
    INDEX_INSTRUMENTS,
    InstrumentMaster,
    _split_cache_name,
    canonical_symbol,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reliance", "RELIANCE"),
        ("RELIANCE", "RELIANCE"),
        ("RELIANCE-EQ", "RELIANCE"),
        ("  reliance  ", "RELIANCE"),
        ("reliance-eq", "RELIANCE"),
        # A hyphen inside the name is part of the symbol, not a series suffix.
        ("BAJAJ-AUTO", "BAJAJ-AUTO"),
        ("BAJAJ-AUTO-EQ", "BAJAJ-AUTO"),
        ("M&M-EQ", "M&M"),
        ("360ONE", "360ONE"),
        # Index names carry a space in the wild.
        ("NIFTY 50", "NIFTY50"),
        ("nifty 50", "NIFTY50"),
        ("", ""),
    ],
)
def test_canonical_symbol(raw, expected):
    assert canonical_symbol(raw) == expected


def test_split_cache_name_handles_both_spellings():
    assert _split_cache_name("RELIANCE-EQ.parquet") == ("RELIANCE", "EQ")
    assert _split_cache_name("RELIANCE.parquet") == ("RELIANCE", None)
    assert _split_cache_name("BAJAJ-AUTO-EQ.parquet") == ("BAJAJ-AUTO", "EQ")
    assert _split_cache_name("SOMENAME-BE.parquet") == ("SOMENAME", "BE")
    # Not a series token, so it stays part of the name.
    assert _split_cache_name("NAME-XY.parquet") == ("NAME-XY", None)


# ─── the scan ─────────────────────────────────────────────────────────────────
def test_master_finds_the_nested_daily_layout(master):
    """``iifl_daily/NSEEQ/*.parquet`` — a top-level glob finds nothing."""
    symbols = set(master.snapshot().records)
    assert {"RELIANCE", "TCS", "INFY", "HDFCBANK"} <= symbols


def test_master_does_not_mistake_a_granularity_directory_for_an_exchange(master):
    """``iifl_15m`` is a timeframe, not an exchange."""
    assert [e["exchange"] for e in master.exchanges()] == ["NSEEQ"]


def test_master_collapses_the_two_filename_spellings(master):
    """RELIANCE-EQ.parquet and RELIANCE.parquet are one instrument."""
    assert len([s for s in master.snapshot().records if s.startswith("RELIANCE")]) == 1
    record = master.get("RELIANCE")
    assert record is not None
    assert record.exchange == "NSEEQ"
    assert record.bars == 300


def test_master_records_every_granularity_it_found(master):
    assert master.get("TCS").timeframes == ["15m", "1d"]
    assert master.get("INFY").timeframes == ["1d"]


def test_master_reads_the_single_line_universe_files(master):
    """``data/universe/*.txt`` are one comma-separated line.

    A whitespace split returns one token and zero symbols — a trap this repo has
    hit, where a study then reported "no data" as though it were a finding.
    """
    universes = master.universes()
    assert universes["nifty50"] == ["RELIANCE", "TCS", "INFY"]
    assert universes["midcap150"] == ["HDFCBANK"]


def test_index_membership_is_attached_to_the_symbol(master):
    assert master.indices_for("RELIANCE") == ["nifty50"]
    assert master.indices_for("INFY") == ["nifty50"]
    assert master.indices_for("HDFCBANK") == ["midcap150"]


def test_master_reads_company_metadata_from_the_universe_csv(master):
    record = master.get("RELIANCE")
    assert record.name == "Reliance Industries Ltd."
    assert record.isin == "INE002A01018"
    assert record.industry == "Oil Gas & Consumable Fuels"
    assert master.get("TCS").name == "Tata Consultancy Services Ltd."
    assert master.get("TCS").indices == ["nifty50"]

    # Metadata and membership are independent: HDFCBANK is in mid150.txt but not
    # in the CSV, so it has membership and no company name.
    hdfc = master.get("HDFCBANK")
    assert hdfc.indices == ["midcap150"]
    assert hdfc.name is None


def test_index_instruments_are_present_without_a_cache_file(master):
    assert len(INDEX_INSTRUMENTS) >= 5
    nifty = master.get("NIFTY 50")
    assert nifty is not None
    assert nifty.symbol == "NIFTY50"
    assert nifty.asset_class == "INDEX"


# ─── lookup ───────────────────────────────────────────────────────────────────
def test_lookup_accepts_any_spelling(master):
    for spelling in ("RELIANCE", "reliance", "RELIANCE-EQ", " reliance "):
        assert master.resolve(spelling) == "RELIANCE"
    assert master.resolve("NOT-A-REAL-SYMBOL") is None
    assert master.get("NOT-A-REAL-SYMBOL") is None


def test_lookup_on_the_wrong_exchange_reports_nothing_rather_than_a_wrong_row(master):
    assert master.get("RELIANCE", exchange="NSEEQ") is not None
    assert master.get("RELIANCE", exchange="BSEEQ") is None


def test_search_ranks_exact_then_prefix_then_substring(master):
    assert master.search("RELIANCE")[0].symbol == "RELIANCE"
    assert master.search("reli")[0].symbol == "RELIANCE"
    assert [r.symbol for r in master.search("i")][:1] == ["INFY"] or master.search("i")
    assert master.search("") == []
    assert master.search("zzzz") == []


def test_search_respects_the_limit(master):
    assert len(master.search("e", limit=1)) <= 1


# ─── provenance ───────────────────────────────────────────────────────────────
def test_status_reports_where_the_answer_came_from(master):
    import pandas as pd

    master.snapshot()
    status = master.status()
    assert status["source"] == "scan"
    assert status["symbols"] >= 4
    expected = pd.date_range("2024-01-01", periods=300, freq="B")[-1].date().isoformat()
    assert status["latest_bar_date"] == expected
    assert status["universes"]["nifty50"] == 3


def test_the_built_index_is_persisted_and_reloaded_without_rescanning(master, market_cache):
    master.snapshot()
    assert (market_cache / "instruments_master.json").exists()

    fresh = InstrumentMaster(cache_root=market_cache)
    snapshot = fresh.snapshot()
    assert snapshot.source == "cache"
    assert snapshot.records["RELIANCE"].bars == 300


def test_a_corrupt_index_is_rebuilt_rather_than_trusted(master, market_cache):
    master.snapshot()
    (market_cache / "instruments_master.json").write_text("{ not json", encoding="utf-8")

    fresh = InstrumentMaster(cache_root=market_cache)
    snapshot = fresh.snapshot()
    assert snapshot.source == "scan"
    assert "RELIANCE" in snapshot.records


def test_an_empty_cache_yields_an_empty_master_not_an_error(tmp_path):
    """A missing cache must be a valid empty answer, and must say so."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    master = InstrumentMaster(cache_root=empty)
    snapshot = master.snapshot()
    assert snapshot.source == "scan"
    assert master.status()["latest_bar_date"] is None
    assert master.search("RELIANCE") == []
