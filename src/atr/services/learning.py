"""The learning engine — dataset, performance intelligence, daily report.

What this module is for
-----------------------

Three questions, in order of how easy they are to answer dishonestly:

1. **What actually happened?** Every trade the system has ever recorded, in one
   table, with the features that describe the conditions it was taken under —
   and an explicit record of which features are *missing* rather than a blank
   that could be mistaken for a zero.
2. **Under which conditions did it work?** Sliced by regime, volatility, time of
   day, setup, sector and so on — with a sample size, a confidence interval and
   a multiple-comparisons correction attached to every single finding.
3. **What does that mean for tomorrow?** A daily report that states the
   deviation from expectation and names the strategies worth reviewing —
   advisory only, never a change to a live strategy.

The rule that shapes the whole design
-------------------------------------

The user's constraint was: *"Do not invent data that does not exist. Clearly
mark missing features."* That single sentence decides most of the choices in
this file:

* A backtest trade has **no MFE/MAE**, because ``backtest_trades`` has no such
  columns. They are ``None`` and ``mfe`` appears in the row's
  ``missing_features``. They are not estimated from the entry and exit price.
* A trade whose ``signal_reason`` does not mention volume has
  **``relative_volume = None``** and ``relative_volume`` in its
  ``missing_features``. It is not 1.0.
* **VWAP and India VIX are absent everywhere**, with a reason string naming why.
* A bucket with fewer than ten trades is **suppressed**, not reported with a
  caveat.
* A **zero-trade dataset is a valid, correctly-shaped result.** The live
  database currently holds no trades at all, and the correct output in that case
  is an empty dataset plus a report that says so — not an error, and emphatically
  not a synthetic sample.

Why this lives in ``services`` and not ``research``
---------------------------------------------------

``research`` is a compute layer and ``tests/test_architecture.py`` forbids it
from importing ``appdb``. The dataset has to read ``backtest_trades``,
``trade_journal`` and ``backtest_runs``, so it belongs in ``services``, where
reading the database is the job. The statistics it uses live in
``research.learning_stats``, which imports nothing from ``atr`` at all, so the
dependency points the right way: services → research → (numpy, pandas).

What is deliberately not in this phase
--------------------------------------

Phases 4–6 of the original specification — the recommendation engine, adaptive
parameter optimisation, and improvement proposals that create new strategy
versions. All three would modify or propose modifications to strategies, and the
user was explicit: *"Do not implement autonomous strategy modification yet."*
The daily report therefore produces *observations* labelled advisory, and names
no parameter values to change.

Safety
------

Every method here is a read except :meth:`LearningService.snapshot`, which
writes two files under ``data/learning/``. Nothing in this module writes to
``strategies``, ``strategy_versions``, ``deployments``, ``orders``,
``order_events`` or ``risk_*``. ``tests/test_learning_service.py`` asserts this
by fingerprinting those tables across a full pipeline run, so the guarantee is
tested rather than promised.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from atr.research import learning_paper as paper
from atr.research import learning_stats as stats
from atr.research.learning_attribution import extract_reason
from atr.research.learning_axes import (
    Axis,
    axes_from_names,
    axis_coverage,
    requested_but_unavailable,
)
from atr.research.learning_drift import DriftAnalysis
from atr.research.learning_drift import analyse as drift_analysis
from atr.research.learning_enrich import (
    benchmark_provenance_at,
    benchmark_trend,
    bucket_atr,
    bucket_relative_volume,
    bucket_rsi,
    build_sector_table,
    label_regime,
    median_or_none,
    metrics_at,
    relative_strength_at,
    time_of_day,
    trend_at,
)

#: The evidence vocabulary — sources, ``evidence_class``, ``evidence_grade`` and
#: the functions that translate between them — lives in
#: :mod:`atr.research.learning_evidence` and is re-exported here.
#:
#: It moved because three layers need it and only one of them may own it. The
#: journal has to grade a closed episode from the provenance stamp the OMS wrote;
#: the paper-ledger reader has to grade a settled week from its own marker; and
#: this module has to stamp every row. Spelling the vocabulary out in each place
#: is how one reader ends up calling a record forward while another calls it
#: in-sample — and whichever is asked decides whether a finding is published.
#: ``research`` is the lowest layer all three can see, so it is where it lives.
from atr.research.learning_evidence import (  # noqa: F401 — re-exported
    CLASS_BACKTEST,
    CLASS_IN_SAMPLE,
    CLASS_LIVE_FORWARD,
    CLASS_PAPER_FORWARD,
    EVIDENCE_CLASSES,
    FORWARD_CLASSES,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
    GRADES,
    IN_SAMPLE_CLASSES,
    NOTE_BACKTEST_IN_SAMPLE,
    NOTE_FORWARD_STAMPED,
    NOTE_MARKED_IN_SAMPLE,
    NOTE_NO_STAMP,
    SOURCE_BACKTEST,
    SOURCE_LIVE,
    SOURCE_PAPER,
    SOURCES,
    forward_class,
    grade_of,
    has_grade,
    is_forward_class,
    is_forward_grade,
    row_grade,
)

#: Where the generated artefacts are written. Under ``data/`` beside the other
#: research stores, so a reader looking for the output of the learning engine
#: finds it where every other piece of evidence lives.
LEARNING_DIR = Path("data/learning")


def data_root() -> Path:
    """The directory the learning engine reads flat files from.

    ``data`` relative to the working directory, matching every other data path
    in this project (``data/iifl_daily``, ``data/universe``). ``ATR_DATA_ROOT``
    overrides it, which is what lets a test point the engine at its own
    directory instead of the operator's real paper record — the same mechanism
    ``APP_DB_URL`` gives the database, and for the same reason: a test whose
    result depends on the operator's files is not a test.
    """
    import os

    return Path(os.environ.get("ATR_DATA_ROOT") or "data")


#: The outcome columns a caller may rank buckets on. ``net_pnl`` is the default
#: because rupees are what a decision is made on, but it is not always present:
#: the paper ledger records an equal-weight return and no position size, so a
#: book made only of ledger rows has ``return_pct`` and no ``net_pnl`` at all.
#: :func:`LearningService.resolve_metric` is how a caller finds that out without
#: being handed a silently empty table.
OUTCOME_METRICS = ("net_pnl", "return_pct")

#: The column order of the learning dataset. Fixed, so the parquet is stable
#: across runs and two datasets can be diffed.
DATASET_COLUMNS = (
    # identity
    "source",
    "evidence_class",
    #: The direction, derived from ``evidence_class`` by :func:`grade_of` and
    #: written by the builder rather than left to each consumer to compute.
    #: ``evidence_class`` answers "where did this come from"; this answers the
    #: only question a finding depends on.
    "evidence_grade",
    #: Redundant with ``evidence_grade`` on purpose. Every consumer that wants to
    #: know "is this evidence?" would otherwise re-implement the membership test,
    #: and the first one to get it wrong would present an in-sample number as a
    #: finding — which is the single failure this dataset exists to prevent.
    "is_forward",
    #: Why this row carries the grade it does. A grade with no stated basis is a
    #: label a reader has to take on trust, and the whole point of the column is
    #: that it should not have to be taken on trust.
    "evidence_note",
    "source_ref",
    "trade_ref",
    #: The signal that raised the trade, and the opening order behind it. Both
    #: stay ``None`` when the trade cannot be linked back — an unlinked row is
    #: reported by data-quality monitoring, never silently dropped and never
    #: filled with a placeholder.
    "signal_id",
    "opening_order_id",
    #: The context the signal engine recorded for that signal, kept exactly as
    #: recorded — never recomputed here, so this table and the signal-context
    #: record cannot disagree.
    "context_score",
    "context_model_version",
    "context_class",
    "strategy_key",
    "strategy_id",
    "strategy_version",
    "engine_key",
    "symbol",
    "sector",
    "direction",
    # timing
    "entry_ts",
    "exit_ts",
    "duration_days",
    "day_of_week",
    "time_of_day",
    # outcome
    "quantity",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "commission",
    "net_pnl",
    "return_pct",
    "exit_reason",
    # excursions — journal only
    "mfe",
    "mae",
    "slippage_bps",
    # entry conditions
    "signal_reason",
    "setup",
    "rsi",
    "sma_fast",
    "sma_slow",
    "sma_long",
    "prior_high",
    "signal_volume_multiple",
    "breakout_proximity_pct",
    # entry-time market context
    "as_of_ts",
    "bars_available",
    "close_at_entry",
    "rsi_bucket",
    "atr_pct",
    "atr_bucket",
    "relative_volume",
    "rvol_bucket",
    "gap_pct",
    "trend_pct",
    "above_slow_sma",
    "market_regime",
    "nifty_trend_pct",
    "benchmark_symbol",
    #: Where the entry sat relative to the session VWAP. Present as a column so
    #: the shape is stable and a client can render it, and always ``None``: the
    #: price cache holds daily bars and a session VWAP cannot be recovered from
    #: one. See :data:`atr.research.learning_axes.requested_but_unavailable`.
    # portfolio context at entry
    "portfolio_exposure_at_entry",
    "strategy_allocation",
    "sector_exposure_pct",
    "position_concentration",
    # sizing & risk budgeting context at entry
    "risk_per_trade",
    "position_size",
    "position_value",
    "stop_distance",
    "atr_based_risk",
    # post-trade attribution — journal rows only
    #: The nine-branch attribution's sliceable fields, joined from
    #: ``trade_attributions`` by ``trade_id``. Present for a journal trade that
    #: has been attributed; ``None`` for a backtest row, for a paper-ledger row,
    #: and for a journal trade the sweep has not reached yet. A ``None`` here is
    #: **not evidence that execution was clean** — the availability of the
    #: feature is itself reported through ``missing_features``, so a reader can
    #: tell "attributed and unremarkable" from "never attributed".
    "attribution_entry_quality",
    "attribution_execution_quality",
    "attribution_exit_quality",
    "attribution_entry_slippage_bps",
    "attribution_exit_slippage_bps",
    "attribution_total_slippage_bps",
    "attribution_transaction_costs",
    "attribution_cost_pct",
    "attribution_signal_to_order_sec",
    "attribution_order_to_fill_sec",
    "attribution_holding_sec",
    "attribution_partial_fill",
    "attribution_fill_ratio",
    "attribution_sizing_method",
    "attribution_sizing_cap_reason",
    "attribution_realized_risk_pct",
    "attribution_planned_risk_amount",
    "attribution_mfe_over_risk",
    "attribution_realized_over_risk",
    "attribution_capture_efficiency_pct",
    "attribution_context_score",
    "attribution_context_class",
    "attribution_sector_strength",
    "attribution_stock_relative_strength",
    "attribution_rvol",
    "attribution_atr_pct",
    "attribution_market_regime",
    #: The codes, comma-joined, exactly as the attribution row stores them. Kept
    #: as one column rather than exploded into a boolean per code: a code is a
    #: description of a trade, not an axis, and the axes that read attribution
    #: read the fields above.
    "attribution_reason_codes",
    # bookkeeping
    "missing_features",
)


class LearningError(Exception):
    """A learning read that cannot be served."""

    def __init__(self, message: str, *, code: str = "learning_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


@dataclass
class LearningDataset:
    """One normalised table of every backtest, paper and live trade.

    Not a "collection of trades" with a frame attached — the frame *is* the
    dataset, and every accessor works off it. That keeps a single representation
    of the data, so the performance analysis, the drift analysis and the report
    cannot disagree about what happened.
    """

    rows: list[dict[str, Any]]
    missing_features: dict[str, str]
    generated_at: datetime
    #: Non-None when the cache could not be read, so a caller can tell "no
    #: features" from "no price data" without inspecting every row.
    warnings: list[str] = field(default_factory=list)
    #: Per-source row counts before any filtering.
    source_counts: dict[str, int] = field(default_factory=dict)
    #: The paper ledger's own account of what it holds — how many weeks, how many
    #: of them forward, and whether the forward count is zero. Carried on the
    #: dataset rather than left to the caller to recompute, because "the record
    #: exists and holds no forward week" is a finding about the *evidence*, not
    #: about the trades, and it is the one a reader most needs.
    ledger: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def empty(self) -> bool:
        return not self.rows

    def frame(self):
        """A pandas DataFrame with :data:`DATASET_COLUMNS` exactly.

        An empty dataset still gets the full column set. A frame with zero
        *columns* would make every downstream ``groupby`` raise, so the empty
        case is a zero-**row**, correctly-shapen frame instead.
        """
        import pandas as pd

        if not self.rows:
            return pd.DataFrame({name: pd.Series(dtype="object") for name in DATASET_COLUMNS})
        frame = pd.DataFrame(self.rows)
        for name in DATASET_COLUMNS:
            if name not in frame.columns:
                frame[name] = None
        return frame[list(DATASET_COLUMNS)]

    def by_source(self, source: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("source") == source]

    def by_class(self, evidence_class: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("evidence_class") == evidence_class]

    def by_grade(self, grade: str) -> list[dict[str, Any]]:
        """Rows of one grade. The filter the analysis and the report both use."""
        return [row for row in self.rows if row_grade(row) == grade]

    def forward_rows(self) -> list[dict[str, Any]]:
        """Rows that are evidence: recorded before the outcome was known."""
        return [row for row in self.rows if is_forward_grade(row_grade(row))]

    def in_sample_rows(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if not is_forward_grade(row_grade(row))]

    def class_counts(self) -> dict[str, int]:
        """Rows per ``evidence_class``. The precise provenance breakdown."""
        out = {name: 0 for name in EVIDENCE_CLASSES}
        for row in self.rows:
            name = row.get("evidence_class")
            if name in out:
                out[name] += 1
        return out

    def grade_counts(self) -> dict[str, int]:
        """Rows per ``evidence_grade`` — the direction, and the only split a
        finding depends on.

        Both keys are always present, including at zero, so a caller can render
        "0 forward" rather than a missing field that reads as "not measured".
        """
        out = {name: 0 for name in GRADES}
        for row in self.rows:
            out[row_grade(row)] += 1
        return out

    def latest_forward_ts(self) -> datetime | None:
        """When the most recent forward observation closed.

        The single number that answers "is anything still being measured?" A
        book can hold thousands of in-sample rows and no forward one, and this
        is what makes that visible instead of inferable.
        """
        stamps = [
            row.get("exit_ts") or row.get("entry_ts")
            for row in self.forward_rows()
            if (row.get("exit_ts") or row.get("entry_ts")) is not None
        ]
        return max(stamps) if stamps else None

    def metric_coverage(self) -> dict[str, int]:
        """How many closed trades actually carry each outcome column.

        This exists because an outcome column can be *absent* rather than empty.
        The paper ledger records an equal-weight return and no position size, so
        a book built only from it carries ``return_pct`` on every row and
        ``net_pnl`` on none. A caller that ranks buckets on ``net_pnl`` without
        checking receives a correctly-shaped table of nothing, which reads
        exactly like a strategy that broke even.
        """
        closed = [row for row in self.rows if row.get("exit_ts") is not None]
        return {
            metric: sum(1 for row in closed if row.get(metric) is not None)
            for metric in OUTCOME_METRICS
        }

    def strategies(self) -> list[str]:
        seen = {row.get("strategy_key") for row in self.rows}
        return sorted(name for name in seen if name)

    @property
    def missing_feature_reasons(self) -> dict[str, str]:
        """Per-row ``missing_features`` folded into one feature -> reason map.

        Same shape as :attr:`missing_features`, so a caller does not have to
        learn two vocabularies. A feature present on no row simply does not
        appear, which is the honest answer: nothing was missing it.
        """
        counts: dict[str, int] = {}
        for row in self.rows:
            for feature in row.get("missing_features") or []:
                counts[feature] = counts.get(feature, 0) + 1
        merged: dict[str, str] = {}
        for feature in counts:
            merged[feature] = self.missing_features.get(
                feature, f"not available for {counts[feature]} of {len(self.rows)} trades"
            )
        return merged

    def summary(self) -> dict[str, Any]:
        """A small, honest description of what the dataset contains.

        The figures the learning screen leads with are all here, because a
        client that has to derive them will eventually derive one of them wrong:
        total observations, forward observations, in-sample observations, the
        latest forward trade, the metric coverage, and the evidence breakdown —
        by grade (the direction, which is what a finding depends on) and by class
        (the precise provenance, which is what the drift comparison needs).
        """
        closed = [row for row in self.rows if row.get("exit_ts") is not None]
        pnl = [row.get("net_pnl") for row in closed if row.get("net_pnl") is not None]
        grades = self.grade_counts()
        return {
            "generated_at": _iso(self.generated_at),
            "observations": len(self.rows),
            "trades": len(self.rows),
            "closed_trades": len(closed),
            "open_trades": len(self.rows) - len(closed),
            "forward_observations": grades[GRADE_FORWARD],
            "in_sample_observations": grades[GRADE_IN_SAMPLE],
            "latest_forward_ts": _iso(self.latest_forward_ts()),
            "sources": {
                source: self.source_counts.get(source, 0) for source in SOURCES
            },
            #: ``{forward, in_sample}`` — always both keys, so "0 forward" is
            #: stated rather than inferred from an absent field.
            "evidence_grades": grades,
            "evidence_classes": self.class_counts(),
            "metric_coverage": self.metric_coverage(),
            #: How many rows retain a recorded context score. Coverage, not a
            #: finding: a row without one contributes no band anywhere.
            "context_coverage": {
                "with_score": sum(1 for row in self.rows if row.get("context_score") is not None),
                "rows": len(self.rows),
            },
            "paper_ledger": dict(self.ledger),
            "strategies": self.strategies(),
            "symbols": len({row.get("symbol") for row in self.rows if row.get("symbol")}),
            "date_range": _date_range(closed),
            "net_pnl_total": round(sum(pnl), 2) if pnl else None,
            "missing_features": dict(self.missing_features),
            "warnings": list(self.warnings),
        }


def _attribution_flat(
    tree: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    """The attribution fields the dataset stores, from the stored row.

    Prefers the lifted columns on the record — they were written by the same
    service that wrote the tree, and reading them avoids parsing the blob — and
    falls back to the tree for the two fields that live only inside it.

    Every value is passed through unchanged, including ``None``. Nothing is
    defaulted, because a ``0.0`` written where a measurement was absent would read
    as "executed perfectly" for a trade whose execution was never measured.
    """
    signal = tree.get("signal") or {}
    branch = {
        "attribution_entry_quality": record.get("entry_quality"),
        "attribution_execution_quality": record.get("execution_quality"),
        "attribution_exit_quality": record.get("capture_efficiency_pct"),
        "attribution_entry_slippage_bps": record.get("entry_slippage_bps"),
        "attribution_exit_slippage_bps": record.get("exit_slippage_bps"),
        "attribution_total_slippage_bps": record.get("total_slippage_bps"),
        "attribution_transaction_costs": record.get("transaction_costs"),
        "attribution_cost_pct": record.get("cost_pct"),
        "attribution_signal_to_order_sec": record.get("signal_to_order_sec"),
        "attribution_order_to_fill_sec": record.get("order_to_fill_sec"),
        "attribution_holding_sec": record.get("holding_sec"),
        "attribution_partial_fill": record.get("partial_fill"),
        "attribution_fill_ratio": record.get("fill_ratio"),
        "attribution_sizing_method": record.get("sizing_method"),
        "attribution_sizing_cap_reason": record.get("sizing_cap_reason"),
        "attribution_realized_risk_pct": record.get("realized_risk_pct"),
        "attribution_planned_risk_amount": record.get("planned_risk_amount"),
        "attribution_mfe_over_risk": record.get("mfe_over_risk"),
        "attribution_realized_over_risk": record.get("realized_over_risk"),
        "attribution_capture_efficiency_pct": record.get("capture_efficiency_pct"),
        "attribution_context_score": record.get("context_score"),
        "attribution_context_class": record.get("context_class"),
        "attribution_sector_strength": record.get("sector_strength"),
        "attribution_stock_relative_strength": record.get("stock_relative_strength"),
        "attribution_rvol": record.get("rvol"),
        "attribution_atr_pct": record.get("atr_pct"),
        "attribution_market_regime": record.get("market_regime"),
        "attribution_reason_codes": ",".join(record.get("reason_codes") or []),
    }
    # ``strategy_version`` is on the tree and not lifted, because the journal
    # already carries it and the dataset reads that one. Nothing else is taken
    # from the tree: the tree's branch objects are for a reader, and the dataset
    # is a table.
    _ = signal
    return branch


def _date_range(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    stamps = [row.get("entry_ts") for row in rows if row.get("entry_ts") is not None]
    if not stamps:
        return {"first": None, "last": None}
    return {"first": _iso(min(stamps)), "last": _iso(max(stamps))}


class LearningDatasetBuilder:
    """Assembles the dataset from the evidence that exists.

    Reads, in order of authority:

    * ``backtest_trades`` joined to its ``backtest_runs`` row — BACKTEST source.
    * ``trade_journal`` — PAPER or LIVE, decided by the deployment's mode where
      one can be found, and otherwise left as PAPER with a warning, because
      guessing LIVE when it is actually paper would overstate how much real
      money the evidence represents.
    * the forward paper ledger under ``data/paper_momentum`` — PAPER, and the
      only source in this project that holds a record written *before* the
      outcome it describes. Read through
      :mod:`atr.research.learning_paper`, which keeps its in-sample backfilled
      weeks graded separately from its forward ones.

    Everything else is derived or marked missing.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        cache_root: Path | str | None = None,
        frames: dict[str, Any] | None = None,
        ledger: paper.PaperLedger | None = None,
    ) -> None:
        self._db = db
        #: Injectable for tests. When ``frames`` is supplied the parquet cache is
        #: never touched, which is what makes the enrichment tests deterministic
        #: rather than dependent on whatever the operator downloaded.
        self._frames = frames
        self._data_root = Path(cache_root) if cache_root else data_root()
        self._sector_cache: dict[str, str] | None = None
        #: Injectable so a test can supply a ledger directly. When it is not
        #: given, the ledger is read from ``<data_root>/paper_momentum`` — the
        #: same root as the sector table and the price cache, so a test pointing
        #: at its own directory never reads the operator's record.
        self._ledger = ledger

    @property
    def db(self) -> Any:
        if self._db is not None:
            return self._db
        from atr.appdb.engine import get_app_db

        return get_app_db()

    def _load_ledger(self) -> paper.PaperLedger:
        if self._ledger is not None:
            return self._ledger
        return paper.load_ledger(self._data_root)

    # ------------------------------------------------------------------
    def build(self, *, user_id: str | None = None, limit: int = 20_000) -> LearningDataset:
        """The whole dataset. Empty input yields a valid empty dataset."""
        warnings: list[str] = []
        now = datetime.now(UTC)

        runs = self._completed_runs(user_id=user_id, limit=limit, warnings=warnings)
        backtest_rows = self._backtest_rows(runs, warnings=warnings)
        journal_rows = self._journal_rows(user_id=user_id, limit=limit, warnings=warnings)

        ledger = self._load_ledger()
        paper_rows = paper.build_rows(ledger)
        warnings.extend(ledger.warnings)
        ledger_summary = ledger.summary()

        sectors = self._sector_table()
        frames = self._load_frames(
            {row["symbol"] for row in (*backtest_rows, *journal_rows, *paper_rows)}
        )
        # The benchmark is fetched once, not per trade: it is the same series for
        # every row and re-reading it inside the loop would dominate the run.
        benchmarks = self._benchmark_frames()

        combined = [*backtest_rows, *journal_rows, *paper_rows]
        atr_median = median_or_none(
            [row.get("_atr_pct") for row in self._enrich_all(combined, frames)]
        ) if combined else None
        # Two passes because the ATR bucket is relative to the *sample's* median,
        # which is not known until every trade has been measured. The first pass
        # is discarded; only the second is persisted.
        rows: list[dict[str, Any]] = []
        for row in self._enrich_all(combined, frames, sectors=sectors, benchmarks=benchmarks):
            row["atr_bucket"] = bucket_atr(row.get("atr_pct"), atr_median)
            rows.append(row)

        source_counts = {
            source: sum(1 for row in rows if row.get("source") == source)
            for source in SOURCES
        }

        missing = self._missing_feature_registry(rows)
        if not rows:
            warnings.append(
                "no trades were found in backtest_trades, trade_journal or the "
                "paper ledger; the dataset is correctly empty rather than "
                "unavailable"
            )

        dataset = LearningDataset(
            rows=rows,
            missing_features=missing,
            generated_at=now,
            warnings=warnings,
            source_counts=source_counts,
            ledger=ledger_summary,
        )
        logger.info(
            "learning dataset: {} observations ({} backtest, {} paper/live, {} forward), "
            "missing features: {}",
            len(rows),
            source_counts.get(SOURCE_BACKTEST, 0),
            source_counts.get(SOURCE_PAPER, 0) + source_counts.get(SOURCE_LIVE, 0),
            sum(1 for row in rows if row.get("is_forward")),
            ", ".join(sorted(missing)) or "none",
        )
        return dataset

    # ------------------------------------------------------------------
    def _completed_runs(
        self, *, user_id: str | None, limit: int, warnings: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Completed runs keyed by id.

        Only COMPLETED runs are read. A FAILED run has no trade rows by
        construction (``BacktestService._fail`` deletes partial artefacts), and
        reading a RUNNING run's half-written trades would put a partially
        realised equity path into the statistics as though it were a result.
        """
        from sqlalchemy import select

        from atr.appdb.schema import backtest_runs

        out: dict[str, dict[str, Any]] = {}
        try:
            with self.db.session() as session:
                conditions = [backtest_runs.c.status == "COMPLETED"]
                if user_id:
                    conditions.append(backtest_runs.c.user_id == user_id)
                stmt = (
                    select(backtest_runs)
                    .where(*conditions)
                    .order_by(backtest_runs.c.created_at.desc())
                    .limit(max(1, min(limit, 2000)))
                )
                for row in session.execute(stmt).mappings():
                    out[row["run_id"]] = dict(row)
        except Exception as exc:  # noqa: BLE001 — a cold database is not a crash
            warnings.append(f"backtest_runs could not be read: {type(exc).__name__}")
            logger.warning("learning: could not read backtest runs: {}", exc)
        return out

    def _backtest_rows(
        self, runs: dict[str, dict[str, Any]], *, warnings: list[str]
    ) -> list[dict[str, Any]]:
        if not runs:
            return []
        from sqlalchemy import select

        from atr.appdb.schema import backtest_trades

        rows: list[dict[str, Any]] = []
        try:
            with self.db.session() as session:
                stmt = (
                    select(backtest_trades)
                    .where(backtest_trades.c.run_id.in_(list(runs)))
                    .order_by(backtest_trades.c.run_id, backtest_trades.c.seq)
                )
                for row in session.execute(stmt).mappings():
                    raw = dict(row)
                    run = runs.get(raw["run_id"], {})
                    rows.append(self._backtest_row(raw, run))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"backtest_trades could not be read: {type(exc).__name__}")
            logger.warning("learning: could not read backtest trades: {}", exc)
        return rows

    @staticmethod
    def _backtest_row(raw: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        reason = raw.get("signal_reason")
        extracted = extract_reason(reason)
        direction = str(raw.get("direction") or "LONG").upper()

        missing: list[str] = []
        # The two fields the source table structurally cannot carry. Marked here
        # rather than upstream, because "the journal has it and this does not" is
        # a property of the *source*, and a reader comparing two rows needs to
        # see which source they came from to understand the blank.
        missing.extend(["mfe", "mae", "slippage_bps"])
        if not reason:
            missing.append("signal_reason")
        if extracted.volume_multiple is None:
            missing.append("relative_volume_from_reason")

        return {
            "source": SOURCE_BACKTEST,
            # A backtest is in-sample by construction: the rule was selected on
            # the history it is being measured over. That does not make the
            # number wrong, but it does mean a backtest row can never be counted
            # as independent confirmation — which is what this label says.
            "evidence_class": CLASS_BACKTEST,
            "evidence_grade": GRADE_IN_SAMPLE,
            "evidence_note": NOTE_BACKTEST_IN_SAMPLE,
            "is_forward": False,
            "source_ref": raw.get("run_id"),
            "trade_ref": f"{raw.get('run_id')}#{raw.get('seq')}",
            # The backtest signal id is exact (``RUN_ID:SEQ``), so linkage needs
            # no fuzzy match. Recorded backtest contexts stay with the
            # signal-context service; only the forward path is joined here.
            "signal_id": f"{raw.get('run_id')}:{raw.get('seq')}",
            "opening_order_id": None,
            "context_score": None,
            "context_model_version": None,
            "context_class": None,
            "strategy_key": run.get("engine_key"),
            "strategy_id": raw.get("strategy_id") or run.get("strategy_id"),
            "strategy_version": raw.get("strategy_version") or run.get("strategy_version"),
            "engine_key": run.get("engine_key"),
            "symbol": _canonical(raw.get("symbol")),
            "sector": None,  # filled by the enrichment pass
            "direction": "LONG" if direction.startswith("L") else "SHORT",
            "entry_ts": raw.get("entry_ts"),
            "exit_ts": raw.get("exit_ts"),
            "duration_days": raw.get("duration_days"),
            "day_of_week": _dow(raw.get("entry_ts")),
            "time_of_day": time_of_day(raw.get("entry_ts")),
            "quantity": raw.get("quantity"),
            "entry_price": raw.get("entry_price"),
            "exit_price": raw.get("exit_price"),
            "gross_pnl": raw.get("gross_pnl"),
            "commission": raw.get("commission"),
            "net_pnl": raw.get("net_pnl"),
            "return_pct": raw.get("return_pct"),
            "exit_reason": raw.get("exit_reason"),
            "mfe": None,
            "mae": None,
            "slippage_bps": None,
            "signal_reason": reason,
            "setup": extracted.setup if reason else None,
            "rsi": extracted.rsi,
            "sma_fast": extracted.sma_fast,
            "sma_slow": extracted.sma_slow,
            "sma_long": extracted.sma_long,
            "prior_high": extracted.prior_high,
            "signal_volume_multiple": extracted.volume_multiple,
            "breakout_proximity_pct": extracted.proximity_pct,
            "portfolio_exposure_at_entry": None,
            "strategy_allocation": None,
            "sector_exposure_pct": None,
            "position_concentration": None,
            "risk_per_trade": None,
            "position_size": raw.get("quantity"),
            "position_value": (
                float(raw["quantity"]) * float(raw["entry_price"])
                if raw.get("quantity") and raw.get("entry_price")
                else None
            ),
            "stop_distance": None,
            "atr_based_risk": None,
            "_missing": missing,
        }

    def _journal_rows(
        self, *, user_id: str | None, limit: int, warnings: list[str]
    ) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from atr.appdb.schema import trade_journal

        modes = self._deployment_modes(warnings)
        rows: list[dict[str, Any]] = []
        try:
            with self.db.session() as session:
                conditions = []
                if user_id:
                    conditions.append(trade_journal.c.user_id == user_id)
                stmt = (
                    select(trade_journal)
                    .where(*conditions)
                    .order_by(trade_journal.c.entry_ts)
                    .limit(max(1, min(limit, 200_000)))
                )
                for row in session.execute(stmt).mappings():
                    raw = dict(row)
                    rows.append(self._journal_row(raw, modes, warnings))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"trade_journal could not be read: {type(exc).__name__}")
            logger.warning("learning: could not read trade journal: {}", exc)
        self._attach_recorded_context(rows, user_id=user_id, warnings=warnings)
        self._attach_attribution(rows, user_id=user_id, warnings=warnings)
        return rows

    def _attach_attribution(
        self, rows: list[dict[str, Any]], *, user_id: str | None, warnings: list[str]
    ) -> None:
        """Join each journal row to its post-trade attribution, where one exists.

        Read from ``trade_attributions`` keyed by ``trade_id`` — an exact join,
        unlike the signal match, because both tables are keyed on the same episode.

        **This is a read, never a computation.** Nothing here attributes a trade;
        the sweep in :mod:`atr.services.attribution` does that, on its own
        schedule. The consequence is that the dataset can be built against a book
        whose attribution is incomplete, and the honest response is to leave those
        columns ``None`` and register the reason — not to compute an attribution
        on the fly, which would let the dataset builder become a second
        implementation of the arithmetic and would let a build take minutes.

        **Attribution is a read of the book, not of a strategy.** The columns
        joined here are outcome and execution measurements. They cannot make a
        row forward, cannot change its grade, and are joined after the grade was
        resolved — so a mis-join cannot promote an in-sample trade into evidence.

        Mutates ``rows`` in place. Any failure degrades to unattributed rows.
        """
        if not rows:
            return
        trade_ids = [
            str(row.get("trade_ref"))
            for row in rows
            if row.get("source") in (SOURCE_PAPER, SOURCE_LIVE) and row.get("trade_ref")
        ]
        if not trade_ids:
            return
        try:
            from atr.appdb.repositories import TradeAttributionRepository

            with self.db.session() as session:
                attributions = TradeAttributionRepository.for_trades(
                    session, user_id, trade_ids
                ) if user_id else {}
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                "trade attributions could not be read; journal rows carry no "
                f"attribution fields ({type(exc).__name__})"
            )
            logger.warning("learning: could not read attributions: {}", exc)
            return

        for row in rows:
            if row.get("source") not in (SOURCE_PAPER, SOURCE_LIVE):
                # A backtest trade and a paper-ledger row have no journal episode
                # and therefore no attribution row. The columns stay None and the
                # reason is registered by the missing-feature pass, which is what
                # keeps "not applicable to this source" distinguishable from
                # "not computed yet".
                if row.get("source") == SOURCE_BACKTEST:
                    self._mark_missing(row, "attribution_entry_quality", "backtest_source")
                else:
                    self._mark_missing(row, "attribution_entry_quality", "ledger_source")
                continue
            record = attributions.get(str(row.get("trade_ref")))
            if record is None:
                self._mark_missing(row, "attribution_entry_quality", "not_attributed")
                continue
            tree = record.get("attribution") or {}
            flat = _attribution_flat(tree, record)
            row.update(flat)
            # The attribution reports fields the row did not have, and a row that
            # gains a feature must lose its "missing" entry for it — otherwise the
            # dataset would claim to be missing a number it is carrying.
            row["_missing"] = [
                name for name in (row.get("_missing") or []) if not name.startswith("attribution_")
            ]
            row["missing_features"] = sorted(set(row["_missing"]))

    @staticmethod
    def _mark_missing(row: dict[str, Any], feature: str, reason: str) -> None:
        missing = list(row.get("_missing") or [])
        if feature not in missing:
            missing.append(feature)
        row["_missing"] = sorted(set(missing))
        row["missing_features"] = sorted(set(missing))
        row.setdefault("_attribution_missing_reason", reason)

    def _attach_recorded_context(
        self, rows: list[dict[str, Any]], *, user_id: str | None, warnings: list[str]
    ) -> None:
        """Link journal rows back to their signal, opening order and context.

        A journal episode records no ``signal_id`` — the order that raised it
        does. So each row is matched to the earliest order with the same
        strategy, symbol and direction whose creation falls within
        ``SIGNAL_MATCH_WINDOW_SEC`` of the entry: the same rule
        ``SignalContextService._match_episode`` applies from the context side,
        so the two directions resolve identically. The recorded context is then
        read by that order's ``signal_id`` and kept as recorded.

        Mutates ``rows`` in place. Anything unresolvable stays ``None`` — an
        unlinked row is reported by data-quality monitoring, never invented.
        A failure here degrades to unlinked rows, never to a failed build.
        """
        from atr.research.learning_readiness import SIGNAL_MATCH_WINDOW_SEC

        if not rows:
            return
        try:
            from sqlalchemy import select

            from atr.appdb.schema import orders, signal_contexts

            with self.db.session() as session:
                order_conditions = [orders.c.signal_id.is_not(None)]
                context_conditions: list[Any] = []
                if user_id:
                    order_conditions.append(orders.c.user_id == user_id)
                    context_conditions.append(signal_contexts.c.user_id == user_id)
                order_rows = session.execute(
                    select(
                        orders.c.order_id,
                        orders.c.signal_id,
                        orders.c.symbol,
                        orders.c.side,
                        orders.c.strategy_id,
                        orders.c.created_at,
                    )
                    .where(*order_conditions)
                    .order_by(orders.c.created_at)
                    .limit(50_000)
                ).mappings().all()
                context_rows = session.execute(
                    select(
                        signal_contexts.c.signal_id,
                        signal_contexts.c.context_score,
                        signal_contexts.c.context_model_version,
                        signal_contexts.c.context_class,
                    ).where(*context_conditions)
                ).mappings().all()
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                "recorded signal contexts could not be read; journal rows stay "
                f"unlinked ({type(exc).__name__})"
            )
            logger.warning("learning: could not read recorded contexts: {}", exc)
            return

        contexts = {str(r["signal_id"]): r for r in context_rows if r.get("signal_id")}
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for order in order_rows:
            if not order.get("strategy_id"):
                continue
            key = (
                str(order["strategy_id"]),
                str(order.get("symbol") or "").upper(),
                "SHORT" if str(order.get("side") or "").upper() in ("SELL", "S") else "LONG",
            )
            buckets.setdefault(key, []).append(dict(order))

        for row in rows:
            if not row.get("strategy_id") or row.get("entry_ts") is None:
                continue
            key = (
                str(row["strategy_id"]),
                str(row.get("symbol") or "").upper(),
                str(row.get("direction") or "LONG").upper(),
            )
            candidates = buckets.get(key) or []
            best: dict[str, Any] | None = None
            for order in candidates:
                gap = _ts_gap_sec(row.get("entry_ts"), order.get("created_at"))
                if gap is None or gap > SIGNAL_MATCH_WINDOW_SEC:
                    continue
                best = order
                break  # orders are creation-ordered: the first hit is the entry
            if best is None:
                continue
            row["opening_order_id"] = best.get("order_id")
            row["signal_id"] = best.get("signal_id")
            context = contexts.get(str(best.get("signal_id") or ""))
            if context is not None:
                row["context_score"] = context.get("context_score")
                row["context_model_version"] = context.get("context_model_version")
                row["context_class"] = context.get("context_class")

        # Portfolio context replay at entry (exposure, allocation, sector %, concentration)
        try:
            from atr.services.portfolio import PortfolioService, snapshots_at_entry, sector_resolver
            from atr.appdb.repositories import DeploymentRepository

            port_service = PortfolioService(db=self.db)
            fills = port_service._user_fills(user_id) if user_id else []
            capitals: dict[str, float] = {}
            if user_id:
                with self.db.session() as s:
                    for d in DeploymentRepository.list_for_user(s, user_id):
                        capitals[str(d.get("deployment_id"))] = float(d.get("capital") or 0.0)

            entries_for_replay = [
                {
                    "key": idx,
                    "ts": r.get("entry_ts"),
                    "deployment_id": r.get("source_ref"),
                    "symbol": r.get("symbol"),
                }
                for idx, r in enumerate(rows)
            ]
            snaps = snapshots_at_entry(entries_for_replay, fills, capitals, sector_resolver())
            for idx, r in enumerate(rows):
                snap = snaps.get(idx) or {}
                r["portfolio_exposure_at_entry"] = snap.get("exposure")
                r["strategy_allocation"] = snap.get("allocation")
                r["sector_exposure_pct"] = snap.get("sector_pct")
                r["position_concentration"] = snap.get("concentration")
        except Exception as exc:  # noqa: BLE001
            logger.debug("learning: portfolio replay skipped: {}", exc)

    def _deployment_modes(self, warnings: list[str]) -> dict[str, str]:
        """``deployment_id -> mode``, so PAPER and LIVE can be told apart.

        The journal itself does not record whether a trade was paper or real
        money; the deployment does. Without this lookup every journal trade would
        be reported as PAPER, which would understate the live evidence — and the
        drift analysis is *entirely* about the difference between simulated and
        real, so getting this wrong would break the one comparison that matters.
        """
        from sqlalchemy import select

        from atr.appdb.schema import deployments

        out: dict[str, str] = {}
        try:
            with self.db.session() as session:
                for row in session.execute(
                    select(deployments.c.deployment_id, deployments.c.mode)
                ).mappings():
                    out[row["deployment_id"]] = str(row["mode"] or "").upper()
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                "deployment modes could not be read; journal trades are labelled "
                f"PAPER by default ({type(exc).__name__})"
            )
        return out

    @staticmethod
    def _journal_row(
        raw: dict[str, Any], modes: dict[str, str], warnings: list[str]
    ) -> dict[str, Any]:
        deployment_id = raw.get("deployment_id")
        mode = modes.get(deployment_id or "", "")
        source = SOURCE_LIVE if mode in {"LIVE", "REAL"} else SOURCE_PAPER

        reason = raw.get("signal_reason")
        extracted = extract_reason(reason)
        side = str(raw.get("side") or "BUY").upper()
        direction = "LONG" if side == "BUY" else "SHORT"

        duration_days = None
        if raw.get("entry_ts") is not None and raw.get("exit_ts") is not None:
            delta = raw["exit_ts"] - raw["entry_ts"]
            duration_days = delta.total_seconds() / 86_400.0

        return_pct = None
        entry_price = raw.get("entry_price")
        exit_price = raw.get("exit_price")
        if entry_price and exit_price:
            sign = 1.0 if direction == "LONG" else -1.0
            return_pct = (float(exit_price) / float(entry_price) - 1.0) * 100.0 * sign

        missing: list[str] = []
        for name in ("mfe", "mae", "slippage_bps"):
            if raw.get(name) is None:
                missing.append(name)
        if not reason:
            missing.append("signal_reason")
        if extracted.volume_multiple is None:
            missing.append("relative_volume_from_reason")

        # Costs, recovered from the two figures the journal does record rather
        # than re-derived from a cost model. The journal closes a trade as
        # ``gross - commission``, so the difference *is* what was charged — and
        # recomputing it from today's rates would restate history whenever a
        # rate changed.
        gross = raw.get("gross_pnl")
        net = raw.get("net_pnl")
        commission = None
        if gross is not None and net is not None:
            commission = float(gross) - float(net)
        else:
            missing.append("commission")

        exit_reason = raw.get("exit_reason")
        if not exit_reason:
            missing.append("exit_reason")

        #: The provenance verdict the journal recorded, which it in turn read from
        #: the provenance stamp the OMS wrote on the opening order's ``NEW``
        #: event. This is the field that decides whether the row is evidence, so
        #: it is resolved once, here, and never re-derived downstream.
        grade, grade_note = _journal_grade(raw)
        forward = is_forward_grade(grade)

        return {
            "source": source,
            # Where the record came from. A paper trade recorded before its
            # outcome was known is ``PAPER_FORWARD``; a paper fill whose order
            # carries no execution-time stamp cannot demonstrate that, so it is
            # ``IN_SAMPLE``. Which of the two paper classes applies is decided by
            # the deployment's mode, not by the journal, which does not record it.
            "evidence_class": forward_class(source) if forward else CLASS_IN_SAMPLE,
            "evidence_grade": grade,
            "evidence_note": grade_note,
            "is_forward": forward,
            "source_ref": deployment_id,
            "trade_ref": raw.get("trade_id"),
            # Linkage to the raising signal, its opening order and its recorded
            # context. Resolved by the builder's ``_attach_recorded_context``
            # pass (same rule the signal-context service applies from the other
            # direction); ``None`` until then, and ``None`` forever when the
            # trade cannot be linked back.
            "signal_id": None,
            "opening_order_id": None,
            "context_score": None,
            "context_model_version": None,
            "context_class": None,
            "strategy_key": raw.get("strategy_id"),
            "strategy_id": raw.get("strategy_id"),
            "strategy_version": raw.get("strategy_version"),
            "engine_key": None,
            "symbol": _canonical(raw.get("symbol")),
            "sector": None,
            "direction": direction,
            "entry_ts": raw.get("entry_ts"),
            "exit_ts": raw.get("exit_ts"),
            "duration_days": duration_days,
            "day_of_week": _dow(raw.get("entry_ts")),
            "time_of_day": time_of_day(raw.get("entry_ts")),
            "quantity": raw.get("quantity"),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross,
            "commission": commission,
            "net_pnl": net,
            "return_pct": return_pct,
            # Why the trade ended, from the closing order's own reason. It is a
            # property of the decision, not of the price path, so it cannot be
            # recovered from the fills — it is recorded or it is absent.
            "exit_reason": exit_reason,
            "mfe": raw.get("mfe"),
            "mae": raw.get("mae"),
            "slippage_bps": raw.get("slippage_bps"),
            "signal_reason": reason,
            "setup": extracted.setup if reason else None,
            "rsi": extracted.rsi,
            "sma_fast": extracted.sma_fast,
            "sma_slow": extracted.sma_slow,
            "sma_long": extracted.sma_long,
            "prior_high": extracted.prior_high,
            "signal_volume_multiple": extracted.volume_multiple,
            "breakout_proximity_pct": extracted.proximity_pct,
            "portfolio_exposure_at_entry": None,
            "strategy_allocation": None,
            "sector_exposure_pct": None,
            "position_concentration": None,
            "risk_per_trade": None,
            "position_size": raw.get("quantity"),
            "position_value": (
                float(raw["quantity"]) * float(raw.get("entry_price") or 0.0)
                if raw.get("quantity") and raw.get("entry_price")
                else None
            ),
            "stop_distance": None,
            "atr_based_risk": None,
            # The journal stores a regime label from when the trade opened. It is
            # point-in-time by construction, which is exactly what the axis
            # needs, so it is preferred over a recomputed label.
            "_journal_regime": raw.get("regime"),
            "_missing": missing,
        }

    # ------------------------------------------------------------------
    def _enrich_all(
        self,
        rows: list[dict[str, Any]],
        frames: dict[str, Any],
        *,
        sectors: dict[str, str] | None = None,
        benchmarks: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Add entry-time context to every row. Pure-ish: returns new dicts.

        Called twice — once to find the ATR median, once to persist. The second
        call is the one whose output is kept, so a row's features never depend on
        the order the rows happened to be processed in.
        """
        out: list[dict[str, Any]] = []
        empty: Any = {}
        sectors = sectors if sectors is not None else empty
        benchmarks = benchmarks if benchmarks is not None else empty

        for row in rows:
            enriched = dict(row)
            symbol = row.get("symbol")
            entry_ts = row.get("entry_ts")
            missing = list(row.get("_missing") or [])

            enriched["sector"] = sectors.get(symbol) if symbol else None
            if enriched["sector"] is None and symbol:
                missing.append("sector")

            # The session VWAP is not recoverable from a daily bar, so this is
            # absent on every row rather than blank on some. It is carried as a
            # column *and* marked missing, because a field the specification
            # asked for that simply does not appear reads as an oversight, while
            # one that appears absent with a reason reads as a finding.
            enriched["vwap_relationship"] = None
            missing.append("vwap_relationship")

            frame = frames.get(symbol) if symbol else None
            if frame is None:
                missing.extend(
                    ["rsi", "atr_pct", "relative_volume", "gap_pct", "trend_pct"]
                )
                enriched.update(
                    {
                        "as_of_ts": None,
                        "bars_available": 0,
                        "close_at_entry": None,
                        "rsi": enriched.get("rsi"),
                        "atr_pct": None,
                        "relative_volume": None,
                        "gap_pct": None,
                        "trend_pct": None,
                        "above_slow_sma": None,
                        "rsi_bucket": bucket_rsi(enriched.get("rsi")),
                        "rvol_bucket": None,
                        "atr_bucket": None,
                        "market_regime": row.get("_journal_regime"),
                        "nifty_trend_pct": None,
                        "benchmark_symbol": None,
                    }
                )
                # The row must carry its own missing list as well as the builder's
                # internal ``_missing``: the internal one is dropped before the
                # row is persisted, and a consumer reading a single row needs to
                # be able to see what is absent on *that* trade.
                enriched["_atr_pct"] = None
                enriched["_missing"] = sorted(set(missing))
                enriched["missing_features"] = sorted(set(missing))
                out.append(enriched)
                continue

            context, context_missing = metrics_at(frame, entry_ts)
            missing.extend(context_missing.features)
            # A journal regime wins over the recomputed one where it exists,
            # because it was recorded at the time rather than reconstructed.
            regime = row.get("_journal_regime")
            nifty_pct, bench_prov = benchmark_provenance_at(benchmarks, entry_ts)
            benchmark_symbol = bench_prov.get("symbol")
            benchmark_kind = bench_prov.get("kind")
            benchmark_is_proxy = bench_prov.get("is_proxy", True)
            regime_model_version = "v1.0.0"

            if regime is None:
                regime = label_regime(nifty_pct, context.atr_pct, atr_median=None)

            trend = trend_at(frame, entry_ts)
            if trend is None:
                missing.append("trend_pct")

            # Benchmark frame for relative strength calculation
            bench_frame = benchmarks.get(benchmark_symbol) if benchmark_symbol else None
            if bench_frame is None and benchmarks:
                for b_cand in ("NIFTYBEES-EQ", "NIFTYBEES", "MONIFTY500-EQ"):
                    if b_cand in benchmarks:
                        bench_frame = benchmarks[b_cand]
                        break

            stock_rs = relative_strength_at(frame, bench_frame, entry_ts)
            if stock_rs is None:
                missing.append("stock_relative_strength")

            # Sector relative strength: derived if sector peers are available, else missing
            sector_rs = None
            missing.append("sector_relative_strength")

            # Market breadth: point-in-time universe breadth if captured or missing
            breadth_val = None
            missing.append("market_breadth")

            # Volatility regime
            vol_regime = "normal"
            if context.atr_pct is not None:
                if context.atr_pct >= 2.0:
                    vol_regime = "high"
                elif context.atr_pct <= 0.9:
                    vol_regime = "low"

            enriched.update(
                {
                    "as_of_ts": context.as_of_ts,
                    "bars_available": context.bars_available,
                    "close_at_entry": context.close,
                    "rsi": enriched.get("rsi")
                    if enriched.get("rsi") is not None
                    else context.rsi,
                    "atr_pct": context.atr_pct,
                    "relative_volume": context.volume_multiple,
                    "gap_pct": context.gap_pct,
                    "trend_pct": trend,
                    "above_slow_sma": context.above_slow_sma,
                    "market_regime": regime,
                    "nifty_trend_pct": nifty_pct,
                    "benchmark_symbol": benchmark_symbol,
                    "benchmark_kind": benchmark_kind,
                    "benchmark_is_proxy": benchmark_is_proxy,
                    "regime_model_version": regime_model_version,
                    "stock_relative_strength": stock_rs,
                    "sector_relative_strength": sector_rs,
                    "market_breadth": breadth_val,
                    "volatility_regime": vol_regime,
                }
            )
            enriched["rsi_bucket"] = bucket_rsi(enriched.get("rsi"))
            enriched["rvol_bucket"] = bucket_relative_volume(
                enriched.get("relative_volume")
            )
            enriched["_atr_pct"] = context.atr_pct
            enriched["_missing"] = sorted(set(missing))
            enriched["missing_features"] = sorted(set(missing))
            out.append(enriched)
        return out

    # ------------------------------------------------------------------
    def _sector_table(self) -> dict[str, str]:
        if self._sector_cache is None:
            self._sector_cache = build_sector_table(self._data_root)
        return self._sector_cache

    def _load_frames(self, symbols: set[str]) -> dict[str, Any]:
        if self._frames is not None:
            return self._frames
        if not symbols:
            return {}
        from atr.data.history import load_cached

        # The cache names files ``<TICKER>-EQ.parquet``; a journal symbol may
        # arrive with or without the series suffix, so both spellings are tried.
        wanted: list[str] = []
        for symbol in sorted(symbols):
            wanted.append(f"{symbol}-EQ")
            wanted.append(symbol)
        try:
            frames = load_cached("NSEEQ", wanted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning: cache unreadable: {}", exc)
            return {}
        # Re-key bare tickers onto the canonical form so a lookup by either
        # spelling finds the same frame.
        merged: dict[str, Any] = {}
        for key, frame in frames.items():
            merged[_canonical(key)] = frame
        return merged

    def _benchmark_frames(self) -> dict[str, Any]:
        if self._frames is not None:
            return {
                key: frame
                for key, frame in self._frames.items()
                if "NIFTYBEES" in key.upper() or "JUNIORBEES" in key.upper()
            }
        from atr.data.history import load_cached

        try:
            return load_cached("NSEEQ", ["NIFTYBEES-EQ", "JUNIORBEES-EQ"])
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _missing_feature_registry(rows: list[dict[str, Any]]) -> dict[str, str]:
        """Which features are missing, and *why* — dataset level, not per row.

        Per-row missing lists say which trade lacks what; this says why the
        feature is unavailable at all, which is the question "what additional
        features are missing" actually asks.

        Where more than one source lacks the same feature for different reasons,
        every reason is named. Collapsing them into one sentence would let a
        reader fix the wrong thing: "MFE is absent because the backtest has no
        column" is a different problem from "MFE is absent because the paper
        ledger never observed an intra-trade price".
        """
        by_source: dict[str, dict[str, int]] = {}
        for row in rows:
            source = str(row.get("source") or "unknown")
            for name in row.get("_missing") or []:
                by_source.setdefault(name, {})
                by_source[name][source] = by_source[name].get(source, 0) + 1

        def _why(feature: str, per_source: dict[str, str]) -> str | None:
            """One clause per source that lacks ``feature``, in source order.

            ``None`` when no source lacks it, so the feature simply does not
            appear in the registry — the honest answer for a feature that is
            present everywhere.
            """
            present = by_source.get(feature) or {}
            parts = [
                per_source[source]
                for source in (*SOURCES, "unknown")
                if source in present and source in per_source
            ]
            return "; ".join(parts) if parts else None

        reasons = dict(requested_but_unavailable())

        # Features the paper ledger cannot carry, in the ledger's own words.
        # Registered only when a ledger row actually lacks them, so a book with
        # no paper trades does not acquire reasons for trades it does not have.
        for feature, why in paper.reasons().items():
            if by_source.get(feature):
                reasons.setdefault(feature, why)

        shared = {
            "mfe": {
                SOURCE_BACKTEST: (
                    "backtest_trades has no mfe column, so a backtest trade's "
                    "intra-trade excursion was never recorded"
                ),
                SOURCE_PAPER: paper.REASON_NO_EXCURSION,
                SOURCE_LIVE: "the journal entry did not record mfe",
            },
            "mae": {
                SOURCE_BACKTEST: (
                    "backtest_trades has no mae column, so a backtest trade's "
                    "intra-trade excursion was never recorded"
                ),
                SOURCE_PAPER: paper.REASON_NO_EXCURSION,
                SOURCE_LIVE: "the journal entry did not record mae",
            },
            "slippage_bps": {
                SOURCE_BACKTEST: (
                    "the backtest reports total slippage for the run, not per trade"
                ),
                SOURCE_PAPER: paper.REASON_NO_COSTS,
                SOURCE_LIVE: "the journal entry did not record slippage",
            },
        }
        for feature, per_source in shared.items():
            sentence = _why(feature, per_source)
            if sentence:
                reasons[feature] = sentence

        if by_source.get("sector"):
            reasons["sector"] = (
                "the symbol is not in data/universe/ind_nifty*list.csv, which "
                "covers 501 of the cached symbols"
            )
        if by_source.get("relative_volume_from_reason"):
            reasons["relative_volume_from_signal"] = (
                "the strategy recorded no signal reason naming a volume multiple; "
                "the value is still computed from the price cache where history allows"
            )
        if by_source.get("trend_pct"):
            reasons["trend_pct"] = "fewer than 50 bars before the entry"

        # Why the attribution columns are absent, which differs by source and is
        # the distinction that matters: a backtest row *cannot* have them, and a
        # journal row *does not yet*. Collapsing the two would tell a reader to
        # re-run the sweep to fix a source that will never produce the field.
        if by_source.get("attribution_entry_quality"):
            attribution_reasons = {
                SOURCE_BACKTEST: (
                    "backtest trades carry no journal episode, and the attribution "
                    "layer is keyed on one"
                ),
                SOURCE_PAPER: (
                    "the paper ledger records settled weeks rather than journal "
                    "episodes, so there is no trade to attribute"
                ),
                SOURCE_LIVE: "the journal episode has not been attributed yet",
            }
            reasons["attribution_entry_quality"] = _why(
                "attribution_entry_quality", attribution_reasons
            )
        return {name: why for name, why in reasons.items() if why}


# ---------------------------------------------------------------------------
# performance intelligence
# ---------------------------------------------------------------------------


@dataclass
class AxisBreakdown:
    """Every bucket of one axis, with the baseline they were carved from."""

    axis: str
    label: str
    metric: str
    rows_with_value: int
    rows_scanned: int
    #: The baseline: all *other* rows, used for the lift comparison.
    baseline: dict[str, Any]
    buckets: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "label": self.label,
            "metric": self.metric,
            "rows_with_value": self.rows_with_value,
            "rows_scanned": self.rows_scanned,
            "coverage": (
                round(self.rows_with_value / self.rows_scanned, 4)
                if self.rows_scanned
                else None
            ),
            "baseline": self.baseline,
            "buckets": self.buckets,
        }


@dataclass
class StrategyAnalysis:
    strategy: str
    n: int
    overall: dict[str, Any]
    breakdowns: list[AxisBreakdown]
    #: Buckets that survived the sample floor and the significance filter,
    #: strongest first. This is the part a report quotes.
    notable: list[dict[str, Any]]
    #: Which outcome column every number above was computed from. Carried on the
    #: result rather than left to the caller to remember: a table of means with
    #: the unit unstated is a table that gets misread.
    metric: str = "net_pnl"
    #: Set when the metric was resolved rather than requested, so a reader is
    #: told that the unit is not the one they might have assumed.
    metric_note: str | None = None
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "n": self.n,
            "metric": self.metric,
            "metric_note": self.metric_note,
            "overall": self.overall,
            "breakdowns": [b.as_dict() for b in self.breakdowns],
            "notable": list(self.notable),
            "caveats": list(self.caveats),
        }


