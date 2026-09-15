"""The screener service: universes, the scan, ranking, and saving.

What it does
------------

1. Resolve a universe name to a list of symbols (via ``InstrumentMaster``).
2. Load those symbols' cached daily frames.
3. Evaluate the condition tree per symbol.
4. Rank the survivors.
5. Return the requested display columns, plus per-symbol evidence.

The frame cache
---------------

Reading 3,000 parquet files takes ~10 s, which is far too slow to pay on every
keystroke but fast enough to pay once. Frames are therefore held in a short-lived
process cache keyed by exchange, so a second scan over the same exchange is
milliseconds. The TTL exists because the cache is a *snapshot of a moving
market*: a scan run at 16:00 and one run the next morning must not silently
return the same rows, so a stale entry is discarded rather than served.

Nothing here is incremental. A screen re-evaluates from the cached bars every
time, because an incremental screen that drifts from a full one is a screen you
cannot reproduce — and reproducibility is the whole basis for trusting a result.

Ranking
-------

Ranking is explicit rather than incidental. A screen sorted by whatever key the
dict happened to have is a screen whose order changes between runs, and you
cannot tell whether a symbol moved because the market moved or because the sort
did. ``SORT_FIELDS`` names the permitted keys, with a documented meaning for
each, so the ordering is a stated decision.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from atr.data import history as history_module
from atr.instruments.service import DAILY_DIR
from atr.screener.conditions import (
    ConditionError,
    Evidence,
    Node,
    count_nodes,
    evaluate_symbol,
    iter_leaves,
    parse_node,
)

logger = logging.getLogger("atr.screener")

# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------
#: A screen with more leaves than this is almost certainly a mistake — or a
#: denial-of-service attempt. Evaluating is O(leaves x symbols) over pandas
#: series, so the cost is real.
MAX_LEAVES = 60
#: Depth bound, because a deeply nested group is a stack-overflow risk and no
#: human builds a 20-deep screen on purpose.
MAX_DEPTH = 8
#: How long a loaded frame set stays fresh. See the module docstring.
FRAME_TTL_SECONDS = 120.0
#: Sessions a frame needs before its indicators mean anything.
MIN_BARS = 60


class ScreenerError(Exception):
    """A scan could not be run. Carries the code the API returns."""

    def __init__(self, message: str, *, code: str = "screener_error", status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------
#: The display columns a result row may carry. ``expr`` reads the value from a
#: symbol's frame. Every one is a real computation over cached bars — the list
#: deliberately contains no placeholder zeros.
COLUMN_DEFS: tuple[tuple[str, str, str], ...] = (
    # key, label, indicator (resolved via the screener indicator catalog)
    ("ltp", "LTP", "close"),
    ("change_pct", "Change %", "change_pct"),
    ("gap_pct", "Gap %", "gap_pct"),
    ("volume", "Volume", "volume"),
    ("rel_volume", "Rel Volume", "rel_volume"),
    ("turnover", "Turnover", "turnover"),
    ("rsi14", "RSI", "rsi"),
    ("ema20", "EMA 20", "ema"),
    ("ema50", "EMA 50", "ema"),
    ("sma50", "SMA 50", "sma"),
    ("atr_pct", "ATR %", "atr_pct"),
    ("vwap_20d", "VWAP 20d", "vwap_20d"),
    ("prev_high", "Prev High", "prev_high"),
    ("prev_low", "Prev Low", "prev_low"),
    ("high_52w", "52w High", "high_52w"),
    ("low_52w", "52w Low", "low_52w"),
    ("from_52w_high_pct", "Below 52w High %", "from_52w_high_pct"),
    ("range_pct", "Range %", "range_pct"),
    ("close_in_range", "Close in Range %", "close_in_range"),
    ("volatility_20d_pct", "Stdev 20d %", "volatility_20d_pct"),
    ("ret_1m", "Return 1m %", "ret_1m"),
    ("ret_3m", "Return 3m %", "ret_3m"),
)

#: Columns returned when the caller asks for nothing specific. Chosen to answer
#: "what is this, and why is it interesting" without a second request.
DEFAULT_COLUMNS: tuple[str, ...] = (
    "ltp",
    "change_pct",
    "volume",
    "rel_volume",
    "rsi14",
    "ema20",
    "ema50",
    "atr_pct",
)

#: Per-column period overrides, for the indicators where the display convention
#: differs from the catalog default (EMA 20 vs the catalog's EMA 20 — explicit
#: here so changing a catalog default cannot silently relabel a column).
_COLUMN_PERIODS: dict[str, int | None] = {
    "rsi14": 14,
    "ema20": 20,
    "ema50": 50,
    "sma50": 50,
    "atr_pct": 14,
}

COLUMN_LABELS: dict[str, str] = {key: label for key, label, _ in COLUMN_DEFS}
COLUMN_INDICATORS: dict[str, tuple[str, int | None]] = {
    key: (ind, _COLUMN_PERIODS.get(key)) for key, _, ind in COLUMN_DEFS
}


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------
#: Permitted sort keys and what each means. A field absent from this map cannot
#: be sorted on, so the ordering is always a stated choice.
SORT_FIELDS: dict[str, str] = {
    "rel_volume": "Relative volume, high to low — the unusual-volume screen",
    "change_pct": "Day change %, high to low — the momentum screen",
    "turnover": "Close x volume, high to low — the liquidity screen",
    "atr_pct": "ATR as % of price, high to low — the volatility screen",
    "rsi14": "RSI 14, high to low — the strength screen",
    "ret_1m": "One-month return %, high to low",
    "from_52w_high_pct": "Distance below the 52-week high, closest first",
    "volume": "Raw volume, high to low",
    "symbol": "Alphabetical — a stable order for reading, not ranking",
}

DEFAULT_SORT = "rel_volume"


def _sort_value(row: dict[str, Any], field: str) -> Any:
    """The comparable value for a sort field, with a total ordering.

    A missing or unmeasurable value sorts last in every direction rather than
    being treated as zero. Zero is a *plausible* value for most of these columns,
    and a NaN silently coerced to 0 would place an unmeasurable symbol at the
    bottom of a "lowest ATR" screen as if it had been measured.
    """
    if field == "symbol":
        return str(row.get("symbol", ""))
    value = row.get(field)
    if value is None:
        # Fall back to the indicator key for display column names.
        value = row.get(_COLUMN_ALIASES().get(field, field))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _COLUMN_ALIASES() -> dict[str, str]:  # noqa: N802 - module-level table
    """Map API sort names to the row keys that actually hold the value."""
    return {
        "rsi14": "rsi14",
        "atr_pct": "atr_pct",
        "rel_volume": "rel_volume",
    }


def rank_rows(rows: list[dict[str, Any]], sort: str, *, descending: bool = True) -> list[dict[str, Any]]:
    """Sort rows by a permitted field, unmeasurable values last.

    The sort is always ascending over a tuple whose first element is
    ``is_missing``; descending is expressed by negating the *value* rather than
    by reversing the whole tuple. Reversing would also reverse the missingness
    flag, which would float every unmeasurable row to the top of a descending
    sort — the opposite of what a trader wants. Symbol stays un-negated in both
    directions so ties resolve A→Z regardless of the metric's direction.
    """
    field = sort if sort in SORT_FIELDS else DEFAULT_SORT

    if field == "symbol":
        # The symbol column *is* the sort key, so direction is meaningful here
        # rather than a tiebreak.
        return sorted(rows, key=lambda r: str(r.get("symbol", "")), reverse=descending)

    def key(row: dict[str, Any]):
        value = _sort_value(row, field)
        if value is None:
            # Unmeasurable: flag true, magnitude irrelevant, symbol for stability.
            return (True, 0.0, str(row.get("symbol", "")))
        return (False, -value if descending else value, str(row.get("symbol", "")))

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------
@dataclass
class _FrameCache:
    exchange: str
    frames: dict[str, pd.DataFrame]
    loaded_at: float

    def fresh(self) -> bool:
        return (time.monotonic() - self.loaded_at) < FRAME_TTL_SECONDS


_FRAMES: dict[str, _FrameCache] = {}
_FRAMES_LOCK = threading.Lock()


def _canonical_frames(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Re-key frames from filesystem spelling to bare ticker.

    ``load_cached`` keys each frame by the *file* it came from, so the cache
    yields ``"20MICRONS-EQ"``. Every universe in ``InstrumentMaster`` is a bare
    ticker, ``"20MICRONS"``. Looking one up with the other silently misses.

    That miss is not cosmetic: on the real NSE cache only 435 of 2,654 records
    had a bare-ticker duplicate, so every scan quietly evaluated 16% of the
    universe and reported the result as the whole market. Normalising here — at
    the single point where frames enter the screener — fixes it for universes,
    explicit symbol lists, and saved scans alike.

    The ``-EQ`` file wins when both spellings exist: it is the series' canonical
    file and the bare one is a duplicate that may be staler.
    """
    from atr.instruments.service import canonical_symbol

    canonical: dict[str, pd.DataFrame] = {}
    is_eq: dict[str, bool] = {}
    for raw_key, frame in frames.items():
        key = canonical_symbol(raw_key)
        if not key:
            continue
        # Prefer the "-EQ" file: it is the series' canonical file and the bare
        # one is a duplicate that may be staler. Otherwise first-wins, so the
        # result does not depend on dict ordering.
        better = key not in canonical or (raw_key.endswith("-EQ") and not is_eq.get(key, False))
        if better:
            canonical[key] = frame
            is_eq[key] = raw_key.endswith("-EQ")
    return canonical


