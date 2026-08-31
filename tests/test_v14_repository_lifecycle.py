from __future__ import annotations

import sqlite3

import pytest

from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import (
    DefaultFeatureComparator,
    HashingEncoder,
    JoiningContextFusionProvider,
)
from agent_mock_service.domain import FeatureSet, MockSample, RequestAffinityInfo
from agent_mock_service.recall import SemanticRecaller
from agent_mock_service.repository import SQLiteRepository, compute_sample_hash


def sample(
    *,
    endpoint="/agent",
    scenario="s",
    position=1,
    round_id=1,
    query="query",
    answer="answer",
    hard=None,
):
    return MockSample(
        endpoint,
        query,
        answer,
        scenario,
        round_id,
        position,
        FeatureSet(hard or {}, {}),
    )


class CountingEncoder(HashingEncoder):
    fingerprint = "lifecycle-counting-v1"

    def __init__(self):
        super().__init__()
        self.document_calls = 0
        self.batch_calls = 0

    def encode_document(self, text):
        self.document_calls += 1
        return super().encode_document(text)

    def encode_documents(self, texts):
        self.batch_calls += 1
        return super().encode_documents(texts)


@pytest.fixture
def repository(tmp_path):
    encoder = CountingEncoder()
    value = SQLiteRepository(
        tmp_path / "lifecycle.db",
        ScenarioContextBuilder(JoiningContextFusionProvider(), encoder),
    )
    yield value, encoder
    value.close()


def test_sample_hash_includes_position_id_and_features():
    original = sample()
    assert compute_sample_hash(original) != compute_sample_hash(sample(position=2))
    assert compute_sample_hash(original) != compute_sample_hash(sample(hard={"device": "E2"}))


def test_sample_hash_requires_stable_json_features():
    with pytest.raises(TypeError, match="stably JSON-serializable"):
        compute_sample_hash(sample(hard={"devices": {"E1", "E2"}}))


def test_changed_features_for_existing_position_is_not_duplicate(repository):
    repo, _ = repository
    stored = repo.register(sample(hard={"device": "E1"}))
    with pytest.raises(ValueError, match="uniquely identify"):
        repo.register(sample(hard={"device": "E2"}))
    assert repo.get_by_hash(stored.sample_hash).availability.registry_times == 1


def test_duplicate_registration_merges_unique_affinity(repository):
    repo, _ = repository
    first = RequestAffinityInfo(request_ip="10.0.0.1", user_agent="one")
    second = RequestAffinityInfo(request_ip="10.0.0.2", user_agent="two")
    stored = repo.register(sample(), first)
    repo.register(sample(), second)
    repo.register(sample(), second)
    scenario = repo.get("s")
    assert repo.get_by_hash(stored.sample_hash).availability.registry_times == 3
    assert {(item.request_ip, item.user_agent) for item in scenario.registry_affinity_infos} == {
        ("10.0.0.1", "one"),
        ("10.0.0.2", "two"),
    }


def test_unregister_decrements_then_last_call_physically_deletes(repository):
    repo, _ = repository
    stored = repo.register(sample())
    repo.register(sample())
    assert repo.unregister(stored.sample_hash) is False
    assert repo.get_by_hash(stored.sample_hash).availability.registry_times == 1
    assert repo.unregister(stored.sample_hash) is True
    assert repo.get_by_hash(stored.sample_hash) is None
    assert repo.get("s") is None
    assert repo.unregister(stored.sample_hash) is False


def test_unregister_affinity_rules(repository):
    repo, _ = repository
    first = RequestAffinityInfo(request_ip="10.0.0.1")
    second = RequestAffinityInfo(request_ip="10.0.0.2")
    stored = repo.register(sample(), first)
    repo.register(sample(), second)
    repo.register(sample())
    repo.unregister(stored.sample_hash, first)
    assert [item.request_ip for item in repo.get("s").registry_affinity_infos] == ["10.0.0.2"]
    repo.unregister(stored.sample_hash)
    assert [item.request_ip for item in repo.get("s").registry_affinity_infos] == ["10.0.0.2"]