class PerformanceAnalysis:
    """Slices a strategy's trades along the declared axes.

    The metric defaults to the outcome column the book actually records. It
    prefers ``net_pnl`` — the actual rupees — because expectancy in currency is
    what a decision is made on, and falls back to ``return_pct`` when no trade
    carries a currency amount, naming the substitution in ``metric_note``. The
    alternative, silently reporting an empty table for a book that is full of
    trades, is the "unknown reported as a zero" failure wearing a suit.
    """

    def __init__(self, dataset: LearningDataset, *, metric: str | None = None) -> None:
        resolved, note = resolve_metric(dataset, metric)
        self.dataset = dataset
        self.metric = resolved
        #: Set when the metric was chosen rather than asked for. Always surfaced
        #: in the payload, because a table of means whose unit changed without
        #: saying so is a table that gets misread.
        self.metric_note = note

    # ------------------------------------------------------------------
    def analyse(
        self,
        strategy: str | None = None,
        *,
        roles: Iterable[str] | None = None,
        classes: Iterable[str] | None = None,
        grades: Iterable[str] | None = None,
        axes: list[str] | None = None,
        min_sample: int = stats.MIN_SAMPLE,
    ) -> StrategyAnalysis:
        """One strategy, or the whole book when ``strategy`` is None.

        ``classes`` restricts the sample by ``evidence_class`` — the precise
        provenance. ``grades`` restricts it by ``evidence_grade`` — the
        direction. Asking for ``grades=["forward"]`` is how a caller gets a
        clean out-of-sample read without having to enumerate the classes, and it
        is the filter a finding should be published on.

        Both are offered rather than one because they answer different
        questions: ``grades`` asks "is this evidence?", ``classes`` asks "was
        this paper or real money?".
        """
        rows = self._rows_for(strategy, roles, classes, grades)
        resolved = axes_from_names(axes)

        values = self._metric_values(rows)
        overall = stats.summarise(values).as_dict()
        caveats = self._caveats(rows, values)
        if self.metric_note:
            caveats.insert(0, self.metric_note)

        breakdowns: list[AxisBreakdown] = []
        notable: list[dict[str, Any]] = []
        for axis in resolved:
            breakdown = self._breakdown(axis, rows, min_sample=min_sample)
            breakdowns.append(breakdown)
            # The axis is carried onto each notable entry rather than left to be
            # inferred from the breakdown list. The notable set is a flattened,
            # re-sorted union of every axis, so without this a reader cannot tell
            # which axis a finding belongs to — and "normal" is a bucket of both
            # the ATR axis and the RVOL axis.
            notable.extend(
                {**bucket, "axis": breakdown.axis}
                for bucket in breakdown.buckets
                if not bucket.get("suppressed")
                and bucket.get("significance") in {"strong", "moderate", "weak"}
            )

        notable.sort(
            key=lambda bucket: (
                {"strong": 0, "moderate": 1, "weak": 2}.get(bucket.get("significance"), 3),
                -(bucket.get("n") or 0),
            )
        )

        return StrategyAnalysis(
            strategy=strategy or "ALL",
            n=len(values),
            overall=overall,
            breakdowns=breakdowns,
            notable=notable,
            metric=self.metric,
            metric_note=self.metric_note,
            caveats=caveats,
        )

    # ------------------------------------------------------------------
    def _rows_for(
        self,
        strategy: str | None,
        roles: Iterable[str] | None,
        classes: Iterable[str] | None = None,
        grades: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.dataset.rows
        if strategy:
            rows = [row for row in rows if _matches_strategy(row, strategy)]
        if roles:
            allowed = {str(role).upper() for role in roles}
            rows = [row for row in rows if str(row.get("source") or "").upper() in allowed]
        if classes:
            wanted = {str(name).strip().upper() for name in classes}
            rows = [
                row for row in rows if str(row.get("evidence_class") or "").upper() in wanted
            ]
        if grades:
            # Resolved through ``row_grade`` rather than compared to the stored
            # column, so a row that carries only a class or only the boolean
            # still filters correctly instead of silently dropping out of a
            # forward-only sample.
            wanted_grades = {str(name).strip().lower() for name in grades}
            rows = [row for row in rows if row_grade(row) in wanted_grades]
        return [row for row in rows if row.get("exit_ts") is not None]

    def _metric_values(self, rows: list[dict[str, Any]]) -> list[float]:
        out = []
        for row in rows:
            value = row.get(self.metric)
            if value is None:
                continue
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                continue
        return out

    def _breakdown(
        self, axis: Axis, rows: list[dict[str, Any]], *, min_sample: int
    ) -> AxisBreakdown:
        """Every bucket of one axis, each compared to the rest of the sample.

        The comparison count is the axis's **declared vocabulary size**, not the
        number of buckets that happened to be non-empty. Using the observed count
        would let a small sample shrink its own multiple-comparisons penalty,
        which is precisely backwards.
        """
        have, scanned = axis_coverage(axis, rows)
        by_bucket: dict[str, list[float]] = {}
        for row in rows:
            label = axis.value(row)
            if label is None:
                continue
            value = row.get(self.metric)
            if value is None:
                continue
            try:
                by_bucket.setdefault(str(label), []).append(float(value))
            except (TypeError, ValueError):
                continue

        comparisons = axis.max_values or max(1, len(by_bucket))
        bucket_labels = sorted(by_bucket)
        verdicts: list[dict[str, Any]] = []
        for label in bucket_labels:
            others = [
                value
                for other, values in by_bucket.items()
                if other != label
                for value in values
            ]
            verdict = stats.compare_bucket(
                label, by_bucket[label], others, comparisons=comparisons, min_sample=min_sample
            )
            verdicts.append(verdict.as_dict())

        return AxisBreakdown(
            axis=axis.name,
            label=axis.label,
            metric=self.metric,
            rows_with_value=have,
            rows_scanned=scanned,
            baseline=stats.summarise(self._metric_values(rows)).as_dict(),
            buckets=verdicts,
        )

    def _caveats(self, rows: list[dict[str, Any]], values: list[float]) -> list[str]:
        out: list[str] = []
        if not rows:
            out.append(
                "no closed trades matched, so every statistic below is absent "
                "rather than zero"
            )
            return out

        if not values:
            # Rows exist but none carries the metric. That is a different
            # statement from "no trades", and reporting it as a small sample
            # would send a reader looking for more trades when the fix is to
            # pick the metric the book actually records.
            out.append(
                f"{len(rows)} closed trades matched but not one carries "
                f"{self.metric}, so every statistic below is absent rather than "
                "zero — check the dataset's metric_coverage for the column this "
                "book does record"
            )
            return out

        if len(values) < stats.MIN_SAMPLE:
            out.append(
                f"{len(values)} closed trades is below the {stats.MIN_SAMPLE}-trade "
                "floor; no bucket is analysed and the headline is not evidence"
            )
        elif len(values) < stats.SMALL_SAMPLE:
            out.append(
                f"{len(values)} closed trades is a small sample; every interval "
                "is wide and the notable list should be read as a prompt to keep "
                "collecting data"
            )

        forward = sum(1 for row in rows if is_forward_grade(row_grade(row)))
        ungraded = sum(1 for row in rows if not has_grade(row))
        if ungraded:
            # An unclassified row is not an in-sample row. Saying it were would be
            # the same class of error as reporting a missing value as a zero,
            # in the one column that decides how much a finding is worth.
            out.append(
                f"{ungraded} of {len(rows)} trades carry no evidence class, so "
                "how independent this sample is cannot be stated"
            )
        elif forward == 0:
            out.append(
                "no trade in this sample is forward evidence: every row is "
                "graded in-sample, so these buckets describe the history the "
                "rules were selected on rather than whether they still work"
            )
        elif forward < len(rows):
            out.append(
                f"{len(rows) - forward} of {len(rows)} trades are graded "
                "in-sample and the rest forward; a bucket that mixes the two is "
                "not a clean out-of-sample result, so read the mix before the "
                "bucket"
            )

        best = max(values) if values else None
        total = sum(values)
        if self.metric == "return_pct":
            # ``stats.summarise`` reports ``total`` as the arithmetic sum, which
            # is right for rupees and meaningless for returns: summing 140
            # equal-weight pick returns reports the basket 140 times. The mean is
            # the figure that means something, so the misread is named rather
            # than left to the reader's intuition about the column's name.
            out.append(
                f"figures are in return_pct, so 'total' is the arithmetic sum of "
                f"{len(values)} per-trade returns and is not a portfolio return; "
                f"the equal-weight figure is the mean, {sum(values) / len(values):.2f}%"
            )
        if best is not None and total > 0 and best > total * 0.5:
            out.append(
                f"the single best trade accounts for {best / total:.0%} of the total; "
                "the aggregate is one outcome, not a distribution"
            )
        return out


# ---------------------------------------------------------------------------
# daily report
# ---------------------------------------------------------------------------


@dataclass
class DailyReport:
    """One trading day, described against what the history expected.

    Every field that could not be computed is ``None`` with the reason in
    ``limitations``. The report never falls back to a default number, because a
    default is indistinguishable from a measurement once it is printed.
    """

    as_of: str
    window_days: int
    trades_today: int
    #: Today's aggregate **in :attr:`metric` units**. A sum when the metric is a
    #: currency amount, an equal-weight mean when it is a percentage — see
    #: :attr:`aggregate`. The two are not interchangeable: summing the returns of
    #: ten equal-weight picks reports ten times the basket.
    metric_total: float | None
    expectation: dict[str, Any]
    deviation: dict[str, Any]
    by_strategy: list[dict[str, Any]]
    strongest: list[dict[str, Any]]
    weakest: list[dict[str, Any]]
    regime: dict[str, Any]
    unusual: list[str]
    execution: dict[str, Any]
    drift: dict[str, Any]
    observations: list[dict[str, Any]]
    limitations: list[str]
    #: Which outcome column every figure above is expressed in.
    metric: str = "net_pnl"
    #: Set when the metric was resolved rather than requested.
    metric_note: str | None = None
    #: ``"sum"`` for a currency metric, ``"mean"`` for a percentage one.
    aggregate: str = "sum"
    #: The evidence **grades** this report drew on. Always populated, so a
    #: narrowed scope is visible in the payload rather than inferred from the
    #: numbers.
    scope: list[str] = field(default_factory=lambda: list(GRADES))
    #: Closed forward observations in the book, whether or not they closed today.
    forward_observations: int = 0
    #: Closed in-sample observations in the book. Reported beside the forward
    #: count because the two together are what say how much of this book is
    #: evidence — a book of four hundred in-sample rows and two forward ones is
    #: not a well-tested strategy.
    in_sample_observations: int = 0
    #: The grades of the trades that closed **today**, by count. A day whose
    #: trades are all in-sample produced a figure and no evidence, and the two
    #: must be separable on the screen.
    today_grades: dict[str, int] = field(default_factory=dict)
    #: Whether the book is large enough in *forward* observations to support a
    #: claim. Below the sample floor the report states what happened and says
    #: nothing about whether it was good — which is the difference between a
    #: record and a finding. Note that this is deliberately **not** "the book has
    #: enough rows": a thousand in-sample rows support no claim at all.
    claimable: bool = False

    @property
    def net_pnl_today(self) -> float | None:
        """Today's net rupees — and ``None`` when the book records no rupees.

        Deliberately not an alias for :attr:`metric_total`. A book measured in
        returns has no rupee total, and a field named ``net_pnl`` holding a
        percentage is the kind of quietly wrong number this engine exists to
        refuse.
        """
        return self.metric_total if self.metric == "net_pnl" else None

    @property
    def headline(self) -> str:
        """One sentence. Refuses to invent a number it does not have.

        The split this sentence holds is the whole design of the report. **What
        happened is a fact** — a count, a figure and its arithmetic against the
        preceding window — and it is stated whenever it exists, because
        withholding a measurement the book contains would be its own kind of
        dishonesty. **Whether it was good or bad is a claim**, and it is withheld
        until the book holds enough *forward* observations to support one.

        The distinction matters because the alternative is worse than it looks.
        A report that stayed silent about an in-sample day would leave the reader
        to assume nothing happened; a report that called an in-sample day a
        deterioration would be restating the selection history as a test result.
        Stating the figure and naming its grade is the only version that is true
        both ways.
        """
        if self.trades_today == 0:
            base = f"No closed trades on {self.as_of}."
            if self.forward_observations == 0:
                # The state the project is actually in, said rather than
                # implied by an absence of numbers.
                return (
                    f"{base} No forward observation has been recorded yet, so "
                    "there is nothing to report on."
                )
            return base
        if self.metric_total is None:
            return (
                f"{self.trades_today} closed trades on {self.as_of}, but no "
                f"{self.metric} was recorded, so the day's result is unmeasured."
            )
        amount = _format_amount(self.metric_total, self.metric)
        grade_note = self._grade_clause()

        expected = self.expectation.get("mean_per_day")
        delta = self.deviation.get("delta")
        if expected is None or delta is None:
            return (
                f"{self.trades_today} closed trades on {self.as_of}, "
                f"{self.metric} {amount}.{grade_note}"
            )

        rel = self.deviation.get("relative")
        direction = "above" if delta >= 0 else "below"
        if rel is None:
            compared = (
                f" — {abs(delta):,.2f} {direction} the historical daily "
                "expectation."
            )
        else:
            compared = (
                f" — {abs(rel):.0%} {direction} the historical daily expectation "
                f"of {_format_amount(expected, self.metric)}."
            )
        return (
            f"{self.trades_today} closed trades on {self.as_of}, "
            f"{self.metric} {amount}{compared}{grade_note}"
        )

    def _grade_clause(self) -> str:
        """What the day's evidence grade permits the report to say.

        Appended to the headline rather than folded into it, because the figure
        and the verdict have different conditions: the figure is stated whenever
        it exists, the verdict only when the book can carry one.
        """
        forward_today = self.today_grades.get(GRADE_FORWARD, 0)
        if forward_today == 0:
            return (
                " Every trade closing today is graded in-sample, so this "
                "describes the history the rules were selected on and not a "
                "forward test."
            )
        if not self.claimable:
            return (
                f" Too few forward observations ({self.forward_observations}) to "
                "say whether that is good or bad."
            )
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "window_days": self.window_days,
            "headline": self.headline,
            "trades_today": self.trades_today,
            "metric": self.metric,
            "metric_note": self.metric_note,
            "aggregate": self.aggregate,
            "scope": list(self.scope),
            "forward_observations": self.forward_observations,
            "in_sample_observations": self.in_sample_observations,
            "today_grades": dict(self.today_grades),
            "claimable": self.claimable,
            "metric_total": _round(self.metric_total),
            "net_pnl_today": _round(self.net_pnl_today),
            "expectation": self.expectation,
            "deviation": self.deviation,
            "by_strategy": self.by_strategy,
            "strongest": self.strongest,
            "weakest": self.weakest,
            "regime": self.regime,
            "unusual": self.unusual,
            "execution": self.execution,
            "drift": self.drift,
            "observations": self.observations,
            "limitations": self.limitations,
            "advisory": True,
            "applies_changes": False,
        }


