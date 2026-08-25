from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Literal, Mapping, TypeVar

HardFeaturesT = TypeVar("HardFeaturesT")
SoftFeaturesT = TypeVar("SoftFeaturesT")


class FeatureRelation(str, Enum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class MatchDecision(str, Enum):
    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    MISS = "MISS"


class RecallFusionMode(str, Enum):
    SEMANTIC_ONLY = "semantic_only"
    CONTEXT_ONLY = "context_only"
    SCORE_FUSION = "score_fusion"
    HALF = "half"
    DOUBLE = "double"


class ResponseProtocol(str, Enum):
    HTTP_JSON = "HTTP_JSON"
    SSE = "SSE"


@dataclass(slots=True)
class FeatureSet(Generic[HardFeaturesT, SoftFeaturesT]):
    hard_features: HardFeaturesT
    soft_features: SoftFeaturesT


@dataclass(slots=True)
class RequestAffinityInfo:
    request_ip: str | None = None
    subnet_mask: str | None = None
    user_agent: str | None = None
    auth_identity_hash: str | None = None
    trace_header: str | None = None


@dataclass(slots=True)
class InvokeAvailability:
    registry_times: int = 1
    invoked_times: int = 0


@dataclass(slots=True)
class MockSample:
    endpoint_id: str
    mocked_query: str
    mocked_answer: str
    sample_id: str
    round_id: str | int
    position_id: int = 0
    features: FeatureSet[Any, Any] = field(default_factory=lambda: FeatureSet({}, {}))
    sample_hash: str = ""
    availability: InvokeAvailability = field(default_factory=InvokeAvailability)


@dataclass(slots=True)
class ScenarioContext:
    raw_context: str
    fused_context: str
    embedding: list[float] | None = None


@dataclass(slots=True, frozen=True)
class ContextMessage:
    role: Literal["question", "answer"]
    content: str


@dataclass(slots=True)
class ScenarioPosition(Generic[HardFeaturesT, SoftFeaturesT]):
    scenario_id: str
    position: int
    sample: MockSample
    context: ScenarioContext
    features: FeatureSet[HardFeaturesT, SoftFeaturesT]


@dataclass(slots=True)
class Scenario(Generic[HardFeaturesT, SoftFeaturesT]):
    scenario_id: str
    positions: list[ScenarioPosition[HardFeaturesT, SoftFeaturesT]]
    registry_affinity_infos: list[RequestAffinityInfo] = field(default_factory=list)

    @property
    def rounds(self) -> list[str | int]:
        return list(dict.fromkeys(position.sample.round_id for position in self.positions))


@dataclass(slots=True)
class RuntimeInteraction:
    request_id: str
    endpoint_id: str
    scenario_id: str
    position: int
    round_id: str | int
    actual_query: str
    returned_answer: str


@dataclass(slots=True)
class ScenarioHypothesisState:
    scenario_id: str
    possible_positions: dict[int, float] = field(default_factory=dict)
    matched_positions: list[int] = field(default_factory=list)


@dataclass(slots=True)
class LaneRuntimeState:
    lane_id: str
    interactions: list[RuntimeInteraction] = field(default_factory=list)
    active_rounds: set[str | int] = field(default_factory=set)
    scenario_hypotheses: dict[str, ScenarioHypothesisState] = field(default_factory=dict)
    created_at: float = 0.0
    last_active_at: float = 0.0
    state_version: int = 0


@dataclass(slots=True)
class SemanticEvidence:
    raw_query: str
    candidate_query: str
    scenario_id: str
    candidate_position: int
    embedding_similarity: float


@dataclass(slots=True)
class ContextEvidence:
    raw_query: str
    lane_id: str
    runtime_fusion_inputs: list[str]
    runtime_fused_context: str
    scenario_fused_context: str
    scenario_id: str
    candidate_position: int
    embedding_similarity: float


@dataclass(slots=True)
class FeatureEvidence:
    query_features: Any
    scenario_features: Any
    features_evidence: Any


@dataclass(slots=True)
class AffinityEvidence:
    query_affinity_info: RequestAffinityInfo
    scenario_affinity_infos: list[RequestAffinityInfo]
    affinity_raw: float


@dataclass(slots=True)
class LocalRecallCandidate:
    sample_hash: str
    scenario_id: str
    position: int
    similarity: float
    sample: MockSample


@dataclass(slots=True)
class ContextRecallCandidate:
    lane_id: str
    scenario_id: str
    position: int
    similarity: float


@dataclass(slots=True)
class PositionScore:
    scenario_id: str
    position: int
    lane_id: str | None = None
    semantic_raw: float = 0.0
    context_raw: float = 0.0
    feature_raw: float = 0.0
    affinity_raw: float = 0.0
    semantic_llr: float = 0.0
    context_llr: float = 0.0
    feature_llr: float = 0.0
    affinity_llr: float = 0.0
    transition_prior: float = 0.0
    availability_prior: float = 0.0
    total_log_score: float = 0.0
    posterior: float = 0.0


@dataclass(slots=True)
class PositionPathEvidence:
    scenario_id: str
    position: int
    lane_id: str | None
    semantic_raw: float
    context_raw: float
    feature_raw: float
    affinity_raw: float
    round_id: str | int
    availability: InvokeAvailability
    active_rounds: set[str | int]


@dataclass(slots=True)
class JudgeCandidate:
    scenario_id: str
    position: int
    lane_id: str | None
    endpoint_id: str
    mocked_query: str
    score: PositionScore
    scenario_context: str
    runtime_context: str | None


@dataclass(slots=True)
class ScenarioJudgeCandidate:
    scenario_id: str
    score: float
    posterior: float
    positions: list[JudgeCandidate]


@dataclass(slots=True)
class JudgeResult:
    decision: MatchDecision
    scenario_id: str | None = None
    position: int | None = None
    lane_id: str | None = None
    confidence: float = 0.0
    short_reason: str = ""
    raw_output: str | None = None


@dataclass(slots=True)
class MockRequest:
    endpoint_id: str
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)
    upstream_request_id: str | None = None
    response_protocol: ResponseProtocol | None = None


@dataclass(slots=True)
class MockResponse:
    request_id: str
    decision: MatchDecision
    status_code: int
    body: Any
    headers: dict[str, str]
    scenario_id: str | None = None
    position: int | None = None
