import time
from dataclasses import replace

from agent_mock_service import AgentMockRuntime, MockRequest, MockSample, PositionRef, RuntimeConfig
from agent_mock_service.defaults import JoiningContextFusionProvider
from agent_mock_service.domain import (
    JudgeResult,
    LaneRuntimeState,
    MatchDecision,
    RecallFusionMode,
    ScenarioHypothesisState,
)
from agent_mock_service.repository import SQLiteRepository


class Fusion(JoiningContextFusionProvider):
    def __init__(self):
        self.static = []

    def fuse(self, messages):
        self.static.append(messages[-1].content)
        return super().fuse(messages)


class Pick:
    fingerprint = "pick-two-v1"

    def rerank(self, query, candidates, *, top_n=None):
        return [
            c
            for c in candidates
            if c.scenario_id == "s" and c.position in (0, 5) and c.lane_id is None
        ]


def populate(repo, count=100):
    repo.register(
        MockSample(
            "/history", "history", "ancestor answer", "history", 1, 0, context_source_positions=[]
        )
    )
    for i in range(count):
        repo.register(
            MockSample(
                "/x",
                f"query {i}",
                f"secret {i}",
                "s" if i < 6 else f"other{i}",
                10 if i < 5 else 20,
                i,
                context_source_positions=[PositionRef("history", 0)],
            )
        )


