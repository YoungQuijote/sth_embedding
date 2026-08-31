from __future__ import annotations

import inspect

import pytest

from agent_mock_service.api import create_fastapi_app
from agent_mock_service.calibration import (
    CalibratedDistribution,
    CalibrationBootstrapper,
    CalibrationProfile,
    StaticCalibrationCorpusProvider,
)
from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import (
    DefaultFeatureComparator,
    HashingEncoder,
    JoiningContextFusionProvider,
)
from agent_mock_service.domain import (
    CalibrationSample,
    FeatureSet,
    LocalRecallCandidate,
    MockSample,
    RequestAffinityInfo,
    RuntimeInteraction,
)
from agent_mock_service.lane import LaneManager
from agent_mock_service.repository import SQLiteRepository
from agent_mock_service.runtime import AgentMockRuntime
from agent_mock_service.scoring import BayesEvidenceBuilder, BayesPositionScorer


class CountingEncoder(HashingEncoder):
    def __init__(self, fingerprint="counting"):
        super().__init__()
        self.fingerprint = fingerprint
        self.document_calls = 0

    def encode_document(self, text):
        self.document_calls += 1
        return super().encode_document(text)


class LookupEncoder:
    name = "lookup"
    version = "1"
    fingerprint = "lookup-v1"

    def __init__(self, vectors):
        self.vectors = vectors

    def encode_query(self, text):
        return self.vectors[text]

    def encode_document(self, text):
        return self.vectors[text]

    def encode_documents(self, texts):
        return [self.vectors[text] for text in texts]


def sample(scenario, round_id, position, query, answer="answer"):
    return MockSample("/agent", query, answer, scenario, round_id, position, FeatureSet({}, {}))


@pytest.fixture
def repository(tmp_path):
    encoder = CountingEncoder()
    value = SQLiteRepository(
        tmp_path / "v12.db", ScenarioContextBuilder(JoiningContextFusionProvider(), encoder)
    )
    yield value, encoder
    value.close()


def test_lane_advances_after_all_positions_in_round_match(repository):
    repo, _ = repository
    repo.register(sample("s", 10, 1, "q10a"))
    repo.register(sample("s", 10, 2, "q10b"))
    repo.register(sample("s", 20, 3, "q20"))
    repo.register(sample("s", 30, 4, "q30"))
    scenario = repo.get("s")
    lanes = LaneManager()
    lane = lanes.update(scenario, RuntimeInteraction("r1", "/agent", "s", 1, 10, "q", "a"))
    assert lane.active_rounds == {10, 20}
    lane = lanes.update(
        scenario, RuntimeInteraction("r2", "/agent", "s", 2, 10, "q", "a"), lane.lane_id
    )
    assert lane.active_rounds == {20, 30}


def test_lane_supports_non_contiguous_round_ids(repository):
    repo, _ = repository
    for round_id, position in ((10, 1), (20, 2), (40, 3)):
        repo.register(sample("s", round_id, position, f"q{round_id}"))
    scenario = repo.get("s")
    lanes = LaneManager()
    lane = lanes.update(scenario, RuntimeInteraction("r1", "/agent", "s", 1, 10, "q", "a"))
    assert lane.active_rounds == {20, 40}
    lane = lanes.update(
        scenario, RuntimeInteraction("r2", "/agent", "s", 2, 20, "q", "a"), lane.lane_id
    )
    assert lane.active_rounds == {40}


def test_dynamic_availability_is_fresh_without_context_reencoding(repository):
    repo, encoder = repository
    registered = repo.register(sample("s", 1, 1, "q"))
    repo.get("s")
    calls = encoder.document_calls
    repo.increment_invoked(registered.sample_hash)
    second = repo.get("s")
    assert second.positions[0].sample.availability.invoked_times == 1
    assert encoder.document_calls == calls
    repo.register(sample("s", 1, 1, "q"))
    third = repo.get("s")
    assert third.positions[0].sample.availability.registry_times == 2
    assert encoder.document_calls == calls


def test_runtime_rebinds_repository_to_single_encoder_space(repository):
    repo, first_encoder = repository
    repo.register(sample("s", 1, 1, "q"))
    repo.get("s")
    second_encoder = CountingEncoder("second-space")
    AgentMockRuntime(
        repo,
        encoder=second_encoder,
        calibration=CalibrationProfile("neutral", "x", second_encoder.fingerprint),
    )
    assert repo.context_builder.encoder is second_encoder
    repo.get("s")
    assert first_encoder.document_calls == 1
    assert second_encoder.document_calls == 1


