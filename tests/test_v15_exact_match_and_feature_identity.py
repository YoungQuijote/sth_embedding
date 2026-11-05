from __future__ import annotations

from agent_mock_service.calibration import CalibrationProfile
from agent_mock_service.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.domain import (
    FeatureSet,
    JudgeResult,
    MatchDecision,
    MockRequest,
    MockSample,
    RequestAffinityInfo,
)
from agent_mock_service.runtime import AgentMockRuntime
from agent_mock_service.repository import SQLiteRepository


class CountingFeatureExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, query):
        self.calls += 1
        return FeatureSet({}, {})


class CountingEncoder(HashingEncoder):
    fingerprint = "exact-fast-path-encoder"

    def __init__(self):
        super().__init__()
        self.query_calls = 0
        self.document_calls = 0

    def encode_query(self, text):
        self.query_calls += 1
        return super().encode_query(text)

    def encode_document(self, text):
        self.document_calls += 1
        return super().encode_document(text)


class CountingFusion(JoiningContextFusionProvider):
    def __init__(self):
        self.calls = 0

    def fuse(self, inputs):
        self.calls += 1
        return super().fuse(inputs)


class CountingAffinityExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, headers):
        self.calls += 1
        return RequestAffinityInfo()


class CountingJudge:
    model_name = "counting"
    prompt_version = "v1"

    def __init__(self):
        self.calls = 0

    def judge(self, query, candidates):
        self.calls += 1
        position = candidates[0].positions[0]
        return JudgeResult(
            MatchDecision.MATCH,
            position.scenario_id,
            position.position,
            position.lane_id,
            1.0,
            "test",
        )


def sample(position=1, query="exact"):
    return MockSample("/agent", query, f"answer-{position}", "s", 1, position)


def runtime(repository, extractor, encoder, judge, fusion=None, affinity=None):
    return AgentMockRuntime(
        repository,
        feature_extractor=extractor,
        encoder=encoder,
        judge=judge,
        fusion=fusion,
        affinity_extractor=affinity,
        calibration=CalibrationProfile("n", "d", encoder.fingerprint),
    )


def test_exact_match_does_not_call_feature_extractor_encoder_or_judge(tmp_path):
    repository = SQLiteRepository(tmp_path / "exact.db")
    repository.register(sample())
    extractor = CountingFeatureExtractor()
    encoder = CountingEncoder()
    judge = CountingJudge()
    fusion = CountingFusion()
    affinity = CountingAffinityExtractor()
    response = runtime(repository, extractor, encoder, judge, fusion, affinity).handle(
        MockRequest("/agent", "exact")
    )
    assert response.decision is MatchDecision.MATCH
    assert extractor.calls == 0
    assert encoder.query_calls == 0
    assert encoder.document_calls == 0
    assert fusion.calls == 0
    assert affinity.calls == 0
    assert judge.calls == 0
    repository.close()


def test_ambiguous_exact_query_still_calls_feature_extractor_and_judge(tmp_path):
    repository = SQLiteRepository(tmp_path / "ambiguous.db")
    repository.register(sample(1))
    repository.register(sample(2))
    extractor = CountingFeatureExtractor()
    encoder = CountingEncoder()
    judge = CountingJudge()
    response = runtime(repository, extractor, encoder, judge).handle(MockRequest("/agent", "exact"))
    assert response.decision is MatchDecision.MATCH
    assert extractor.calls == 1
    assert encoder.query_calls == 1
    assert judge.calls == 1
    repository.close()


def test_non_exact_query_calls_feature_extractor_once(tmp_path):
    repository = SQLiteRepository(tmp_path / "semantic.db")
    repository.register(sample())
    extractor = CountingFeatureExtractor()
    encoder = CountingEncoder()
    judge = CountingJudge()
    response = runtime(repository, extractor, encoder, judge).handle(
        MockRequest("/agent", "semantic paraphrase")
    )
    assert response.decision is MatchDecision.MATCH
    assert extractor.calls == 1
    assert encoder.query_calls == 1
    assert judge.calls == 1
    repository.close()
