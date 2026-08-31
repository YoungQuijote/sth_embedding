from __future__ import annotations

from dataclasses import dataclass, field

from .domain import RecallFusionMode, ResponseProtocol

SENTENCE_TRANSFORMER_ENCODER: str | None = None


@dataclass(slots=True)
class RuntimeConfig:
    semantic_recall_k: int = 50
    context_recall_k: int = 50
    scenario_recall_n: int = 20
    recall_fusion_mode: RecallFusionMode = RecallFusionMode.SCORE_FUSION
    ambiguity_margin: float = 0.02
    buffer_timeout: float = 0.0
    lane_ttl_seconds: float = 900.0
    availability_alpha: float = 1.0
    availability_gamma: float = 2.0
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
