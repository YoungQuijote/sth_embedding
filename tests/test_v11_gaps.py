from __future__ import annotations

import inspect
import asyncio
from dataclasses import replace

import pytest

from agent_mock_service.calibration import (
    CalibrationBootstrapper,
    CalibrationProfile,
    StaticCalibrationCorpusProvider,
)
from agent_mock_service.config import RuntimeConfig
from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import (
    HashingEncoder,
    JoiningContextFusionProvider,
    create_default_encoder,
)
from agent_mock_service.domain import (
    FeatureSet,
    CalibrationSample,
    JudgeCandidate,
    JudgeResult,
    MatchDecision,
    MockRequest,
    MockSample,
    PositionScore,
    RecallFusionMode,
    RuntimeInteraction,
)
from agent_mock_service.lane import LaneManager
from agent_mock_service.repository import SQLiteRepository
from agent_mock_service.response import sse_event_stream
from agent_mock_service.runtime import AgentMockRuntime
from agent_mock_service.scoring import ScenarioScoreAggregator


class CountingEncoder(HashingEncoder):
    fingerprint = "counting-v1"

    def __init__(self):
        super().__init__()
        self.query_calls = 0
        self.document_calls = 0
        self.document_batch_calls = 0

    def encode_query(self, text):
        self.query_calls += 1
        return super().encode_query(text)

    def encode_document(self, text):
        self.document_calls += 1
        return super().encode_document(text)

    def encode_documents(self, texts):
        self.document_batch_calls += 1
        return [self.encode(text) for text in texts]


def make_sample(scenario, round_id, position, query, answer, endpoint="/agent"):
    return MockSample(endpoint, query, answer, scenario, round_id, position, FeatureSet({}, {}))


@pytest.fixture
def repository(tmp_path):
    encoder = HashingEncoder()
    repository = SQLiteRepository(
        tmp_path / "v11.db", ScenarioContextBuilder(JoiningContextFusionProvider(), encoder)
    )
    yield repository
    repository.close()


def test_context_contains_previous_queries_and_answers(repository):
    repository.register(make_sample("s", 1, 1, "Q1a", "A1a"))
    repository.register(make_sample("s", 1, 2, "Q1b", "A1b"))
    repository.register(make_sample("s", 2, 3, "Q2", "A2"))
    scenario = repository.get("s")
    context = next(item.context.fused_context for item in scenario.positions if item.position == 3)
    assert all(
        value in context
        for value in (
            "Question:\nQ1a",
            "Answer:\nA1a",
            "Question:\nQ1b",
            "Answer:\nA1b",
            "Question:\nQ2",
        )
    )
    assert "A2" not in context


def test_same_round_siblings_do_not_leak_answers(repository):
    repository.register(make_sample("s", 1, 1, "Q1a", "A1a"))
    repository.register(make_sample("s", 1, 2, "Q1b", "A1b"))
    scenario = repository.get("s")
    contexts = {item.position: item.context.fused_context for item in scenario.positions}
    assert "A1b" not in contexts[1]
    assert "A1a" not in contexts[2]


def test_same_scenario_can_have_multiple_active_lanes(repository):
    repository.register(make_sample("s", 1, 1, "q1", "a1"))
    repository.register(make_sample("s", 2, 2, "q2", "a2"))
    scenario = repository.get("s")
    lanes = LaneManager()
    first = lanes.update(scenario, RuntimeInteraction("r1", "/agent", "s", 1, 1, "one", "a1"))
    second = lanes.update(scenario, RuntimeInteraction("r2", "/agent", "s", 1, 1, "two", "a1"))
    assert first.lane_id != second.lane_id
    assert len(lanes.find_all_for_scenario("s")) == 2


