from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mock_service.calibration import (
    CalibrationBootstrapper,
    CalibrationProfile,
    fit_distribution,
)
from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.domain import (
    FeatureSet,
    MatchDecision,
    MockRequest,
    MockSample,
    RecallFusionMode,
)
from agent_mock_service.lane import LaneManager
from agent_mock_service.recall import fuse_recall
from agent_mock_service.repository import SQLiteRepository, compute_sample_hash
from agent_mock_service.runtime import AgentMockRuntime
from agent_mock_service.scoring import availability_prior
from agent_mock_service.trace import JsonlTraceWriter, MemoryTraceWriter


@pytest.fixture
def repository(tmp_path):
    builder = ScenarioContextBuilder(JoiningContextFusionProvider(), HashingEncoder())
    value = SQLiteRepository(tmp_path / "mock.db", builder)
    yield value
    value.close()


def sample(
    query="query",
    answer="answer",
    scenario="1",
    round_id=1,
    position=1,
    endpoint="/agent",
    hard=None,
):
    return MockSample(
        endpoint, query, answer, scenario, round_id, position, FeatureSet(hard or {}, {})
    )


def test_hash_and_duplicate_registration(repository):
    original = sample()
    assert (
        compute_sample_hash(original)
        == "589e3ef990f35ff6b05ff81f805ddf2ee6f01f786b4dc4773544fb511197372b"
    )
    first = repository.register(original)
    second = repository.register(original)
    assert first.sample_hash == second.sample_hash
    assert second.availability.registry_times == 2
    assert compute_sample_hash(sample(answer="changed")) != first.sample_hash


def test_position_identity_is_stable(repository):
    repository.register(sample())
    with pytest.raises(ValueError):
        repository.register(sample(query="different"))


def test_scenario_context_excludes_current_and_same_round_answers(repository):
    repository.register(sample("q1", "secret-a1", round_id=1, position=1))
    repository.register(sample("q1b", "secret-a1b", round_id=1, position=2))
    repository.register(sample("q2", "secret-a2", round_id=2, position=3))
    scenario = repository.get("1")
    assert scenario is not None
    contexts = {item.position: item.context.fused_context for item in scenario.positions}
    assert "secret-a1" not in contexts[1]
    assert "secret-a1" not in contexts[2]
    assert "secret-a1" in contexts[3] and "secret-a1b" in contexts[3]
    assert "secret-a2" not in contexts[3]


def test_direct_match_returns_only_stored_answer_and_no_single_round_lane(repository):
    stored = repository.register(sample(" exact ", "stored answer"))
    trace = MemoryTraceWriter()
    runtime = AgentMockRuntime(repository, trace=trace)
    response = runtime.handle(MockRequest("/agent", "exact"))
    assert response.decision is MatchDecision.MATCH
    assert response.body == {"answer": "stored answer"}
    assert repository.get_by_hash(stored.sample_hash).availability.invoked_times == 1
    assert runtime.lanes.active() == []
    assert trace.events[0]["selection"]["path"] == "DIRECT_MATCH"


class KeywordEncoder:
    name = "keyword"
    version = "1"
    fingerprint = "keyword-v1"

    def encode(self, text):
        text = text.casefold()
        return [float("rome" in text or "罗马" in text), float("weather" in text or "天气" in text)]

    def encode_query(self, text):
        return self.encode(text)

    def encode_document(self, text):
        return self.encode(text)

    def encode_documents(self, texts):
        return [self.encode(text) for text in texts]


def test_semantic_match_and_endpoint_hard_scope(repository):
    repository.register(sample("Rome seven-day weather", "weather answer", endpoint="/weather"))
    repository.register(
        sample("Rome seven-day weather", "traffic answer", scenario="2", endpoint="/traffic")
    )
    runtime = AgentMockRuntime(repository, encoder=KeywordEncoder())
    response = runtime.handle(MockRequest("/weather", "罗马天气适合出行吗"))
    assert response.body == {"answer": "weather answer"}
    assert response.scenario_id == "1"