def load_frames(
    exchange: str,
    symbols: Iterable[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    """Cached daily frames for an exchange, or for a symbol subset, keyed bare.

    A subset read bypasses the cache: it is cheap (only the named files) and
    caching it would evict the full set that the next whole-market scan wants.
    """
    exchange = str(exchange or "NSEEQ").upper()

    if symbols is not None:
        from atr.instruments.service import get_instrument_master

        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        master = get_instrument_master()
        # Read against the *master's* cache root rather than a module-level one,
        # so the loader and the universe index cannot disagree about which
        # directory is being scanned — the failure mode being a scan that reads
        # one cache while the universe was built from another.
        #
        # ``cache_root`` is the parent of the per-exchange directories, so the
        # exchange dir is ``<cache_root>/iifl_daily/<EXCHANGE>``.
        root = Path(getattr(master, "cache_root", history_module.CACHE_ROOT))
        outdir = root / DAILY_DIR / exchange
        # Cache files are named by *series* — the real name is
        # ``20MICRONS-EQ.parquet``, not ``20MICRONS.parquet``. Resolve each ticker
        # to its actual file stem via the master before reading, or the lookup
        # silently finds nothing for most of the universe.
        stems: list[str] = []
        for symbol in wanted:
            record = master.get(symbol)
            cache_file = getattr(record, "cache_file", None) if record else None
            stems.append(Path(cache_file).stem if cache_file else symbol)

        frames: dict[str, pd.DataFrame] = {}
        for stem in stems:
            path = outdir / f"{stem}.parquet"
            if not path.exists():
                continue
            try:
                frames[stem] = pd.read_parquet(path)
            except Exception:  # noqa: BLE001 - one corrupt file must not stop a scan
                continue
        return _canonical_frames(frames)

    with _FRAMES_LOCK:
        entry = _FRAMES.get(exchange)
        if entry is not None and entry.fresh() and not force:
            return entry.frames

    t0 = time.monotonic()
    from atr.instruments.service import get_instrument_master

    # Glob the master's cache root rather than trusting ``load_cached``'s own
    # module-level root, so the loader and the universe index always read the
    # same directory.
    root = Path(getattr(get_instrument_master(), "cache_root", history_module.CACHE_ROOT))
    outdir = root / DAILY_DIR / exchange
    raw: dict[str, pd.DataFrame] = {}
    for path in sorted(outdir.glob("*.parquet")):
        try:
            raw[path.stem] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 - skip a corrupt file, keep the scan
            continue
    frames = _canonical_frames(raw)
    logger.info(
        "screener: loaded %d frames for %s in %.1fs",
        len(frames),
        exchange,
        time.monotonic() - t0,
    )
    with _FRAMES_LOCK:
        _FRAMES[exchange] = _FrameCache(exchange=exchange, frames=frames, loaded_at=time.monotonic())
    return frames


def clear_frame_cache() -> None:
    """Drop the frame cache. For tests, and after a history sync."""
    with _FRAMES_LOCK:
        _FRAMES.clear()


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------
class ScreenerService:
    """Runs screens over the local NSE universe."""

    def __init__(self, db: Any = None) -> None:
        #: An explicitly injected database (tests). When None, ``db`` resolves the
        #: process-wide instance on *every* access rather than caching the first
        #: one: ``get_app_db`` is an lru_cache that can be cleared and rebuilt
        #: (``reset_app_db_cache``), and a service that pinned the first handle
        #: would keep writing through a disposed engine after a rebuild. That
        #: showed up as a foreign-key failure on a row whose parent plainly
        #: existed — the write was going to a different database.
        self._db = db

    @property
    def db(self) -> Any:
        if self._db is not None:
            return self._db
        from atr.appdb.engine import get_app_db

        return get_app_db()
    # -- universes ------------------------------------------------------
    @staticmethod
    def _master() -> Any:
        from atr.instruments.service import get_instrument_master

        return get_instrument_master()

    def universes(self, exchange: str = "NSEEQ") -> list[dict[str, Any]]:
        """Named universes with their real, loadable sizes.

        ``size`` counts symbols whose daily bars are actually cached, not the
        membership list's length. The index CSV is authoritative about who
        *belongs*; the cache is authoritative about who can be *scanned*, and
        reporting the first as if it were the second would promise rows that
        cannot be produced.
        """
        master = self._master()
        snapshot = master.snapshot()
        out: list[dict[str, Any]] = []

        for name in sorted(snapshot.universes):
            members = snapshot.universes[name]
            scanned = [
                s for s in members if (rec := master.get(s)) is not None and rec.has_daily
            ]
            out.append(
                {
                    "name": name,
                    "label": _universe_label(name),
                    "size": len(scanned),
                    "members": len(members),
                    "missing_history": len(members) - len(scanned),
                }
            )

        everything = sorted(
            rec.symbol
            for rec in snapshot.records.values()  # type: ignore[attr-defined]
            if rec.exchange.upper() == exchange.upper() and rec.has_daily
        ) if hasattr(snapshot, "records") else []
        if everything:
            out.insert(
                0,
                {
                    "name": "all",
                    "label": "All cached NSE equities",
                    "size": len(everything),
                    "members": len(everything),
                    "missing_history": 0,
                },
            )
        return out

    def symbols_for(self, universe: str, exchange: str = "NSEEQ") -> list[str]:
        """Resolve a universe name to scannable symbols."""
        name = str(universe or "all").strip().lower()
        master = self._master()

        if name in ("all", ""):
            snapshot = master.snapshot()
            records = getattr(snapshot, "records", None)
            if records:
                return sorted(
                    rec.symbol
                    for rec in records.values()
                    if rec.exchange.upper() == exchange.upper() and rec.has_daily
                )
            return sorted(master.snapshot().universes.get("nifty50", []))

        members = master.universe(name)
        if not members:
            known = ", ".join(sorted(master.snapshot().universes)) or "none"
            raise ScreenerError(
                f"unknown universe {name!r}. Available: all, {known}",
                code="unknown_universe",
            )
        return [s for s in members if (rec := master.get(s)) is not None and rec.has_daily]

    # -- scanning -------------------------------------------------------
    def run(
        self,
        *,
        conditions: Any,
        universe: str = "all",
        exchange: str = "NSEEQ",
        symbols: list[str] | None = None,
        columns: list[str] | None = None,
        sort: str = DEFAULT_SORT,
        limit: int = 50,
        descending: bool = True,
        min_bars: int = MIN_BARS,
    ) -> dict[str, Any]:
        """Evaluate a screen. Returns rows, evidence, and what was scanned."""
        t0 = time.monotonic()

        root = self._prepare_tree(conditions)
        groups, leaves = count_nodes(root)

        exchange = str(exchange or "NSEEQ").upper()
        wanted = [s.strip().upper() for s in (symbols or []) if str(s).strip()]
        # An explicitly empty list means "no symbols", not "every symbol".
        # Treating it as a filter that happens to be blank would turn a caller's
        # bug into a full-universe scan — slow at best, and at worst a screen
        # over a universe nobody asked for.
        empty_selection = symbols is not None and not wanted

        if empty_selection:
            universe_symbols: list[str] = []
        elif wanted:
            universe_symbols = wanted
        else:
            universe_symbols = self.symbols_for(universe, exchange)

        if not universe_symbols and not empty_selection:
            raise ScreenerError(
                f"universe {universe!r} resolved to no symbols with cached history",
                code="empty_universe",
                status=422,
            )

        frames = load_frames(exchange, universe_symbols)
        requested = self._resolve_columns(columns, root)

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        unmeasurable_counts: dict[str, int] = {}

        for symbol in universe_symbols:
            df = frames.get(symbol)
            if df is None:
                continue
            try:
                matched, evidence = evaluate_symbol(df, root, min_bars=min_bars)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
                errors.append({"symbol": symbol, "error": str(exc)[:160]})
                continue
            if not matched:
                continue

            row = self._build_row(symbol, df, requested)
            row["why"] = [e.as_dict() for e in evidence if e.passed]
            row["evidence"] = [e.as_dict() for e in evidence]
            row["setup"] = _setup_label(evidence)
            rows.append(row)
            for e in evidence:
                if e.unmeasurable:
                    unmeasurable_counts[e.label] = unmeasurable_counts.get(e.label, 0) + 1

        rows = rank_rows(rows, sort, descending=descending)
        matched = len(rows)
        limited = rows[: max(int(limit), 0)] if limit else rows
        elapsed = round(time.monotonic() - t0, 3)

        return {
            "as_of": _as_of(frames),
            "exchange": exchange,
            "universe": universe,
            "universe_size": len(universe_symbols),
            "scanned": len(frames),
            "matched": matched,
            "returned": len(limited),
            "sort": sort if sort in SORT_FIELDS else DEFAULT_SORT,
            "descending": descending,
            "columns": requested,
            "conditions": {
                "groups": groups,
                "leaves": leaves,
                "summary": root.describe(),
            },
            "elapsed_s": elapsed,
            "rows": limited,
            "errors": errors[:20],
            # Surfaced rather than hidden: if a condition was unmeasurable for
            # most of the universe, the screen is telling you about your data,
            # not about the market.
            "warnings": (
                ["no symbols were given, so nothing was scanned"] if empty_selection else []
            )
            + [
                f"{label} could not be computed for {count} scanned symbols"
                for label, count in sorted(unmeasurable_counts.items(), key=lambda kv: -kv[1])[:5]
            ],
        }

    # -- validation -----------------------------------------------------
    def validate(self, conditions: Any) -> dict[str, Any]:
        """Structural validation without scanning. Never raises for data issues.

        Catches :class:`ScreenerError` as well as :class:`ConditionError`,
        because ``_prepare_tree`` deliberately translates parse failures into
        ``ScreenerError`` for the *run* path. Missing that meant a malformed tree
        raised out of ``validate`` — so the one endpoint whose contract is "report,
        do not fail" was returning a 500 for exactly the input it exists to
        describe.
        """
        warnings: list[str] = []
        try:
            root = self._prepare_tree(conditions)
        except ScreenerError as exc:
            return {
                "valid": False,
                "error": str(exc),
                "code": exc.code,
                "path": None,
                "warnings": [],
            }
        except ConditionError as exc:
            return {
                "valid": False,
                "error": str(exc),
                "code": exc.code,
                "path": exc.path,
                "warnings": [],
            }

        groups, leaves = count_nodes(root)
        for leaf in iter_leaves(root):
            spec = leaf.spec
            if not spec.available:
                warnings.append(
                    f"{spec.label} is not available in this build — requires {spec.requires}"
                )
            if leaf.period and not spec.takes_period:
                warnings.append(
                    f"{spec.label} ignores 'period'; the period was not applied"
                )
        return {
            "valid": True,
            "groups": groups,
            "leaves": leaves,
            "summary": root.describe(),
            "warnings": warnings,
        }

    # -- internals ------------------------------------------------------
    def _prepare_tree(self, conditions: Any) -> Node:
        if conditions is None:
            raise ScreenerError("provide at least one condition", code="no_conditions")
        try:
            root = parse_node(conditions if isinstance(conditions, dict) else {"conditions": conditions})
        except ConditionError as exc:
            raise ScreenerError(str(exc), code=exc.code) from exc

        groups, leaves = count_nodes(root)
        if leaves == 0:
            raise ScreenerError(
                "the condition tree has no conditions to evaluate",
                code="no_conditions",
            )
        if leaves > MAX_LEAVES:
            raise ScreenerError(
                f"too many conditions ({leaves}); the limit is {MAX_LEAVES}",
                code="too_many_conditions",
            )
        if groups > MAX_DEPTH * 4:
            raise ScreenerError(
                f"condition tree is too nested ({groups} groups)", code="too_nested"
            )
        for leaf in iter_leaves(root):
            spec = leaf.spec
            if not spec.available:
                raise ScreenerError(
                    f"{spec.label} is not available in this build — requires {spec.requires}",
                    code="indicator_unavailable",
                )
        return root

    @staticmethod
    def _resolve_columns(columns: list[str] | None, root: Node) -> list[str]:
        """Display columns: what was asked for, plus what the screen tested.

        The union matters. A screen on "RSI < 35" whose result table has no RSI
        column makes the reader open every row to check the claim, so any
        indicator named in the tree is added to the output whether or not it was
        requested.
        """
        known = set(COLUMN_LABELS)
        requested: list[str] = []
        for key in columns or ():
            k = str(key).strip().lower()
            if k in known and k not in requested:
                requested.append(k)
        if not requested:
            requested = list(DEFAULT_COLUMNS)

        # Add a column for any leaf indicator we have a display column for.
        for leaf in iter_leaves(root):
            key = leaf.indicator
            if key in ("sma", "ema") and leaf.period:
                key = f"{key}{leaf.period}"
            if key in known and key not in requested:
                requested.append(key)
        return requested

    @staticmethod
    def _build_row(symbol: str, df: pd.DataFrame, columns: list[str]) -> dict[str, Any]:
        from atr.screener.indicators import indicator_series, last_value

        row: dict[str, Any] = {"symbol": symbol}
        for key in columns:
            indicator, period = COLUMN_INDICATORS.get(key, (key, None))
            value = last_value(indicator_series(df, indicator, period))
            row[key] = None if value != value else round(value, 4)
        return row

    # -- saved scans ----------------------------------------------------
    def save(
        self,
        user_id: str,
        *,
        name: str,
        definition: dict[str, Any],
        description: str | None = None,
    ) -> dict[str, Any]:
        from atr.appdb.repositories import ScreenerRepository

        if not str(name or "").strip():
            raise ScreenerError("a saved scan needs a name", code="missing_name")

        report = self.validate(definition.get("conditions"))
        if not report["valid"]:
            raise ScreenerError(
                f"the scan definition is invalid: {report['error']}",
                code=report.get("code") or "invalid_definition",
            )

        with self.db.session() as session:
            if ScreenerRepository.name_taken(session, user_id, name):
                raise ScreenerError(
                    f"a scan named {name!r} already exists", code="name_taken", status=409
                )
            row = ScreenerRepository.create(
                session,
                user_id=user_id,
                name=name,
                definition=definition,
                description=description,
            )
        return _scan_out(row)

    def list_saved(self, user_id: str) -> list[dict[str, Any]]:
        from atr.appdb.repositories import ScreenerRepository

        with self.db.session() as session:
            rows = ScreenerRepository.list_for_user(session, user_id)
        return [_scan_out(r) for r in rows]

    def get_saved(self, user_id: str, scan_id: str) -> dict[str, Any]:
        from atr.appdb.repositories import ScreenerRepository

        with self.db.session() as session:
            row = ScreenerRepository.get(session, scan_id, user_id)
        if row is None:
            # 404 rather than 403: a scan that is not yours must not be
            # distinguishable from one that does not exist.
            raise ScreenerError("scan not found", code="not_found", status=404)
        return _scan_out(row)

    def delete_saved(self, user_id: str, scan_id: str) -> bool:
        from atr.appdb.repositories import ScreenerRepository

        with self.db.session() as session:
            deleted = ScreenerRepository.delete(session, scan_id, user_id)
        if not deleted:
            raise ScreenerError("scan not found", code="not_found", status=404)
        return True

    def update_saved(
        self,
        user_id: str,
        scan_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        definition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit a saved scan in place. Absent fields are left alone.

        Lives here rather than in the route because the persistence layer is the
        service's business: a route that opens its own session and calls a
        repository directly puts the ownership check somewhere the next caller
        has to remember to repeat it. ``get_saved`` is called first so a foreign
        or missing id fails with the same 404 the other endpoints give, before
        anything is written.

        A new definition is validated before it is stored, for the same reason
        ``save`` validates: a screen that cannot run is worse than no screen.
        """
        from atr.appdb.repositories import ScreenerRepository

        self.get_saved(user_id, scan_id)  # 404 early, before touching anything

        if definition is not None:
            report = self.validate(definition.get("conditions"))
            if not report["valid"]:
                raise ScreenerError(
                    f"the scan definition is invalid: {report['error']}",
                    code=report.get("code") or "invalid_definition",
                )

        with self.db.session() as session:
            current = ScreenerRepository.get(session, scan_id, user_id) or {}
            renaming = (
                name is not None
                and (name or "").strip().lower()
                != (current.get("name") or "").strip().lower()
            )
            # Only a *rename* can collide. Re-saving under the same name must not
            # trip the uniqueness check against the row being edited.
            if renaming and ScreenerRepository.name_taken(session, user_id, name):
                raise ScreenerError(
                    f"a scan named {name!r} already exists",
                    code="name_taken",
                    status=409,
                )
            changed = ScreenerRepository.update(
                session,
                scan_id,
                user_id,
                name=name,
                description=description,
                definition=definition,
            )
        if not changed:
            raise ScreenerError("scan not found", code="not_found", status=404)
        return self.get_saved(user_id, scan_id)

    def run_saved(self, user_id: str, scan_id: str, **overrides: Any) -> dict[str, Any]:
        """Run a stored scan by id, so a saved screen is one call to re-run."""
        saved = self.get_saved(user_id, scan_id)
        definition = saved.get("definition") or {}
        params = {
            "conditions": definition.get("conditions"),
            "universe": definition.get("universe", "all"),
            "exchange": definition.get("exchange", "NSEEQ"),
            "columns": definition.get("columns"),
            "sort": definition.get("sort", DEFAULT_SORT),
            "limit": definition.get("limit", 50),
        }
        params.update({k: v for k, v in overrides.items() if v is not None})
        result = self.run(**params)
        result["saved_scan"] = {"scan_id": saved["scan_id"], "name": saved["name"]}
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _universe_label(name: str) -> str:
    return {
        "nifty50": "NIFTY 50",
        "next50": "NIFTY Next 50",
        "midcap150": "NIFTY Midcap 150",
        "smallcap250": "NIFTY Smallcap 250",
    }.get(name, name.replace("_", " ").title())


def _as_of(frames: dict[str, pd.DataFrame]) -> str | None:
    """The newest bar date across the scanned frames.

    Reported as the data's own date rather than today's, because a screen run on
    a Sunday is a screen of Friday's market and saying otherwise would be a lie
    about what the numbers are.
    """
    newest: Any = None
    for df in frames.values():
        if df is None or df.empty or "ts" not in df.columns:
            continue
        try:
            ts = pd.Timestamp(df["ts"].iloc[-1])
        except Exception:  # noqa: BLE001
            continue
        if newest is None or ts > newest:
            newest = ts
    return None if newest is None else newest.date().isoformat()


def _setup_label(evidence: list[Evidence]) -> str:
    """A short name for the pattern a match represents.

    Derived from which leaves passed, so it is a description of the screen's own
    conditions rather than an editorial comment — "Breakout" here means "the
    breakout conditions you wrote are true", not a recommendation.
    """
    passed = {e.indicator for e in evidence if e.passed}
    if "gap_pct" in passed and "rel_volume" in passed:
        return "Gap + volume"
    if "rel_volume" in passed and ("prev_high" in passed or "high_52w" in passed):
        return "Volume breakout"
    if "rsi" in passed and "from_52w_high_pct" in passed:
        return "Momentum pullback"
    if {"ema", "sma"} & passed:
        return "Trend"
    if "atr_pct" in passed or "volatility_20d_pct" in passed:
        return "Volatility"
    if "change_pct" in passed:
        return "Move"
    return "Match"


def _scan_out(row: dict[str, Any]) -> dict[str, Any]:
    definition = row.get("definition")
    if isinstance(definition, str):
        import json

        try:
            definition = json.loads(definition)
        except (ValueError, TypeError):
            definition = {}
    return {
        "scan_id": row.get("scan_id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "definition": definition or {},
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


_SERVICE: ScreenerService | None = None
_SERVICE_LOCK = threading.Lock()


def get_screener_service() -> ScreenerService:
    """The process-wide screener service."""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = ScreenerService()
    return _SERVICE


def reset_screener_service() -> None:
    """Drop the singleton and the frame cache. For tests."""
    global _SERVICE
    _SERVICE = None
    clear_frame_cache()
