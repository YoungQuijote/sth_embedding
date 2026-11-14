import threading
from concurrent.futures import ThreadPoolExecutor

from agent_mock_service import AgentMockRuntime, MockSample
from agent_mock_service.mock_core.defaults import DefaultFeatureComparator, HashingEncoder
from agent_mock_service.mock_core.derived import DerivedStore
from agent_mock_service.mock_core.domain import FeatureSet
from agent_mock_service.mock_core.recall import SemanticRecaller
from agent_mock_service.mock_core.repository import SQLiteRepository


class Encoder(HashingEncoder):
    def __init__(self):
        super().__init__(8)
        self.inputs = []

    def encode_documents(self, texts):
        self.inputs.extend(texts)
        return super().encode_documents(texts)


class Extractor:
    fingerprint = "feature-v1"

    def __init__(self):
        self.value = "first"
        self.calls = 0

    def extract(self, query):
        self.calls += 1
        return FeatureSet({"key": self.value}, {})


def test_vectors_restart_missing_one_and_metadata_refresh(tmp_path):
    path = tmp_path / "x.db"
    repo = SQLiteRepository(path)
    sample = MockSample("/x", "query", "answer", "s", 1, 1)
    first = repo.register(sample)
    encoder = Encoder()
    store = DerivedStore(repo)
    recaller = SemanticRecaller(encoder, store)
    comparator = DefaultFeatureComparator()

    def recall():
        return recaller.recall(
            "paraphrase", FeatureSet({}, {}), repo.list_endpoint("/x"), 20, comparator
        )

    recall()
    repo.register(sample)
    repo.increment_invoked(first.sample_hash)
    result = recall()[0]
    assert result.sample.availability.registry_times == 2
    assert result.sample.availability.invoked_times == 1
    assert encoder.inputs == ["query"]
    repo.close()
    repo = SQLiteRepository(path)
    encoder = Encoder()
    recaller = SemanticRecaller(encoder, DerivedStore(repo))
    recall()
    assert encoder.inputs == []
    repo.register(MockSample("/x", "second", "answer", "s", 1, 2))
    recall()
    assert encoder.inputs == ["second"]
    repo.delete(first.sample_hash)
    assert [r.position for r in recall()] == [2]
    assert encoder.inputs == ["second"]


def test_feature_provenance_versions_preserve_counts_and_first_value(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    extractor = Extractor()
    runtime = AgentMockRuntime(repo, feature_extractor=extractor)
    sample = MockSample("/x", "query", "answer", "s", 1, 1)
    first = runtime.register(sample)
    extractor.value = "changed but same version"
    runtime.register(sample)
    current = runtime.derived_store.feature_views(repo.list_all(), extractor)[0]
    assert current.features.hard_features == {"key": "first"}
    assert current.availability.registry_times == 2
    repo.increment_invoked(first.sample_hash)
    extractor.fingerprint = "feature-v2"
    current = runtime.derived_store.feature_views(repo.list_all(), extractor)[0]
    assert current.features.hard_features == {"key": "changed but same version"}
    assert current.availability.registry_times == 2 and current.availability.invoked_times == 1
    assert current.sample_hash == first.sample_hash


def test_unversioned_features_reextracted_before_strict_filtering(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    sample = repo.register(
        MockSample("/x", "query", "answer", "s", 1, 1, FeatureSet({"key": "old"}, {}))
    )
    extractor = Extractor()
    current = DerivedStore(repo).feature_views([sample], extractor)[0]
    assert current.features.hard_features == {"key": "first"}
    assert extractor.calls == 1


def test_delete_during_encoding_cannot_publish_deleted_sample(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    stored = repo.register(MockSample("/x", "query", "answer", "s", 1, 1))
    started, release = threading.Event(), threading.Event()

    class Blocking(Encoder):
        def encode_documents(self, texts):
            started.set()
            assert release.wait(5)
            return super().encode_documents(texts)

    recaller = SemanticRecaller(Blocking(), DerivedStore(repo))
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(
            recaller.recall,
            "paraphrase",
            FeatureSet({}, {}),
            repo.list_all(),
            10,
            DefaultFeatureComparator(),
        )
        assert started.wait(3)
        repo.delete(stored.sample_hash)
        release.set()
        assert task.result(timeout=5) == []
    assert not recaller.indexes["/x"].samples


def test_corrupt_and_encoder_changed_vectors_rebuilt(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    sample = repo.register(MockSample("/x", "query", "answer", "s", 1, 1))
    encoder = Encoder()
    store = DerivedStore(repo)
    store.document_vectors([sample], encoder)
    with repo._write():
        repo._connection.execute("UPDATE sample_embeddings SET vector='[null]'")
    store.document_vectors([sample], encoder)
    assert encoder.inputs == ["query", "query"]
    encoder.fingerprint = "new-version"
    store.document_vectors([sample], encoder)
    assert encoder.inputs == ["query"] * 3
    assert HashingEncoder(16).fingerprint != HashingEncoder(32).fingerprint


def test_bm25_top_n_reranking_reuses_persisted_vectors_after_restart(tmp_path):
    from agent_mock_service import MockRequest, RuntimeConfig
    from agent_mock_service.mock_core.domain import RecallFusionMode

    path = tmp_path / "rerank.db"
    repo = SQLiteRepository(path)
    repo.register(MockSample("/x", "query first", "answer", "a", 1, 1))
    repo.register(MockSample("/x", "query second", "answer", "b", 1, 1))
    config = RuntimeConfig(
        recall_fusion_mode=RecallFusionMode.SINGLE, single_recall_source="bm25", reranker_top_n=1
    )
    first = AgentMockRuntime(repo, encoder=Encoder(), config=config)
    assert first.handle(MockRequest("/x", "query")).status_code == 200
    repo.close()
    repo = SQLiteRepository(path)
    encoder = Encoder()
    second = AgentMockRuntime(repo, encoder=encoder, config=config)
    assert second.handle(MockRequest("/x", "query")).status_code == 200
    assert encoder.inputs == []
