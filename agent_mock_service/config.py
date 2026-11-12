from __future__ import annotations

import math
from dataclasses import dataclass, field

from .domain import RecallFusionMode, ResponseProtocol

SENTENCE_TRANSFORMER_ENCODER: str | None = None


@dataclass(slots=True)
class RuntimeConfig:
    semantic_recall_k: int = 50
    context_recall_k: int = 50
    scenario_recall_n: int = 20
    bm25_enabled: bool = True
    bm25_recall_k: int = 50
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    recall_fusion_mode: RecallFusionMode = RecallFusionMode.UNION
    single_recall_source: str = "semantic"
    rrf_k: int = 60
    fused_recall_k: int | None = None
    reranker_type: str = "bi_encoder"
    reranker_top_n: int | None = None
    runtime_llr_weights: dict[str, float] = field(
        default_factory=lambda: {
            "semantic": 1.0,
            "context": 0.0,
            "bm25": 0.0,
            "feature": 0.0,
        }
    )
    ambiguity_margin: float = 0.02
    buffer_timeout: float = 0.0
    lane_ttl_seconds: float = 900.0
    availability_alpha: float = 1.0
    availability_gamma: float = 2.0
    availability_prior_weight: float = 0.1823215567939546
    default_response_protocol: ResponseProtocol = ResponseProtocol.HTTP_JSON
    sentence_transformer_encoder: str | None = SENTENCE_TRANSFORMER_ENCODER
    bootstrap_hard_negative_k: int = 3
    bootstrap_easy_negative_k: int = 3
    bootstrap_random_seed: int = 42
    allow_default_plugin: bool = True
    transition_prior: dict[int, float] = field(
        default_factory=lambda: {-2: 0.0, -1: 0.1, 0: 1.0, 1: 0.9, 2: 0.5, 3: 0.3}
    )
    transition_floor: float = 0.1

    def __post_init__(self) -> None:
        for name in ("semantic_recall_k", "context_recall_k", "bm25_recall_k"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.bm25_k1 <= 0 or not 0 <= self.bm25_b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        if self.rrf_k < 0:
            raise ValueError("rrf_k must not be negative")
        if self.fused_recall_k is not None and self.fused_recall_k <= 0:
            raise ValueError("fused_recall_k must be positive when configured")
        if self.reranker_top_n is not None and self.reranker_top_n <= 0:
            raise ValueError("reranker_top_n must be positive when configured")
        if self.single_recall_source not in {"semantic", "context", "bm25"}:
            raise ValueError("single_recall_source must be semantic, context, or bm25")
        if any(not math.isfinite(value) for value in self.runtime_llr_weights.values()):
            raise ValueError("runtime LLR weights must be finite")
