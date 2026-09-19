"""Automated Daily Learning Cycle Service.

Pipeline:
PAPER_FORWARD TRADES
→ DATASET UPDATE
→ STATISTICAL ANALYSIS
→ PATTERN DETECTION
→ HYPOTHESIS GENERATION
→ OPTIMIZATION CANDIDATE
→ RESEARCH QUEUE / PERSISTENCE

Guarantees & Constraints:
- Runs after market session (or on-demand).
- Collects newly closed PAPER_FORWARD trades.
- Reuses existing PerformanceAnalysis, DailyLearningReport, and compare_sources.
- Evaluates forward trades across all declared axes.
- Enforces strict safeguards: suppresses n < 10, statistically weak findings, and isolated outliers.
- Feeds eligible hypotheses into the existing controlled optimization layer.
- Never automatically approves recommendations or changes live strategies.
- Completely reproducible: records cycle ID, execution date, strategies analyzed,
  trades processed, observations generated, hypotheses generated, candidates,
  recommendations, errors, and runtime.
- Never manufactures forward evidence from backtests or backfills.
- Returns comprehensive failure reasons when data or sample size is insufficient.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from atr.appdb.engine import AppDatabase, get_app_db, utcnow
from atr.appdb.repositories import (
    LearningObservationRepository,
    OptimizationRecommendationRepository,
    StrategyRepository,
    SystemStateRepository,
)
from atr.optimization.adaptive import extract_adaptive_parameters
from atr.optimization.candidates import (
    OptimizationCandidate,
    generate_candidates_from_observations,
)
from atr.research import learning_stats as stats
from atr.research.learning_axes import (
    DEFAULT_AXES,
    axes_from_names,
)
from atr.research.learning_drift import compare_sources
from atr.research.learning_evidence import CLASS_PAPER_FORWARD, GRADE_FORWARD
from atr.services.learning import (
    LearningDataset,
    LearningService,
    PerformanceAnalysis,
    get_learning_service,
)
from atr.services.optimization import OptimizationService

IST = ZoneInfo("Asia/Kolkata")
KEY_LAST_CYCLE = "learning.last_cycle"
KEY_CYCLE_HISTORY = "learning.cycle_history"


@dataclass
class ResearchHypothesis:
    """A research candidate statement derived from empirical forward evidence."""

    hypothesis_id: str
    strategy_id: str
    strategy_version: int | None
    parameter_target: str
    statement: str
    condition_bucket: str
    sample_size: int
    baseline_mean: float | None
    bucket_mean: float | None
    lift: float | None
    p_value: float | None
    confidence: str
    evidence_class: str = CLASS_PAPER_FORWARD
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DailyLearningCycleReport:
    """Result of an automated daily learning cycle execution."""

    cycle_id: str
    execution_date: str
    started_at: str
    completed_at: str
    runtime_seconds: float
    status: str  # SUCCESS, NO_DATA, INSUFFICIENT_SAMPLE, PARTIAL, ERROR
    strategies_analyzed: list[str]
    trades_processed: int
    new_forward_trades_count: int
    observations_generated: int
    hypotheses_generated: int
    candidates_generated: int
    recommendations_generated: int
    recommendation_ids: list[str]
    drift_detected: dict[str, bool]
    insufficient_data_strategies: list[str]
    notes: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DailyLearningCycleService:
    """Orchestrates end-of-day learning cycle across forward trading observations."""

    def __init__(
        self,
        db: AppDatabase | None = None,
        learning_service: LearningService | None = None,
        optimization_service: OptimizationService | None = None,
    ) -> None:
        self._db = db
        self._learning_service = learning_service
        self._optimization_service = optimization_service

    @property
    def db(self) -> AppDatabase:
        if self._db is not None:
            return self._db
        return get_app_db()

    @property
    def learning(self) -> LearningService:
        if self._learning_service is not None:
            return self._learning_service
        return get_learning_service()

    @property
    def optimization(self) -> OptimizationService:
        if self._optimization_service is not None:
            return self._optimization_service
        return OptimizationService(db=self.db, learning_service=self.learning)

    def run_cycle(
        self,
        *,
        as_of: datetime | None = None,
        strategy_id: str | None = None,
        min_sample_size: int = 10,
        user_id: str = "system",
        feed: Any = None,
    ) -> DailyLearningCycleReport:
        """Execute one complete automated daily learning cycle.

        1. Refresh and collect genuine PAPER_FORWARD trades.
        2. Run PerformanceAnalysis across all axes with sample size guards (n >= 10).
        3. Run DriftAnalysis (BACKTEST vs PAPER_FORWARD).
        4. Generate ResearchHypotheses for statistically significant observations.
        5. Feed hypotheses into OptimizationService to evaluate adaptive candidates.
        6. Persist observations, recommendations, and cycle execution metrics.
        """
        start_wall = time.perf_counter()
        cycle_id = f"cycle_{uuid.uuid4().hex[:12]}"
        now_ist = as_of or datetime.now(IST)
        execution_date = now_ist.strftime("%Y-%m-%d")
        started_at = utcnow().isoformat()

        notes: list[str] = []
        errors: list[str] = []
        recommendation_ids: list[str] = []
        strategies_analyzed: list[str] = []
        insufficient_data_strategies: list[str] = []
        drift_detected: dict[str, bool] = {}

        # 1. Update Learning Dataset with latest forward paper trades
        try:
            dataset: LearningDataset = self.learning.dataset(refresh=True)
        except Exception as exc:
            logger.exception("Learning cycle failed to build dataset: {}", exc)
            return DailyLearningCycleReport(
                cycle_id=cycle_id,
                execution_date=execution_date,
                started_at=started_at,
                completed_at=utcnow().isoformat(),
                runtime_seconds=round(time.perf_counter() - start_wall, 3),
                status="ERROR",
                strategies_analyzed=[],
                trades_processed=0,
                new_forward_trades_count=0,
                observations_generated=0,
                hypotheses_generated=0,
                candidates_generated=0,
                recommendations_generated=0,
                recommendation_ids=[],
                drift_detected={},
                insufficient_data_strategies=[],
                notes=["Dataset build failed"],
                errors=[str(exc)],
            )

        # Filter strictly for PAPER_FORWARD
        forward_trades = [
            r for r in dataset.rows
            if str(r.get("evidence_class") or "").upper() == CLASS_PAPER_FORWARD
            or (str(r.get("source") or "").upper() == "PAPER" and str(r.get("evidence_grade") or "").lower() == "forward")
        ]

        if strategy_id:
            forward_trades = [
                r for r in forward_trades
                if str(r.get("strategy_id") or r.get("strategy_key") or "") == strategy_id
            ]

        total_forward_trades = len(forward_trades)
        if total_forward_trades == 0:
            notes.append("No genuine PAPER_FORWARD trades found in dataset; safe zero-result completion.")
            report = DailyLearningCycleReport(
                cycle_id=cycle_id,
                execution_date=execution_date,
                started_at=started_at,
                completed_at=utcnow().isoformat(),
                runtime_seconds=round(time.perf_counter() - start_wall, 3),
                status="NO_DATA",
                strategies_analyzed=[],
                trades_processed=0,
                new_forward_trades_count=0,
                observations_generated=0,
                hypotheses_generated=0,
                candidates_generated=0,
                recommendations_generated=0,
                recommendation_ids=[],
                drift_detected={},
                insufficient_data_strategies=[],
                notes=notes,
                errors=errors,
            )
            self._persist_cycle(report)
            return report

        # Group forward trades by strategy
        by_strategy: dict[str, list[dict[str, Any]]] = {}
        for t in forward_trades:
            s_id = str(t.get("strategy_id") or t.get("strategy_key") or "UNKNOWN")
            by_strategy.setdefault(s_id, []).append(t)

        all_observations_saved = 0
        all_hypotheses_generated: list[ResearchHypothesis] = []
        all_candidates_count = 0
        all_recommendations_count = 0

        # Run per-strategy learning & pattern detection
        for s_id, s_trades in by_strategy.items():
            strategies_analyzed.append(s_id)
            n_trades = len(s_trades)

            if n_trades < min_sample_size:
                insufficient_data_strategies.append(s_id)
                notes.append(
                    f"Strategy {s_id} has {n_trades} forward trades (requires min {min_sample_size}); skipping."
                )
                continue

            # 2. Performance Analysis across all declared axes
            strategy_dataset = LearningDataset(
                rows=s_trades,
                missing_features={},
                generated_at=dataset.generated_at,
            )
            analysis = PerformanceAnalysis(strategy_dataset).analyse(
                strategy=s_id,
                min_sample=min_sample_size,
            )

            # 3. Drift Analysis (Compare Backtest baseline vs Forward paper trades)
            backtest_trades = [
                r for r in dataset.rows
                if (str(r.get("strategy_id") or r.get("strategy_key") or "") == s_id)
                and (
                    str(r.get("evidence_class") or "").upper() in ("BACKTEST", "IN_SAMPLE")
                    or str(r.get("source") or "").upper() == "BACKTEST"
                )
            ]
            if len(backtest_trades) >= min_sample_size and len(s_trades) >= min_sample_size:
                drift_pair = compare_sources(
                    backtest_trades,
                    s_trades,
                    reference="BACKTEST",
                    comparison="PAPER_FORWARD",
                    min_sample=min_sample_size,
                )
                has_drift = bool(drift_pair.deteriorated or drift_pair.improved)
                drift_detected[s_id] = has_drift
                if has_drift:
                    notes.append(
                        f"Drift detected for strategy {s_id}: deteriorated={drift_pair.deteriorated}, improved={drift_pair.improved}"
                    )
            else:
                drift_detected[s_id] = False

            # 4. Resolve strategy version to inspect adaptive parameters
            try:
                ver_row = self.optimization.get_strategy_version(s_id)
                def_raw = ver_row.get("definition")
                s_def = json.loads(def_raw) if isinstance(def_raw, str) else (def_raw or {})
                adaptive_params = extract_adaptive_parameters(s_def)
                strat_version = ver_row.get("version")
            except Exception as exc:
                notes.append(f"Strategy {s_id} version could not be resolved: {exc}")
                strat_version = None
                s_def = {}
                adaptive_params = {}

            # Persist genuine forward observations with strict n >= min_sample_size
            strat_observations: list[dict[str, Any]] = []
            with self.db.session() as session:
                for breakdown in analysis.breakdowns:
                    for bucket in breakdown.buckets:
                        if bucket.get("suppressed"):
                            continue
                        n = bucket.get("n", 0)
                        if n < min_sample_size:
                            continue

                        p_val = bucket.get("p_adjusted")
                        sig = bucket.get("significance")
                        # Never promote weak observations or isolated outliers
                        if sig not in ("strong", "moderate") and (p_val is None or p_val > 0.10):
                            continue

                        cond_bucket = f"{breakdown.axis}:{bucket.get('label')}"
                        conf = (1.0 - p_val) if p_val is not None else (0.95 if sig == "strong" else 0.75)
                        source_ids = [r.get("trade_ref") or r.get("trade_id") for r in s_trades if r.get("trade_ref") or r.get("trade_id")][:100]

                        obs_record = LearningObservationRepository.record(
                            session,
                            strategy_id=s_id,
                            strategy_version=strat_version,
                            date=execution_date,
                            metric=analysis.metric,
                            condition_bucket=cond_bucket,
                            sample_size=n,
                            statistical_result=bucket,
                            evidence_class=CLASS_PAPER_FORWARD,
                            confidence=conf,
                            source_trades=source_ids,
                        )
                        strat_observations.append(obs_record)
                        all_observations_saved += 1
                session.commit()

            if not adaptive_params:
                notes.append(f"Strategy {s_id} has no parameters marked adaptive; no optimization candidates generated.")
                continue

            # Convert observations to hypotheses
            candidates: list[OptimizationCandidate] = []

            for obs in strat_observations:
                stat_res = obs.get("statistical_result") or {}
                n = obs.get("sample_size") or 0
                bucket_stats = stat_res.get("stats") or {}
                lift = stat_res.get("lift")
                cond = obs.get("condition_bucket", "")
                axis_name, _, bucket_label = cond.partition(":")

                # Build candidate using existing optimizer generator
                cand_list = generate_candidates_from_observations(
                    s_def,
                    [
                        {
                            "axis": axis_name,
                            "sample_size": n,
                            "statement": f"{cond} lift {lift}",
                            "evidence": {
                                "axis": axis_name,
                                "bucket": bucket_label or cond,
                                "n": n,
                                "lift": lift,
                                "stats": bucket_stats,
                            },
                        }
                    ],
                    min_sample_size=min_sample_size,
                )

                for cand in cand_list:
                    candidates.append(cand)
                    hyp = ResearchHypothesis(
                        hypothesis_id=f"hyp_{uuid.uuid4().hex[:10]}",
                        strategy_id=s_id,
                        strategy_version=ver_row.get("version"),
                        parameter_target=cand.parameter_name,
                        statement=f"Strategy {s_id} may perform better when {cand.parameter_name} is set to {cand.proposed_value} (observed {cond}, lift {lift}).",
                        condition_bucket=cond,
                        sample_size=n,
                        baseline_mean=bucket_stats.get("baseline_mean"),
                        bucket_mean=bucket_stats.get("mean"),
                        lift=lift,
                        p_value=stat_res.get("p_adjusted"),
                        confidence="High" if stat_res.get("significance") == "strong" else "Medium",
                    )
                    all_hypotheses_generated.append(hyp)

            all_candidates_count += len(candidates)

            # 5. Feed into existing Controlled Optimization Layer
            if candidates:
                try:
                    recs = self.optimization.run_optimization(
                        s_id,
                        version=ver_row.get("version"),
                        user_id=user_id,
                        feed=feed,
                        observations=[
                            {
                                "axis": h.condition_bucket.split(":")[0],
                                "sample_size": h.sample_size,
                                "statement": h.statement,
                                "evidence": {
                                    "axis": h.condition_bucket.split(":")[0],
                                    "bucket": h.condition_bucket,
                                    "n": h.sample_size,
                                    "lift": h.lift,
                                    "stats": {"mean": h.bucket_mean, "lift": h.lift},
                                },
                            }
                            for h in all_hypotheses_generated
                            if h.strategy_id == s_id
                        ],
                        min_sample_size=min_sample_size,
                    )
                    all_recommendations_count += len(recs)
                    for r in recs:
                        recommendation_ids.append(r["recommendation_id"])
                        notes.append(
                            f"Generated recommendation {r['recommendation_id'][:8]} ({r['status']}) for {r['parameter']}."
                        )
                except Exception as exc:
                    err_msg = f"Optimization run failed for strategy {s_id}: {exc}"
                    logger.exception(err_msg)
                    errors.append(err_msg)

        # 5b. Context-Aware Signal Engine observations. Best effort: an analysis
        # miss must never gate the cycle, exactly as context enrichment never
        # gates a signal.
        try:
            from atr.signal_context.service import get_signal_context_service

            context_observations = (
                get_signal_context_service().record_effectiveness_observations(
                    user_id,
                    strategy_id=strategy_id,
                    min_sample=min_sample_size,
                )
            )
            if context_observations:
                all_observations_saved += len(context_observations)
                notes.append(
                    f"Recorded {len(context_observations)} context-score observation(s) "
                    "from the signal context engine."
                )
        except Exception as exc:  # noqa: BLE001 — context analysis never gates the cycle
            notes.append(f"Context effectiveness analysis skipped: {exc}")
            logger.warning("context effectiveness analysis skipped: {}", exc)

        status = "SUCCESS"
        if errors and not recommendation_ids and not all_observations_saved:
            status = "ERROR"
        elif errors:
            status = "PARTIAL"
        elif not all_observations_saved and insufficient_data_strategies:
            status = "INSUFFICIENT_SAMPLE"

        report = DailyLearningCycleReport(
            cycle_id=cycle_id,
            execution_date=execution_date,
            started_at=started_at,
            completed_at=utcnow().isoformat(),
            runtime_seconds=round(time.perf_counter() - start_wall, 3),
            status=status,
            strategies_analyzed=strategies_analyzed,
            trades_processed=total_forward_trades,
            new_forward_trades_count=total_forward_trades,
            observations_generated=all_observations_saved,
            hypotheses_generated=len(all_hypotheses_generated),
            candidates_generated=all_candidates_count,
            recommendations_generated=all_recommendations_count,
            recommendation_ids=recommendation_ids,
            drift_detected=drift_detected,
            insufficient_data_strategies=insufficient_data_strategies,
            notes=notes,
            errors=errors,
        )

        self._persist_cycle(report)
        return report

    def _persist_cycle(self, report: DailyLearningCycleReport) -> None:
        """Store reproducible cycle report in durable system_state."""
        rep_dict = report.to_dict()
        with self.db.session() as session:
            SystemStateRepository.set(
                session,
                KEY_LAST_CYCLE,
                rep_dict,
                actor="learning_scheduler",
                reason="Daily learning cycle completion",
            )
            # Maintain rolling history of last 30 cycles
            history: list[dict[str, Any]] = SystemStateRepository.get_value(
                session, KEY_CYCLE_HISTORY, []
            ) or []
            if not isinstance(history, list):
                history = []
            history.insert(0, rep_dict)
            history = history[:30]
            SystemStateRepository.set(
                session,
                KEY_CYCLE_HISTORY,
                history,
                actor="learning_scheduler",
                reason="Rolling cycle history update",
            )

    def get_latest_cycle(self) -> dict[str, Any] | None:
        """Fetch latest stored learning cycle status and diagnostics."""
        with self.db.session() as session:
            return SystemStateRepository.get_value(session, KEY_LAST_CYCLE, None)

    def get_cycle_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch historical learning cycles."""
        with self.db.session() as session:
            hist = SystemStateRepository.get_value(session, KEY_CYCLE_HISTORY, []) or []
            return hist[:limit] if isinstance(hist, list) else []


_DAILY_LEARNING: DailyLearningCycleService | None = None


def get_daily_learning_service() -> DailyLearningCycleService:
    global _DAILY_LEARNING
    if _DAILY_LEARNING is None:
        _DAILY_LEARNING = DailyLearningCycleService()
    return _DAILY_LEARNING


def reset_daily_learning_service() -> None:
    global _DAILY_LEARNING
    _DAILY_LEARNING = None
