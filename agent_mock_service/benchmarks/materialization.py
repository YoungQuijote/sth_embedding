"""Deterministic call-count benchmark. Run: python benchmarks/materialization.py."""

from __future__ import annotations

import json
import logging
import math
import statistics
import tempfile
import time
from pathlib import Path

from agent_mock_service import AgentMockRuntime, MockRequest, MockSample, PositionRef, RuntimeConfig
from agent_mock_service.src.mock_core.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.src.mock_core.domain import RecallFusionMode
from agent_mock_service.src.mock_core.repository import SQLiteRepository


class Encoder(HashingEncoder):
    def __init__(self):
        super().__init__(32)
        self.document_count = 0
        self.query_count = 0

    def encode_document(self, text):
        self.document_count += 1
        return super().encode_document(text)

    def encode_query(self, text):
        self.query_count += 1
        return super().encode_query(text)


class Fusion(JoiningContextFusionProvider):
    def __init__(self):
        self.count = 0
        self.fail = False

    def fuse(self, messages):
        self.count += 1
        if self.fail:
            raise RuntimeError("benchmark injected failure")
        return super().fuse(messages)


def runtime(repo):
    return AgentMockRuntime(
        repo,
        encoder=Encoder(),
        fusion=Fusion(),
        config=RuntimeConfig(
            recall_fusion_mode=RecallFusionMode.SEMANTIC_ONLY,
            semantic_recall_k=100,
            reranker_top_n=2,
        ),
    )


def measure(name, rt, repetitions):
    before = (rt.fusion.count, rt.encoder.document_count, rt.encoder.query_count)
    latencies, targets = [], []
    for _ in range(repetitions):
        # This benchmark isolates static work. Dynamic lanes have separate regression tests.
        rt.lanes._lanes.clear()
        start = time.perf_counter()
        response = rt.handle(MockRequest("/x", "query paraphrase 7"))
        latencies.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200, rt.trace.events[-1]
        targets.append(sum(len(v) for v in rt.trace.events[-1].get("materialization", {}).values()))
    after = (rt.fusion.count, rt.encoder.document_count, rt.encoder.query_count)
    return {
        "scenario": name,
        "samples": 100,
        "requests": repetitions,
        "target_refs_per_request": targets,
        "static_fuse": after[0] - before[0],
        "document_encode": after[1] - before[1],
        "dynamic_query_encode": after[2] - before[2],
        "dynamic_lane_fuse": 0,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 3),
    }


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "benchmark.db"
        repo = SQLiteRepository(path)
        repo.register(
            MockSample(
                "/history", "history", "answer", "history", 1, 0, context_source_positions=[]
            )
        )
        for i in range(100):
            repo.register(
                MockSample(
                    "/x",
                    f"query {i}",
                    f"answer {i}",
                    f"s{i}",
                    1,
                    i,
                    context_source_positions=[PositionRef("history", 0)],
                )
            )
        rt = runtime(repo)
        results = [measure("cold", rt, 1), measure("hot", rt, 20)]
        # Warm all targets explicitly before the restart experiment.
        repo.rebuild_position_contexts([PositionRef(f"s{i}", i) for i in range(100)])
        repo.close()
        repo = SQLiteRepository(path)
        rt = runtime(repo)
        results.append(measure("restart_after_prematerialization", rt, 20))
        first = repo.list_endpoint("/x")[0]
        from dataclasses import replace

        start_counts = (rt.fusion.count, rt.encoder.document_count)
        rt.register(replace(first, mocked_query="updated query 0"), force_registry=True)
        registration_counts = {
            "static_fuse": rt.fusion.count - start_counts[0],
            "document_encode": rt.encoder.document_count - start_counts[1],
        }
        result = measure("first_query_after_local_change", rt, 1)
        result["registration_precompute"] = registration_counts
        results.append(result)
        repo.delete_scenario("history", force=True)
        results.append(measure("missing_dependency_degradation", rt, 20))
        repo.register(
            MockSample(
                "/history", "history", "answer", "history", 1, 0, context_source_positions=[]
            )
        )
        rt.fusion.fingerprint = "benchmark-failure-v2"
        rt.fusion.fail = True
        results.append(measure("failed_materialization_degradation", rt, 20))
        repo.close()
    print(
        json.dumps(
            {
                "model": "deterministic HashingEncoder(32) and Joining fusion; no real-model latency claim",
                "baseline_call_bound": "Legacy full-scenario path would prepare all 100 retrieved positions before Top-N=2",
                "measurements": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