def test_local_match_does_not_reuse_ambiguous_same_scenario_lane(repository):
    repository.register(make_sample("s", 1, 1, "q1", "a1"))
    repository.register(make_sample("s", 2, 2, "q2", "a2"))

    class LocalFirstJudge:
        model_name = "local"
        prompt_version = "v1"

        def judge(self, query, candidates):
            position = next(
                position
                for scenario in candidates
                for position in scenario.positions
                if position.position == 1 and position.lane_id is None
            )
            return JudgeResult(
                MatchDecision.MATCH, position.scenario_id, position.position, None, 1.0, "local"
            )

    runtime = AgentMockRuntime(
        repository,
        config=RuntimeConfig(recall_fusion_mode=RecallFusionMode.SEMANTIC_ONLY),
        judge=LocalFirstJudge(),
        calibration=CalibrationProfile("n", "", "sha256-token-256-v1"),
    )
    runtime.handle(MockRequest("/agent", "q1"))
    # A local, non-direct match must create another runtime path, never take an arbitrary lane.
    runtime.handle(MockRequest("/agent", " q1 paraphrase "))
    assert len(runtime.lanes.find_all_for_scenario("s")) == 2


class SelectLaneJudge:
    model_name = "select-lane"
    prompt_version = "v1"

    def __init__(self, lane_id):
        self.lane_id = lane_id

    def judge(self, query, candidates):
        position = next(
            position
            for scenario in candidates
            for position in scenario.positions
            if position.lane_id == self.lane_id
        )
        return JudgeResult(
            MatchDecision.MATCH,
            position.scenario_id,
            position.position,
            position.lane_id,
            1.0,
            "test",
        )


def test_context_match_updates_exact_selected_lane(repository):
    repository.register(make_sample("s", 1, 1, "first", "answer"))
    repository.register(make_sample("s", 2, 2, "follow up", "done"))
    scenario = repository.get("s")
    lanes = LaneManager()
    first = lanes.update(scenario, RuntimeInteraction("r1", "/agent", "s", 1, 1, "Rome", "answer"))
    second = lanes.update(
        scenario, RuntimeInteraction("r2", "/agent", "s", 1, 1, "Siemens", "answer")
    )
    runtime = AgentMockRuntime(
        repository,
        lanes=lanes,
        judge=SelectLaneJudge(first.lane_id),
        calibration=CalibrationProfile("n", "", "sha256-token-256-v1"),
    )
    response = runtime.handle(MockRequest("/agent", "follow-up paraphrase"))
    assert response.status_code == 200
    assert len(first.interactions) == 2
    assert len(second.interactions) == 1


class CaptureJudge:
    model_name = "capture"
    prompt_version = "v1"

    def __init__(self):
        self.candidates = []

    def judge(self, query, candidates):
        self.candidates = candidates
        return JudgeResult(MatchDecision.MISS, short_reason="captured")


def test_judge_receives_only_candidate_specific_lane_context(repository):
    for scenario_id, topic in (("rome", "Rome"), ("fridge", "Siemens")):
        repository.register(make_sample(scenario_id, 1, 1, f"start {topic}", f"answer {topic}"))
        repository.register(make_sample(scenario_id, 2, 2, "follow up", f"done {topic}"))
    lanes = LaneManager()
    for scenario_id, topic in (("rome", "Rome"), ("fridge", "Siemens")):
        scenario = repository.get(scenario_id)
        lanes.update(
            scenario,
            RuntimeInteraction(topic, "/agent", scenario_id, 1, 1, topic, f"answer {topic}"),
        )
    judge = CaptureJudge()
    AgentMockRuntime(
        repository,
        lanes=lanes,
        judge=judge,
        calibration=CalibrationProfile("n", "", "sha256-token-256-v1"),
    ).handle(MockRequest("/agent", "a follow-up question"))
    for scenario in judge.candidates:
        for position in scenario.positions:
            if position.lane_id and scenario.scenario_id == "rome":
                assert (
                    "Rome" in position.runtime_context and "Siemens" not in position.runtime_context
                )
            if position.lane_id and scenario.scenario_id == "fridge":
                assert (
                    "Siemens" in position.runtime_context and "Rome" not in position.runtime_context
                )


