from __future__ import annotations

import concurrent.futures
import sys
import types
from dataclasses import replace

import pytest

from agent_mock_service.mock_core.bm25 import BM25Recaller, DefaultTokenizer
from agent_mock_service.mock_core.calibration import (
    CalibratedDistribution,
    CalibrationBootstrapper,
    CalibrationConfig,
    CalibrationProfile,
)
from agent_mock_service.mock_core.config import RuntimeConfig
from agent_mock_service.mock_core.defaults import HashingEncoder
from agent_mock_service.mock_core.domain import (
    BM25RecallCandidate,
    CalibrationSample,
    ContextRecallCandidate,
    FeatureRelation,
    FeatureSet,
    InvokeAvailability,
    JudgeResult,
    LocalRecallCandidate,
    MatchDecision,
    MockRequest,
    MockSample,
    PositionPathEvidence,
    RecallCandidate,
    RecallFusionMode,
)
from agent_mock_service.mock_core.execution import ExecutionResourcePool
from agent_mock_service.mock_core.recall import fuse_recall_candidates
from agent_mock_service.mock_core.repository import SQLiteRepository
from agent_mock_service.mock_core.reranker import BiEncoderReranker, CrossEncoderReranker
from agent_mock_service.mock_core.runtime import AgentMockRuntime
from agent_mock_service.mock_core.scoring import BayesPositionScorer


class WhitespaceTokenizer:
    fingerprint = "whitespace-v1"

    def tokenize(self, text: str) -> list[str]:
        return text.casefold().split()


def sample(query: str, scenario: str, position: int = 1, endpoint: str = "/search") -> MockSample:
    return MockSample(
        endpoint,
        query,
        f"answer-{scenario}",
        scenario,
        1,
        position,
        sample_hash=f"{endpoint}-{scenario}-{position}",
    )


def test_bm25_matches_terms_and_is_endpoint_scoped() -> None:
    recaller = BM25Recaller(WhitespaceTokenizer())
    weather = [sample("rome weather forecast", "rome")]
    hardware = [sample("rome weather forecast", "hardware", endpoint="/hardware")]
    assert recaller.recall("rome forecast", weather, 10)[0].scenario_id == "rome"
    assert recaller.recall("rome forecast", hardware, 10)[0].scenario_id == "hardware"
    assert set(recaller.indexes) == {"/search", "/hardware"}


def test_hybrid_runtime_configuration_is_validated() -> None:
    assert RuntimeConfig().recall_fusion_mode is RecallFusionMode.UNION
    with pytest.raises(ValueError, match="BM25"):
        RuntimeConfig(bm25_b=1.5)
    with pytest.raises(ValueError, match="single_recall_source"):
        RuntimeConfig(single_recall_source="unknown")


def test_bm25_tokenizer_fingerprint_and_corpus_changes_refresh_index() -> None:
    tokenizer = WhitespaceTokenizer()
    recaller = BM25Recaller(tokenizer)
    first = sample("alpha beta", "a")
    assert recaller.recall("alpha", [first], 10)
    old_fingerprint = recaller.indexes["/search"].fingerprint
    tokenizer.fingerprint = "whitespace-v2"
    second = sample("gamma delta", "b")
    assert recaller.recall("gamma", [second], 10)[0].scenario_id == "b"
    assert recaller.indexes["/search"].fingerprint != old_fingerprint


def test_bm25_concurrent_retrieval_is_safe() -> None:
    recaller = BM25Recaller(DefaultTokenizer())
    samples = [sample(f"model-{index} weather", str(index), index) for index in range(20)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: recaller.recall("weather", samples, 5), range(30)))
    assert all(len(result) == 5 for result in results)


def test_bm25_index_converges_after_repository_register_and_delete(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "bm25-lifecycle.sqlite")
    recaller = BM25Recaller(WhitespaceTokenizer())
    first = repository.register(sample("rome weather", "rome"))
    assert recaller.recall("rome", repository.list_endpoint("/search"), 10)
    second = repository.register(sample("paris weather", "paris", 2))
    assert {
        item.scenario_id
        for item in recaller.recall("weather", repository.list_endpoint("/search"), 10)
    } == {"rome", "paris"}
    repository.delete(first.sample_hash)
    assert [
        item.scenario_id
        for item in recaller.recall("weather", repository.list_endpoint("/search"), 10)
    ] == ["paris"]
    repository.delete(second.sample_hash)
    assert recaller.recall("weather", repository.list_endpoint("/search"), 10) == []
    repository.close()


