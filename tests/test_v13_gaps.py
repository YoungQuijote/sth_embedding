from __future__ import annotations

import sqlite3

import pytest

from agent_mock_service.calibration import CalibratedDistribution, CalibrationProfile
from agent_mock_service.defaults import HashingEncoder
from agent_mock_service.domain import (
    FeatureRelation,
    FeatureSet,
    ContextRecallCandidate,
    InvokeAvailability,
    JudgeResult,
    MatchDecision,
    MockRequest,
    MockSample,
    PositionPathEvidence,
    RequestAffinityInfo,
    LaneRuntimeState,
    ScenarioHypothesisState,
)
from agent_mock_service.plugin import BusinessPlugin, BusinessPluginRegistry
from agent_mock_service.repository import SQLiteRepository
from agent_mock_service.runtime import AgentMockRuntime
from agent_mock_service.scoring import (
    BayesEvidenceBuilder,
    BayesPositionScorer,
    calibrated_llr,
    transition_prior,
)


def sample(endpoint, scenario, position, round_id, query, answer="answer"):
    return MockSample(endpoint, query, answer, scenario, round_id, position, FeatureSet({}, {}))


def calibrated_profile():
    distribution = CalibratedDistribution([0.5], [0.1, 0.9], [0.9, 0.1])
    return CalibrationProfile(
        "v",
        "d",
        HashingEncoder.fingerprint,
        distributions={
            name: distribution for name in ("semantic", "context", "feature", "affinity")
        },
    )


@pytest.mark.parametrize(
    ("field", "llr_field"),
    (
        ("scenario_semantic_raw", "semantic_llr"),
        ("context_raw", "context_llr"),
        ("feature_raw", "feature_llr"),
        ("affinity_raw", "affinity_llr"),
    ),
)
def test_missing_evidence_is_neutral_but_observed_zero_is_calibrated(field, llr_field):
    profile = calibrated_profile()
    values = {
        "scenario_semantic_raw": None,
        "position_semantic_raw": None,
        "context_raw": None,
        "feature_raw": None,
        "affinity_raw": None,
    }
    evidence = PositionPathEvidence(
        "s",
        1,
        None,
        **values,
        round_id=1,
        availability=InvokeAvailability(),
        transition_distance=None,
    )
    scorer = BayesPositionScorer(profile, {}, 0.1, 1.0, 2.0)
    assert getattr(scorer.score(evidence), llr_field) == 0.0
    values[field] = 0.0
    observed = PositionPathEvidence(
        "s",
        1,
        None,
        **values,
        round_id=1,
        availability=InvokeAvailability(),
        transition_distance=None,
    )
    assert getattr(scorer.score(observed), llr_field) == calibrated_llr(
        profile, field.removesuffix("_raw").replace("scenario_", ""), 0.0
    )
    assert getattr(scorer.score(observed), llr_field) != 0.0


def test_same_scenario_position_cannot_exist_in_two_endpoints(tmp_path):
    repository = SQLiteRepository(tmp_path / "unique.db")
    repository.register(sample("/weather", "s", 1, 1, "weather"))
    with pytest.raises(ValueError, match="uniquely identify one ScenarioPosition"):
        repository.register(sample("/traffic", "s", 1, 1, "traffic"))
    repository.register(sample("/traffic", "s", 2, 2, "traffic"))
    assert len(repository.get("s").positions) == 2
    repository.close()


