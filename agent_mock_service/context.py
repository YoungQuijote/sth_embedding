from __future__ import annotations

from typing import Any

from .contracts import ContextFusionProvider, EmbeddingEncoder
from .domain import Scenario, ScenarioContext, ScenarioPosition


class ScenarioContextBuilder:
    """Build leak-free contexts: a position sees prior answers, never its own answer."""

    def __init__(self, fusion: ContextFusionProvider, encoder: EmbeddingEncoder) -> None:
        self.fusion = fusion
        self.encoder = encoder

    def build(self, scenario: Scenario[Any, Any]) -> Scenario[Any, Any]:
        ordered = sorted(scenario.positions, key=lambda item: (_round_key(item.sample.round_id), item.position))
        history: list[str] = []
        positions: list[ScenarioPosition[Any, Any]] = []
        current_round: str | int | None = None
        round_answers: list[str] = []
        for position in ordered:
            if current_round is not None and position.sample.round_id != current_round:
                history.extend(round_answers)
                round_answers = []
            current_round = position.sample.round_id
            inputs = [*history, position.sample.mocked_query]
            raw = "\n".join(inputs)
            fused = self.fusion.fuse(inputs)
            positions.append(
                ScenarioPosition(position.scenario_id, position.position, position.sample, ScenarioContext(raw, fused, self.encoder.encode(fused)), position.features)
            )
            # Same-round positions are unordered and therefore cannot see sibling answers.
            round_answers.append(position.sample.mocked_answer)
        return Scenario(scenario.scenario_id, positions, scenario.registry_affinity_infos)


def _round_key(value: str | int) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))
