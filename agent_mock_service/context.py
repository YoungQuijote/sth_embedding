from __future__ import annotations

import threading
from typing import Any

from .contracts import ContextFusionProvider, EmbeddingEncoder
from .domain import ContextMessage, Scenario, ScenarioContext, ScenarioPosition


class ScenarioContextBuilder:
    """Build leak-free contexts: a position sees prior answers, never its own answer."""

    def __init__(self, fusion: ContextFusionProvider, encoder: EmbeddingEncoder) -> None:
        self.fusion = fusion
        self.encoder = encoder

    def build(self, scenario: Scenario[Any, Any]) -> Scenario[Any, Any]:
        ordered = sorted(
            scenario.positions, key=lambda item: (_round_key(item.sample.round_id), item.position)
        )
        history: list[ContextMessage] = []
        positions: list[ScenarioPosition[Any, Any]] = []
        current_round: str | int | None = None
        round_facts: list[ContextMessage] = []
        for position in ordered:
            if current_round is not None and position.sample.round_id != current_round:
                history.extend(round_facts)
                round_facts = []
            current_round = position.sample.round_id
            inputs = [*history, ContextMessage("question", position.sample.mocked_query)]
            raw = self.fusion.fuse(inputs)
            fused = self.fusion.fuse(inputs)
            positions.append(
                ScenarioPosition(
                    position.scenario_id,
                    position.position,
                    position.sample,
                    ScenarioContext(raw, fused, self.encoder.encode_document(fused)),
                    position.features,
                )
            )
            # Same-round positions are unordered and therefore cannot see sibling answers.
            round_facts.extend(
                (
                    ContextMessage("question", position.sample.mocked_query),
                    ContextMessage("answer", position.sample.mocked_answer),
                )
            )
        return Scenario(scenario.scenario_id, positions, scenario.registry_affinity_infos)


def _round_key(value: str | int) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


class ScenarioContextCache:
    """Thread-safe lazy cache for derived scenarios and their document embeddings."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], Scenario[Any, Any]] = {}
        self._lock = threading.RLock()

    def get(
        self, scenario_id: str, encoder_fingerprint: str, data_fingerprint: str
    ) -> Scenario[Any, Any] | None:
        with self._lock:
            return self._values.get((scenario_id, encoder_fingerprint, data_fingerprint))

    def put(
        self,
        scenario_id: str,
        encoder_fingerprint: str,
        data_fingerprint: str,
        scenario: Scenario[Any, Any],
    ) -> None:
        with self._lock:
            self._values[(scenario_id, encoder_fingerprint, data_fingerprint)] = scenario

    def invalidate(self, scenario_id: str) -> None:
        with self._lock:
            for key in [key for key in self._values if key[0] == scenario_id]:
                del self._values[key]
