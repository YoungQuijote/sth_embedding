import sqlite3

import pytest

from agent_mock_service.src.mock_core.dag import DependencyError, resolve_context
from agent_mock_service.src.mock_core.domain import MockSample, PositionRef
from agent_mock_service.src.mock_core.repository import SQLiteRepository


def sample(s="s", p=1, r=1, sources=None, answer="answer"):
    return MockSample("/x", f"{s}-q{p}", answer, s, r, p, context_source_positions=sources)


def test_three_modes_default_full_history_and_round_normalization(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(sample(p=1))
    repo.register(sample(p=2, r="00000001", sources=[]))
    repo.finalize_round("s", "1", expected_position_count=2)
    repo.register(sample(p=3, r=2, sources=[PositionRef("s", 2)]))
    repo.finalize_round("s", 2)
    repo.register(sample(p=4, r=3))
    assert resolve_context(PositionRef("s", 4), repo.snapshot()).ancestors == tuple(
        PositionRef("s", p) for p in (1, 2, 3)
    )
    repo.close()
    repo = SQLiteRepository(tmp_path / "x.db")
    assert [s.context_source_positions for s in repo.list_all()] == [
        None,
        [],
        [PositionRef("s", 2)],
        None,
    ]
    assert resolve_context(PositionRef("s", 4), repo.snapshot()).position_context_completeness
    repo.close()


def test_cross_scenario_cycle_and_indirect_leak_rollback(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(sample("a", sources=[PositionRef("b", 1)]))
    with pytest.raises(DependencyError):
        repo.register(sample("b", sources=[PositionRef("a", 1)]), force_registry=True)
    assert repo.list_endpoint("/x")[0].sample_id == "a"
    assert len(repo.list_all()) == 1
    repo.register(sample("b", sources=[PositionRef("a", 2)]))
    with pytest.raises(DependencyError, match="leakage"):
        repo.register(sample("a", 2, 2, sources=[]))
    assert len(repo.list_all()) == 2


def test_missing_ref_restore_round_sealing_and_source_delete(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(sample("a", sources=[PositionRef("b", 1)]), round_position_completeness=True)
    assert not repo.get_position_state(PositionRef("a", 1)).position_context_completeness
    b = repo.register(sample("b", sources=[]))
    assert repo.get_position_state(PositionRef("a", 1)).position_context_completeness
    repo.delete_scenario("b", force=True)
    assert not repo.get_position_state(PositionRef("a", 1)).position_context_completeness
    assert repo.snapshot().rounds["a", "1"].round_position_completeness == "SEALED"
    repo.register(sample("b", sources=[]))
    assert repo.get_position_state(PositionRef("a", 1)).position_context_completeness
    assert repo.get_by_hash(b.sample_hash)


def test_force_counts_sealed_and_atomic_finalize(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    first = repo.register(sample(), round_position_completeness=True)
    repo.register(sample())
    assert repo.snapshot().rounds["s", "1"].round_position_completeness == "SEALED"
    with pytest.raises(ValueError):
        repo.register(sample(p=2))
    repo.finalize_round("s", 1, expected_position_count=1)
    with pytest.raises(ValueError):
        repo.register(sample(p=2), force_registry=True, round_position_completeness=True)
    assert len(repo.list_all()) == 1
    assert repo.get_by_hash(first.sample_hash).availability.registry_times == 2
    new = repo.register(sample(answer="changed"), force_registry=True)
    assert new.availability.registry_times == 1 and new.availability.invoked_times == 0
    assert repo.snapshot().rounds["s", "1"].round_position_completeness == "OPEN"


def test_incompatible_database_not_changed(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_fact(value TEXT)")
        conn.execute("INSERT INTO old_fact VALUES('preserve')")
    with pytest.raises(RuntimeError):
        SQLiteRepository(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM old_fact").fetchall() == [("preserve",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("old_fact",)
        ]


def test_diamond_same_round_cross_scenario_and_missing_round_not_inferred(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(sample("a", 1, 10, sources=[]))
    repo.register(sample("b", 1, 10, sources=[PositionRef("a", 1)]))
    repo.register(sample("c", 1, 10, sources=[PositionRef("a", 1)]))
    repo.register(sample("d", 1, 10, sources=[PositionRef("c", 1), PositionRef("b", 1)]))
    resolution = resolve_context(PositionRef("d", 1), repo.snapshot())
    assert resolution.position_context_completeness
    assert resolution.ancestors == tuple(PositionRef(s, 1) for s in ("a", "b", "c"))
    repo.finalize_round("a", 10)
    repo.register(sample("a", 2, 20))
    assert resolve_context(PositionRef("a", 2), repo.snapshot()).position_context_completeness


def test_default_invalidation_vs_explicit_and_counter_only_mutation(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    first = repo.register(sample(p=1, sources=[]))
    repo.register(sample(p=1, sources=[]))
    repo.finalize_round("s", 1)
    repo.register(sample(p=2, r=2))
    repo.register(sample(p=3, r=2, sources=[PositionRef("s", 1)]))
    assert repo.unregister(first.sample_hash) is False
    assert repo.snapshot().rounds["s", "1"].round_position_completeness == "SEALED"
    repo.register(sample(p=4, r=1, sources=[]), force_registry=True)
    assert not resolve_context(PositionRef("s", 2), repo.snapshot()).position_context_completeness
    assert resolve_context(PositionRef("s", 3), repo.snapshot()).position_context_completeness
    repo.finalize_round("s", 1)
    assert PositionRef("s", 4) in resolve_context(PositionRef("s", 2), repo.snapshot()).ancestors


def test_finalize_is_idempotent_and_unknown_round_rejected(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(sample())
    first = repo.finalize_round("s", 1, expected_position_count=1)
    assert repo.finalize_round("s", "00000001", expected_position_count=1) == first
    with pytest.raises(KeyError):
        repo.finalize_round("unknown", 1)
    with pytest.raises(ValueError):
        repo.finalize_round("s", 1, expected_position_count=2)
