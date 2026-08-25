from __future__ import annotations

from typing import Any, Iterable

from .contracts import ContextFusionProvider, EmbeddingEncoder, FeatureComparator
from .defaults import cosine_similarity
from .domain import ContextRecallCandidate, FeatureRelation, FeatureSet, LaneRuntimeState, LocalRecallCandidate, MockSample, RecallFusionMode, Scenario


class SemanticRecaller:
    def __init__(self, encoder: EmbeddingEncoder, comparator: FeatureComparator) -> None:
        self.encoder = encoder
        self.comparator = comparator

    def recall(self, query: str, features: FeatureSet[Any, Any], samples: Iterable[MockSample], k: int) -> list[LocalRecallCandidate]:
        query_embedding = self.encoder.encode(query)
        candidates = []
        for sample in samples:
            relation, _ = self.comparator.compare(features, sample.features)
            if relation is FeatureRelation.CONFLICT:
                continue
            similarity = cosine_similarity(query_embedding, self.encoder.encode(sample.mocked_query))
            candidates.append(LocalRecallCandidate(sample.sample_hash, str(sample.sample_id), sample.position_id, similarity, sample))
        return sorted(candidates, key=lambda item: item.similarity, reverse=True)[:k]


class LaneContextRecaller:
    def __init__(self, encoder: EmbeddingEncoder, fusion: ContextFusionProvider) -> None:
        self.encoder = encoder
        self.fusion = fusion

    def recall(self, query: str, lanes: Iterable[LaneRuntimeState], scenarios: dict[str, Scenario[Any, Any]], endpoint_id: str, k: int) -> list[ContextRecallCandidate]:
        candidates: list[ContextRecallCandidate] = []
        for lane in lanes:
            history = [value for interaction in lane.interactions for value in (interaction.actual_query, interaction.returned_answer)]
            runtime_context = self.fusion.fuse([*history, query])
            runtime_embedding = self.encoder.encode(runtime_context)
            for scenario_id in lane.scenario_hypotheses:
                scenario = scenarios.get(scenario_id)
                if not scenario:
                    continue
                for position in scenario.positions:
                    if position.sample.endpoint_id != endpoint_id or position.sample.round_id not in lane.active_rounds:
                        continue
                    embedding = position.context.embedding or self.encoder.encode(position.context.fused_context)
                    candidates.append(ContextRecallCandidate(lane.lane_id, scenario_id, position.position, cosine_similarity(runtime_embedding, embedding)))
        return sorted(candidates, key=lambda item: item.similarity, reverse=True)[:k]


def fuse_recall(local: list[LocalRecallCandidate], context: list[ContextRecallCandidate], mode: RecallFusionMode, k: int) -> tuple[list[LocalRecallCandidate], list[ContextRecallCandidate]]:
    if mode is RecallFusionMode.SEMANTIC_ONLY:
        return local[:k], []
    if mode is RecallFusionMode.CONTEXT_ONLY:
        return [], context[:k]
    if mode is RecallFusionMode.DOUBLE:
        return local[:k], context[:k]
    if mode is RecallFusionMode.HALF:
        half = k // 2
        selected_local, selected_context = local[:half], context[:half]
        if k % 2:
            next_local = local[half] if len(local) > half else None
            next_context = context[half] if len(context) > half else None
            if next_local and (not next_context or next_local.similarity >= next_context.similarity):
                selected_local.append(next_local)
            elif next_context:
                selected_context.append(next_context)
        return selected_local, selected_context
    combined = [(item.similarity, "local", item) for item in local] + [(item.similarity, "context", item) for item in context]
    selected = sorted(combined, key=lambda item: item[0], reverse=True)[:k]
    return ([item for _, kind, item in selected if kind == "local"], [item for _, kind, item in selected if kind == "context"])
