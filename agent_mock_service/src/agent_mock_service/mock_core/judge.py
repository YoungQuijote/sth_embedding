from __future__ import annotations

from .domain import JudgeResult, MatchDecision, ScenarioJudgeCandidate


class FakeJudge:
    """Deterministic judge for local execution; production LLM judges implement the same contract."""

    model_name = "deterministic-fake-judge"
    prompt_version = "v1-no-candidate-answers"

    def judge(self, query: str, candidates: list[ScenarioJudgeCandidate]) -> JudgeResult:
        if not candidates:
            return JudgeResult(MatchDecision.MISS, short_reason="no eligible candidate")
        scenario = max(candidates, key=lambda item: item.score)
        candidate = max(scenario.positions, key=lambda item: item.score.total_log_score)
        return JudgeResult(
            MatchDecision.MATCH,
            candidate.scenario_id,
            candidate.position,
            candidate.lane_id,
            scenario.posterior,
            "highest calibrated scenario and position-path score",
        )
