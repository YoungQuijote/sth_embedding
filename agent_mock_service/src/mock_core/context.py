from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from .contracts import ContextFusionProvider, EmbeddingEncoder, FusionProviderResolver
from .domain import ContextMessage, Scenario, ScenarioContext, ScenarioPosition
from .execution import ExecutionResourcePool


class ScenarioContextBuilder:
    """Build leak-free contexts: a position sees prior answers, never its own answer."""

    def __init__(
        self,
        fusion: ContextFusionProvider | FusionProviderResolver,
        encoder: EmbeddingEncoder,
        execution_resources: ExecutionResourcePool | None = None,
    ) -> None:
        self.fusion = fusion
        self.encoder = encoder
        self.execution_resources = execution_resources or ExecutionResourcePool()

    def _resolve_fusion(self, endpoint_id: str) -> ContextFusionProvider:
        resolver = getattr(self.fusion, "resolve", None)
        return resolver(endpoint_id) if resolver is not None else self.fusion  # type: ignore[return-value]

    @property
    def fingerprint(self) -> str:
        fusion_fingerprint = getattr(self.fusion, "fingerprint", None)
        if fusion_fingerprint is None:
            raise ValueError("fusion requires a reliable fingerprint")
        return f"{self.encoder.fingerprint}:{fusion_fingerprint}"

    def build(self, scenario: Scenario[Any, Any]) -> Scenario[Any, Any]:
        return self.build_many([scenario])[0]

    def build_many(self, scenarios: Sequence[Scenario[Any, Any]]) -> list[Scenario[Any, Any]]:
        """Fuse a cold scenario batch with one managed fan-out/fan-in boundary."""
        scenario_plans = [self._prepare(scenario) for scenario in scenarios]
        flat_plans = [plan for plans in scenario_plans for plan in plans]

        # Fan out every managed position in every scenario before waiting for any
        # result. The resource-scoped executors remain the sole concurrency limit.
        fused_values: list[str | Future[str] | None] = [
            self.execution_resources.submit_component(fusion, fusion.fuse, inputs)
            for _, inputs, _, fusion in flat_plans
        ]
        for index, (_, inputs, _, fusion) in enumerate(flat_plans):
            if fused_values[index] is None:
                fused_values[index] = fusion.fuse(inputs)

        results: list[Scenario[Any, Any]] = []
        offset = 0
        for scenario, plans in zip(scenarios, scenario_plans, strict=True):
            values = fused_values[offset : offset + len(plans)]
            offset += len(plans)
            positions: list[ScenarioPosition[Any, Any]] = []
            for (position, _, raw, _), fused_value in zip(plans, values, strict=True):
                fused = fused_value.result() if isinstance(fused_value, Future) else fused_value
                assert fused is not None
                positions.append(
                    ScenarioPosition(
                        position.scenario_id,
                        position.position,
                        position.sample,
                        ScenarioContext(raw, fused, self.encoder.encode_document(fused)),
                        position.features,
                    )
                )
            results.append(
                Scenario(scenario.scenario_id, positions, scenario.registry_affinity_infos)
            )
        return results

    def _prepare(
        self, scenario: Scenario[Any, Any]
    ) -> list[
        tuple[
            ScenarioPosition[Any, Any],
            list[ContextMessage],
            str,
            ContextFusionProvider,
        ]
    ]:
        ordered = sorted(
            scenario.positions, key=lambda item: (_round_key(item.sample.round_id), item.position)
        )
        history: list[ContextMessage] = []
        plans: list[
            tuple[
                ScenarioPosition[Any, Any],
                list[ContextMessage],
                str,
                ContextFusionProvider,
            ]
        ] = []
        current_round: str | int | None = None
        round_facts: list[ContextMessage] = []
        for position in ordered:
            if current_round is not None and position.sample.round_id != current_round:
                history.extend(round_facts)
                round_facts = []
            current_round = position.sample.round_id
            inputs = [*history, ContextMessage("question", position.sample.mocked_query)]
            raw = serialize_context_messages(inputs)
            fusion = self._resolve_fusion(position.sample.endpoint_id)
            plans.append((position, inputs, raw, fusion))
            # Same-round positions are unordered and therefore cannot see sibling answers.
            round_facts.extend(
                (
                    ContextMessage("question", position.sample.mocked_query),
                    ContextMessage("answer", position.sample.mocked_answer),
                )
            )

        return plans


def _round_key(value: str | int) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def serialize_context_messages(inputs: list[ContextMessage]) -> str:
    """Deterministically serialize raw messages without invoking business fusion."""
    return "\n\n".join(
        f"{'Question' if message.role == 'question' else 'Answer'}:\n{message.content.strip()}"
        for message in inputs
        if message.content.strip()
    )


class PositionContextCache:
    """Bounded acceleration for validated SQLite rows; callers receive independent values."""

    def __init__(self, capacity: int = 256) -> None:
        self.capacity = capacity
        self._values: dict[tuple, ScenarioContext] = {}
        self._lock = threading.RLock()

    def get(self, key: tuple) -> ScenarioContext | None:
        with self._lock:
            value = self._values.get(key)
            return deepcopy(value) if value is not None else None

    def put(self, key: tuple, value: ScenarioContext) -> None:
        with self._lock:
            if len(self._values) >= self.capacity:
                self._values.pop(next(iter(self._values)))
            self._values[key] = deepcopy(value)