def _recall_inputs():
    a = sample("alpha", "a")
    b = sample("beta", "b")
    semantic = [LocalRecallCandidate(a.sample_hash, "a", 1, 0.9, a)]
    context = [ContextRecallCandidate("lane-1", "a", 1, 0.8)]
    bm25 = [BM25RecallCandidate(b.sample_hash, "b", 1, 3.0, 1, b)]
    return a, b, semantic, context, bm25


def test_union_and_single_preserve_sources_and_lane_paths() -> None:
    a, b, semantic, context, bm25 = _recall_inputs()
    union = fuse_recall_candidates(
        semantic,
        context,
        bm25,
        RecallFusionMode.UNION,
        k=1,
        samples=[a, b],
    )
    assert {(item.scenario_id, item.lane_id) for item in union} == {
        ("a", None),
        ("a", "lane-1"),
        ("b", None),
    }
    assert next(
        item for item in union if item.scenario_id == "a" and item.lane_id is None
    ).source_scores == {"semantic": 0.9}
    single = fuse_recall_candidates(
        semantic,
        context,
        bm25,
        RecallFusionMode.SINGLE,
        k=10,
        single_source="bm25",
        samples=[a, b],
    )
    assert [(item.scenario_id, item.lane_id) for item in single] == [("b", None)]


def test_rrf_calculation_and_tie_break_are_deterministic() -> None:
    a, b, semantic, context, bm25 = _recall_inputs()
    bm25.insert(0, BM25RecallCandidate(a.sample_hash, "a", 1, 4.0, 1, a))
    bm25[1] = replace(bm25[1], rank=2)
    result = fuse_recall_candidates(
        semantic,
        context,
        bm25,
        RecallFusionMode.RRF,
        k=10,
        rrf_k=60,
        samples=[a, b],
    )
    local_a = next(item for item in result if item.scenario_id == "a" and item.lane_id is None)
    assert local_a.fusion_score == pytest.approx(2 / 61)
    assert result == fuse_recall_candidates(
        semantic,
        context,
        bm25,
        RecallFusionMode.RRF,
        k=10,
        rrf_k=60,
        samples=[a, b],
    )


def test_bi_encoder_reranker_preserves_bm25_only_coverage_without_top_n() -> None:
    candidate = RecallCandidate("s", 1, None, "h", "query", {"bm25": 2.0}, {"bm25": 1})
    result = BiEncoderReranker(HashingEncoder()).rerank("different", [candidate])
    assert result[0].scenario_id == "s"
    assert result[0].rerank_score is None


def test_cross_encoder_batches_query_and_mocked_query_without_answers(monkeypatch) -> None:
    observed = []

    class FakeCrossEncoder:
        def __init__(self, model_path, **kwargs):
            self.model_path = model_path

        def predict(self, pairs, batch_size):
            observed.extend(pairs)
            return [0.1, 0.9]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(CrossEncoder=FakeCrossEncoder, __version__="test"),
    )
    pool = ExecutionResourcePool()
    pool.register("cross", 1)
    reranker = CrossEncoderReranker(
        "/tmp/model",
        execution_resources=pool,
        execution_resource_id="cross",
        batch_size=2,
    )
    candidates = [
        RecallCandidate("a", 1, None, "a", "first query"),
        RecallCandidate("b", 2, "lane", "b", "second query"),
    ]
    try:
        result = reranker.rerank("agent query", candidates)
        assert [(item.scenario_id, item.lane_id) for item in result] == [
            ("b", "lane"),
            ("a", None),
        ]
        assert observed == [
            ("agent query", "first query"),
            ("agent query", "second query"),
        ]
        assert all("answer" not in text for pair in observed for text in pair)
    finally:
        pool.shutdown()


class SoftComparator:
    fingerprint = "soft-comparator-v1"

    def compare(self, query: FeatureSet, candidate: FeatureSet):
        return FeatureRelation.MATCH, float(query.soft_features == candidate.soft_features)


