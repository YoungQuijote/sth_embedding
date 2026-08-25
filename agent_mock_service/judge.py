from __future__ import annotations

from .domain import JudgeCandidate, JudgeResult, MatchDecision


class FakeJudge:
    """Deterministic judge for local execution; production LLM judges implement the same contract."""

    model_name = "deterministic-fake-judge"
    prompt_version = "v1-no-candidate-answers"

    def judge(self, query: str, lane_context: list[str], candidates: list[JudgeCandidate]) -> JudgeResult:
        if not candidates:
            return JudgeResult(MatchDecision.MISS, short_reason="no eligible candidate")
        candidate = max(candidates, key=lambda item: item.score.total_log_score)
        return JudgeResult(MatchDecision.MATCH, candidate.scenario_id, candidate.position, candidate.score.posterior, "highest calibrated scenario score")