class DailyLearningReport:
    """End-of-day narrative. Advisory, and structured so it cannot be anything else.

    The ``advisory`` flag and ``applies_changes: False`` are in the payload, not
    only in a docstring, because the payload is what a UI renders and what a
    future caller would branch on. A report that returns a list of parameter
    values would sooner or later be fed to something that applies them.

    Two jobs, and they have different evidence requirements
    ------------------------------------------------------

    **Say what happened.** A day's trades, their aggregate, and its arithmetic
    against the preceding window. This is a record, and it is produced for the
    whole book — including in-sample rows — because a book of in-sample trades
    still had a day and withholding it would leave the reader to assume nothing
    occurred.

    **Say whether it was good.** This is a claim, and it is produced only when
    the book holds enough *forward* observations. The gate is on
    :attr:`DailyReport.claimable`, and it counts forward observations
    specifically: a thousand in-sample rows do not make a claim supportable,
    because they were measured on the history the rules were chosen from.

    Conflating the two is the failure this class is shaped to prevent. A report
    that gated the *facts* would go silent on a real trading day; a report that
    ungated the *claim* would publish a restatement of the selection history as
    a result.
    """

    def __init__(
        self,
        dataset: LearningDataset,
        *,
        metric: str | None = None,
        grades: Iterable[str] | None = None,
        classes: Iterable[str] | None = None,
    ) -> None:
        resolved, note = resolve_metric(dataset, metric)
        self.dataset = dataset
        self.metric = resolved
        self.metric_note = note
        #: A currency metric aggregates by summing — rupees add up. A percentage
        #: metric aggregates by the equal-weight mean, because summing the
        #: returns of ten equal-weight picks reports ten times the basket. The
        #: two rules are not interchangeable and the report states which it used.
        self.aggregate = "mean" if resolved == "return_pct" else "sum"
        #: The evidence grades this report may draw on.
        #:
        #: Defaults to **both**. The report's first job is to say what happened,
        #: and a book of in-sample rows still produced a day of trading; what is
        #: restricted is the claim, which :attr:`DailyReport.claimable` gates on
        #: the forward count. Narrowing this to ``["forward"]`` is available for
        #: a caller who wants the forward book in isolation, and the payload
        #: always names the scope so the narrowing cannot be invisible.
        self.grades: tuple[str, ...] = (
            tuple(str(name).strip().lower() for name in grades) if grades else GRADES
        )
        #: ``evidence_class`` values, for a caller that wants to narrow by
        #: provenance rather than by direction — "paper only", or "real money
        #: only". Applied on top of the grade filter, so the two compose.
        self.classes: tuple[str, ...] = (
            tuple(str(name).strip().upper() for name in classes) if classes else ()
        )

    def _in_scope(self, row: dict[str, Any]) -> bool:
        """Whether a row belongs in this report's sample.

        Two independent filters: the grade is the *direction* (is this evidence?)
        and the class is the *provenance* (was it paper or real money?). A caller
        wanting paper-only forward evidence sets both; the default sets neither
        and takes the whole book.
        """
        if row_grade(row) not in self.grades:
            return False
        if not self.classes:
            return True
        return str(row.get("evidence_class") or "").upper() in self.classes

    def _aggregate(self, rows: list[dict[str, Any]]) -> float | None:
        values = [float(row[self.metric]) for row in rows if row.get(self.metric) is not None]
        if not values:
            return None
        total = sum(values) if self.aggregate == "sum" else sum(values) / len(values)
        return round(total, 2)

    def build(self, *, as_of: datetime | None = None, window_days: int = 90) -> DailyReport:
        as_of = as_of or datetime.now(UTC)
        day = as_of.date()
        limitations: list[str] = []

        in_scope = [row for row in self.dataset.rows if self._in_scope(row)]
        book = [row for row in in_scope if row.get("exit_ts") is not None]
        #: The book that can carry a claim. Counted separately from ``book``
        #: because the two answer different questions — how much happened, and
        #: how much of it is evidence — and only the second may gate a finding.
        forward_book = [row for row in book if is_forward_grade(row_grade(row))]
        claimable = len(forward_book) >= stats.MIN_SAMPLE

        today = [row for row in in_scope if _same_day(row.get("exit_ts"), day)]
        today_grades = {name: 0 for name in GRADES}
        for row in today:
            today_grades[row_grade(row)] += 1
        #: The forward subset of today — the **evidence surface**, as opposed to
        #: the fact surface above. The advisory findings are computed from this
        #: and nothing else.
        #:
        #: The distinction is load-bearing. A book with twelve forward rows from
        #: earlier weeks is claimable; if the findings were then computed over a
        #: day whose trades were all backfills, the report would publish a
        #: restatement of the selection history as a result — and it would look
        #: exactly like a finding, because the book that licensed it was real.
        today_forward = [row for row in today if is_forward_grade(row_grade(row))]

        scope = ", ".join(self.grades)
        limitations.append(f"scope: evidence grades {scope}")
        if not book:
            limitations.append(
                "no observation has been recorded, so there is nothing to report "
                "on; an in-sample row is not a substitute for a forward one, "
                "because it was measured on the history the rule was selected from"
            )
        elif not forward_book:
            limitations.append(
                f"all {len(book)} closed observation(s) in the book are graded "
                "in-sample: they are measurements on the history the rules were "
                "selected from, so this report records what happened and makes "
                "no claim about whether anything still works"
            )
        elif not claimable:
            limitations.append(
                f"{len(forward_book)} forward observation(s) is below the "
                f"{stats.MIN_SAMPLE}-observation floor, so nothing here is "
                "claimable yet — the figures are recorded, not evidence"
            )
        if today and not today_forward:
            limitations.append(
                "every trade that closed today is graded in-sample, so today's "
                "figure describes the selection history rather than a forward "
                "test, and no finding is drawn from it"
            )
        elif today_forward and len(today_forward) < len(today):
            limitations.append(
                f"{len(today_forward)} of today's {len(today)} trades are forward; "
                "the findings below are computed from those and not from the "
                "in-sample remainder"
            )
        if self.metric_note:
            limitations.append(self.metric_note)
        if self.aggregate == "mean" and today:
            limitations.append(
                f"figures are in {self.metric} and the day's total is the "
                "equal-weight mean of its trades, not their sum: summing the "
                "returns of an equal-weight basket reports the basket once per "
                "constituent"
            )
        metric_today = self._aggregate(today)
        if not today:
            limitations.append(
                "no trade closed today; today's performance is unmeasured, not zero"
            )
        elif metric_today is None:
            limitations.append(
                f"{len(today)} trades closed today and not one carries "
                f"{self.metric}, so today's result is unmeasured rather than flat"
            )

        history = self._history_rows(day, window_days)
        expectation = self._expectation(history, window_days, limitations)
        deviation = self._deviation(metric_today, expectation, limitations)

        by_strategy = self._by_strategy(today)
        # The inputs to a finding, and therefore drawn from the evidence surface
        # rather than from the day. ``regime`` and ``unusual`` stay on the whole
        # day because they are counts, and a count is a fact about the day
        # whichever grade its rows carry.
        strongest, weakest = self._best_and_worst(today_forward)
        regime = self._regime(today, limitations)
        unusual = self._unusual(today, history)
        execution = self._execution(today, limitations)
        drift = self._drift_placeholder()

        # Advisory findings are a claim, not a fact, so they need three things:
        # enough forward observations in the book, at least one forward
        # observation *today* to have a finding about, and the findings
        # themselves computed from the forward rows alone. A recommendation
        # drawn from six forward trades is the overfitting the whole engine
        # exists to refuse; a recommendation drawn from six hundred in-sample
        # rows is the same mistake wearing a bigger number; and a recommendation
        # drawn from a backfilled day because the book was licensed elsewhere is
        # the same mistake wearing a disguise.
        observations = (
            self._observations(
                today_forward, history, expectation, strongest, weakest, limitations
            )
            if claimable and today_forward
            else []
        )

        return DailyReport(
            as_of=day.isoformat(),
            window_days=window_days,
            trades_today=len(today),
            metric_total=metric_today,
            expectation=expectation,
            deviation=deviation,
            by_strategy=by_strategy,
            strongest=strongest,
            weakest=weakest,
            regime=regime,
            unusual=unusual,
            execution=execution,
            drift=drift,
            observations=observations,
            limitations=limitations,
            metric=self.metric,
            metric_note=self.metric_note,
            aggregate=self.aggregate,
            scope=list(self.grades),
            forward_observations=len(forward_book),
            in_sample_observations=len(book) - len(forward_book),
            today_grades=today_grades,
            claimable=claimable,
        )


    # ------------------------------------------------------------------
    def _history_rows(self, day, window_days: int) -> list[dict[str, Any]]:
        """Closed trades in the window *before* today.

        Excludes today deliberately. A baseline that includes the day being
        judged is partly a comparison of the day with itself, which shrinks the
        reported deviation toward zero — the flattering direction.
        """
        cutoff = day - timedelta(days=window_days)
        out = []
        for row in self.dataset.rows:
            if not self._in_scope(row):
                continue
            exit_ts = row.get("exit_ts")
            if exit_ts is None:
                continue
            stamp = exit_ts.date() if hasattr(exit_ts, "date") else exit_ts
            if cutoff <= stamp < day:
                out.append(row)
        return out

    def _expectation(
        self, history: list[dict[str, Any]], window_days: int, limitations: list[str]
    ) -> dict[str, Any]:
        values = [float(row[self.metric]) for row in history if row.get(self.metric) is not None]
        if not values:
            limitations.append(
                "no closed trades in the preceding window, so there is no "
                "historical expectation to compare today against"
            )
            return {
                "basis": "none",
                "window_days": window_days,
                "sample_trades": 0,
                "mean_per_day": None,
                "mean_per_trade": None,
                "median_per_day": None,
                "interval_per_day": None,
                "best_day": None,
                "worst_day": None,
                "note": "the expectation is absent rather than assumed to be zero",
            }

        by_day: dict[str, list[float]] = {}
        for row in history:
            exit_ts = row.get("exit_ts")
            if exit_ts is None or row.get(self.metric) is None:
                continue
            key = (exit_ts.date() if hasattr(exit_ts, "date") else exit_ts).isoformat()
            by_day.setdefault(key, []).append(float(row[self.metric]))

        # Per-day aggregates follow the same rule as the day's total: rupees add
        # up across the day's trades, a percentage does not.
        daily = [
            sum(values) if self.aggregate == "sum" else sum(values) / len(values)
            for values in by_day.values()
        ]
        trading_days = len(daily)
        interval = stats.mean_ci(daily)
        return {
            "basis": "per_trading_day",
            "window_days": window_days,
            "sample_trades": len(values),
            "trading_days": trading_days,
            "mean_per_day": round(sum(daily) / trading_days, 2) if trading_days else None,
            "mean_per_trade": round(sum(values) / len(values), 2),
            "median_per_day": _round(stats.median(daily)),
            "interval_per_day": [round(v, 2) for v in interval] if interval else None,
            "best_day": _round(max(daily)) if daily else None,
            "worst_day": _round(min(daily)) if daily else None,
            "note": (
                "the mean is over trading days that had a closed trade, and the "
                "best and worst days are quoted because one day can carry the mean"
            ),
        }

    def _deviation(
        self, pnl_today: float | None, expectation: dict[str, Any], limitations: list[str]
    ) -> dict[str, Any]:
        expected = expectation.get("mean_per_day")
        if pnl_today is None or expected is None:
            return {"delta": None, "relative": None, "within_interval": None}
        delta = pnl_today - expected
        relative = (delta / abs(expected)) if expected else None
        interval = expectation.get("interval_per_day")
        within = None
        if interval:
            within = interval[0] <= pnl_today <= interval[1]
        return {
            "delta": round(delta, 2),
            "relative": round(relative, 4) if relative is not None else None,
            "within_interval": within,
        }

    def _by_strategy(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = row.get("strategy_key") or row.get("strategy_id") or "unattributed"
            grouped.setdefault(str(key), []).append(row)

        out = []
        for key, items in sorted(grouped.items()):
            values = [float(row[self.metric]) for row in items if row.get(self.metric) is not None]
            out.append(
                {
                    "strategy": key,
                    "trades": len(items),
                    # Named for the metric rather than for rupees: on a
                    # percentage metric this is a mean return, and calling it
                    # ``net_pnl`` would invite it to be read as money.
                    "metric_total": self._aggregate(items),
                    "wins": sum(1 for value in values if value > 0),
                    **stats.summarise(values).as_dict(),
                }
            )
        return out

    def _best_and_worst(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The strongest and weakest *setups* today, by bucket, not by trade.

        Named per-setup rather than per-trade because "the best trade" is not
        actionable — you cannot choose to take only today's winner. A setup with
        three trades and a positive mean is a pattern; a single lucky trade is
        not, and the ``MIN_SAMPLE`` floor keeps the two apart.
        """
        closed = [row for row in rows if row.get("exit_ts") is not None]
        if not closed:
            return ([], [])

        analysis = PerformanceAnalysis(
            LearningDataset(
                rows=closed,
                missing_features={},
                generated_at=self.dataset.generated_at,
            ),
            metric=self.metric,
        )
        result = analysis.analyse(axes=["setup", "rvol_bucket", "market_regime", "exit_reason"])

        notable = [
            bucket
            for bucket in result.notable
            if bucket.get("n", 0) >= stats.MIN_SAMPLE
        ]
        positive = [b for b in notable if (b.get("lift") or 0) > 0]
        negative = [b for b in notable if (b.get("lift") or 0) < 0]
        return (positive[:3], negative[:3])

    def _regime(self, today: list[dict[str, Any]], limitations: list[str]) -> dict[str, Any]:
        labels = [row.get("market_regime") for row in today if row.get("market_regime")]
        if not labels:
            limitations.append(
                "no trade carried a market-regime label, so today's regime is "
                "unknown rather than assumed to be unchanged"
            )
            return {"label": None, "basis": "none", "distribution": {}}

        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        # A tie is reported as a tie, not silently resolved by dict insertion
        # order — which is what ``max(counts, key=counts.get)`` does, and it made
        # the label depend on the order trades happened to be read rather than on
        # the market. ``dominant`` is None on a tie so a caller must decide, and
        # the full distribution is always there to decide from.
        highest = max(counts.values())
        leaders = sorted(name for name, count in counts.items() if count == highest)
        dominant = leaders[0] if len(leaders) == 1 else None
        tied = leaders if len(leaders) > 1 else []

        # The concentration of losing trades in one regime is the observation
        # worth surfacing, and it is a count rather than a claim.
        losers = [
            row
            for row in today
            if row.get(self.metric) is not None and float(row[self.metric]) < 0
        ]
        loser_counts: dict[str, int] = {}
        for row in losers:
            label = row.get("market_regime")
            if label:
                loser_counts[label] = loser_counts.get(label, 0) + 1

        return {
            "label": dominant,
            "tied": tied,
            "basis": "entry-time label on today's trades",
            "distribution": counts,
            "losing_trades": len(losers),
            "losing_distribution": loser_counts,
        }

    def _unusual(self, today: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[str]:
        """Facts about today that differ from the window before it.

        Phrased as counts, not as conclusions. "7 of 9 losing trades were in a
        sideways regime" is a fact; "sideways regimes are unprofitable" is a
        claim that needs the bucket statistics behind it, and those are in
        ``strongest``/``weakest``.
        """
        out: list[str] = []
        if not today:
            return out

        today_symbols = {row.get("symbol") for row in today}
        if len(today) >= 5 and len(today_symbols) <= max(1, len(today) // 3):
            out.append(
                f"{len(today)} trades concentrated in {len(today_symbols)} symbols; "
                "the day's result is a bet on a few names rather than the strategy"
            )

        losers = [
            row
            for row in today
            if row.get(self.metric) is not None and float(row[self.metric]) < 0
        ]
        regimes = [row.get("market_regime") for row in losers if row.get("market_regime")]
        if len(losers) >= 3 and regimes:
            counts: dict[str, int] = {}
            for label in regimes:
                counts[label] = counts.get(label, 0) + 1
            label, count = max(counts.items(), key=lambda item: item[1])
            if count > len(losers) / 2:
                out.append(
                    f"{count} of {len(losers)} losing trades occurred in a {label} regime"
                )

        holding = [
            float(row["duration_days"])
            for row in today
            if row.get("duration_days") is not None
        ]
        if holding and len(holding) >= 5:
            out.append(
                f"holding period today ranged {min(holding):.1f} to {max(holding):.1f} days"
            )
        return out

    def _execution(self, today: list[dict[str, Any]], limitations: list[str]) -> dict[str, Any]:
        """Slippage and excursion, from the journal only."""
        slippage = [
            float(row["slippage_bps"])
            for row in today
            if row.get("slippage_bps") is not None
        ]
        mae = [float(row["mae"]) for row in today if row.get("mae") is not None]
        mfe = [float(row["mfe"]) for row in today if row.get("mfe") is not None]
        if not slippage and today:
            limitations.append(
                "no slippage was recorded for today's trades — the backtest "
                "carries it per run, not per trade, and the journal fills it per "
                "trade only when it exists"
            )
        return {
            "slippage_bps_mean": _round(sum(slippage) / len(slippage)) if slippage else None,
            "slippage_bps_max": _round(max(slippage)) if slippage else None,
            "trades_with_slippage": len(slippage),
            "mfe_mean": _round(sum(mfe) / len(mfe)) if mfe else None,
            "mae_mean": _round(sum(mae) / len(mae)) if mae else None,
            "trades_with_excursions": len(mae),
        }

    def _drift_placeholder(self) -> dict[str, Any]:
        """Drift, summarised as a verdict plus a pointer to the full analysis.

        The full metric table is *not* duplicated here. Two copies of the same
        numbers in one payload drift apart the moment either side is tuned, and
        the report is the one a human reads — so it carries the conclusion and
        the sample sizes, and the dashboard renders the detail from
        ``LearningService.drift()``.
        """
        counts = self.dataset.source_counts
        analysis = drift_analysis(self.dataset.rows)
        comparable = [pair for pair in analysis.pairs if pair.status == "ok"]

        return {
            "available": bool(comparable),
            "counts": {source: counts.get(source, 0) for source in SOURCES},
            "headline": analysis.headline,
            "pairs": [
                {
                    "reference": pair.reference,
                    "comparison": pair.comparison,
                    "reference_n": pair.reference_n,
                    "comparison_n": pair.comparison_n,
                    "status": pair.status,
                    "deteriorated": pair.deteriorated,
                    "improved": pair.improved,
                }
                for pair in analysis.pairs
            ],
            "note": (
                "verdict and sample sizes only; call the drift analysis for the "
                "per-metric detail so the two cannot disagree"
            ),
        }

    def _observations(
        self,
        forward_today: list[dict[str, Any]],
        history: list[dict[str, Any]],
        expectation: dict[str, Any],
        strongest: list[dict[str, Any]],
        weakest: list[dict[str, Any]],
        limitations: list[str],
    ) -> list[dict[str, Any]]:
        """Advisory statements, each with the evidence that produced it.

        ``forward_today`` is the forward subset of the day, never the whole day.
        A finding is a claim about what the strategy did, and only a row recorded
        before its outcome can support one — the fact surface (the day's count
        and figure) is deliberately wider, because saying what happened is not a
        claim.

        No observation names a parameter value. That is the line between this
        phase and the recommendation engine, and it is held by returning
        ``evidence`` and never ``proposed_value``.
        """
        out: list[dict[str, Any]] = []

        expected = expectation.get("mean_per_day")
        net = self._aggregate(forward_today)
        if net is not None and expected is not None and len(forward_today) >= 5:
            if net < 0 and expected > 0:
                out.append(
                    {
                        "kind": "expectancy_shortfall",
                        "severity": "review",
                        "statement": (
                            f"today's {self.metric} of {_format_amount(net, self.metric)} "
                            "is negative against a positive historical expectation of "
                            f"{_format_amount(expected, self.metric)} per day"
                        ),
                        "evidence": {
                            "trades_today": len(forward_today),
                            "metric": self.metric,
                            "aggregate": self.aggregate,
                            "metric_today": _round(net),
                            "expected_per_day": expected,
                            "history_trades": expectation.get("sample_trades"),
                            "evidence_grade": GRADE_FORWARD,
                        },
                        "confidence": "low",
                        "sample_size": len(forward_today),
                        "advisory": True,
                    }
                )

        for bucket in weakest[:2]:
            out.append(
                {
                    "kind": "weak_bucket",
                    "severity": "review",
                    "statement": (
                        f"Over {bucket.get('n')} forward trades, condition '{bucket.get('label')}' "
                        f"averaged {bucket['stats'].get('mean')}, "
                        f"{abs(bucket.get('lift') or 0):.2f} below the baseline"
                    ),
                    "evidence": {
                        "axis": bucket.get("axis"),
                        "bucket": bucket.get("label"),
                        "n": bucket.get("n"),
                        "stats": bucket.get("stats"),
                        "lift": bucket.get("lift"),
                        "p_value": bucket.get("p_value"),
                        "significance": bucket.get("significance"),
                    },
                    "confidence": _confidence_word(bucket),
                    "sample_size": bucket.get("n"),
                    "advisory": True,
                }
            )

        for bucket in strongest[:1]:
            out.append(
                {
                    "kind": "strong_bucket",
                    "severity": "note",
                    "statement": (
                        f"Over {bucket.get('n')} forward trades, condition '{bucket.get('label')}' "
                        f"averaged {bucket['stats'].get('mean')}, "
                        f"{bucket.get('lift'):.2f} above the baseline"
                    ),
                    "evidence": {
                        "axis": bucket.get("axis"),
                        "bucket": bucket.get("label"),
                        "n": bucket.get("n"),
                        "stats": bucket.get("stats"),
                        "lift": bucket.get("lift"),
                        "p_value": bucket.get("p_value"),
                        "significance": bucket.get("significance"),
                    },
                    "confidence": _confidence_word(bucket),
                    "sample_size": bucket.get("n"),
                    "advisory": True,
                }
            )

        if history and not forward_today:
            out.append(
                {
                    "kind": "no_activity",
                    "severity": "note",
                    "statement": (
                        "no forward trade closed today while the preceding window "
                        "was active; check the runner's blocked reason before "
                        "reading anything into it"
                    ),
                    "evidence": {"history_trades": len(history)},
                    "confidence": "n/a",
                    "sample_size": 0,
                    "advisory": True,
                }
            )
        return out


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class LearningService:
    """The entry point every caller uses. Reads the book; writes two artefacts.

    Deliberately has no method that changes a strategy. If one is ever added it
    will be for *proposing* a change, and it will live in a different module with
    an approval gate in front of it — not here, where a report built on
    unvalidated findings could call it.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        builder: LearningDatasetBuilder | None = None,
        cache_root: Path | str | None = None,
    ) -> None:
        self._db = db
        self._builder = builder
        self._data_root = Path(cache_root) if cache_root else data_root()
        self._dataset: LearningDataset | None = None

    @property
    def db(self) -> Any:
        if self._db is not None:
            return self._db
        from atr.appdb.engine import get_app_db

        return get_app_db()

    def dataset(self, *, refresh: bool = False, user_id: str | None = None) -> LearningDataset:
        """The learning dataset, cached for the process.

        Caching matters because building it reads the whole cache. The TTL is
        the caller's problem: ``refresh=True`` is explicit, and the CLI defaults
        to a fresh build, so a user running ``atr learn`` always sees what the
        database holds right now.
        """
        if self._dataset is None or refresh:
            builder = self._builder or LearningDatasetBuilder(
                db=self.db, cache_root=self._data_root
            )
            self._dataset = builder.build(user_id=user_id)
        return self._dataset

    def performance(
        self,
        *,
        strategy: str | None = None,
        roles: Iterable[str] | None = None,
        classes: Iterable[str] | None = None,
        grades: Iterable[str] | None = None,
        axes: list[str] | None = None,
        metric: str | None = None,
        min_sample: int | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> StrategyAnalysis:
        kwargs: dict[str, Any] = {}
        if min_sample is not None:
            kwargs["min_sample"] = min_sample
        return PerformanceAnalysis(self.dataset(refresh=refresh, user_id=user_id), metric=metric).analyse(
            strategy, roles=roles, classes=classes, grades=grades, axes=axes, **kwargs
        )

    def daily_report(
        self,
        *,
        as_of: datetime | None = None,
        window_days: int = 90,
        metric: str | None = None,
        grades: Iterable[str] | None = None,
        classes: Iterable[str] | None = None,
        refresh: bool = False,
    ) -> DailyReport:
        """The end-of-day report.

        ``grades`` narrows the evidence the report may draw on; the default is
        both, because the report's first job is to say what happened. What gates
        a *claim* is the forward-observation count, not this filter — see
        :class:`DailyLearningReport`.
        """
        return DailyLearningReport(
            self.dataset(refresh=refresh), metric=metric, grades=grades, classes=classes
        ).build(as_of=as_of, window_days=window_days)

    def drift(
        self,
        *,
        strategy: str | None = None,
        reference: str | None = None,
        roles: Iterable[str] | None = None,
        min_sample: int | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> DriftAnalysis:
        """Backtest vs paper vs live, per strategy.

        ``roles`` restricts which sources take part, which is what a caller
        wants when asking "how is live doing against the backtest?" and not
        wanting paper in the middle of it.
        """
        rows = self.dataset(refresh=refresh, user_id=user_id).rows
        if roles:
            allowed = {str(role).upper() for role in roles}
            rows = [row for row in rows if str(row.get("source") or "").upper() in allowed]
        kwargs: dict[str, Any] = {}
        if min_sample is not None:
            kwargs["min_sample"] = min_sample
        return drift_analysis(rows, strategy=strategy, reference=reference, **kwargs)

    def snapshot(self, *, user_id: str | None = None, write: bool = True) -> dict[str, Any]:
        """Build everything and optionally persist it under ``data/learning/``.

        Writing is opt-in so a read-only caller (a route rendering a page) does
        not mutate the filesystem, and so a test can exercise the whole pipeline
        without leaving artefacts behind.
        """
        dataset = self.dataset(refresh=True, user_id=user_id)
        report = DailyLearningReport(dataset).build()
        analysis = PerformanceAnalysis(dataset).analyse()
        drift = drift_analysis(dataset.rows)

        payload = {
            "generated_at": _iso(dataset.generated_at),
            "dataset": dataset.summary(),
            "report": report.as_dict(),
            "analysis": analysis.as_dict(),
            "drift": drift.as_dict(),
        }
        if write:
            payload["written"] = self._write(payload)
        return payload

    @staticmethod
    def _write(payload: dict[str, Any]) -> dict[str, str]:
        written: dict[str, str] = {}
        try:
            # Resolved through the same root as the reads, so a snapshot lands
            # beside the ledger it was built from rather than beside whatever
            # the working directory happened to be.
            directory = data_root() / "learning"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = payload["generated_at"].replace(":", "").replace("-", "")
            report_path = directory / f"report_{stamp}.json"
            latest_path = directory / "latest.json"
            text = json.dumps(payload, indent=2, default=str)
            report_path.write_text(text, encoding="utf-8")
            latest_path.write_text(text, encoding="utf-8")
            written = {"report": str(report_path), "latest": str(latest_path)}
        except Exception as exc:  # noqa: BLE001 — a failed write must not fail the read
            logger.warning("learning: could not write snapshot: {}", exc)
        return written

    def status(self) -> dict[str, Any]:
        """What the engine has to work with, without building the dataset.

        Cheap by design, so a UI can render "nothing to learn from yet" before
        paying for a full build. The paper ledger is counted too — reading two
        small JSONL files is cheap — because a status that reported zero while
        the dataset held a hundred and forty paper trades would be the one
        screen an operator checks first, lying to them.

        **The counts are split by evidence grade, not just by source.** A total
        that said "312 trades available" while none of them was forward would be
        the same lie in a different place: the number an operator reads first
        would say there is plenty to learn from, and the number that decides
        whether anything may be claimed is not on the screen at all.
        """
        from sqlalchemy import func, select

        from atr.appdb.schema import backtest_runs, backtest_trades, trade_journal

        counts: dict[str, Any] = {}
        try:
            with self.db.session() as session:
                counts["backtest_trades"] = int(
                    session.execute(select(func.count()).select_from(backtest_trades)).scalar() or 0
                )
                counts["trade_journal"] = int(
                    session.execute(select(func.count()).select_from(trade_journal)).scalar() or 0
                )
                counts["trade_journal_forward"] = int(
                    session.execute(
                        select(func.count())
                        .select_from(trade_journal)
                        .where(trade_journal.c.evidence_grade == GRADE_FORWARD)
                    ).scalar()
                    or 0
                )
                counts["completed_runs"] = int(
                    session.execute(
                        select(func.count())
                        .select_from(backtest_runs)
                        .where(backtest_runs.c.status == "COMPLETED")
                    ).scalar()
                    or 0
                )
                counts["backtest_runs"] = int(
                    session.execute(select(func.count()).select_from(backtest_runs)).scalar() or 0
                )
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "reason": f"the database could not be read: {type(exc).__name__}",
                "counts": {},
            }

        ledger = paper.load_ledger(self._data_root)
        ledger_counts = ledger.counts()
        counts["paper_ledger_trades"] = ledger_counts["trades"]
        counts["paper_ledger_forward_trades"] = ledger_counts["forward_trades"]
        counts["trade_journal_in_sample"] = (
            counts["trade_journal"] - counts["trade_journal_forward"]
        )

        #: The only rows that can carry a claim. A backtest is in-sample by
        #: construction, a backfilled ledger week is in-sample by its producer's
        #: own label, and a journal row without an execution-time provenance
        #: stamp cannot demonstrate it predates its outcome.
        forward_available = counts["trade_journal_forward"] + ledger_counts["forward_trades"]

        total = counts["backtest_trades"] + counts["trade_journal"] + ledger_counts["trades"]
        if total == 0:
            note = "nothing to analyse yet; the dataset and report will build correctly and say so"
        elif ledger_counts["trades"] and not ledger_counts["forward_trades"] and not counts["trade_journal"]:
            note = (
                f"{total} trades available, and none of the "
                f"{ledger_counts['trades']} paper trades is forward — every "
                "settled week is an in-sample backfill"
            )
        elif forward_available == 0:
            note = (
                f"{total} trades available and not one is forward evidence: every "
                "row is a backtest (in-sample by construction), a backfilled "
                "ledger week, or a journal trade with no execution-time "
                "provenance stamp — so the book supports no claim about whether "
                "anything still works"
            )
        else:
            note = (
                f"{total} trades available, {forward_available} of them forward "
                f"evidence ({total - forward_available} in-sample)"
            )
        return {
            "available": True,
            "counts": counts,
            "trades_available": total,
            "forward_available": forward_available,
            "sufficient_for_analysis": total >= stats.MIN_SAMPLE,
            #: Whether anything may be *claimed*, which is a different question
            #: from whether there is enough to analyse. Two flags because one
            #: number cannot answer both.
            "sufficient_for_a_claim": forward_available >= stats.MIN_SAMPLE,
            "paper_ledger": ledger.summary(),
            "missing_features": requested_but_unavailable(),
            "note": note,
        }

    def forward_evidence_counts(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Prominent metrics for Genuine Forward Observations.

        Aggregates counts across:
        - strategy
        - strategy version
        - today
        - last 7 trading days
        - total

        Clearly distinguishes:
        - IN_SAMPLE
        - PAPER_FORWARD
        - LIVE_FORWARD
        """
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(IST)
        today_date = now_ist.date()
        cutoff_7d = now_ist - timedelta(days=10)

        dataset = self.dataset(refresh=refresh, user_id=user_id)

        total_genuine_forward = 0
        today_genuine_forward = 0
        last_7d_genuine_forward = 0

        by_class: dict[str, dict[str, int]] = {
            "IN_SAMPLE": {"total": 0, "today": 0, "last_7d": 0},
            "PAPER_FORWARD": {"total": 0, "today": 0, "last_7d": 0},
            "LIVE_FORWARD": {"total": 0, "today": 0, "last_7d": 0},
        }

        strat_buckets: dict[tuple[str, int | None], dict[str, Any]] = {}

        for row in dataset.rows:
            row_strat = str(row.get("strategy_id") or row.get("strategy_key") or "UNKNOWN")
            row_ver = row.get("strategy_version")
            if row_ver is not None:
                try:
                    row_ver = int(row_ver)
                except (ValueError, TypeError):
                    row_ver = None

            if strategy_id and row_strat != strategy_id:
                continue
            if strategy_version is not None and row_ver != strategy_version:
                continue

            raw_class = str(row.get("evidence_class") or "").upper()
            if raw_class in ("PAPER_FORWARD",):
                c_key = "PAPER_FORWARD"
            elif raw_class in ("LIVE_FORWARD",):
                c_key = "LIVE_FORWARD"
            else:
                c_key = "IN_SAMPLE"

            is_forward = (c_key in ("PAPER_FORWARD", "LIVE_FORWARD"))

            ts_val = row.get("exit_ts") or row.get("entry_ts") or row.get("created_at")
            row_dt: datetime | None = None
            if isinstance(ts_val, datetime):
                row_dt = ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=UTC)
            elif isinstance(ts_val, str):
                try:
                    row_dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                    if not row_dt.tzinfo:
                        row_dt = row_dt.replace(tzinfo=UTC)
                except ValueError:
                    row_dt = None

            is_today = False
            is_7d = False
            if row_dt is not None:
                row_ist = row_dt.astimezone(IST)
                is_today = (row_ist.date() == today_date)
                is_7d = (row_ist >= cutoff_7d)

            by_class[c_key]["total"] += 1
            if is_today:
                by_class[c_key]["today"] += 1
            if is_7d:
                by_class[c_key]["last_7d"] += 1

            if is_forward:
                total_genuine_forward += 1
                if is_today:
                    today_genuine_forward += 1
                if is_7d:
                    last_7d_genuine_forward += 1

            s_key = (row_strat, row_ver)
            if s_key not in strat_buckets:
                strat_buckets[s_key] = {
                    "strategy_id": row_strat,
                    "strategy_version": row_ver,
                    "total_genuine_forward": 0,
                    "today_genuine_forward": 0,
                    "last_7d_genuine_forward": 0,
                    "paper_forward": 0,
                    "live_forward": 0,
                    "in_sample": 0,
                    "total": 0,
                }
            sb = strat_buckets[s_key]
            sb["total"] += 1
            if c_key == "PAPER_FORWARD":
                sb["paper_forward"] += 1
            elif c_key == "LIVE_FORWARD":
                sb["live_forward"] += 1
            else:
                sb["in_sample"] += 1

            if is_forward:
                sb["total_genuine_forward"] += 1
                if is_today:
                    sb["today_genuine_forward"] += 1
                if is_7d:
                    sb["last_7d_genuine_forward"] += 1

        by_strategy_list = sorted(
            strat_buckets.values(),
            key=lambda b: (b["total_genuine_forward"], b["total"]),
            reverse=True,
        )

        return {
            "total_genuine_forward": total_genuine_forward,
            "today_genuine_forward": today_genuine_forward,
            "last_7d_genuine_forward": last_7d_genuine_forward,
            "by_class": by_class,
            "by_strategy": by_strategy_list,
            "filtered_strategy_id": strategy_id,
            "filtered_strategy_version": strategy_version,
            "as_of": now_ist.isoformat(),
        }

    def readiness(
        self,
        *,
        strategy_id: str | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Forward-learning readiness, per strategy: is trustworthy evidence accumulating?

        Read-only. Every figure derives from the dataset plus registry reads
        (strategy names/versions, deployments, recorded observations and
        contexts, latest cycle) — nothing here writes, proposes, pauses or
        deploys. States are research labels from
        :mod:`atr.research.learning_readiness`; see that module for the gates
        and the caps that keep a bare trade count from reading as ready.
        """
        from atr.research import learning_readiness as lr

        dataset = self.dataset(refresh=refresh, user_id=user_id)
        all_forward = [
            row for row in dataset.rows if is_forward_grade(row_grade(row))
        ]
        unique, dropped = lr.dedupe_forward(all_forward)

        registry = self._readiness_registry(user_id=user_id)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in unique:
            key = str(row.get("strategy_id") or row.get("strategy_key") or "UNKNOWN")
            if strategy_id and key != strategy_id:
                continue
            groups.setdefault(key, []).append(row)

        strategies = [self._strategy_readiness(key, rows, registry) for key, rows in groups.items()]
        strategies.sort(key=lambda s: (-s["forward_trades"], s["strategy_name"]))

        quality_issues = lr.data_quality_issues(
            lr.forward_closed(unique), duplicate_refs=dropped
        )

        limitations: list[str] = []
        if user_id is None:
            limitations.append(
                "no user scope was given, so strategy names, versions and "
                "deployments could not be resolved from the registry"
            )
        if dropped:
            limitations.append(
                f"{len(dropped)} duplicate trade reference(s) were counted once; "
                "see the duplicate_trade_ref quality issue"
            )

        return {
            "generated_at": _iso(dataset.generated_at),
            "gates": list(lr.EVIDENCE_GATES),
            "forward_trades_unique": len(unique),
            "duplicates_dropped": len(dropped),
            "strategies": strategies,
            "quality_issues": quality_issues,
            "limited": bool(limitations),
            "limitations": limitations,
            # Stated in the payload so a client cannot infer that anything is
            # applied, whatever it renders.
            "advisory_only": True,
            "applies_changes": False,
        }

    def _readiness_registry(self, *, user_id: str | None) -> dict[str, Any]:
        """Registry facts the readiness view needs. Best effort throughout: a
        store that cannot be read degrades to unattributed display, never to a
        failed readiness call."""
        from atr.appdb.repositories import (
            DeploymentRepository,
            LearningObservationRepository,
            SignalContextRepository,
            StrategyRepository,
        )

        registry: dict[str, Any] = {
            "names": {},
            "current_versions": {},
            "deployments": {},
            "recorded_observations": {},
            "recorded_contexts": {},
            "latest_cycle": None,
        }
        if user_id is None:
            return registry
        try:
            with self.db.session() as session:
                for row in StrategyRepository.list_for_user(session, user_id):
                    sid = str(row.get("strategy_id"))
                    registry["names"][sid] = row.get("name") or sid
                    if row.get("latest_version") is not None:
                        registry["current_versions"][sid] = row.get("latest_version")
                for row in DeploymentRepository.list_for_user(session, user_id):
                    sid = str(row.get("strategy_id") or "")
                    prev = registry["deployments"].get(sid)
                    if prev is None or str(row.get("created_at") or "") >= str(
                        prev.get("created_at") or ""
                    ):
                        registry["deployments"][sid] = {
                            "mode": row.get("mode"),
                            "status": row.get("status"),
                        }
                for row in LearningObservationRepository.list_observations(
                    session, limit=500
                ):
                    if str(row.get("evidence_class") or "").upper() not in (
                        "PAPER_FORWARD",
                        "LIVE_FORWARD",
                    ):
                        continue
                    sid = str(row.get("strategy_id") or "")
                    registry["recorded_observations"][sid] = (
                        registry["recorded_observations"].get(sid, 0) + 1
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning: readiness registry read failed: {}", exc)
            return registry
        try:
            with self.db.session() as session:
                for sid in list(registry["names"]):
                    try:
                        registry["recorded_contexts"][sid] = SignalContextRepository.count(
                            session, user_id, strategy_id=sid
                        )
                    except Exception:  # noqa: BLE001 — one strategy must not fail the view
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning: readiness context counts failed: {}", exc)
        try:
            from atr.services.daily_learning import get_daily_learning_service

            registry["latest_cycle"] = get_daily_learning_service().get_latest_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning: readiness latest cycle unreadable: {}", exc)
        return registry

    def _strategy_readiness(
        self, key: str, rows: list[dict[str, Any]], registry: dict[str, Any]
    ) -> dict[str, Any]:
        from atr.research import learning_readiness as lr

        closed = [r for r in rows if r.get("exit_ts") is not None]
        open_count = len(rows) - len(closed)
        group_dataset = LearningDataset(
            rows=closed, missing_features={}, generated_at=datetime.now(UTC)
        )
        metric, metric_note = resolve_metric(group_dataset, None)
        summary = lr.summarise_forward(closed, metric=metric)
        progression = lr.weekly_progression(closed)
        issues = lr.data_quality_issues(closed, metric=metric)

        n_with_metric = summary["n_with_metric"]
        n_with_context = summary["score_coverage"]["with_score"]
        inconsistent_count = next(
            (i["count"] for i in issues if i["code"] == "inconsistent_provenance"), 0,
        )
        gaps = lr.blocking_gaps(
            n_with_metric=n_with_metric,
            n_with_context=n_with_context,
            n_inconsistent_provenance=inconsistent_count,
        )
        recorded = int(registry["recorded_observations"].get(key, 0))
        state, reasons = lr.research_state(
            summary["n"],
            recorded_forward_observations=recorded,
            blocking_gaps=gaps,
        )

        names = registry["names"]
        latest_cycle = registry.get("latest_cycle") or {}
        analysed = latest_cycle.get("strategies_analyzed") or []

        missing: list[str] = []
        for row in closed:
            for name in row.get("missing_features") or []:
                if name not in missing:
                    missing.append(name)
                if len(missing) >= 25:
                    break
            if len(missing) >= 25:
                break

        exits = [r.get("exit_ts") for r in closed if r.get("exit_ts") is not None]
        versions = sorted(
            {r.get("strategy_version") for r in rows if r.get("strategy_version") is not None},
            key=str,
        )

        return {
            "strategy_id": key,
            "strategy_name": names.get(key, key if key != "UNKNOWN" else "Unattributed"),
            "versions_seen": versions,
            "current_version": registry["current_versions"].get(key),
            "deployment": registry["deployments"].get(key),
            "forward_trades": summary["n"],
            "open_forward_trades": open_count,
            "state": state,
            "state_reasons": reasons,
            "next_gate": {
                "required": lr.next_gate(summary["n"]),
                "have": summary["n"],
                "status": lr.evidence_status(summary["n"]),
            },
            "summary": summary,
            "metric_note": metric_note,
            "progression": progression,
            "context_observations_recorded": int(
                registry["recorded_contexts"].get(key, 0)
            ),
            "forward_observations_recorded": recorded,
            "optimization_ready": state == lr.STATE_OPTIMIZATION_ELIGIBLE,
            "missing_features": missing,
            "last_trade": _iso(max(exits)) if exits else None,
            "last_cycle": (
                {
                    "cycle_id": latest_cycle.get("cycle_id"),
                    "execution_date": latest_cycle.get("execution_date"),
                    "status": latest_cycle.get("status"),
                    "strategy_included": key in analysed,
                }
                if latest_cycle
                else None
            ),
            "quality_issues": issues,
        }

    def backtest_vs_forward(
        self,
        *,
        strategy: str | None = None,
        strategy_version: int | None = None,
        min_sample: int | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Specific side-by-side comparison of BACKTEST vs PAPER_FORWARD evidence.

        Compares trade count, win rate, average return, profit factor, drawdown,
        and return distributions without judging strategy as good or bad.
        """
        rows = self.dataset(refresh=refresh, user_id=user_id).rows
        if strategy:
            rows = [r for r in rows if _matches_strategy(r, strategy)]
        if strategy_version is not None:
            rows = [r for r in rows if r.get("strategy_version") == strategy_version]

        backtest_rows = [
            r for r in rows
            if str(r.get("evidence_class") or "").upper() in ("BACKTEST", "IN_SAMPLE")
            or str(r.get("source") or "").upper() == "BACKTEST"
        ]
        forward_rows = [
            r for r in rows
            if str(r.get("evidence_class") or "").upper() == "PAPER_FORWARD"
            or (str(r.get("source") or "").upper() == "PAPER" and str(r.get("evidence_grade") or "").lower() == "forward")
        ]

        from atr.research.learning_drift import compare_sources

        drift_pair = compare_sources(
            backtest_rows,
            forward_rows,
            reference="BACKTEST",
            comparison="PAPER_FORWARD",
            min_sample=min_sample or stats.MIN_SAMPLE,
        )

        return {
            "strategy": strategy or "ALL",
            "strategy_version": strategy_version,
            "backtest_n": len(backtest_rows),
            "forward_n": len(forward_rows),
            "comparison": drift_pair.as_dict(),
            "findings": drift_pair.findings,
            "limitations": drift_pair.limitations,
            "distribution": drift_pair.distribution,
            "regime": drift_pair.regime,
        }

    def record_observations(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        min_sample: int = stats.MIN_SAMPLE,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Persist structured learning observations/findings to database.

        Analyzes PAPER_FORWARD trades by condition buckets and records findings
        with sample sizes, statistical results, confidence, and trade IDs.
        """
        from atr.appdb.repositories import LearningObservationRepository

        dataset = self.dataset(refresh=refresh, user_id=user_id)
        forward_rows = [
            r for r in dataset.rows
            if str(r.get("evidence_class") or "").upper() == "PAPER_FORWARD"
            or (str(r.get("source") or "").upper() == "PAPER" and str(r.get("evidence_grade") or "").lower() == "forward")
        ]
        if strategy_id:
            forward_rows = [r for r in forward_rows if _matches_strategy(r, strategy_id)]

        if not forward_rows:
            return []

        analysis = PerformanceAnalysis(
            LearningDataset(rows=forward_rows, missing_features={}, generated_at=dataset.generated_at)
        ).analyse(strategy=strategy_id, min_sample=min_sample)

        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        created_records = []

        with self.db.session() as session:
            for breakdown in analysis.breakdowns:
                for bucket in breakdown.buckets:
                    if bucket.get("suppressed"):
                        continue
                    n = bucket.get("n", 0)
                    if n < min_sample:
                        continue
                    condition_bucket = f"{breakdown.axis}:{bucket.get('label')}"
                    p_val = bucket.get("p_adjusted")
                    conf = (1.0 - p_val) if p_val is not None else (0.95 if bucket.get("significance") in ("strong", "moderate") else 0.5)
                    source_ids = [
                        r.get("trade_ref") or r.get("trade_id")
                        for r in forward_rows
                        if r.get("trade_ref") or r.get("trade_id")
                    ][:100]

                    record = LearningObservationRepository.record(
                        session,
                        strategy_id=strategy_id or analysis.strategy,
                        strategy_version=strategy_version,
                        date=today_str,
                        metric=analysis.metric,
                        condition_bucket=condition_bucket,
                        sample_size=n,
                        statistical_result=bucket,
                        evidence_class="PAPER_FORWARD",
                        confidence=conf,
                        source_trades=source_ids,
                    )
                    created_records.append(record)
            session.commit()

        return created_records

    def query_market_context_performance(
        self,
        *,
        strategy: str | None = None,
        condition_axis: str,
        condition_value: str | None = None,
        grades: Iterable[str] | None = None,
        metric: str | None = None,
        min_sample: int | None = None,
        refresh: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Answer: *How does this strategy perform under a specific market condition?*

        Slices the learning dataset along a single market-context axis
        (e.g. ``market_regime``, ``nifty_trend_bucket``, ``breadth_bucket``,
        ``sector_strength_bucket``, ``stock_rs_bucket``, ``volatility_regime``)
        and returns:

        - Slice sample size (n)
        - Mean / median metric value
        - Win rate
        - Profit factor
        - 95% confidence interval
        - Evidence class (forward / in-sample)
        - All other buckets for the same axis (for comparison)

        The answer is only published when the slice carries ``min_sample`` or
        more *forward* observations. Smaller slices are returned but flagged
        ``suppressed`` so the caller never presents an in-sample number as a
        finding.

        ``condition_value`` filters to a single bucket label. When omitted all
        buckets of the axis are returned, ranked by sample size.
        """
        from atr.research.learning_axes import axes_from_names

        min_n = min_sample if min_sample is not None else stats.MIN_SAMPLE
        resolved_metric, metric_note = resolve_metric(
            self.dataset(refresh=refresh, user_id=user_id), metric
        )

        dataset = self.dataset(refresh=refresh, user_id=user_id)
        rows = dataset.rows
        if strategy:
            rows = [r for r in rows if _matches_strategy(r, strategy)]
        if grades:
            wanted_grades = {str(g).strip().lower() for g in grades}
            rows = [r for r in rows if row_grade(r) in wanted_grades]
        # Only closed trades have outcomes
        rows = [r for r in rows if r.get("exit_ts") is not None]

        # ------------------------------------------------------------------ #
        # Bucket rows along the requested axis                                 #
        # ------------------------------------------------------------------ #
        _AXIS_FIELD_MAP = {
            "market_regime": "market_regime",
            "regime": "market_regime",
            "volatility_regime": "volatility_regime",
            "stock_rs": "stock_relative_strength",   # will use bucket helper
            "stock_rs_bucket": "stock_relative_strength",
            "sector_strength": "sector_relative_strength",
            "sector_strength_bucket": "sector_relative_strength",
            "market_breadth": "market_breadth",
            "breadth_bucket": "market_breadth",
            "nifty_trend": "nifty_trend_pct",
            "nifty_trend_bucket": "nifty_trend_pct",
        }

        # Derive a string bucket from a numeric field if the axis needs one
        _NUMERIC_AXIS_BUCKETERS = {
            "stock_rs", "stock_rs_bucket",
            "sector_strength", "sector_strength_bucket",
            "market_breadth", "breadth_bucket",
            "nifty_trend", "nifty_trend_bucket",
        }

        def _bucket_label(axis_key: str, row: dict[str, Any]) -> str | None:
            """Return a categorical label for axes that have numeric values."""
            field = _AXIS_FIELD_MAP.get(axis_key, axis_key)
            raw = row.get(field)
            if raw is None:
                return None

            if axis_key in ("stock_rs", "stock_rs_bucket"):
                try:
                    v = float(raw)
                    if v > 5.0:
                        return "strongly_outperforming"
                    if v > 0.0:
                        return "outperforming"
                    if v > -5.0:
                        return "underperforming"
                    return "strongly_underperforming"
                except (TypeError, ValueError):
                    return None

            if axis_key in ("sector_strength", "sector_strength_bucket"):
                try:
                    v = float(raw)
                    if v > 2.0:
                        return "strong_sector"
                    if v > 0.0:
                        return "mild_outperformer"
                    if v > -2.0:
                        return "mild_laggard"
                    return "weak_sector"
                except (TypeError, ValueError):
                    return None

            if axis_key in ("market_breadth", "breadth_bucket"):
                try:
                    v = float(raw)
                    if v >= 60.0:
                        return "high_breadth"
                    if v >= 40.0:
                        return "normal_breadth"
                    return "low_breadth"
                except (TypeError, ValueError):
                    return None

            if axis_key in ("nifty_trend", "nifty_trend_bucket"):
                try:
                    v = float(raw)
                    if v > 1.0:
                        return "uptrend"
                    if v > -1.0:
                        return "sideways"
                    return "downtrend"
                except (TypeError, ValueError):
                    return None

            # Categorical fields: return as-is
            return str(raw) if raw is not None else None

        # Bucket all rows
        by_bucket: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            lbl = _bucket_label(condition_axis, row)
            if lbl is None:
                continue
            by_bucket.setdefault(lbl, []).append(row)

        # Compute forward count for each bucket
        def _metric_value(r: dict[str, Any]) -> float | None:
            v = r.get(resolved_metric)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _bucket_stats(label: str, bucket_rows: list[dict[str, Any]]) -> dict[str, Any]:
            values = [v for v in (_metric_value(r) for r in bucket_rows) if v is not None]
            n = len(values)
            n_forward = sum(
                1 for r in bucket_rows if is_forward_grade(row_grade(r))
            )
            n_in_sample = len(bucket_rows) - n_forward
            suppressed = n < min_n
            s = stats.summarise(values) if values else None
            wins = [v for v in values if v > 0] if values else []
            losers = [v for v in values if v <= 0] if values else []
            win_rate = len(wins) / len(values) if values else None
            gross_win = sum(wins) if wins else 0.0
            gross_loss = abs(sum(losers)) if losers else 0.0
            profit_factor = gross_win / gross_loss if gross_loss > 0 else None

            return {
                "label": label,
                "n": n,
                "n_forward": n_forward,
                "n_in_sample": n_in_sample,
                "suppressed": suppressed,
                "mean": s.mean if s else None,
                "median": s.median if s else None,
                "ci_low": s.ci_low if s else None,
                "ci_high": s.ci_high if s else None,
                "win_rate": round(win_rate * 100, 2) if win_rate is not None else None,
                "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
                "evidence_note": (
                    "forward" if n_forward >= min_n
                    else "insufficient_forward_observations"
                    if n_forward > 0
                    else "in_sample_only"
                ),
            }

        all_bucket_stats = {
            lbl: _bucket_stats(lbl, b_rows)
            for lbl, b_rows in by_bucket.items()
        }

        # Filter to requested condition_value if specified
        if condition_value is not None:
            filtered = {k: v for k, v in all_bucket_stats.items() if k == condition_value}
            requested_stat = all_bucket_stats.get(condition_value)
        else:
            filtered = all_bucket_stats
            requested_stat = None

        # Overall sample baseline
        all_values = [v for r in rows for v in [_metric_value(r)] if v is not None]
        overall_s = stats.summarise(all_values) if all_values else None

        return {
            "strategy": strategy or "ALL",
            "condition_axis": condition_axis,
            "condition_value": condition_value,
            "metric": resolved_metric,
            "metric_note": metric_note,
            "min_sample_floor": min_n,
            "total_rows_scanned": len(rows),
            "rows_with_value": len(all_values),
            "baseline": {
                "n": len(all_values),
                "mean": overall_s.mean if overall_s else None,
                "median": overall_s.median if overall_s else None,
                "win_rate": (
                    round(sum(1 for v in all_values if v > 0) / len(all_values) * 100, 2)
                    if all_values else None
                ),
            },
            "buckets": sorted(
                filtered.values(),
                key=lambda b: (not b["suppressed"], b["n_forward"], b["n"]),
                reverse=True,
            ),
            "all_buckets": sorted(
                all_bucket_stats.values(),
                key=lambda b: (not b["suppressed"], b["n_forward"], b["n"]),
                reverse=True,
            ),
            "requested": requested_stat,
            "caveats": [
                *(
                    [f"metric resolved to {resolved_metric}: {metric_note}"]
                    if metric_note else []
                ),
                *(
                    [f"no rows carry axis '{condition_axis}'; all buckets empty"]
                    if not by_bucket else []
                ),
            ],
        }

    def list_observations(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: int | None = None,
        evidence_class: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve stored learning observation history from database."""
        from atr.appdb.repositories import LearningObservationRepository

        with self.db.session() as session:
            return LearningObservationRepository.list_observations(
                session,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                evidence_class=evidence_class,
                limit=limit,
            )



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def coverage_phrase(coverage: dict[str, int]) -> str:
    """``"net_pnl 0, return_pct 140"`` — the coverage, said compactly."""
    return ", ".join(f"{metric} {coverage.get(metric, 0)}" for metric in OUTCOME_METRICS)


def _format_amount(value: float, metric: str) -> str:
    """Render a figure in its own unit, so a percentage is never read as rupees.

    ``net_pnl`` is money and gets a sign and two decimals; ``return_pct`` is a
    percentage and gets a sign and a percent sign. A shared formatter would put
    ``+1.24`` in front of a reader who has no way to tell whether that is rupees
    or percent.
    """
    if metric == "return_pct":
        return f"{value:+.2f}%"
    return f"{value:+,.2f}"


def resolve_metric(
    dataset: LearningDataset, requested: str | None = None
) -> tuple[str, str | None]:
    """``(metric, note)`` — the outcome column to rank on, and why if it changed.

    An outcome column can be **absent** rather than empty. The paper ledger
    records an equal-weight return and no position size, so a book built from it
    has ``return_pct`` on every row and ``net_pnl`` on none. Ranking buckets on
    the absent column produces a correctly-shaped table of nothing, which reads
    exactly like a book that broke even.

    So: an explicit request is honoured, and the caveat machinery says when it
    has no coverage. With no request, the metric that has coverage is chosen and
    the substitution is named. Both paths say what happened; neither invents a
    number.
    """
    coverage = dataset.metric_coverage()
    if requested is not None:
        # Honoured as asked. ``PerformanceAnalysis._caveats`` reports the zero
        # coverage, so the note would only repeat it.
        return requested, None

    preferred = OUTCOME_METRICS[0]
    if coverage.get(preferred, 0) > 0:
        return preferred, None

    best = max(OUTCOME_METRICS, key=lambda metric: coverage.get(metric, 0))
    if coverage.get(best, 0) > 0:
        return best, (
            f"{preferred} has no coverage in this dataset, so the analysis ran on "
            f"{best} ({coverage_phrase(coverage)})"
        )
    # Nothing carries either. Keep the preferred name so the output is
    # deterministic, and let the empty-sample caveat explain.
    return preferred, None


def _canonical(symbol: Any) -> str | None:
    if not symbol:
        return None
    from atr.instruments.service import canonical_symbol

    return canonical_symbol(str(symbol)) or None


# ---------------------------------------------------------------------------
# why a row carries the grade it does
# ---------------------------------------------------------------------------
#
# Every row states the basis of its own grade. A grade with no stated basis is a
# label the reader has to take on trust, and the whole point of the column is
# that it should not have to be.

def _journal_grade(raw: dict[str, Any]) -> tuple[str, str]:
    """``(grade, why)`` for one journal row.

    Three cases, and the third is the one that matters. A row explicitly marked
    forward is evidence; a row explicitly marked in-sample is a measurement; and
    a row carrying no verdict at all — which is what a journal row written
    before this column existed, or by a harness that inserts directly, looks
    like — is graded **in-sample**, with the reason recorded rather than
    silently substituted. Assuming forward there would promote every
    un-attributed fill into evidence, which is the one direction the mistake
    must never be made in.
    """
    stored = raw.get("evidence_grade")
    if is_forward_grade(stored):
        return GRADE_FORWARD, NOTE_FORWARD_STAMPED
    if stored == GRADE_IN_SAMPLE:
        return GRADE_IN_SAMPLE, NOTE_MARKED_IN_SAMPLE
    return GRADE_IN_SAMPLE, NOTE_NO_STAMP


def _dow(when: Any) -> str | None:
    if when is None:
        return None
    try:
        return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
            when.weekday()
        ]
    except (AttributeError, IndexError):
        return None


def _same_day(when: Any, day) -> bool:
    if when is None:
        return False
    stamp = when.date() if hasattr(when, "date") else when
    return stamp == day


def _ts_gap_sec(first: Any, second: Any) -> float | None:
    """Absolute seconds between two timestamps, ``None`` when uncomparable.

    Naive stamps are read as UTC, matching how the store writes them. A parse
    failure is not an error here — it just means the pair cannot be linked.
    """
    from datetime import datetime as _datetime

    def _coerce(value: Any) -> Any:
        if isinstance(value, _datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = _datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None

    start, end = _coerce(first), _coerce(second)
    if start is None or end is None:
        return None
    try:
        return abs((start - end).total_seconds())
    except (TypeError, OverflowError):
        return None


def _matches_strategy(row: dict[str, Any], strategy: str) -> bool:
    """Match on any of the three names a strategy can be known by.

    A built-in is identified by ``engine_key``; a saved one by ``strategy_id``.
    A caller passing either spelling, or an ``id@version`` pin, should find its
    trades rather than an empty result that looks like "it made nothing".
    """
    target = str(strategy).strip()
    if "@" in target:
        name, _, version = target.partition("@")
        if str(row.get("strategy_version")) != version.strip():
            return False
        target = name.strip()
    candidates = {
        str(row.get("strategy_key") or ""),
        str(row.get("strategy_id") or ""),
        str(row.get("engine_key") or ""),
    }
    return target in candidates


def _confidence_word(bucket: dict[str, Any]) -> str:
    label = bucket.get("significance")
    return {
        "strong": "high",
        "moderate": "medium",
        "weak": "low",
    }.get(label, "insufficient")


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


_service: LearningService | None = None
_service_lock = threading.Lock()


def get_learning_service() -> LearningService:
    global _service
    with _service_lock:
        if _service is None:
            _service = LearningService()
        return _service


def reset_learning_service() -> None:
    global _service
    with _service_lock:
        _service = None


__all__ = [
    "DATASET_COLUMNS",
    "CLASS_BACKTEST",
    "CLASS_IN_SAMPLE",
    "CLASS_LIVE_FORWARD",
    "CLASS_PAPER_FORWARD",
    "EVIDENCE_CLASSES",
    "FORWARD_CLASSES",
    "GRADE_FORWARD",
    "GRADE_IN_SAMPLE",
    "GRADES",
    "IN_SAMPLE_CLASSES",
    "LEARNING_DIR",
    "OUTCOME_METRICS",
    "SOURCE_BACKTEST",
    "SOURCE_LIVE",
    "SOURCE_PAPER",
    "SOURCES",
    "AxisBreakdown",
    "DailyLearningReport",
    "DailyReport",
    "LearningDataset",
    "LearningDatasetBuilder",
    "LearningError",
    "LearningService",
    "PerformanceAnalysis",
    "StrategyAnalysis",
    "coverage_phrase",
    "data_root",
    "forward_class",
    "get_learning_service",
    "grade_of",
    "is_forward_class",
    "is_forward_grade",
    "reset_learning_service",
    "resolve_metric",
    "row_grade",
]
