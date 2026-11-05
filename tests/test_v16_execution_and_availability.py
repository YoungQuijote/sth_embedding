from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import HashingEncoder
from agent_mock_service.domain import (
    FeatureSet,
    InvokeAvailability,
    MockSample,
    PositionPathEvidence,
    Scenario,
    ScenarioContext,
    ScenarioPosition,
)
from agent_mock_service.execution import ExecutionResourcePool
from agent_mock_service.scoring import BayesPositionScorer, availability_prior


class ConcurrencyProbe:
    execution_resource_id = "shared"

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def run(self, value: str = "ok") -> str:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            time.sleep(self.delay)
            return value
        finally:
            with self.lock:
                self.active -= 1


def test_resource_pool_never_exceeds_registered_max_concurrency() -> None:
    pool = ExecutionResourcePool()
    pool.register("shared", 4)
    probe = ConcurrencyProbe()
    try:
        futures = [pool.submit_component(probe, probe.run) for _ in range(20)]
        assert all(future is not None for future in futures)
        assert [future.result() for future in futures if future] == ["ok"] * 20
        assert 1 < probe.maximum <= 4
    finally:
        pool.shutdown()


def test_different_resources_do_not_block_each_other() -> None:
    pool = ExecutionResourcePool()
    pool.register("a", 1)
    pool.register("b", 1)
    release = threading.Event()
    started = threading.Event()

    def block() -> None:
        started.set()
        release.wait(2)

    try:
        blocked = pool.submit("a", block)
        assert started.wait(1)
        assert pool.submit("b", lambda: "free").result(timeout=1) == "free"
        release.set()
        blocked.result(timeout=1)
    finally:
        release.set()
        pool.shutdown()


def test_components_sharing_resource_share_same_capacity() -> None:
    pool = ExecutionResourcePool()
    pool.register("shared", 2)
    probe = ConcurrencyProbe()

    class OtherComponent:
        execution_resource_id = "shared"

        def run(self) -> str:
            return probe.run("other")

    other = OtherComponent()
    try:
        futures = [
            pool.submit_component(
                probe if index % 2 else other, (probe if index % 2 else other).run
            )
            for index in range(12)
        ]
        assert all(future is not None for future in futures)
        for future in futures:
            assert future is not None
            future.result()
        assert probe.maximum <= 2
    finally:
        pool.shutdown()


def test_unmanaged_component_executes_directly() -> None:
    pool = ExecutionResourcePool()
    caller_thread = threading.get_ident()
    assert pool.call_component(object(), threading.get_ident) == caller_thread


def test_declared_unknown_resource_fails() -> None:
    component = type("Managed", (), {"execution_resource_id": "missing"})()
    with pytest.raises(KeyError, match="not registered"):
        ExecutionResourcePool().call_component(component, lambda: None)


def test_duplicate_resource_registration_with_different_capacity_fails() -> None:
    pool = ExecutionResourcePool()
    pool.register("shared", 2)
    pool.register("shared", 2)
    with pytest.raises(ValueError, match="already registered"):
        pool.register("shared", 3)
    pool.shutdown()


class ManagedFusion(ConcurrencyProbe):
    def fuse(self, messages: list[object]) -> str:
        content = "|".join(getattr(message, "content") for message in messages)
        return self.run(content)


def _scenario(identifier: str, count: int = 8) -> Scenario:
    positions = []
    for index in range(count):
        sample = MockSample("/x", f"q{index}", f"a{index}", identifier, 10, index)
        positions.append(
            ScenarioPosition(
                identifier,
                index,
                sample,
                ScenarioContext("", ""),
                FeatureSet({}, {}),
            )
        )
    return Scenario(identifier, positions)


def test_managed_fusion_positions_execute_concurrently_with_bound() -> None:
    pool = ExecutionResourcePool()
    pool.register("shared", 4)
    fusion = ManagedFusion()
    try:
        result = ScenarioContextBuilder(fusion, HashingEncoder(), pool).build(_scenario("s"))
        assert [item.position for item in result.positions] == list(range(8))
        assert [item.context.fused_context for item in result.positions] == [
            f"q{index}" for index in range(8)
        ]
        assert 1 < fusion.maximum <= 4
        assert all("a" not in item.context.raw_context for item in result.positions)
    finally:
        pool.shutdown()


def test_two_concurrent_scenario_builds_share_one_resource_limit() -> None:
    pool = ExecutionResourcePool()
    pool.register("shared", 3)
    fusion = ManagedFusion()
    builder = ScenarioContextBuilder(fusion, HashingEncoder(), pool)
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            results = list(callers.map(builder.build, [_scenario("a"), _scenario("b")]))
        assert [result.scenario_id for result in results] == ["a", "b"]
        assert 1 < fusion.maximum <= 3
    finally:
        pool.shutdown()


class IdentityProfile:
    def llr(self, evidence_name: str, raw_value: float) -> float:
        return raw_value if evidence_name == "semantic" else 0.0


def _evidence(semantic: float, availability: InvokeAvailability) -> PositionPathEvidence:
    return PositionPathEvidence(
        "s", 1, None, semantic, semantic, None, None, None, 1, availability, None
    )


def test_availability_raw_formula_unchanged() -> None:
    assert availability_prior(InvokeAvailability(1, 0)) == 1.0
    assert availability_prior(InvokeAvailability(1, 1)) == 0.0


def test_availability_prior_is_weighted() -> None:
    scorer = BayesPositionScorer(IdentityProfile(), {}, 0.0, 1.0, 2.0)
    score = scorer.score(_evidence(0.0, InvokeAvailability(1, 0)))
    assert score.availability_raw == 1.0
    assert score.availability_prior == pytest.approx(math.log(1.2))


def test_zero_availability_is_neutral_not_negative_infinity() -> None:
    scorer = BayesPositionScorer(IdentityProfile(), {}, 0.0, 1.0, 2.0)
    score = scorer.score(_evidence(0.7, InvokeAvailability(1, 1)))
    assert score.availability_raw == score.availability_prior == 0.0
    assert math.isfinite(score.total_log_score)


def test_strong_semantic_evidence_beats_fresh_irrelevant_candidate() -> None:
    scorer = BayesPositionScorer(IdentityProfile(), {}, 0.0, 1.0, 2.0)
    irrelevant = scorer.score(_evidence(-0.095, InvokeAvailability(1, 0)))
    target = scorer.score(_evidence(0.7, InvokeAvailability(1, 1)))
    assert target.total_log_score > irrelevant.total_log_score


def test_availability_weight_zero_disables_availability_effect() -> None:
    scorer = BayesPositionScorer(IdentityProfile(), {}, 0.0, 1.0, 2.0, 0.0)
    score = scorer.score(_evidence(0.0, InvokeAvailability(1, 0)))
    assert score.availability_raw == 1.0
    assert score.availability_prior == 0.0