def test_hard_feature_conflict_eliminates_candidate(repository):
    repository.register(sample("device error", "E1 answer", scenario="e1", hard={"code": "E1"}))
    repository.register(
        sample("device error", "E2 answer", scenario="e2", position=2, hard={"code": "E2"})
    )

    class Extractor:
        def extract(self, query):
            return FeatureSet({"code": "E2"}, {})

    response = AgentMockRuntime(repository, feature_extractor=Extractor()).handle(
        MockRequest("/agent", "which device error")
    )
    assert response.body == {"answer": "E2 answer"}


def test_multi_round_lane_window_and_early_advance(repository):
    repository.register(sample("round one a", "a", round_id=1, position=1))
    repository.register(sample("round one b", "b", round_id=1, position=2))
    repository.register(sample("round two", "c", round_id=2, position=3))
    repository.register(sample("round three", "d", round_id=3, position=4))
    runtime = AgentMockRuntime(repository)
    runtime.handle(MockRequest("/agent", "round one a"))
    lane = runtime.lanes.active()[0]
    assert lane.active_rounds == {1, 2}
    runtime.handle(MockRequest("/agent", "round two"))
    assert runtime.lanes.active()[0].active_rounds == {2, 3}


def test_lane_ttl(repository):
    repository.register(sample("q1", "a1", round_id=1, position=1))
    repository.register(sample("q2", "a2", round_id=2, position=2))
    lanes = LaneManager(ttl_seconds=1)
    runtime = AgentMockRuntime(repository, lanes=lanes)
    runtime.handle(MockRequest("/agent", "q1"))
    lane = lanes.active()[0]
    assert lanes.active(now=lane.last_active_at + 2) == []


def test_atomic_invoked_increment(repository):
    stored = repository.register(sample())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: repository.increment_invoked(stored.sample_hash), range(100)))
    assert repository.get_by_hash(stored.sample_hash).availability.invoked_times == 100


def test_calibration_quantiles_smoothing_and_missing_data():
    distribution = fit_distribution([0.8, 0.9, 1.0], [0.1, 0.2, 0.3], bin_count=3)
    assert distribution.bins != [0.1, 0.2]
    assert all(value > 0 for value in distribution.positive + distribution.negative)
    assert fit_distribution([], [0.1]).llr(0.5) == 0.0
    profile = CalibrationBootstrapper().fit_profile(
        "dataset-fingerprint", "encoder", {"semantic": ([0.9], [0.1])}
    )
    assert profile.llr("semantic", 0.9) > profile.llr("semantic", 0.1)
    assert CalibrationProfile("v", "d", "e").llr("semantic", 0.5) == 0.0


def test_availability_is_soft():
    from agent_mock_service.domain import InvokeAvailability

    assert availability_prior(InvokeAvailability(1, 100)) == 0.0
    assert availability_prior(InvokeAvailability(10, 0)) == 1.0


def test_jsonl_trace_and_sse(repository, tmp_path):
    repository.register(sample(answer="你好"))
    path = tmp_path / "trace.jsonl"
    runtime = AgentMockRuntime(repository, trace=JsonlTraceWriter(path))
    from agent_mock_service.domain import ResponseProtocol

    response = runtime.handle(
        MockRequest("/agent", "query", response_protocol=ResponseProtocol.SSE)
    )
    assert response.headers["content-type"] == "text/event-stream"
    assert response.body == {"answer": "你好"}
    event = json.loads(path.read_text().splitlines()[0])
    assert event["request_id"].startswith("req_")
    assert event["selection"]["selected_sample_hash"]


def test_recall_fusion_modes():
    from agent_mock_service.domain import ContextRecallCandidate, LocalRecallCandidate

    local = [
        LocalRecallCandidate(str(i), "s", i, score, sample(position=i))
        for i, score in enumerate([0.9, 0.7, 0.5])
    ]
    context = [
        ContextRecallCandidate("l", "s", i, score) for i, score in enumerate([0.8, 0.6, 0.4])
    ]
    assert len(fuse_recall(local, context, RecallFusionMode.DOUBLE, 3)[0]) == 3
    assert fuse_recall(local, context, RecallFusionMode.SEMANTIC_ONLY, 2)[1] == []
    half = fuse_recall(local, context, RecallFusionMode.HALF, 3)
    assert sum(map(len, half)) == 3
    fused = fuse_recall(local, context, RecallFusionMode.SCORE_FUSION, 3)
    assert [item.similarity for group in fused for item in group]
