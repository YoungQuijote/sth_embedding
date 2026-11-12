"""Measure the same two-round dataset against the fixed baseline and current checkout."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "15dc1f72ec90302e14bb3d4d68a5a3c6e0b5021a"
CODE = r"""
import json, logging, math, statistics, tempfile, time
from pathlib import Path
from agent_mock_service import AgentMockRuntime, MockRequest, MockSample, RuntimeConfig
from agent_mock_service.domain import RecallFusionMode
from agent_mock_service.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.repository import SQLiteRepository
logging.disable(logging.CRITICAL)
class Encoder(HashingEncoder):
    def __init__(self):
        super().__init__(32)
        self.documents = 0
    def encode_document(self, text):
        self.documents += 1
        return super().encode_document(text)
class Fusion(JoiningContextFusionProvider):
    def __init__(self): self.calls = 0
    def fuse(self, messages):
        self.calls += 1
        return super().fuse(messages)
with tempfile.TemporaryDirectory() as directory:
    repo = SQLiteRepository(Path(directory) / 'benchmark.db')
    for i in range(100):
        sid = f's{i:03d}'
        repo.register(MockSample('/history', f'history {i}', f'prior {i}', sid, 1, 0))
        repo.register(MockSample('/x', f'query {i}', f'answer {i}', sid, 2, 1))
        if hasattr(repo, 'finalize_round'):
            repo.finalize_round(sid, 1)
    encoder, fusion = Encoder(), Fusion()
    runtime = AgentMockRuntime(repo, encoder=encoder, fusion=fusion, config=RuntimeConfig(
        recall_fusion_mode=RecallFusionMode.SEMANTIC_ONLY, semantic_recall_k=100, reranker_top_n=2))
    result = []
    for name, repetitions in [('cold', 1), ('hot', 20)]:
        before = fusion.calls, encoder.documents
        latencies = []
        for _ in range(repetitions):
            runtime.lanes._lanes.clear()
            started = time.perf_counter()
            response = runtime.handle(MockRequest('/x', 'query paraphrase 7'))
            latencies.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, runtime.trace.events[-1]
        result.append({'scenario': name, 'requests': repetitions,
            'static_fuse': fusion.calls-before[0], 'document_encode': encoder.documents-before[1],
            'p50_ms': round(statistics.median(latencies), 3),
            'p95_ms': round(sorted(latencies)[math.ceil(.95*len(latencies))-1], 3)})
    repo.close()
print(json.dumps(result))
"""


def measure(root: Path):
    result = subprocess.run(
        [sys.executable, "-c", CODE],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return json.loads(result.stdout)


def main():
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.run(
        ["git", "archive", BASELINE, "agent_mock_service"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    with tempfile.TemporaryDirectory() as directory:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as source:
            source.extractall(directory, filter="data")
        baseline = measure(Path(directory))
    current = measure(root)
    print(
        json.dumps(
            {
                "baseline_commit": BASELINE,
                "dataset": "100 scenarios, two rounds each; 100 endpoint documents; Top-N=2; no active lanes",
                "model": "HashingEncoder(32), deterministic Joining fusion",
                "baseline": baseline,
                "current": current,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