def calibration_samples() -> list[CalibrationSample]:
    return [
        CalibrationSample("/x", "rome weather", "", "a", 1, 1, FeatureSet({}, "a")),
        CalibrationSample("/x", "rome forecast", "", "a", 1, 2, FeatureSet({}, "a")),
        CalibrationSample("/x", "paris weather", "", "b", 1, 3, FeatureSet({}, "b")),
        CalibrationSample("/x", "paris forecast", "", "b", 1, 4, FeatureSet({}, "b")),
    ]


def test_multi_evidence_calibration_and_compatibility_fingerprint() -> None:
    bootstrapper = CalibrationBootstrapper()
    config = CalibrationConfig(frozenset({"semantic", "bm25", "feature", "context"}))
    first = bootstrapper.bootstrap(
        calibration_samples(),
        HashingEncoder(),
        config=config,
        tokenizer=WhitespaceTokenizer(),
        feature_comparator=SoftComparator(),
    )
    assert {"semantic", "bm25", "feature"} <= first.distributions.keys()
    assert "context" not in first.distributions
    assert first.evidence_status["context"].startswith("unavailable")
    changed = bootstrapper.bootstrap(
        calibration_samples(),
        HashingEncoder(),
        config=config,
        tokenizer=DefaultTokenizer(),
        feature_comparator=SoftComparator(),
    )
    assert changed.compatibility_fingerprint != first.compatibility_fingerprint
    assert changed.version != first.version


def test_runtime_llr_weight_zero_and_uncalibrated_evidence_are_neutral() -> None:
    profile = CalibrationProfile(
        "v",
        "d",
        "e",
        distributions={
            "semantic": CalibratedDistribution([], [0.9], [0.1]),
            "bm25": CalibratedDistribution([], [0.9], [0.1]),
        },
    )
    evidence = PositionPathEvidence(
        "s", 1, None, 1.0, 1.0, None, None, None, 1, InvokeAvailability(1, 1), None, 3.0
    )
    scorer = BayesPositionScorer(
        profile,
        {},
        0.0,
        1.0,
        2.0,
        0.0,
        {"semantic": 1.0, "bm25": 0.0},
    )
    score = scorer.score(evidence)
    assert score.bm25_llr != 0.0
    assert score.evidence_contributions["bm25"] == 0.0
    assert score.total_log_score == score.evidence_contributions["semantic"]
    neutral = BayesPositionScorer(
        CalibrationProfile("n", "d", "e"),
        {},
        0.0,
        1.0,
        2.0,
        0.0,
        {"bm25": 1.0},
    ).score(evidence)
    assert neutral.bm25_raw == 3.0
    assert neutral.bm25_llr == neutral.evidence_contributions["bm25"] == 0.0


class SelectingJudge:
    model_name = "test"
    prompt_version = "1"

    def __init__(self) -> None:
        self.seen = []

    def judge(self, query, candidates):
        self.seen = candidates
        for scenario in candidates:
            for position in scenario.positions:
                if "rome" in position.mocked_query:
                    return JudgeResult(MatchDecision.MATCH, scenario.scenario_id, position.position)
        return JudgeResult(MatchDecision.MISS)


def test_bm25_only_candidate_reaches_judge_and_repository_answer(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "hybrid.sqlite")
    repository.register(sample("rome seven day forecast", "rome"))
    repository.register(sample("siemens refrigerator manual", "hardware", 2))
    judge = SelectingJudge()
    runtime = AgentMockRuntime(
        repository,
        judge=judge,
        config=RuntimeConfig(
            semantic_recall_k=0,
            recall_fusion_mode=RecallFusionMode.SINGLE,
            single_recall_source="bm25",
        ),
    )
    try:
        response = runtime.handle(MockRequest("/search", "please forecast rome"))
        assert response.decision is MatchDecision.MATCH
        assert response.body["answer"] == "answer-rome"
        assert any(item.scenario_id == "rome" for item in judge.seen)
        event = runtime.trace.events[-1]
        assert event["recall"]["fusion_mode"] == "single"
        assert event["recall"]["bm25"][0]["rank"] == 1
        assert event["reranker"]["input_count"] >= 1
        assert {
            "bm25_raw",
            "bm25_llr",
            "evidence_weights",
            "evidence_contributions",
        } <= event["bayes_scores"][0].keys()
    finally:
        repository.close()
