from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, TypeVar

from .domain import (
    FeatureRelation,
    FeatureSet,
    JudgeCandidate,
    JudgeResult,
    MockSample,
    RequestAffinityInfo,
    Scenario,
)

QueryInputT = TypeVar("QueryInputT", contravariant=True)


class QueryParser(Protocol[QueryInputT]):
    def parse(self, body: QueryInputT) -> str: ...


class EmbeddingEncoder(Protocol):
    name: str
    version: str
    fingerprint: str

    def encode(self, text: str) -> list[float]: ...


class BusinessFeatureExtractor(Protocol):
    def extract(self, query: str) -> FeatureSet[Any, Any]: ...


class FeatureComparator(Protocol):
    def compare(self, query: FeatureSet[Any, Any], candidate: FeatureSet[Any, Any]) -> tuple[FeatureRelation, float]: ...


class ContextFusionProvider(Protocol):
    def fuse(self, inputs: list[str]) -> str: ...


class AffinityExtractor(Protocol):
    def extract(self, headers: Mapping[str, str]) -> RequestAffinityInfo: ...


class Judge(Protocol):
    model_name: str
    prompt_version: str

    def judge(self, query: str, lane_context: list[str], candidates: list[JudgeCandidate]) -> JudgeResult: ...


class SampleRepository(Protocol):
    def register(self, sample: MockSample, affinity: RequestAffinityInfo | None = None) -> MockSample: ...
    def list_endpoint(self, endpoint_id: str) -> list[MockSample]: ...
    def increment_invoked(self, sample_hash: str) -> None: ...
    def get_by_hash(self, sample_hash: str) -> MockSample | None: ...


class ScenarioRepository(Protocol):
    def get(self, scenario_id: str) -> Scenario[Any, Any] | None: ...
    def get_many(self, scenario_ids: Iterable[str]) -> list[Scenario[Any, Any]]: ...


class TraceWriter(Protocol):
    def write(self, event: Mapping[str, Any]) -> None: ...
