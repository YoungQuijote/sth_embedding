from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .contracts import EmbeddingEncoder
from .defaults import cosine_similarity
from .domain import RecallCandidate
from .execution import ExecutionResourcePool


def _stable_rerank(
    candidates: Sequence[RecallCandidate], scores: Sequence[float], top_n: int | None
) -> list[RecallCandidate]:
    values = [
        replace(candidate, rerank_score=float(score))
        for candidate, score in zip(candidates, scores, strict=True)
    ]
    values.sort(
        key=lambda item: (
            -(item.rerank_score if item.rerank_score is not None else float("-inf")),
            item.scenario_id,
            item.position,
            item.lane_id or "",
        )
    )
    return values if top_n is None else values[:top_n]


class BiEncoderReranker:
    """Default reranker reusing dense recall scores and cached document embeddings."""

    def __init__(self, encoder: EmbeddingEncoder, store=None) -> None:
        self.encoder = encoder
        self.store = store
        self.fingerprint = f"bi-encoder-reranker:{encoder.fingerprint}"
        self._document_cache: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_n: int | None = None,
        metrics=None,
    ) -> list[RecallCandidate]:
        # Without explicit truncation, preserve Union coverage and avoid extra encoding.
        if top_n is None:
            return [
                replace(candidate, rerank_score=candidate.source_scores.get("semantic"))
                for candidate in candidates
            ]
        query_embedding = self.encoder.encode_query(query)
        persisted = {}
        if self.store:
            with self.store.repository._read():
                samples = [
                    sample
                    for candidate in candidates
                    if candidate.source_scores.get("semantic") is None
                    and candidate.sample_hash
                    and (sample := self.store.repository.get_by_hash(candidate.sample_hash))
                ]
            persisted = self.store.document_vectors(
                list({sample.sample_hash: sample for sample in samples}.values()),
                self.encoder,
                metrics=metrics,
            )
        scores, retained = [], []
        for candidate in candidates:
            dense = candidate.source_scores.get("semantic")
            if dense is None:
                if self.store:
                    embedding = persisted.get(candidate.sample_hash)
                    if embedding is None:
                        continue  # deleted while encoding; never resurrect a stale candidate
                else:
                    key = f"{self.encoder.fingerprint}:{candidate.sample_hash or candidate.mocked_query}"
                    with self._lock:
                        embedding = self._document_cache.get(key)
                    if embedding is None:
                        embedding = self.encoder.encode_document(candidate.mocked_query)
                        with self._lock:
                            self._document_cache[key] = embedding
                dense = cosine_similarity(query_embedding, embedding)
            retained.append(candidate)
            scores.append(dense)
        return _stable_rerank(retained, scores, top_n)


class CrossEncoderReranker:
    """Optional SentenceTransformers CrossEncoder adapter; never receives mock answers."""

    def __init__(
        self,
        model_path: str,
        *,
        execution_resources: ExecutionResourcePool | None = None,
        execution_resource_id: str | None = None,
        batch_size: int = 32,
        max_length: int | None = None,
        max_candidates: int | None = None,
        fallback: Any | None = None,
    ) -> None:
        from sentence_transformers import CrossEncoder

        if batch_size <= 0:
            raise ValueError("cross-encoder batch_size must be positive")
        if max_candidates is not None and max_candidates <= 0:
            raise ValueError("cross-encoder max_candidates must be positive")
        self.model_path = str(Path(model_path).resolve())
        kwargs = {"max_length": max_length} if max_length is not None else {}
        self.model = CrossEncoder(self.model_path, **kwargs)
        self.batch_size = batch_size
        self.max_candidates = max_candidates
        self.execution_resources = execution_resources or ExecutionResourcePool()
        self._execution_component = (
            type(
                "CrossEncoderExecutionComponent",
                (),
                {"execution_resource_id": execution_resource_id},
            )()
            if execution_resource_id is not None
            else object()
        )
        self.fallback = fallback
        package_version = getattr(__import__("sentence_transformers"), "__version__", "unknown")
        self.fingerprint = hashlib.sha256(
            f"cross-encoder:{self.model_path}:{package_version}:{max_length}".encode()
        ).hexdigest()

    def _predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        values = self.model.predict(pairs, batch_size=self.batch_size)
        return [float(value) for value in values]

    def rerank(
        self,
        query: str,
        candidates: Sequence[RecallCandidate],
        *,
        top_n: int | None = None,
    ) -> list[RecallCandidate]:
        selected = (
            list(candidates[: self.max_candidates])
            if self.max_candidates is not None
            else list(candidates)
        )
        pairs = [(query, candidate.mocked_query) for candidate in selected]
        try:
            scores = self.execution_resources.call_component(
                self._execution_component, self._predict, pairs
            )
        except Exception:
            if self.fallback is None:
                raise
            return self.fallback.rerank(query, candidates, top_n=top_n)
        return _stable_rerank(selected, scores, top_n)