def test_scenario_aggregation_uses_max_and_top_n_scenarios():
    aggregator = ScenarioScoreAggregator()
    candidates = []
    for position in range(10):
        score = PositionScore("a", position, total_log_score=float(position))
        candidates.append(JudgeCandidate("a", position, None, "/a", "q", score, "ctx", None))
    candidates.append(
        JudgeCandidate(
            "b", 1, None, "/a", "q", PositionScore("b", 1, total_log_score=8.5), "ctx", None
        )
    )
    result = aggregator.aggregate(candidates, top_n=2)
    assert len(result) == 2
    assert result[0].scenario_id == "a" and result[0].score == 9.0
    assert len(result[0].positions) == 10
    assert sum(item.posterior for item in result) == pytest.approx(1.0)


def test_sentence_transformer_fallbacks(tmp_path, caplog):
    assert isinstance(create_default_encoder(None), HashingEncoder)
    assert isinstance(create_default_encoder(str(tmp_path / "missing")), HashingEncoder)
    assert isinstance(create_default_encoder(str(tmp_path)), HashingEncoder)
    assert "using HashingEncoder" in caplog.text


def test_endpoint_embedding_index_does_not_reencode_documents(repository):
    repository.register(make_sample("a", 1, 1, "alpha", "A"))
    repository.register(make_sample("b", 1, 2, "beta", "B"))
    encoder = CountingEncoder()
    runtime = AgentMockRuntime(
        repository, encoder=encoder, calibration=CalibrationProfile("n", "", encoder.fingerprint)
    )
    runtime.handle(MockRequest("/agent", "alpha-ish"))
    runtime.handle(MockRequest("/agent", "beta-ish"))
    assert encoder.document_batch_calls == 1
    assert encoder.query_calls == 2


def test_scenario_context_cache_and_invalidation(tmp_path):
    encoder = CountingEncoder()
    repository = SQLiteRepository(
        tmp_path / "cache.db", ScenarioContextBuilder(JoiningContextFusionProvider(), encoder)
    )
    repository.register(make_sample("s", 1, 1, "q1", "a1"))
    repository.get("s")
    first_count = encoder.document_calls
    repository.get("s")
    assert encoder.document_calls == first_count
    repository.register(make_sample("s", 2, 2, "q2", "a2"))
    repository.get("s")
    assert encoder.document_calls > first_count
    repository.close()


def test_calibration_fingerprint_persistence_and_compatibility(repository):
    initial = repository.dataset_fingerprint()
    repository.register(make_sample("a", 1, 1, "alpha", "A"))
    assert repository.dataset_fingerprint() != initial
    repository.register(make_sample("b", 1, 2, "beta", "B"))
    bootstrapper = CalibrationBootstrapper()
    corpus = StaticCalibrationCorpusProvider(
        [
            CalibrationSample("/agent", "alpha", "A", "a", 1, 1),
            CalibrationSample("/agent", "beta", "B", "b", 1, 2),
        ]
    )
    profile = bootstrapper.load_or_bootstrap(repository, HashingEncoder(), corpus)
    assert repository.load_calibration_profile(profile.version).version == profile.version
    assert (
        bootstrapper.load_or_bootstrap(repository, HashingEncoder(), corpus).version
        == profile.version
    )
    different = replace(profile, version="different", encoder_fingerprint="other")
    repository.save_calibration_profile(different)
    assert (
        repository.find_compatible_profile(
            profile.dataset_fingerprint, "other", profile.algorithm_version
        ).version
        == "different"
    )


def test_sse_transport_is_async_streaming():
    stream = sse_event_stream({"answer": "ok"})
    assert inspect.isasyncgen(stream)
    assert asyncio.run(anext(stream)) == 'data: {"answer": "ok"}\n\n'
