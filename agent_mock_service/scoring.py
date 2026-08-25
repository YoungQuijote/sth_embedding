from __future__ import annotations

import math

from .calibration import CalibrationProfile
from .domain import InvokeAvailability, ScenarioScore


def availability_prior(availability: InvokeAvailability, alpha: float = 1.0, gamma: float = 2.0) -> float:
    capacity = 1.0 + alpha * math.log(max(1, availability.registry_times))
    return (max(capacity - availability.invoked_times, 0.0) / capacity) ** gamma


def transition_prior(candidate_round: str | int, active_rounds: set[str | int], table: dict[int, float], floor: float) -> float:
    if not active_rounds:
        return 0.0
    try:
        active = min(int(value) for value in active_rounds)
        delta = int(candidate_round) - active
    except (TypeError, ValueError):
        return floor
    return table.get(delta, floor)


class BayesScenarioScorer:
    """Combine calibrated evidence LLRs with independent runtime priors."""

    def __init__(self, profile: CalibrationProfile, transition_table: dict[int, float], transition_floor: float, availability_alpha: float, availability_gamma: float) -> None:
        self.profile = profile
        self.transition_table = transition_table
        self.transition_floor = transition_floor
        self.availability_alpha = availability_alpha
        self.availability_gamma = availability_gamma

    def score(self, scenario_id: str, position: int, round_id: str | int, availability: InvokeAvailability, semantic: float, context: float, feature: float, affinity: float, active_rounds: set[str | int]) -> ScenarioScore:
        semantic_llr = self.profile.llr("semantic", semantic)
        context_llr = self.profile.llr("context", context)
        feature_llr = self.profile.llr("feature", feature)
        affinity_llr = self.profile.llr("affinity", affinity)
        transition = transition_prior(round_id, active_rounds, self.transition_table, self.transition_floor)
        availability_value = availability_prior(availability, self.availability_alpha, self.availability_gamma)
        total = semantic_llr + context_llr + feature_llr + affinity_llr + transition + availability_value
        return ScenarioScore(scenario_id, position, semantic, context, feature, affinity, semantic_llr, context_llr, feature_llr, affinity_llr, transition, availability_value, total)


def normalize_scores(scores: list[ScenarioScore]) -> None:
    if not scores:
        return
    ceiling = max(score.total_log_score for score in scores)
    weights = [math.exp(score.total_log_score - ceiling) for score in scores]
    total = sum(weights)
    for score, weight in zip(scores, weights, strict=True):
        score.posterior = weight / total