def test_force_delete_ignores_registry_times_and_is_idempotent(repository):
    repo, _ = repository
    stored = repo.register(sample())
    for _ in range(4):
        repo.register(sample())
    assert repo.delete(stored.sample_hash) is True
    assert repo.get_by_hash(stored.sample_hash) is None
    assert repo.delete(stored.sample_hash) is False
    # Endpoint partition remains available and empty.
    assert repo.list_endpoint("/agent") == []


def _register_cross_endpoint_scenario(repo):
    values = [
        sample(endpoint="/weather", position=1, query="weather"),
        sample(endpoint="/traffic", position=2, query="traffic"),
        sample(endpoint="/route", position=3, query="route"),
    ]
    for value in values:
        repo.register(value)
        repo.register(value)
    return values


def test_delete_scenario_unregisters_all_cross_endpoint_positions(repository):
    repo, _ = repository
    values = _register_cross_endpoint_scenario(repo)
    assert repo.delete_scenario("s") == 3
    for value in values:
        stored = repo.get_by_hash(compute_sample_hash(value))
        assert stored.availability.registry_times == 1
    assert repo.delete_scenario("s") == 3
    assert repo.get("s") is None


def test_force_delete_scenario_removes_all_positions(repository):
    repo, _ = repository
    values = _register_cross_endpoint_scenario(repo)
    assert repo.delete_scenario("s", force=True) == 3
    assert all(repo.get_by_hash(compute_sample_hash(value)) is None for value in values)
    assert repo.delete_scenario("s", force=True) == 0


def test_scenario_delete_is_atomic(repository, monkeypatch):
    repo, _ = repository
    values = _register_cross_endpoint_scenario(repo)
    original = repo._delete_locked
    calls = 0

    def fail_second(sample_hash, member=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated delete failure")
        return original(sample_hash, member)

    monkeypatch.setattr(repo, "_delete_locked", fail_second)
    with pytest.raises(RuntimeError, match="simulated"):
        repo.delete_scenario("s", force=True)
    assert all(repo.get_by_hash(compute_sample_hash(value)) is not None for value in values)


def test_register_membership_conflict_rolls_back_endpoint_fact(repository):
    repo, _ = repository
    repo.register(sample(endpoint="/weather", position=1))
    with pytest.raises(ValueError):
        repo.register(sample(endpoint="/traffic", position=1, query="conflict"))
    assert repo.list_endpoint("/traffic") == []
    assert len(repo.get("s").positions) == 1


def test_registry_decrement_does_not_reencode_context(repository):
    repo, encoder = repository
    stored = repo.register(sample())
    repo.register(sample())
    repo.get("s")
    calls = encoder.document_calls
    repo.unregister(stored.sample_hash)
    repo.get("s")
    assert encoder.document_calls == calls


def test_physical_delete_invalidates_and_rebuilds_later_context(repository):
    repo, encoder = repository
    first = repo.register(sample(position=1, round_id=1, query="Q1", answer="A1"))
    repo.register(sample(position=2, round_id=2, query="Q2", answer="A2"))
    before = repo.get("s")
    assert "Q1" in before.positions[1].context.fused_context
    calls = encoder.document_calls
    repo.delete(first.sample_hash)
    after = repo.get("s")
    assert len(after.positions) == 1
    assert "Q1" not in after.positions[0].context.fused_context
    assert encoder.document_calls > calls


def test_embedding_index_lazily_converges_after_delete(repository):
    repo, encoder = repository
    first = repo.register(sample(position=1, query="alpha"))
    second = repo.register(sample(position=2, query="beta"))
    recaller = SemanticRecaller(encoder)
    comparator = DefaultFeatureComparator()
    initial = recaller.recall(
        "alpha", FeatureSet({}, {}), repo.list_endpoint("/agent"), 10, comparator
    )
    assert {item.sample_hash for item in initial} == {first.sample_hash, second.sample_hash}
    assert encoder.batch_calls == 1
    repo.delete(second.sample_hash)
    refreshed = recaller.recall(
        "alpha", FeatureSet({}, {}), repo.list_endpoint("/agent"), 10, comparator
    )
    assert {item.sample_hash for item in refreshed} == {first.sample_hash}
    assert encoder.batch_calls == 2


def test_incompatible_sample_hash_schema_fails_fast(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE repository_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO repository_metadata VALUES('sample_hash_version', '1');
    """)
    connection.close()
    with pytest.raises(RuntimeError, match="incompatible sample hash/schema version"):
        SQLiteRepository(path)
