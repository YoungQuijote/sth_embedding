from __future__ import annotations

import threading
import time

import pytest

from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import HashingEncoder
from agent_mock_service.domain import (
    FeatureSet,
    LaneRuntimeState,
    MockSample,
    RuntimeInteraction,
    Scenario,
    ScenarioContext,
    ScenarioHypothesisState,
    ScenarioPosition,
)
from agent_mock_service.execution import ExecutionResourcePool
from agent_mock_service.recall import LaneContextRecaller
from agent_mock_service.repository import SQLiteRepository


class TrackingFusion:
    execution_resource_id = "fuse"

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def fuse(self, messages: list[object]) -> str:
        contents = [getattr(message, "content") for message in messages]
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append(contents[-1])
        try:
            # Deliberately vary completion order without changing result association.
            time.sleep(self.delay * (1 + (len(contents[-1]) % 3)))
            return "|".join(contents)
        finally:
            with self.lock:
                self.active -= 1


def _register_scenario(
    repository: SQLiteRepository, scenario_id: str, position_count: int = 1
) -> None:
    for position in range(position_count):
        repository.register(
            MockSample(
                "/test",
                f"{scenario_id}-q{position}",
                f"{scenario_id}-a{position}",
                scenario_id,
                10,
                position,
            )
        )


def _repository(tmp_path, capacity: int = 4):
    pool = ExecutionResourcePool()
    pool.register("fuse", capacity)
    fusion = TrackingFusion(0.01)
    repository = SQLiteRepository(tmp_path / "fanout.sqlite")
    repository.bind_context_builder(ScenarioContextBuilder(fusion, HashingEncoder(), pool))
    return repository, pool, fusion


def test_cross_scenario_fuse_uses_full_resource_capacity(tmp_path) -> None:
    repository, pool, fusion = _repository(tmp_path, 4)
    try:
        ids = [f"s{index}" for index in range(4)]
        for scenario_id in ids:
            _register_scenario(repository, scenario_id)
        assert [item.scenario_id for item in repository.get_many(ids)] == ids
        assert fusion.maximum == 4
    finally:
        repository.close()
        pool.shutdown()


def test_cross_scenario_fanout_respects_resource_bound(tmp_path) -> None:
    repository, pool, fusion = _repository(tmp_path, 3)
    try:
        ids = [f"s{index}" for index in range(10)]
        for scenario_id in ids:
            _register_scenario(repository, scenario_id)
        repository.get_many(ids)
        assert 1 < fusion.maximum <= 3
    finally:
        repository.close()
        pool.shutdown()


def test_multi_position_multi_scenario_scheduling_is_deterministic(tmp_path) -> None:
    repository, pool, fusion = _repository(tmp_path, 4)
    try:
        expected = {"s1": 3, "s2": 1, "s3": 4}
        for scenario_id, count in expected.items():
            _register_scenario(repository, scenario_id, count)
        scenarios = repository.get_many(expected)
        assert [item.scenario_id for item in scenarios] == list(expected)
        assert [len(item.positions) for item in scenarios] == list(expected.values())
        assert len(fusion.calls) == sum(expected.values())
        assert fusion.maximum <= 4
        for scenario in scenarios:
            assert [item.position for item in scenario.positions] == list(
                range(expected[scenario.scenario_id])
            )
            for position in scenario.positions:
                assert position.context.fused_context == position.sample.mocked_query
                assert position.sample.mocked_answer not in position.context.raw_context
    finally:
        repository.close()
        pool.shutdown()


def test_get_many_cache_hits_do_not_refuse_and_mixed_order_is_stable(tmp_path) -> None:
    repository, pool, fusion = _repository(tmp_path)
    try:
        for scenario_id in ("s1", "s2", "s3", "s4"):
            _register_scenario(repository, scenario_id)
        repository.get_many(["s1", "s3"])
        assert len(fusion.calls) == 2

        scenarios = repository.get_many(["s1", "s2", "s3", "s4"])
        assert [item.scenario_id for item in scenarios] == ["s1", "s2", "s3", "s4"]
        assert len(fusion.calls) == 4
        repository.get_many(["s1", "s2", "s3", "s4"])
        assert len(fusion.calls) == 4
    finally:
        repository.close()
        pool.shutdown()


def _lane(identifier: str, answer: str) -> LaneRuntimeState:
    return LaneRuntimeState(
        identifier,
        [RuntimeInteraction("r", "/test", identifier, 1, 10, identifier, answer)],
        {10},
        {identifier: ScenarioHypothesisState(identifier)},
    )


def _lane_scenario(identifier: str, encoder: HashingEncoder) -> Scenario:
    sample = MockSample("/test", f"{identifier}-target", "answer", identifier, 10, 1)
    context = f"{identifier}|{identifier}-answer|current"
    return Scenario(
        identifier,
        [
            ScenarioPosition(
                identifier,
                1,
                sample,
                ScenarioContext(context, context, encoder.encode_document(context)),
                FeatureSet({}, {}),
            )
        ],
    )


def test_lane_fuse_fans_out_and_preserves_lane_association() -> None:
    pool = ExecutionResourcePool()
    pool.register("fuse", 4)
    fusion = TrackingFusion(0.01)
    encoder = HashingEncoder()
    lanes = [_lane(f"lane-{index}", f"lane-{index}-answer") for index in range(4)]
    scenarios = {lane.lane_id: _lane_scenario(lane.lane_id, encoder) for lane in lanes}
    try:
        results = LaneContextRecaller(encoder, pool).recall(
            "current", lanes, scenarios, "/test", 10, fusion
        )
        assert 1 < fusion.maximum <= 4
        assert {item.lane_id for item in results} == {lane.lane_id for lane in lanes}
        assert all(item.scenario_id == item.lane_id for item in results)
        assert all(item.similarity == pytest.approx(1.0) for item in results)
    finally:
        pool.shutdown()


def test_build_many_preserves_cross_round_and_sibling_leakage_rules() -> None:
    pool = ExecutionResourcePool()
    pool.register("fuse", 3)
    fusion = TrackingFusion(0.001)
    encoder = HashingEncoder()
    samples = [
        MockSample("/test", "Q1", "A1", "s", 10, 1),
        MockSample("/test", "Q2", "A2", "s", 10, 2),
        MockSample("/test", "Q3", "A3", "s", 20, 3),
    ]
    scenario = Scenario(
        "s",
        [
            ScenarioPosition("s", item.position_id, item, ScenarioContext("", ""), item.features)
            for item in samples
        ],
    )
    try:
        result = ScenarioContextBuilder(fusion, encoder, pool).build_many([scenario])[0]
        first, second, third = [item.context.raw_context for item in result.positions]
        assert "A1" not in first and "A2" not in first
        assert "A1" not in second and "A2" not in second
        assert all(value in third for value in ("Q1", "A1", "Q2", "A2", "Q3"))
        assert "A3" not in third
    finally:
        pool.shutdown()
