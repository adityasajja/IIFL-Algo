"""Optimization service: controls strategy parameter adaptation and recommendation lifecycle.

CRITICAL SAFETY BOUNDARIES:
- Never allow automatic live strategy modification or autonomous deployment.
- Only parameters explicitly marked as adaptive may be optimized.
- Baseline strategy version V_N is strictly immutable; applying an approved recommendation
  produces V_{N+1}.
- Multi-metric evaluation and walk-forward out-of-sample gating are enforced.
- Robustness sensitivity scan ensures stable parameter plateaus over isolated spikes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from loguru import logger

from atr.appdb.engine import get_app_db
from atr.appdb.repositories import (
    OptimizationRecommendationRepository,
    StrategyRepository,
)
from atr.data.base import DataFeed
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.optimization.adaptive import (
    extract_adaptive_parameters,
)
from atr.optimization.candidates import (
    OptimizationCandidate,
    generate_candidates_from_observations,
)
from atr.optimization.evaluator import (
    evaluate_candidate,
)
from atr.services.learning import LearningService, get_learning_service


class OptimizationService:
    """Service governing strategy parameter adaptation, evaluation, and recommendation."""

    def __init__(self, db: Any = None, learning_service: LearningService | None = None) -> None:
        self._db = db
        self._learning_service = learning_service

    @property
    def db(self) -> Any:
        if self._db is not None:
            return self._db
        return get_app_db()

    @property
    def learning_service(self) -> LearningService:
        if self._learning_service is not None:
            return self._learning_service
        return get_learning_service()

    def get_strategy_version(
        self, strategy_id: str, version: int | None = None
    ) -> dict[str, Any]:
        """Fetch a specific strategy version or the latest version."""
        with self.db.session() as session:
            if version is not None:
                row = StrategyRepository.get_version(session, strategy_id, version)
            else:
                row = StrategyRepository.latest_version(session, strategy_id)
            if row is None:
                raise LookupError(
                    f"Strategy {strategy_id} version {version or 'latest'} not found"
                )
            return dict(row)

    def discover_adaptive_parameters(
        self, strategy_id: str, version: int | None = None
    ) -> dict[str, Any]:
        """Extract all adaptive parameters explicitly declared on this strategy version."""
        ver_row = self.get_strategy_version(strategy_id, version)
        definition = ver_row.get("definition")
        if isinstance(definition, str):
            try:
                definition = json.loads(definition)
            except Exception:
                definition = {}

        params = extract_adaptive_parameters(definition)
        return {
            "strategy_id": strategy_id,
            "version": ver_row.get("version"),
            "adaptive_parameters": {k: p.to_dict() for k, p in params.items()},
            "count": len(params),
        }

    def get_forward_observations(
        self, strategy_id: str, *, min_sample: int = 10
    ) -> list[dict[str, Any]]:
        """Retrieve genuine forward-learning observations for this strategy."""
        try:
            report = self.learning_service.daily_report(refresh=False)
            observations = report.observations or []
            # Filter observations relevant to forward evidence
            relevant = []
            for obs in observations:
                n = obs.get("sample_size") or (obs.get("evidence", {}).get("n") if isinstance(obs.get("evidence"), dict) else 0) or 0
                if n >= min_sample:
                    relevant.append(obs)
            return relevant
        except Exception as exc:
            logger.warning("Could not retrieve forward observations: {}", exc)
            return []

    def generate_candidates(
        self,
        strategy_id: str,
        version: int | None = None,
        *,
        observations: list[dict[str, Any]] | None = None,
        min_sample_size: int = 10,
    ) -> list[OptimizationCandidate]:
        """Generate candidate parameter changes from forward learning observations."""
        ver_row = self.get_strategy_version(strategy_id, version)
        definition = ver_row.get("definition")
        if isinstance(definition, str):
            try:
                definition = json.loads(definition)
            except Exception:
                definition = {}

        if observations is None:
            observations = self.get_forward_observations(strategy_id, min_sample=min_sample_size)

        return generate_candidates_from_observations(
            definition,
            observations,
            min_sample_size=min_sample_size,
        )

    def _default_feed(self) -> DataFeed:
        """Fallback synthetic feed for evaluation when live historical market cache is missing."""
        return SyntheticFeed(SyntheticConfig(symbols=("NIFTY50", "RELIANCE"), freq="1D", seed=42))

    def run_optimization(
        self,
        strategy_id: str,
        version: int | None = None,
        *,
        user_id: str,
        feed: DataFeed | None = None,
        observations: list[dict[str, Any]] | None = None,
        min_sample_size: int = 10,
    ) -> list[dict[str, Any]]:
        """Execute full optimization workflow:

        Forward Evidence → Hypothesis → Candidate Generation → Backtest →
        Walk-Forward Validation → Robustness Check → Recommendation.
        """
        ver_row = self.get_strategy_version(strategy_id, version)
        current_version = ver_row.get("version")
        definition = ver_row.get("definition")
        if isinstance(definition, str):
            try:
                definition = json.loads(definition)
            except Exception:
                definition = {}

        adaptive_params = extract_adaptive_parameters(definition)
        if not adaptive_params:
            logger.info("No parameters marked adaptive for strategy {}", strategy_id)
            return []

        candidates = self.generate_candidates(
            strategy_id,
            version=current_version,
            observations=observations,
            min_sample_size=min_sample_size,
        )
        if not candidates:
            logger.info("No eligible candidates generated from forward evidence for strategy {}", strategy_id)
            return []

        eval_feed = feed or self._default_feed()
        recommendations: list[dict[str, Any]] = []

        for cand in candidates:
            param_spec = adaptive_params.get(cand.parameter_name)
            if param_spec is None:
                continue

            report = evaluate_candidate(eval_feed, cand, param_spec)

            rec_id = str(uuid.uuid4())
            rec_data = {
                "recommendation_id": rec_id,
                "strategy_id": strategy_id,
                "source_strategy_version": current_version,
                "parameter": cand.parameter_name,
                "current_value": cand.current_value,
                "proposed_value": cand.proposed_value,
                "reason": report.reason,
                "source_observations": cand.source_observation,
                "sample_size": cand.sample_size,
                "baseline_metrics": report.baseline_metrics.to_dict(),
                "candidate_metrics": report.candidate_metrics.to_dict(),
                "walk_forward_metrics": report.walk_forward_metrics.to_dict(),
                "robustness_results": report.robustness_result.to_dict(),
                "confidence": report.confidence,
                "status": report.status,  # RECOMMENDED or REJECTED
            }

            with self.db.session() as session:
                OptimizationRecommendationRepository.create(session, **rec_data)

            recommendations.append(rec_data)

        return recommendations

    def list_recommendations(
        self, strategy_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List stored optimization recommendations for a strategy."""
        with self.db.session() as session:
            return OptimizationRecommendationRepository.list_for_strategy(
                session, strategy_id, status=status
            )

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        """Get an optimization recommendation by ID."""
        with self.db.session() as session:
            return OptimizationRecommendationRepository.get(session, recommendation_id)

    def approve_recommendation(
        self, recommendation_id: str, user_id: str
    ) -> dict[str, Any]:
        """User explicitly approves an optimization recommendation.

        Transitions status to APPROVED. Does NOT mutate live deployments or orders.
        """
        with self.db.session() as session:
            rec = OptimizationRecommendationRepository.get(session, recommendation_id)
            if rec is None:
                raise LookupError(f"Recommendation {recommendation_id} not found")
            if rec["status"] not in ("PROPOSED", "VALIDATING", "RECOMMENDED"):
                raise ValueError(
                    f"Cannot approve recommendation with current status {rec['status']}"
                )

            return OptimizationRecommendationRepository.update_status(
                session,
                recommendation_id=recommendation_id,
                status="APPROVED",
                reviewed_by=user_id,
            )

    def reject_recommendation(
        self, recommendation_id: str, user_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        """User explicitly rejects an optimization recommendation."""
        with self.db.session() as session:
            rec = OptimizationRecommendationRepository.get(session, recommendation_id)
            if rec is None:
                raise LookupError(f"Recommendation {recommendation_id} not found")

            return OptimizationRecommendationRepository.update_status(
                session,
                recommendation_id=recommendation_id,
                status="REJECTED",
                reviewed_by=user_id,
                rejection_reason=reason,
            )

    def apply_recommendation(
        self, recommendation_id: str, author_user_id: str
    ) -> dict[str, Any]:
        """Apply an APPROVED recommendation to generate a NEW immutable strategy version V_{N+1}.

        Strict Guarantees:
        - V_N remains completely unmodified and immutable.
        - V_{N+1} is created with the new parameter value and change notes referencing the evidence.
        - Must be explicitly APPROVED before applying.
        """
        with self.db.session() as session:
            rec = OptimizationRecommendationRepository.get(session, recommendation_id)
            if rec is None:
                raise LookupError(f"Recommendation {recommendation_id} not found")
            if rec["status"] != "APPROVED":
                raise ValueError(
                    f"Recommendation {recommendation_id} has status '{rec['status']}'; "
                    "only APPROVED recommendations may be applied to produce a new version"
                )

            return OptimizationRecommendationRepository.apply(
                session,
                recommendation_id=recommendation_id,
                author_user_id=author_user_id,
            )
