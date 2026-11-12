from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

from .contracts import ContextFusionProvider, EmbeddingEncoder, FeatureComparator
from .defaults import cosine_similarity
from .domain import (
    BM25RecallCandidate,
    ContextRecallCandidate,
    FeatureRelation,
    FeatureSet,
    LaneRuntimeState,
    LocalRecallCandidate,
    MockSample,
    RecallCandidate,
    RecallFusionMode,
    Scenario,
)
from .execution import ExecutionResourcePool


class SemanticRecaller:
    def __init__(self, encoder: EmbeddingEncoder, store=None) -> None:
        self.encoder = encoder
        self.store = store
        self.indexes: dict[str, EndpointEmbeddingIndex] = {}

    def recall(
        self,
        query: str,
        features: FeatureSet[Any, Any],
        samples: Iterable[MockSample],
        k: int,
        comparator: FeatureComparator,
        timings=None,
        metrics=None,
    ) -> list[LocalRecallCandidate]:
        tick = time.perf_counter()
        query_embedding = (
            self.store.resources.call_component(self.encoder, self.encoder.encode_query, query)
            if self.store and self.store.resources
            else self.encoder.encode_query(query)
        )
        if timings is not None:
            timings["semantic_query_encode_ms"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        sample_list = list(samples)
        endpoint_id = sample_list[0].endpoint_id if sample_list else ""
        index = self.indexes.setdefault(
            endpoint_id, EndpointEmbeddingIndex(self.encoder, self.store)
        )
        index.refresh_if_changed(sample_list, metrics=metrics)
        if timings is not None:
            timings["semantic_index_load_ms"] = (time.perf_counter() - tick) * 1000
        tick = time.perf_counter()
        candidates = []
        for sample, similarity in index.recall(query_embedding, len(sample_list)):
            relation, _ = comparator.compare(features, sample.features)
            if relation is FeatureRelation.CONFLICT:
                continue
            candidates.append(
                LocalRecallCandidate(
                    sample.sample_hash,
                    str(sample.sample_id),
                    sample.position_id,
                    similarity,
                    sample,
                )
            )
        if timings is not None:
            timings["semantic_recall_ms"] = (time.perf_counter() - tick) * 1000
        return sorted(candidates, key=lambda item: item.similarity, reverse=True)[:k]


class LaneContextRecaller:
    def __init__(
        self,
        encoder: EmbeddingEncoder,
        execution_resources: ExecutionResourcePool | None = None,
    ) -> None:
        self.encoder = encoder
        self.execution_resources = execution_resources or ExecutionResourcePool()

    def recall(
        self,
        query: str,
        lanes: Iterable[LaneRuntimeState],
        scenarios: dict[str, Scenario[Any, Any]],
        endpoint_id: str,
        k: int,
        fusion: ContextFusionProvider,
    ) -> list[ContextRecallCandidate]:
        candidates: list[ContextRecallCandidate] = []
        from .domain import ContextMessage

        plans = []
        for lane in lanes:
            messages = []
            for interaction in lane.interactions:
                messages.extend(
                    (
                        ContextMessage("question", interaction.actual_query),
                        ContextMessage("answer", interaction.returned_answer),
                    )
                )
            messages.append(ContextMessage("question", query))
            plans.append((lane, messages))

        # Fan out all managed lane fusions before resolving any one lane. Unmanaged
        # providers intentionally retain direct synchronous execution.
        fused_values: list[str | Future[str] | None] = [
            self.execution_resources.submit_component(fusion, fusion.fuse, messages)
            for _, messages in plans
        ]
        for index, (_, messages) in enumerate(plans):
            if fused_values[index] is None:
                fused_values[index] = fusion.fuse(messages)

        for (lane, _), fused_value in zip(plans, fused_values, strict=True):
            runtime_context = (
                fused_value.result() if isinstance(fused_value, Future) else fused_value
            )
            assert runtime_context is not None
            runtime_embedding = self.encoder.encode_query(runtime_context)
            for scenario_id in lane.scenario_hypotheses:
                scenario = scenarios.get(scenario_id)
                if not scenario:
                    continue
                for position in scenario.positions:
                    if position.sample.endpoint_id != endpoint_id or not _round_in(
                        position.sample.round_id, lane.active_rounds
                    ):
                        continue
                    if position.context is None or position.context.embedding is None:
                        continue
                    embedding = position.context.embedding
                    candidates.append(
                        ContextRecallCandidate(
                            lane.lane_id,
                            scenario_id,
                            position.position,
                            cosine_similarity(runtime_embedding, embedding),
                        )
                    )
        return sorted(candidates, key=lambda item: item.similarity, reverse=True)[:k]


def fuse_recall(
    local: list[LocalRecallCandidate],
    context: list[ContextRecallCandidate],
    mode: RecallFusionMode,
    k: int,
) -> tuple[list[LocalRecallCandidate], list[ContextRecallCandidate]]:
    if mode is RecallFusionMode.UNION:
        return list(local), list(context)
    if mode is RecallFusionMode.SINGLE:
        return local[:k], []
    if mode is RecallFusionMode.RRF:
        ranked = [
            (1.0 / (60 + rank), "local", item) for rank, item in enumerate(local, start=1)
        ] + [(1.0 / (60 + rank), "context", item) for rank, item in enumerate(context, start=1)]
        selected = sorted(
            ranked,
            key=lambda value: (
                -value[0],
                value[2].scenario_id,
                value[2].position,
                getattr(value[2], "lane_id", ""),
            ),
        )[:k]
        return (
            [item for _, source, item in selected if source == "local"],
            [item for _, source, item in selected if source == "context"],
        )
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
            if next_local and (
                not next_context or next_local.similarity >= next_context.similarity
            ):
                selected_local.append(next_local)
            elif next_context:
                selected_context.append(next_context)
        return selected_local, selected_context
    combined = [(item.similarity, "local", item) for item in local] + [
        (item.similarity, "context", item) for item in context
    ]
    selected = sorted(combined, key=lambda item: item[0], reverse=True)[:k]
    return (
        [item for _, kind, item in selected if kind == "local"],
        [item for _, kind, item in selected if kind == "context"],
    )


def fuse_recall_candidates(
    semantic: list[LocalRecallCandidate],
    context: list[ContextRecallCandidate],
    bm25: list[BM25RecallCandidate],
    mode: RecallFusionMode,
    *,
    k: int,
    rrf_k: int = 60,
    single_source: str = "semantic",
    samples: Iterable[MockSample] = (),
) -> list[RecallCandidate]:
    """Fuse source results while preserving local and lane-path identity."""
    sample_map = {(str(sample.sample_id), sample.position_id): sample for sample in samples}
    merged: dict[tuple[str, int, str | None], RecallCandidate] = {}

    def add(
        scenario_id: str,
        position: int,
        lane_id: str | None,
        source: str,
        score: float,
        rank: int,
        sample: MockSample | None = None,
    ) -> None:
        sample = sample or sample_map.get((scenario_id, position))
        key = (scenario_id, position, lane_id)
        candidate = merged.get(key)
        if candidate is None:
            candidate = RecallCandidate(
                scenario_id,
                position,
                lane_id,
                sample.sample_hash if sample else None,
                sample.mocked_query if sample else "",
                fact_revision=sample.fact_revision if sample else 0,
            )
            merged[key] = candidate
        candidate.source_scores[source] = score
        candidate.source_ranks[source] = rank

    for rank, item in enumerate(semantic, start=1):
        add(item.scenario_id, item.position, None, "semantic", item.similarity, rank, item.sample)
    for rank, item in enumerate(context, start=1):
        add(item.scenario_id, item.position, item.lane_id, "context", item.similarity, rank)
    for item in bm25:
        add(item.scenario_id, item.position, None, "bm25", item.score, item.rank, item.sample)

    values = list(merged.values())
    if mode is RecallFusionMode.SINGLE:
        if single_source not in {"semantic", "context", "bm25"}:
            raise ValueError("single_recall_source must be semantic, context, or bm25")
        values = [item for item in values if single_source in item.source_scores]
    elif mode is RecallFusionMode.RRF:
        if rrf_k < 0:
            raise ValueError("rrf_k must not be negative")
        for item in values:
            item.fusion_score = sum(1.0 / (rrf_k + rank) for rank in item.source_ranks.values())
        values.sort(
            key=lambda item: (
                -item.fusion_score,
                item.scenario_id,
                item.position,
                item.lane_id or "",
            )
        )
        return values[:k]
    elif mode not in {RecallFusionMode.UNION}:
        selected_semantic, selected_context = fuse_recall(semantic, context, mode, k)
        allowed = {(item.scenario_id, item.position, None) for item in selected_semantic} | {
            (item.scenario_id, item.position, item.lane_id) for item in selected_context
        }
        values = [item for item in values if item_key(item) in allowed]

    return sorted(
        values,
        key=lambda item: (item.scenario_id, item.position, item.lane_id or ""),
    )


def item_key(item: RecallCandidate) -> tuple[str, int, str | None]:
    return item.scenario_id, item.position, item.lane_id


class EndpointEmbeddingIndex:
    """In-memory endpoint corpus index, rebuilt only when sample hashes change."""

    def __init__(self, encoder: EmbeddingEncoder, store=None) -> None:
        self.encoder = encoder
        self.store = store
        self.endpoint_id = None
        self.samples = []
        self.embeddings = []
        self.encoder_fingerprint = encoder.fingerprint
        self._signature = ()
        self._lock = threading.RLock()
        self._generation = 0

    def build(self, samples, metrics=None):
        values = list(samples)
        fp = self.encoder.fingerprint
        with self._lock:
            self._generation += 1
            generation = self._generation
            cached = (
                dict(zip(self._signature, self.embeddings))
                if fp == self.encoder_fingerprint
                else {}
            )
        if self.store:
            vectors = self.store.document_vectors(values, self.encoder, metrics=metrics)
        else:
            missing = [s for s in values if s.sample_hash not in cached]
            vectors = dict(cached)
            if missing:
                vectors.update(
                    zip(
                        [s.sample_hash for s in missing],
                        self.encoder.encode_documents([s.mocked_query for s in missing]),
                        strict=True,
                    )
                )

        def publish():
            with self._lock:
                if generation != self._generation or fp != self.encoder.fingerprint:
                    return
                self.samples = [s for s in values if s.sample_hash in vectors]
                self.embeddings = [vectors[s.sample_hash] for s in self.samples]
                self._signature = tuple(s.sample_hash for s in self.samples)
                self.encoder_fingerprint = fp
                self.endpoint_id = self.samples[0].endpoint_id if self.samples else None

        if self.store:
            with self.store.repository._read():
                # Membership is authoritative at publication, including removals during encoding.
                values = [
                    replace(current, features=s.features)
                    for s in values
                    if (current := self.store.repository.get_by_hash(s.sample_hash)) is not None
                ]
                publish()
        else:
            publish()

    refresh = build
    refresh_if_changed = build

    def recall(self, query_embedding: list[float], k: int) -> list[tuple[MockSample, float]]:
        def collect():
            with self._lock:
                pairs = list(zip(self.samples, self.embeddings, strict=True))
            if self.store:
                pairs = [
                    (replace(current, features=s.features), vector)
                    for s, vector in pairs
                    if (current := self.store.repository.get_by_hash(s.sample_hash)) is not None
                ]
            return [
                (sample, cosine_similarity(query_embedding, vector)) for sample, vector in pairs
            ]

        if self.store:
            with self.store.repository._read():
                values = collect()
        else:
            values = collect()
        return sorted(values, key=lambda item: item[1], reverse=True)[:k]


def _round_in(round_id: str | int, active_rounds: set[str | int]) -> bool:
    from .dag import normalize_round

    return normalize_round(round_id) in {normalize_round(value) for value in active_rounds}