def test_only_reranker_final_two_materialized_from_hundred(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    populate(repo)
    fusion = Fusion()
    runtime = AgentMockRuntime(
        repo,
        fusion=fusion,
        reranker=Pick(),
        config=RuntimeConfig(
            recall_fusion_mode=RecallFusionMode.SEMANTIC_ONLY, semantic_recall_k=100
        ),
    )
    requests = []
    real = runtime.materializer.ensure_position_contexts

    def spy(refs, *, reason):
        requests.append((set(refs), reason))
        return real(refs, reason=reason)

    runtime.materializer.ensure_position_contexts = spy
    response = runtime.handle(MockRequest("/x", "paraphrase query"))
    assert response.status_code == 200, runtime.trace.events[-1]
    assert requests == [({PositionRef("s", 0), PositionRef("s", 5)}, "final_candidates")]
    assert sorted(fusion.static) == ["query 0", "query 5"]
    assert len(runtime.trace.events[-1]["recall"]["local"]) == 100


def test_lane_five_and_final_two_overlap_one_materialize_union_six(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    populate(repo, 6)
    fusion = Fusion()
    runtime = AgentMockRuntime(repo, fusion=fusion, reranker=Pick())
    lane = LaneRuntimeState(
        "lane",
        active_rounds={10},
        scenario_hypotheses={"s": ScenarioHypothesisState("s")},
        created_at=time.time(),
        last_active_at=time.time(),
    )
    runtime.lanes._lanes["lane"] = lane
    response = runtime.handle(MockRequest("/x", "paraphrase query"))
    assert response.status_code == 200, runtime.trace.events[-1]
    trace = runtime.trace.events[-1]
    first, last = (
        trace["materialization"]["lane_recall"],
        trace["materialization"]["final_candidates"],
    )
    assert len(first) == 5 and len(last) == 2
    assert sum(r["fuse_count"] for r in first + last) == 6
    assert sum(r["embedding_count"] for r in first + last) == 6
    assert [r["cache_hit"] for r in last].count("sqlite") == 1
    assert "ancestor answer" not in str(trace["recall"])


def test_incomplete_context_degrades_semantic_but_not_context_only(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(
        MockSample(
            "/x", "query", "answer", "s", 1, 1, context_source_positions=[PositionRef("missing", 1)]
        )
    )
    runtime = AgentMockRuntime(repo)
    response = runtime.handle(MockRequest("/x", "query paraphrase"))
    assert response.status_code == 200
    event = runtime.trace.events[-1]
    assert event["bayes_scores"][0]["context_raw"] is None
    assert event["judge"]["candidate_scenarios"][0]["positions"][0]["scenario_context"] is None
    only = AgentMockRuntime(
        repo, config=RuntimeConfig(recall_fusion_mode=RecallFusionMode.CONTEXT_ONLY)
    )
    assert only.handle(MockRequest("/x", "paraphrase")).status_code == 404


def test_judge_cannot_select_different_lane_or_replaced_fact(tmp_path):
    for mode in ("lane", "replace"):
        repo = SQLiteRepository(tmp_path / f"{mode}.db")
        sample = repo.register(MockSample("/x", "query", "original", "s", 1, 1))

        class Judge:
            model_name = prompt_version = "test"

            def judge(self, query, candidates, mode=mode, repo=repo, sample=sample):
                if mode == "replace":
                    repo.register(replace(sample, mocked_answer="replacement"), force_registry=True)
                return JudgeResult(
                    MatchDecision.MATCH, "s", 1, "invented" if mode == "lane" else None
                )

        runtime = AgentMockRuntime(repo, judge=Judge())
        response = runtime.handle(MockRequest("/x", "query paraphrase"))
        assert response.status_code == 500
        assert "replacement" not in str(response.body)


def test_open_then_finalize_lane_reevaluation(tmp_path):
    repo = SQLiteRepository(tmp_path / "x.db")
    repo.register(MockSample("/x", "q1", "a1", "s", 10, 1))
    repo.register(MockSample("/x", "q2", "a2", "s", 20, 2))
    runtime = AgentMockRuntime(repo)
    assert runtime.handle(MockRequest("/x", "q1")).status_code == 200
    lane = runtime.lanes.active()[0]
    assert lane.active_rounds == {10, 20}
    repo.finalize_round("s", 10)
    runtime.lanes.reevaluate({"s": repo.get_facts("s")})
    assert lane.active_rounds == {20}
    runtime.handle(MockRequest("/x", "q2"))
    assert runtime.lanes.active()  # terminal remains OPEN
    repo.finalize_round("s", 20)
    runtime.lanes.reevaluate({"s": repo.get_facts("s")})
    assert not runtime.lanes.active()


def test_delete_and_reinsert_same_hash_is_stale_selection(tmp_path):
    repo = SQLiteRepository(tmp_path / "generation.db")
    sample = repo.register(MockSample("/x", "query", "original", "s", 1, 1))

    class Judge:
        model_name = prompt_version = "replace-identical"

        def judge(self, query, candidates):
            repo.delete(sample.sample_hash)
            restored = repo.register(sample)
            assert restored.sample_hash == sample.sample_hash
            assert restored.fact_revision != sample.fact_revision
            return JudgeResult(MatchDecision.MATCH, "s", 1)

    runtime = AgentMockRuntime(repo, judge=Judge())
    response = runtime.handle(MockRequest("/x", "query paraphrase"))
    assert response.status_code == 500
    assert repo.get_by_hash(sample.sample_hash).availability.invoked_times == 0


def test_two_lanes_share_static_context_but_keep_independent_paths(tmp_path):
    repo = SQLiteRepository(tmp_path / "lanes.db")
    populate(repo, 1)
    fusion = Fusion()

    class Capture:
        model_name = prompt_version = "capture"

        def judge(self, query, candidates):
            self.paths = [
                (p.scenario_id, p.position, p.lane_id) for s in candidates for p in s.positions
            ]
            return JudgeResult(MatchDecision.MISS)

    judge = Capture()
    runtime = AgentMockRuntime(repo, fusion=fusion, judge=judge)
    for identifier in ("one", "two"):
        runtime.lanes._lanes[identifier] = LaneRuntimeState(
            identifier,
            active_rounds={10},
            scenario_hypotheses={"s": ScenarioHypothesisState("s")},
            created_at=time.time(),
            last_active_at=time.time(),
        )
    assert runtime.handle(MockRequest("/x", "paraphrase")).status_code == 404
    assert ("s", 0, "one") in judge.paths and ("s", 0, "two") in judge.paths
    trace = runtime.trace.events[-1]
    assert trace["computation"]["static_fuse"] == 1
    assert trace["computation"]["dynamic_lane_fuse"] == 2
