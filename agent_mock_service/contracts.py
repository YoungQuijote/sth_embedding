from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Iterable, Mapping, Protocol, TypeVar

from .domain import (
    FeatureRelation,
    FeatureSet,
    ContextMessage,
    CalibrationProbe,
    CalibrationSample,
    JudgeResult,
    MockSample,
    RequestAffinityInfo,
    Scenario,
    ScenarioJudgeCandidate,
)

QueryInputT = TypeVar("QueryInputT", contravariant=True)


class QueryParser(Protocol[QueryInputT]):
    def parse(self, body: QueryInputT) -> str: ...


class EmbeddingEncoder(Protocol):
    name: str
    version: str
    fingerprint: str

    def encode_query(self, text: str) -> list[float]: ...
    def encode_document(self, text: str) -> list[float]: ...
    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class BusinessFeatureExtractor(Protocol):
    def extract(self, query: str) -> FeatureSet[Any, Any]: ...


class FeatureComparator(Protocol):
    def compare(
        self, query: FeatureSet[Any, Any], candidate: FeatureSet[Any, Any]
    ) -> tuple[FeatureRelation, float]: ...


class ContextFusionProvider(Protocol):
    def fuse(self, inputs: Sequence[ContextMessage]) -> str: ...


class FusionProviderResolver(Protocol):
    fingerprint: str

    def resolve(self, endpoint_id: str) -> ContextFusionProvider: ...


class AffinityExtractor(Protocol):
    def extract(self, headers: Mapping[str, str]) -> RequestAffinityInfo: ...


class Judge(Protocol):
    model_name: str
    prompt_version: str

    def judge(self, query: str, candidates: list[ScenarioJudgeCandidate]) -> JudgeResult: ...


class SampleRepository(Protocol):
    def register(
        self, sample: MockSample, affinity: RequestAffinityInfo | None = None
    ) -> MockSample: ...
    def list_endpoint(self, endpoint_id: str) -> list[MockSample]: ...
    def increment_invoked(self, sample_hash: str) -> None: ...
    def get_by_hash(self, sample_hash: str) -> MockSample | None: ...
    def unregister(self, sample_hash: str, affinity: RequestAffinityInfo | None = None) -> bool: ...
    def delete(self, sample_hash: str) -> bool: ...


class ScenarioRepository(Protocol):
    def get_facts(self, scenario_id: str) -> Scenario[Any, Any] | None: ...
    def get(self, scenario_id: str) -> Scenario[Any, Any] | None: ...
    def get_many(self, scenario_ids: Iterable[str]) -> list[Scenario[Any, Any]]: ...
    def delete_scenario(
        self,
        scenario_id: str,
        *,
        force: bool = False,
        affinity: RequestAffinityInfo | None = None,
    ) -> int: ...


class TraceWriter(Protocol):
    def write(self, event: Mapping[str, Any]) -> None: ...


class CalibrationCorpusProvider(Protocol):
    def load(self) -> Sequence[CalibrationSample]: ...


class CalibrationProbeProvider(Protocol):
    """V2 hook for labelled paraphrases or reconstructed RuntimeTrace probes."""

    def load(self) -> Sequence[CalibrationProbe]: ...
