from dataclasses import replace

from agent_mock_service import AgentMockRuntime, RuntimeConfig
from agent_mock_service.mock_core.calibration import (
    CalibrationBootstrapper,
    CalibrationConfig,
    StaticCalibrationCorpusProvider,
)
from agent_mock_service.mock_core.defaults import DefaultFeatureComparator, HashingEncoder
from agent_mock_service.mock_core.domain import CalibrationSample, FeatureSet
from agent_mock_service.mock_core.repository import SQLiteRepository


def test_calibration_ignores_disabled_features_tokenizer_and_runtime_weights(tmp_path):
    repo = SQLiteRepository(tmp_path / "calibration.db")
    corpus = [
        CalibrationSample("/x", "query first", "answer", "s", 1, 1),
        CalibrationSample("/x", "query second", "answer", "s", 1, 2),
        CalibrationSample("/x", "unrelated", "answer", "other", 1, 1),
    ]
    provider = StaticCalibrationCorpusProvider(corpus)
    first = AgentMockRuntime(repo, calibration_corpus=provider)
    changed = StaticCalibrationCorpusProvider(
        [
            replace(
                s, features=FeatureSet({"key": "updated"}, {}), feature_fingerprint="new-extractor"
            )
            for s in corpus
        ]
    )
    second = AgentMockRuntime(
        repo,
        calibration_corpus=changed,
        config=RuntimeConfig(runtime_llr_weights={"semantic": 0.2, "bm25": 1}),
    )
    assert first.calibration.version == second.calibration.version
    bootstrap = CalibrationBootstrapper()
    config = CalibrationConfig(frozenset({"semantic", "context"}))
    a = bootstrap.bootstrap(corpus, HashingEncoder(), config=config, bm25_k1=1.1)
    b = bootstrap.bootstrap(corpus, HashingEncoder(), config=config, bm25_k1=2.1)
    assert a.version == b.version
    enabled = CalibrationConfig(frozenset({"feature"}))
    a = bootstrap.bootstrap(
        corpus, HashingEncoder(), config=enabled, feature_comparator=DefaultFeatureComparator()
    )
    b = bootstrap.bootstrap(
        changed.samples,
        HashingEncoder(),
        config=enabled,
        feature_comparator=DefaultFeatureComparator(),
    )
    assert a.version != b.version
