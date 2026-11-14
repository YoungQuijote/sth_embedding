import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_mock_service import AgentMockRuntime, MockSample, PositionRef
from agent_mock_service.mock_core.context import ScenarioContextBuilder
from agent_mock_service.mock_core.dag import resolve_context
from agent_mock_service.mock_core.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.mock_core.execution import ExecutionResourcePool
from agent_mock_service.mock_core.repository import SQLiteRepository


class Encoder(HashingEncoder):
    def __init__(self, dimensions=16):
        super().__init__(dimensions)
        self.documents = []

    def encode_document(self, text):
        self.documents.append(text)
        return super().encode_document(text)


class Fusion(JoiningContextFusionProvider):
    fingerprint = "test-fusion-v1"

    def __init__(self):
        self.calls = []
        self.fail = False
        self.started = None
        self.release = None

    def fuse(self, messages):
        self.calls.append(messages)
        if self.started:
            self.started.set()
            assert self.release.wait(5)
        if self.fail:
            raise RuntimeError("injected fusion failure")
        return super().fuse(messages)


def facts(repo):
    source = repo.register(
        MockSample("/a", "source", "source answer", "a", 1, 1, context_source_positions=[])
    )
    target = repo.register(
        MockSample(
            "/b",
            "target",
            "target secret",
            "b",
            1,
            1,
            context_source_positions=[PositionRef("a", 1)],
        )
    )
    return source, target


def bind(repo, fusion=None, encoder=None, pool=None):
    fusion, encoder = fusion or Fusion(), encoder or Encoder()
    repo.bind_context_builder(ScenarioContextBuilder(fusion, encoder, pool))
    return fusion, encoder


def ensure(repo, *refs):
    return repo.materializer.ensure_position_contexts(refs or [PositionRef("b", 1)], reason="query")


def test_restart_reuses_text_and_both_document_vector_types(tmp_path):
    path = tmp_path / "persist.db"
    repo = SQLiteRepository(path)
    facts(repo)
    fusion, encoder = Fusion(), Encoder()
    runtime = AgentMockRuntime(repo, encoder=encoder, fusion=fusion)
    ensure(repo)
    runtime.derived_store.document_vectors(repo.list_all(), encoder)
    assert len(fusion.calls) == 1 and len(encoder.documents) == 3
    repo.close()
    repo = SQLiteRepository(path)
    fusion, encoder = Fusion(), Encoder()
    runtime = AgentMockRuntime(repo, encoder=encoder, fusion=fusion)
    result = ensure(repo).results[PositionRef("b", 1)]
    runtime.derived_store.document_vectors(repo.list_all(), encoder)
    assert result.cache_hit == "sqlite"
    assert fusion.calls == encoder.documents == []
    assert "target secret" not in result.artifact.fused_context
    assert "source answer" in result.artifact.fused_context


