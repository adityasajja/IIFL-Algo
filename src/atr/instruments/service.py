"""Instrument master.

One service that answers "what is this symbol, and what do we have for it" —
canonicalisation, metadata, index membership, and cache freshness.

Three facts about this repo's data drive the design:

1. **The parquet cache uses two spellings.** ``data/iifl_daily/NSEEQ/`` contains
   both ``20MICRONS-EQ.parquet`` and ``360ONE.parquet`` for the same instrument.
   The master canonicalises to the bare ticker and records which file it prefers.
2. **``data/universe/*.txt`` are single-line, comma-separated.** ``.split()``
   returns one giant token and yields zero symbols — a documented trap in this
   repo that silently produces "no data" as if it were a finding.
3. **A full metadata scan takes ~11s** across 3,000+ files. That is far too slow
   for a request path, so the built index is cached to
   ``data/instruments_master.json`` keyed by a fingerprint of the cache
   directory, and rebuilt only when the file set changes.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("atr.instruments")

ROOT = Path(__file__).resolve().parents[3]
CACHE_ROOT = ROOT / "data"

#: NSE series suffixes that may follow a symbol in a cache filename. Kept
#: deliberately narrow: a symbol may legitimately contain a hyphen
#: (``BAJAJ-AUTO``), so a blanket "strip the last -XX" would corrupt it.
_SERIES_TOKENS = frozenset(
    {"EQ", "BE", "BZ", "BL", "BT", "GC", "IL", "IQ", "IV", "NC", "ND", "SM", "ST", "RR"}
)
_SERIES_RE = re.compile(r"^(N[1-9]|W[1-3])$")

#: The daily cache is the only layout that names an exchange, because it nests
#: one directory per exchange: ``iifl_daily/NSEEQ/*.parquet``. Every other
#: ``iifl_*`` directory is named for a *granularity* (``iifl_15m``), so it
#: contributes timeframe coverage but cannot define an instrument's exchange.
DAILY_DIR = "iifl_daily"

#: Cash-market index instruments. Names only — lot sizes, tick sizes and expiries
#: come from the broker contract master, not from a table maintained here.
INDEX_INSTRUMENTS: tuple[tuple[str, str], ...] = (
    ("NIFTY50", "NIFTY 50"),
    ("BANKNIFTY", "NIFTY BANK"),
    ("FINNIFTY", "NIFTY FINANCIAL SERVICES"),
    ("MIDCPNIFTY", "NIFTY MIDCAP SELECT"),
    ("NIFTYNXT50", "NIFTY NEXT 50"),
    ("SENSEX", "BSE SENSEX"),
    ("BANKEX", "BSE BANKEX"),
    ("INDIAVIX", "INDIA VIX"),
)

_UNIVERSE_FILES: dict[str, str] = {
    "nifty50": "n50.txt",
    "midcap150": "mid150.txt",
    "smallcap250": "smallcap250.txt",
}


def canonical_symbol(symbol: str) -> str:
    """Reduce any spelling to the bare ticker.

    ``reliance``, ``RELIANCE-EQ``, ``RELIANCE`` and ``" RELIANCE "`` all become
    ``RELIANCE``. ``NIFTY 50`` becomes ``NIFTY50``.
    """
    if not symbol:
        return ""
    text = str(symbol).strip().upper().replace(" ", "")
    base, sep, tail = text.rpartition("-")
    if sep and base and (tail in _SERIES_TOKENS or _SERIES_RE.match(tail)):
        return base
    return text


@dataclass(slots=True)
class InstrumentRecord:
    symbol: str
    exchange: str
    asset_class: str = "EQUITY"
    series: str | None = None
    name: str | None = None
    isin: str | None = None
    industry: str | None = None
    lot_size: int = 1
    tick_size: float = 0.05
    bars: int = 0
    first_date: str | None = None
    last_date: str | None = None
    cache_file: str | None = None
    #: Which granularities have local data — "1d", "15m", …
    timeframes: list[str] = field(default_factory=list)
    indices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_daily(self) -> bool:
        return "1d" in self.timeframes


@dataclass(slots=True)
class MasterSnapshot:
    built_at: str
    fingerprint: str
    records: dict[str, InstrumentRecord]
    by_exchange: dict[str, list[str]]
    universes: dict[str, list[str]]
    build_seconds: float
    source: str  # "cache" | "scan" | "empty"


class InstrumentMaster:
    """Searchable index over the local market-data cache."""

    def __init__(self, cache_root: Path | None = None, *, ttl_seconds: float = 300.0) -> None:
        self.cache_root = Path(cache_root) if cache_root else CACHE_ROOT
        self.master_file = self.cache_root / "instruments_master.json"
        self.ttl_seconds = ttl_seconds
        self._snapshot: MasterSnapshot | None = None
        self._lock = threading.RLock()
        self._built_monotonic = 0.0
        self._building = False

    # ------------------------------------------------------------- lifecycle
    def fingerprint(self) -> str:
        """Cheap identity of the cache contents.

        File count plus the newest mtime per directory. A full content hash would
        be better and would cost a second per request; this catches every case
        that matters (a symbol added, a file rewritten) and costs a scandir.

        Walks one level into ``iifl_daily`` because that is where the parquets
        actually live — ``iifl_daily/NSEEQ/*.parquet``. Globbing only the top
        level finds zero files and reports a fingerprint that never changes.
        """
        parts: list[str] = []
        for directory in sorted(self.cache_root.glob("iifl_*")):
            if not directory.is_dir():
                continue
            targets = [directory]
            targets.extend(sorted(p for p in directory.iterdir() if p.is_dir()))
            for target in targets:
                count = 0
                newest = 0.0
                for entry in target.iterdir():
                    if entry.suffix not in {".parquet", ".csv"}:
                        continue
                    count += 1
                    try:
                        newest = max(newest, entry.stat().st_mtime)
                    except OSError:
                        continue
                if count:
                    parts.append(f"{target.name}:{count}:{int(newest)}")
        return "|".join(parts) or "empty"

    def snapshot(self) -> MasterSnapshot:
        with self._lock:
            if self._snapshot is not None and self._fresh():
                return self._snapshot
            loaded = self._load_from_disk()
            if loaded is not None and loaded.fingerprint == self.fingerprint():
                self._snapshot = loaded
                self._built_monotonic = time.monotonic()
                return loaded
            # Nothing usable cached — build synchronously. Callers that cannot
            # afford ~11s should warm this at startup instead (see
            # ``atr.api.main``), which is what the API does.
            self._snapshot = self._build()
            self._built_monotonic = time.monotonic()
            return self._snapshot

    def refresh(self, *, force: bool = True) -> MasterSnapshot:
        with self._lock:
            if not force and self._snapshot is not None and self._fresh():
                return self._snapshot
            self._snapshot = self._build()
            self._built_monotonic = time.monotonic()
            return self._snapshot

    def warm(self) -> None:
        """Build off the request path, on a daemon thread. Never raises."""
        if self._building:
            return

        def work() -> None:
            self._building = True
            try:
                self.snapshot()
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning("instrument master warm-up failed: %s", exc)
            finally:
                self._building = False

        threading.Thread(target=work, daemon=True, name="atr-instruments-warm").start()

    def _fresh(self) -> bool:
        return (time.monotonic() - self._built_monotonic) < self.ttl_seconds

    # -------------------------------------------------------------- queries
    def get(self, symbol: str, exchange: str | None = None) -> InstrumentRecord | None:
        snap = self.snapshot()
        key = canonical_symbol(symbol)
        record = snap.records.get(key)
        if record is None:
            return None
        if exchange and record.exchange.upper() != exchange.upper():
            # A symbol exists on another exchange — report it rather than
            # pretending it does not exist.
            return None
        return record

    def resolve(self, symbol: str) -> str | None:
        """Canonical ticker if we know it, else None."""
        key = canonical_symbol(symbol)
        return key if key in self.snapshot().records else None

    def exists(self, symbol: str) -> bool:
        return canonical_symbol(symbol) in self.snapshot().records

    def search(
        self,
        query: str,
        *,
        exchange: str | None = None,
        asset_class: str | None = None,
        limit: int = 50,
    ) -> list[InstrumentRecord]:
        """Ranked search: exact, then prefix, then substring, then name match.

        The ranking is what makes a symbol box usable — typing ``REL`` should put
        ``RELIANCE`` above ``RELINFRA`` only if it is a closer match, and both
        above a company whose *name* merely contains "rel".
        """
        snap = self.snapshot()
        needle = canonical_symbol(query)
        if not needle:
            return []

        exact: list[InstrumentRecord] = []
        prefix: list[InstrumentRecord] = []
        substring: list[InstrumentRecord] = []
        by_name: list[InstrumentRecord] = []

        for symbol, record in snap.records.items():
            if exchange and record.exchange.upper() != exchange.upper():
                continue
            if asset_class and record.asset_class.upper() != asset_class.upper():
                continue
            if symbol == needle:
                exact.append(record)
            elif symbol.startswith(needle):
                prefix.append(record)
            elif needle in symbol:
                substring.append(record)
            elif record.name and needle in canonical_symbol(record.name):
                by_name.append(record)

        for bucket in (prefix, substring, by_name):
            bucket.sort(key=lambda r: (len(r.symbol), r.symbol))
        ordered = exact + prefix + substring + by_name
        return ordered[: max(1, min(limit, 500))]

    def exchanges(self) -> list[dict[str, Any]]:
        snap = self.snapshot()
        return [
            {"exchange": name, "symbols": len(symbols)}
            for name, symbols in sorted(snap.by_exchange.items())
        ]

    def universes(self) -> dict[str, list[str]]:
        return self.snapshot().universes

    def universe(self, name: str) -> list[str]:
        return self.snapshot().universes.get(name, [])

    def indices_for(self, symbol: str) -> list[str]:
        record = self.get(symbol)
        return list(record.indices) if record else []

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        latest = max((r.last_date for r in snap.records.values() if r.last_date), default=None)
        return {
            "source": snap.source,
            "built_at": snap.built_at,
            "build_seconds": round(snap.build_seconds, 2),
            "symbols": len(snap.records),
            "exchanges": self.exchanges(),
            "universes": {k: len(v) for k, v in snap.universes.items()},
            "latest_bar_date": latest,
            "cache_root": str(self.cache_root),
        }

    # --------------------------------------------------------------- building
    def _load_from_disk(self) -> MasterSnapshot | None:
        path = self.master_file
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = {
                key: InstrumentRecord(**value) for key, value in payload["records"].items()
            }
            return MasterSnapshot(
                built_at=payload["built_at"],
                fingerprint=payload["fingerprint"],
                records=records,
                by_exchange=payload["by_exchange"],
                universes=payload["universes"],
                build_seconds=float(payload.get("build_seconds", 0.0)),
                source="cache",
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not be fatal
            logger.warning("instrument master cache unreadable (%s); rebuilding", exc)
            return None

    def _iter_sources(self) -> Iterable[tuple[str | None, Path, str]]:
        """Yield ``(exchange, path, timeframe)`` for every local data file.

        ``exchange`` is None for a file found outside the per-exchange daily
        tree, because nothing in that layout says which exchange it belongs to.
        """
        daily = self.cache_root / DAILY_DIR
        if daily.is_dir():
            for exchange_dir in sorted(p for p in daily.iterdir() if p.is_dir()):
                exchange = exchange_dir.name.upper()
                for entry in sorted(exchange_dir.iterdir()):
                    if entry.is_file() and entry.suffix in {".parquet", ".csv"}:
                        yield exchange, entry, "1d"

        for directory in sorted(self.cache_root.glob("iifl_*")):
            if not directory.is_dir() or directory.name == DAILY_DIR:
                continue
            timeframe = directory.name.removeprefix("iifl_")
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and entry.suffix in {".parquet", ".csv"}:
                    yield None, entry, timeframe

    def _build(self) -> MasterSnapshot:
        started = time.monotonic()
        records: dict[str, InstrumentRecord] = {}
        by_exchange: dict[str, list[str]] = {}
        unassigned: list[str] = []

        # --- index instruments first, so a cache file can enrich them ---------
        for symbol, name in INDEX_INSTRUMENTS:
            records[symbol] = InstrumentRecord(
                symbol=symbol, exchange="NSEEQ", asset_class="INDEX", name=name
            )

        metadata = self._load_universe_metadata()
        for exchange, entry, timeframe in self._iter_sources():
            symbol, series = _split_cache_name(entry.name)
            if not symbol:
                continue
            record = records.get(symbol)
            if record is None:
                record = InstrumentRecord(
                    symbol=symbol, exchange=exchange or "", series=series
                )
                records[symbol] = record
                if exchange is None:
                    unassigned.append(symbol)
            elif exchange and not record.exchange:
                record.exchange = exchange
            if series and not record.series:
                record.series = series
            if timeframe not in record.timeframes:
                record.timeframes.append(timeframe)

            # Prefer a daily parquet as the reference file — it is the series
            # every screen and backtest reads.
            if timeframe == "1d" and entry.suffix == ".parquet":
                record.cache_file = str(entry.relative_to(self.cache_root).as_posix())
                bars, first, last = _parquet_span(entry)
                record.bars = bars
                record.first_date = first
                record.last_date = last

        # Intraday-only symbols inherit the exchange when the daily tree names
        # exactly one; otherwise they are left as UNKNOWN rather than guessed at.
        known_exchanges = {
            r.exchange for r in records.values() if r.exchange and r.symbol not in unassigned
        }
        fallback = next(iter(known_exchanges)) if len(known_exchanges) == 1 else "UNKNOWN"
        for symbol in unassigned:
            records[symbol].exchange = fallback
        if unassigned:
            logger.info(
                "%d symbol(s) found only outside the daily tree; exchange set to %s",
                len(unassigned),
                fallback,
            )

        for symbol, record in records.items():
            meta = metadata.get(symbol)
            if meta:
                record.name = record.name or meta.get("name")
                record.isin = meta.get("isin")
                record.industry = meta.get("industry")
                if meta.get("series"):
                    record.series = record.series or meta["series"]
            record.timeframes.sort()
            by_exchange.setdefault(record.exchange or "UNKNOWN", []).append(symbol)

        for name in by_exchange:
            by_exchange[name].sort()

        universes = self._load_universes()
        for universe_name, members in universes.items():
            for member in members:
                record = records.get(member)
                if record is not None and universe_name not in record.indices:
                    record.indices.append(universe_name)

        snapshot = MasterSnapshot(
            built_at=datetime.now().isoformat(timespec="seconds"),
            fingerprint=self.fingerprint(),
            records=records,
            by_exchange=by_exchange,
            universes=universes,
            build_seconds=time.monotonic() - started,
            source="scan",
        )
        self._write(snapshot)
        logger.info(
            "instrument master built: %d symbols in %.1fs",
            len(records),
            snapshot.build_seconds,
        )
        return snapshot

    def _write(self, snapshot: MasterSnapshot) -> None:
        try:
            payload = {
                "built_at": snapshot.built_at,
                "fingerprint": snapshot.fingerprint,
                "build_seconds": snapshot.build_seconds,
                "records": {k: v.as_dict() for k, v in snapshot.records.items()},
                "by_exchange": snapshot.by_exchange,
                "universes": snapshot.universes,
            }
            self.master_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.master_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            # Atomic replace: a crash mid-write must not leave a truncated index
            # that the next start would read as an empty market.
            tmp.replace(self.master_file)
        except Exception as exc:  # noqa: BLE001 - a cache write is not worth failing over
            logger.warning("could not persist instrument master: %s", exc)

    def _load_universe_metadata(self) -> dict[str, dict[str, Any]]:
        """Company name, ISIN and industry from the niftyindices CSVs."""
        out: dict[str, dict[str, Any]] = {}
        universe_dir = self.cache_root / "universe"
        if not universe_dir.is_dir():
            return out
        for csv_path in sorted(universe_dir.glob("ind_*list.csv")):
            try:
                import csv as _csv

                with csv_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for row in _csv.DictReader(fh):
                        raw = row.get("Symbol") or row.get("SYMBOL") or ""
                        symbol = canonical_symbol(raw)
                        if not symbol:
                            continue
                        out.setdefault(
                            symbol,
                            {
                                "name": (row.get("Company Name") or "").strip() or None,
                                "isin": (row.get("ISIN Code") or "").strip() or None,
                                "industry": (row.get("Industry") or "").strip() or None,
                                "series": (row.get("Series") or "").strip() or None,
                            },
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read %s: %s", csv_path, exc)
        return out

    def _load_universes(self) -> dict[str, list[str]]:
        """Index membership.

        ``data/universe/*.txt`` hold a **single comma-separated line**. Splitting
        on whitespace yields one token and zero symbols, which is the documented
        trap this repo has hit before.
        """
        out: dict[str, list[str]] = {}
        universe_dir = self.cache_root / "universe"
        if not universe_dir.is_dir():
            return out
        for name, filename in _UNIVERSE_FILES.items():
            path = universe_dir / filename
            if not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("could not read %s: %s", path, exc)
                continue
            symbols: list[str] = []
            for token in re.split(r"[,\r\n;]+", raw):
                symbol = canonical_symbol(token)
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
            out[name] = symbols
        return out


def _split_cache_name(filename: str) -> tuple[str, str | None]:
    """``BAJAJ-AUTO-EQ.parquet`` → ``("BAJAJ-AUTO", "EQ")``."""
    stem = filename.rsplit(".", 1)[0]
    base, sep, tail = stem.rpartition("-")
    if sep and base and (tail in _SERIES_TOKENS or _SERIES_RE.match(tail)):
        return base, tail
    return stem, None


def _parquet_span(path: Path) -> tuple[int, str | None, str | None]:
    """Row count and date range, reading only the timestamp column.

    A full read of 3,000 files takes ~56s; reading one column takes ~9s. Neither
    belongs on a request path, which is why the result is cached.
    """
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["ts"])
        rows = table.num_rows
        if rows == 0:
            return 0, None, None
        column = table.column("ts").to_pylist()
        first = _as_date(column[0])
        last = _as_date(column[-1])
        return rows, first, last
    except Exception as exc:  # noqa: BLE001 - one unreadable file is not fatal
        logger.debug("could not read span for %s: %s", path.name, exc)
        return 0, None, None


def _as_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


_master: InstrumentMaster | None = None
_master_lock = threading.Lock()


def get_instrument_master() -> InstrumentMaster:
    global _master
    if _master is None:
        with _master_lock:
            if _master is None:
                _master = InstrumentMaster()
    return _master


def reset_instrument_master() -> None:
    """Drop the singleton. Tests use this."""
    global _master
    with _master_lock:
        _master = None


def iter_symbols(records: Iterable[InstrumentRecord]) -> list[str]:
    return [r.symbol for r in records]


__all__ = [
    "INDEX_INSTRUMENTS",
    "InstrumentMaster",
    "InstrumentRecord",
    "MasterSnapshot",
    "canonical_symbol",
    "get_instrument_master",
    "reset_instrument_master",
]