def test_mismatched_calibration_encoder_cannot_silently_run(repository):
    repo, _ = repository
    with pytest.raises(ValueError, match="fingerprints differ"):
        AgentMockRuntime(
            repo,
            encoder=CountingEncoder("runtime"),
            calibration=CalibrationProfile("bad", "x", "other"),
        )


def test_semantic_bootstrap_is_leave_one_out_scenario_max():
    corpus = [
        CalibrationSample("/agent", name, "a", scenario, 1, index)
        for index, (scenario, name) in enumerate(
            (
                ("A", "A1"),
                ("A", "A2"),
                ("A", "A3"),
                ("B", "B1"),
                ("B", "B2"),
                ("C", "C1"),
                ("C", "C2"),
            ),
            1,
        )
    ]
    encoder = LookupEncoder(
        {
            "A1": [1.0, 0.0],
            "A2": [0.8, 0.6],
            "A3": [0.6, 0.8],
            "B1": [0.5, 0.8660254],
            "B2": [0.7, 0.7141428],
            "C1": [-1.0, 0.0],
            "C2": [-0.8, -0.6],
        }
    )
    positive, negative = CalibrationBootstrapper(1, 1, 42).semantic_scores(corpus, encoder)
    assert positive[0] == pytest.approx(0.8)
    assert negative[0] == pytest.approx(0.7)
    assert positive.count(1.0) == 1
    assert len(positive) == len(corpus) + 1


def test_calibration_corpus_lifecycle_is_independent_from_registry(repository):
    repo, _ = repository
    provider = StaticCalibrationCorpusProvider(
        [
            CalibrationSample("/agent", "a1", "a", "A", 1, 1),
            CalibrationSample("/agent", "a2", "a", "A", 1, 2),
            CalibrationSample("/agent", "b1", "b", "B", 1, 3),
        ]
    )
    encoder = HashingEncoder()
    first = AgentMockRuntime(repo, encoder=encoder, calibration_corpus=provider).calibration
    repo.register(sample("runtime-only", 1, 1, "new runtime sample"))
    second = AgentMockRuntime(repo, encoder=encoder, calibration_corpus=provider).calibration
    assert first.version == second.version
    changed_provider = StaticCalibrationCorpusProvider(
        [*provider.samples, CalibrationSample("/agent", "b2", "b", "B", 1, 4)]
    )
    changed = AgentMockRuntime(
        repo, encoder=encoder, calibration_corpus=changed_provider
    ).calibration
    assert changed.dataset_fingerprint != first.dataset_fingerprint
    assert changed.version != first.version
    assert "context" not in first.distributions
    assert first.llr("context", 0.99) == 0.0


def test_runtime_semantic_llr_uses_shared_scenario_max(repository):
    repo, _ = repository
    stored = [repo.register(sample("s", 1, index, f"q{index}")) for index in range(1, 4)]
    scenario = repo.get("s")
    local = [
        LocalRecallCandidate(value.sample_hash, "s", value.position_id, similarity, value)
        for value, similarity in zip(stored, (0.8, 0.4, 0.2), strict=True)
    ]
    evidence = BayesEvidenceBuilder().build(
        [scenario],
        "/agent",
        FeatureSet({}, {}),
        RequestAffinityInfo(),
        local,
        [],
        {},
        DefaultFeatureComparator(),
    )
    profile = CalibrationProfile(
        "v",
        "d",
        "sha256-token-256-v1",
        distributions={"semantic": CalibratedDistribution([0.5], [0.2, 0.8], [0.8, 0.2])},
    )
    scorer = BayesPositionScorer(profile, {}, 0.0, 1.0, 2.0)
    scores = [scorer.score(item[0]) for item in evidence]
    assert [score.scenario_semantic_raw for score in scores] == [0.8, 0.8, 0.8]
    assert [score.position_semantic_raw for score in scores] == [0.8, 0.4, 0.2]
    assert len({score.semantic_llr for score in scores}) == 1


def test_raw_context_is_not_business_fused(repository):
    repo, _ = repository

    class Fusion:
        def fuse(self, inputs):
            return "BUSINESS-FUSED"

    encoder = CountingEncoder("fusion-space")
    repo.bind_context_builder(ScenarioContextBuilder(Fusion(), encoder))
    repo.register(sample("s", 1, 1, "raw question"))
    context = repo.get("s").positions[0].context
    assert context.raw_context == "Question:\nraw question"
    assert context.fused_context == "BUSINESS-FUSED"


def test_fastapi_adapter_offloads_sync_runtime():
    source = inspect.getsource(create_fastapi_app)
    assert "run_in_threadpool" in source