def test_encoder_only_change_and_own_answer_reuse(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    _source, target = facts(repo)
    fusion, encoder = bind(repo)
    ensure(repo)
    repo.register(replace(target, mocked_answer="new secret"), force_registry=True)
    assert len(fusion.calls) == len(encoder.documents) == 1
    second = Encoder(32)
    bind(repo, fusion, second)
    result = ensure(repo).results[PositionRef("b", 1)]
    assert len(fusion.calls) == 1 and len(second.documents) == 1
    assert len(result.artifact.embedding) == 32


def test_raw_read_is_pure_and_failure_retry_is_local(tmp_path, caplog):
    repo = SQLiteRepository(tmp_path / "x.db")
    facts(repo)
    fusion, encoder = bind(repo)
    assert repo.get_facts_many(["a", "b"])[1].positions[0].context is None
    assert repo.get_position_contexts([PositionRef("b", 1)])[PositionRef("b", 1)].artifact is None
    assert fusion.calls == encoder.documents == []
    fusion.fail = True
    failed = ensure(repo).results[PositionRef("b", 1)]
    assert failed.status == "FAILED"
    assert repo.get_position_state(PositionRef("b", 1)).materialization_status == "FAILED"
    # An ancestor's failed artifact is irrelevant to raw QA completeness.
    repo.register(
        MockSample(
            "/c", "child", "secret", "c", 1, 1, context_source_positions=[PositionRef("b", 1)]
        )
    )
    assert resolve_context(PositionRef("c", 1), repo.snapshot()).position_context_completeness
    fusion.fail = False
    assert ensure(repo, PositionRef("c", 1)).results[PositionRef("c", 1)].artifact
    assert repo.get_position_state(PositionRef("b", 1)).materialization_status == "FAILED"
    assert ensure(repo).results[PositionRef("b", 1)].artifact
    assert "Lazy Fuse" in caplog.text


@pytest.mark.parametrize("change", ["answer", "delete", "encoder", "fusion", "failure"])
def test_barrier_rejects_old_cross_scenario_success_or_failure(tmp_path, change):
    repo = SQLiteRepository(tmp_path / "x.db")
    source, _target = facts(repo)
    fusion, _encoder = bind(repo)
    fusion.started, fusion.release = threading.Event(), threading.Event()
    fusion.fail = change == "failure"
    old = repo.materializer
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(old.ensure_position_contexts, [PositionRef("b", 1)], reason="query")
        assert fusion.started.wait(3)
        # Replace the binding: mutation can actively build the new version without blocking on the old model.
        new_fusion, new_encoder = bind(repo)
        if change in {"answer", "failure"}:
            repo.register(replace(source, mocked_answer="updated ancestor"), force_registry=True)
        elif change == "delete":
            repo.delete(source.sample_hash)
        elif change == "encoder":
            new_encoder.fingerprint = "new-encoder"
            ensure(repo)
        else:
            new_fusion.fingerprint = "new-fusion"
            ensure(repo)
        fusion.release.set()
        result = future.result(timeout=5).results[PositionRef("b", 1)]
    assert result.stale_discard and result.artifact is None
    if change != "delete":
        assert ensure(repo).results[PositionRef("b", 1)].artifact
        assert repo.get_position_state(PositionRef("b", 1)).materialization_status == "READY"
    else:
        assert (
            repo.get_position_contexts([PositionRef("b", 1)])[PositionRef("b", 1)].status
            == "incomplete"
        )


def test_corrupt_vector_reencodes_without_refusion(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    facts(repo)
    fusion, encoder = bind(repo)
    ensure(repo)
    with repo._write():
        repo._connection.execute("UPDATE context_embeddings SET vector='[1,2]'")
    result = ensure(repo).results[PositionRef("b", 1)]
    assert result.artifact and len(result.artifact.embedding) == 16
    assert len(fusion.calls) == 1 and len(encoder.documents) == 2


def test_atomic_cross_scenario_delete_rollback(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "x.db")
    facts(repo)
    repo.register(MockSample("/other", "other", "answer", "a", 1, 2, context_source_positions=[]))
    bind(repo)
    ensure(repo)
    old = repo._delete_locked

    def delete(sample_hash, member=None):
        result = old(sample_hash, member)
        if member and member["position_id"] == 2:
            raise RuntimeError("abort whole transaction")
        return result

    monkeypatch.setattr(repo, "_delete_locked", delete)
    with pytest.raises(RuntimeError):
        repo.delete_scenario("a", force=True)
    assert len(repo.list_all()) == 3
    assert repo.get_position_state(PositionRef("b", 1)).materialization_status == "READY"
    assert repo.get_position_contexts([PositionRef("b", 1)])[PositionRef("b", 1)].artifact


def test_capacity_one_registration_and_query_share_pool(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    pool = ExecutionResourcePool()
    pool.register("model", 1)
    fusion, encoder = Fusion(), Encoder()
    fusion.execution_resource_id = encoder.execution_resource_id = "model"
    AgentMockRuntime(repo, fusion=fusion, encoder=encoder, execution_resources=pool)
    try:
        facts(repo)
        assert repo.last_materialization.results[PositionRef("b", 1)].artifact
        assert ensure(repo).results[PositionRef("b", 1)].artifact
        assert len(fusion.calls) == 1
    finally:
        pool.shutdown()


def test_missing_fingerprint_is_reported_without_undoing_registration(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    fusion = type("NoFingerprint", (), {"fuse": lambda self, messages: "wrong"})()
    bind(repo, fusion)
    _, target = facts(repo)
    assert repo.get_by_hash(target.sample_hash)
    assert "fingerprint" in repo.last_materialization.results[PositionRef("b", 1)].error


def test_independent_process_restores_persisted_context_and_vectors(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "process.db"
    repo = SQLiteRepository(path)
    facts(repo)
    bind(repo)
    ensure(repo)
    from agent_mock_service.mock_core.derived import DerivedStore

    DerivedStore(repo).document_vectors(repo.list_all(), repo.context_builder.encoder)
    repo.close()
    code = """
import sys
from agent_mock_service.repository import SQLiteRepository
from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import HashingEncoder
from agent_mock_service.domain import PositionRef
from agent_mock_service.derived import DerivedStore
class Encoder(HashingEncoder):
    def encode_document(self, text): raise AssertionError('unexpected encoding')
    def encode_documents(self, texts): raise AssertionError('unexpected document batch')
class Fusion:
    fingerprint = 'test-fusion-v1'
    def fuse(self, messages): raise AssertionError('unexpected Fuse')
repo = SQLiteRepository(sys.argv[1], ScenarioContextBuilder(Fusion(), Encoder(16)))
result = repo.materializer.ensure_position_contexts([PositionRef('b', 1)], reason='query')
assert result.results[PositionRef('b', 1)].artifact
assert len(DerivedStore(repo).document_vectors(repo.list_all(), repo.context_builder.encoder)) == 2
repo.close()
print('persisted hit without model calls')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "persisted hit" in result.stdout


def test_target_plugin_fingerprint_only_not_ancestor_or_registry_revision(tmp_path):
    from agent_mock_service import BusinessPlugin, BusinessPluginRegistry
    from agent_mock_service.mock_core.defaults import (
        DefaultFeatureComparator,
        DefaultQueryParser,
        EmptyFeatureExtractor,
    )
    from agent_mock_service.mock_core.judge import FakeJudge

    repo = SQLiteRepository(tmp_path / "x.db")
    facts(repo)
    source, target = Fusion(), Fusion()
    source.fail = True
    registry = BusinessPluginRegistry(allow_default_plugin=False)

    def plugin(endpoint, fusion):
        return BusinessPlugin(
            endpoint,
            DefaultQueryParser(),
            EmptyFeatureExtractor(),
            DefaultFeatureComparator(),
            fusion,
            FakeJudge(),
        )

    registry.register(plugin("/a", source))
    registry.register(plugin("/b", target))
    AgentMockRuntime(repo, business_plugins=registry, encoder=Encoder())
    assert ensure(repo).results[PositionRef("b", 1)].artifact
    assert len(target.calls) == 1 and len(source.calls) == 0
    source.fingerprint = "ancestor-changed"
    registry.register(plugin("/unrelated", Fusion()))
    assert ensure(repo).results[PositionRef("b", 1)].cache_hit == "sqlite"
    assert len(target.calls) == 1
    target.fingerprint = "target-changed"
    assert ensure(repo).results[PositionRef("b", 1)].artifact
    assert len(target.calls) == 2


def test_same_version_concurrent_outputs_keep_text_vector_pair_and_ready(tmp_path):
    repo = SQLiteRepository(tmp_path / "same.db")
    facts(repo)
    barrier = threading.Barrier(2)

    class Nondeterministic(Fusion):
        def fuse(self, messages):
            value = str(threading.get_ident())
            barrier.wait(timeout=5)
            return value

    _fusion, encoder = bind(repo, Nondeterministic())
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(ensure, repo) for _ in range(2)]
        results = [f.result(timeout=5).results[PositionRef("b", 1)] for f in futures]
    assert all(r.artifact for r in results)
    assert results[0].artifact.fused_context == results[1].artifact.fused_context
    assert results[0].artifact.embedding == encoder.encode(results[0].artifact.fused_context)
    assert repo.get_position_state(PositionRef("b", 1)).materialization_status == "READY"


def test_post_commit_infrastructure_failure_does_not_repeat_registration(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "post.db")
    bind(repo)

    def unavailable(*args, **kwargs):
        raise RuntimeError("storage unavailable after fact commit")

    monkeypatch.setattr(repo.materializer, "ensure_position_contexts", unavailable)
    stored = repo.register(MockSample("/x", "q", "a", "s", 1, 1))
    assert repo.get_by_hash(stored.sample_hash).availability.registry_times == 1
    assert "storage unavailable" in repo.last_post_commit_error
