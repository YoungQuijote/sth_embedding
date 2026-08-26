from __future__ import annotations

import math

from .calibration import CalibrationProfile
from collections import defaultdict
from typing import Any

from .contracts import FeatureComparator
from .defaults import ipv4_affinity
from .domain import (
    ContextRecallCandidate,
    FeatureRelation,
    FeatureSet,
    InvokeAvailability,
    JudgeCandidate,
    LaneRuntimeState,
    LocalRecallCandidate,
    PositionPathEvidence,
    PositionScore,
    RequestAffinityInfo,
    Scenario,
    ScenarioJudgeCandidate,
)


def availability_prior(
    availability: InvokeAvailability, alpha: float = 1.0, gamma: float = 2.0
) -> float:
    capacity = 1.0 + alpha * math.log(max(1, availability.registry_times))
    return (max(capacity - availability.invoked_times, 0.0) / capacity) ** gamma


def transition_prior(
    candidate_round: str | int, active_rounds: set[str | int], table: dict[int, float], floor: float
) -> float:
    if not active_rounds:
        return 0.0
    try:
        active = min(int(value) for value in active_rounds)
        delta = int(candidate_round) - active
    except (TypeError, ValueError):
        return floor
    return table.get(delta, floor)


class BayesPositionScorer:
    """Combine calibrated evidence LLRs with independent runtime priors."""

    def __init__(
        self,
        profile: CalibrationProfile,
        transition_table: dict[int, float],
        transition_floor: float,
        availability_alpha: float,
        availability_gamma: float,
    ) -> None:
        self.profile = profile
        self.transition_table = transition_table
        self.transition_floor = transition_floor
        self.availability_alpha = availability_alpha
        self.availability_gamma = availability_gamma

    def score(self, evidence: PositionPathEvidence) -> PositionScore:
        semantic_llr = self.profile.llr("semantic", evidence.scenario_semantic_raw)
        context_llr = self.profile.llr("context", evidence.context_raw)
        feature_llr = self.profile.llr("feature", evidence.feature_raw)
        affinity_llr = self.profile.llr("affinity", evidence.affinity_raw)
        transition = transition_prior(
            evidence.round_id, evidence.active_rounds, self.transition_table, self.transition_floor
        )
        availability_value = availability_prior(
            evidence.availability, self.availability_alpha, self.availability_gamma
        )
        total = (
            semantic_llr
            + context_llr
            + feature_llr
            + affinity_llr
            + transition
            + availability_value
        )
        return PositionScore(
            evidence.scenario_id,
            evidence.position,
            evidence.lane_id,
            evidence.scenario_semantic_raw,
            evidence.position_semantic_raw,
            evidence.context_raw,
            evidence.feature_raw,
            evidence.affinity_raw,
            semantic_llr,
            context_llr,
            feature_llr,
            affinity_llr,
            transition,
            availability_value,
            total,
        )


def normalize_scores(scores: list[PositionScore]) -> None:
    if not scores:
        return
    ceiling = max(score.total_log_score for score in scores)
    weights = [math.exp(score.total_log_score - ceiling) for score in scores]
    total = sum(weights)
    for score, weight in zip(scores, weights, strict=True):
        score.posterior = weight / total


class BayesEvidenceBuilder:
    """Build independent local and lane-backed position-path evidence without re-encoding."""

    def __init__(self, comparator: FeatureComparator) -> None:
        self.comparator = comparator

    def build(
        self,
        scenarios: list[Scenario[Any, Any]],
        endpoint_id: str,
        query_features: FeatureSet[Any, Any],
        affinity: RequestAffinityInfo,
        local: list[LocalRecallCandidate],
        context: list[ContextRecallCandidate],
        lanes: dict[str, LaneRuntimeState],
    ) -> list[tuple[PositionPathEvidence, Any, str | None]]:
        local_map = {(item.scenario_id, item.position): item.similarity for item in local}
        scenario_semantic = {
            scenario.scenario_id: max(
                (
                    similarity
                    for (scenario_id, _), similarity in local_map.items()
                    if scenario_id == scenario.scenario_id
                ),
                default=0.0,
            )
            for scenario in scenarios
        }
        context_paths = {
            (item.scenario_id, item.position, item.lane_id): item.similarity for item in context
        }
        results = []
        for scenario in scenarios:
            affinity_score = ipv4_affinity(affinity, scenario.registry_affinity_infos)
            for position in scenario.positions:
                if position.sample.endpoint_id != endpoint_id:
                    continue
                relation, feature_score = self.comparator.compare(query_features, position.features)
                if relation is FeatureRelation.CONFLICT:
                    continue
                semantic = local_map.get((scenario.scenario_id, position.position))
                if semantic is not None:
                    results.append(
                        (
                            PositionPathEvidence(
                                scenario.scenario_id,
                                position.position,
                                None,
                                scenario_semantic[scenario.scenario_id],
                                semantic,
                                0.0,
                                feature_score,
                                affinity_score,
                                position.sample.round_id,
                                position.sample.availability,
                                set(),
                            ),
                            position,
                            None,
                        )
                    )
                for (
                    scenario_id,
                    candidate_position,
                    lane_id,
                ), context_score in context_paths.items():
                    if (scenario_id, candidate_position) != (
                        scenario.scenario_id,
                        position.position,
                    ):
                        continue
                    lane = lanes[lane_id]
                    runtime_context = "\n\n".join(
                        f"Question:\n{item.actual_query}\n\nAnswer:\n{item.returned_answer}"
                        for item in lane.interactions
                    )
                    results.append(
                        (
                            PositionPathEvidence(
                                scenario.scenario_id,
                                position.position,
                                lane_id,
                                scenario_semantic[scenario.scenario_id],
                                semantic or 0.0,
                                context_score,
                                feature_score,
                                affinity_score,
                                position.sample.round_id,
                                position.sample.availability,
                                lane.active_rounds,
                            ),
                            position,
                            runtime_context,
                        )
                    )
        return results


class ScenarioScoreAggregator:
    """Aggregate by maximum path score and normalize posterior between scenarios."""

    def aggregate(
        self, candidates: list[JudgeCandidate], top_n: int
    ) -> list[ScenarioJudgeCandidate]:
        grouped: dict[str, list[JudgeCandidate]] = defaultdict(list)
        for candidate in candidates:
            grouped[candidate.scenario_id].append(candidate)
        if not grouped:
            return []
        maxima = {
            scenario_id: max(item.score.total_log_score for item in positions)
            for scenario_id, positions in grouped.items()
        }
        ceiling = max(maxima.values())
        weights = {scenario_id: math.exp(score - ceiling) for scenario_id, score in maxima.items()}
        total = sum(weights.values())
        scenarios = [
            ScenarioJudgeCandidate(
                scenario_id, maxima[scenario_id], weights[scenario_id] / total, positions
            )
            for scenario_id, positions in grouped.items()
        ]
        return sorted(scenarios, key=lambda item: item.score, reverse=True)[:top_n]


# Backward compatible alias for v1 integrations.
BayesScenarioScorer = BayesPositionScorer