def test_old_database_without_global_position_uniqueness_is_rejected(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE scenario_membership(
      scenario_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, sample_hash TEXT PRIMARY KEY,
      position_id INTEGER NOT NULL, affinity_json TEXT NOT NULL DEFAULT '[]');
    INSERT INTO scenario_membership VALUES('s','/a','one',1,'[]');
    INSERT INTO scenario_membership VALUES('s','/b','two',1,'[]');
    """)
    connection.close()
    with pytest.raises(RuntimeError, match="[Rr]ebuild or migrate"):
        SQLiteRepository(path)


def test_transition_uses_round_ordinal_distance():
    table = {-1: 0.1, 0: 1.0, 1: 0.9, 2: 0.5}
    assert transition_prior(1, table, 0.01) == 0.9
    assert transition_prior(None, table, 0.01) == 0.0
    assert transition_prior(10, table, 0.01) == 0.01


def test_evidence_builder_computes_ordinal_transition_and_missing_values(tmp_path):
    repository = SQLiteRepository(tmp_path / "ordinal.db")
    repository.register(sample("/agent", "s", 1, 10, "q10"))
    repository.register(sample("/agent", "s", 2, 20, "q20"))
    repository.register(sample("/agent", "s", 3, 40, "q40"))
    scenario = repository.get("s")
    lane = LaneRuntimeState(
        "lane",
        active_rounds={10, 20},
        scenario_hypotheses={"s": ScenarioHypothesisState("s")},
    )
    evidence = BayesEvidenceBuilder().build(
        [scenario],
        "/agent",
        FeatureSet({}, {}),
        RequestAffinityInfo(),
        [],
        [ContextRecallCandidate("lane", "s", 2, 0.7)],
        {"lane": lane},
        RecordingComparator(),
    )[0][0]
    assert evidence.transition_distance == 1
    assert evidence.scenario_semantic_raw is None
    assert evidence.position_semantic_raw is None
    assert evidence.context_raw == 0.7
    assert evidence.feature_raw is None
    assert evidence.affinity_raw is None
    assert transition_prior(evidence.transition_distance, {1: 0.9}, 0.1) == 0.9
    repository.close()


class RecordingParser:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.calls = 0

    def parse(self, body):
        self.calls += 1
        return f"{self.endpoint} paraphrase"


class RecordingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, query):
        self.calls += 1
        return FeatureSet({}, {})


class RecordingComparator:
    def __init__(self):
        self.calls = 0

    def compare(self, query, candidate):
        self.calls += 1
        return FeatureRelation.UNKNOWN, 0.0


class PrefixFusion:
    def __init__(self, prefix):
        self.prefix = prefix
        self.fingerprint = f"fusion-{prefix}"

    def fuse(self, inputs):
        return self.prefix + ":" + "|".join(message.content for message in inputs)


class RecordingJudge:
    model_name = "recording"
    prompt_version = "v1"

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.calls = 0

    def judge(self, query, candidates):
        self.calls += 1
        position = next(
            position
            for scenario in candidates
            for position in scenario.positions
            if position.endpoint_id == self.endpoint
        )
        return JudgeResult(
            MatchDecision.MATCH,
            position.scenario_id,
            position.position,
            position.lane_id,
            1.0,
            "plugin route",
        )


def make_plugin(endpoint):
    parser = RecordingParser(endpoint)
    extractor = RecordingExtractor()
    comparator = RecordingComparator()
    fusion = PrefixFusion(endpoint)
    judge = RecordingJudge(endpoint)
    return (
        BusinessPlugin(endpoint, parser, extractor, comparator, fusion, judge),
        parser,
        extractor,
        comparator,
        fusion,
        judge,
    )


def test_runtime_routes_business_components_by_endpoint_and_shares_resources(tmp_path):
    repository = SQLiteRepository(tmp_path / "plugins.db")
    repository.register(sample("/weather", "s", 1, 1, "weather preset", "sunny"))
    repository.register(sample("/traffic", "s", 2, 2, "traffic preset", "clear"))
    weather = make_plugin("/weather")
    traffic = make_plugin("/traffic")
    registry = BusinessPluginRegistry(allow_default_plugin=False)
    registry.register(weather[0])
    registry.register(traffic[0])
    encoder = HashingEncoder()
    runtime = AgentMockRuntime(
        repository,
        business_plugins=registry,
        encoder=encoder,
        calibration=CalibrationProfile("n", "d", encoder.fingerprint),
    )

    weather_response = runtime.handle(MockRequest("/weather", {"business": "weather"}))
    assert weather_response.status_code == 200
    assert weather[1].calls == weather[2].calls == weather[5].calls == 1
    assert weather[3].calls >= 2  # recall filter plus evidence construction
    assert traffic[1].calls == traffic[2].calls == traffic[5].calls == 0

    scenario = repository.get("s")
    contexts = {
        position.position: position.context.fused_context for position in scenario.positions
    }
    assert contexts[1].startswith("/weather:")
    assert contexts[2].startswith("/traffic:")
    assert "sunny" in contexts[2]

    traffic_response = runtime.handle(MockRequest("/traffic", {"business": "traffic"}))
    assert traffic_response.status_code == 200
    assert traffic[1].calls == traffic[2].calls == traffic[5].calls == 1
    assert runtime.repository is repository
    assert runtime.encoder is encoder
    assert runtime.business_plugins is registry
    assert len(runtime.lanes.active()) <= 1  # terminal match closes the shared cross-endpoint lane
    repository.close()


def test_direct_cross_endpoint_matches_share_one_lane(tmp_path):
    repository = SQLiteRepository(tmp_path / "lane.db")
    repository.register(sample("/weather", "s", 1, 1, "weather", "sunny"))
    repository.register(sample("/traffic", "s", 2, 2, "traffic", "clear"))
    weather = make_plugin("/weather")[0]
    traffic = make_plugin("/traffic")[0]
    # Direct parsers are used here so the deterministic direct-match path is exercised.
    weather.parser = type("Parser", (), {"parse": lambda self, body: "weather"})()
    traffic.parser = type("Parser", (), {"parse": lambda self, body: "traffic"})()
    registry = BusinessPluginRegistry(allow_default_plugin=False)
    registry.register(weather)
    registry.register(traffic)
    runtime = AgentMockRuntime(repository, business_plugins=registry, encoder=HashingEncoder())
    runtime.handle(MockRequest("/weather", {}))
    lane = runtime.lanes.active()[0]
    runtime.handle(MockRequest("/traffic", {}))
    assert len(lane.interactions) == 2
    assert {item.endpoint_id for item in lane.interactions} == {"/weather", "/traffic"}
    repository.close()


def test_plugin_registry_duplicate_default_and_missing_endpoint_behavior(tmp_path):
    plugin = make_plugin("/weather")[0]
    registry = BusinessPluginRegistry(allow_default_plugin=False)
    registry.register(plugin)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(plugin)
    repository = SQLiteRepository(tmp_path / "missing.db")
    runtime = AgentMockRuntime(repository, business_plugins=registry, encoder=HashingEncoder())
    response = runtime.handle(MockRequest("/unknown", {}))
    assert response.status_code == 404
    assert response.decision is MatchDecision.MISS
    repository.close()
