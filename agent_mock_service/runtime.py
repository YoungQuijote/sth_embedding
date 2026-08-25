from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from .calibration import CalibrationProfile
from .config import RuntimeConfig
from .contracts import AffinityExtractor, BusinessFeatureExtractor, ContextFusionProvider, EmbeddingEncoder, FeatureComparator, Judge, QueryParser, TraceWriter
from .defaults import DefaultAffinityExtractor, DefaultFeatureComparator, DefaultQueryParser, EmptyFeatureExtractor, HashingEncoder, JoiningContextFusionProvider, ipv4_affinity
from .domain import JudgeCandidate, MatchDecision, MockRequest, MockResponse, RuntimeInteraction
from .judge import FakeJudge
from .lane import LaneManager
from .recall import LaneContextRecaller, SemanticRecaller, fuse_recall
from .repository import SQLiteRepository
from .response import ResponseRendererRegistry
from .scoring import BayesScenarioScorer, normalize_scores
from .trace import MemoryTraceWriter


class AgentMockRuntime:
    """Thin orchestrator; answers are loaded only after a position has been selected."""

    def __init__(self, repository: SQLiteRepository, *, config: RuntimeConfig | None = None, parser: QueryParser[Any] | None = None, encoder: EmbeddingEncoder | None = None, feature_extractor: BusinessFeatureExtractor | None = None, feature_comparator: FeatureComparator | None = None, fusion: ContextFusionProvider | None = None, affinity_extractor: AffinityExtractor | None = None, judge: Judge | None = None, calibration: CalibrationProfile | None = None, lanes: LaneManager | None = None, renderers: ResponseRendererRegistry | None = None, trace: TraceWriter | None = None) -> None:
        self.repository = repository
        self.config = config or RuntimeConfig()
        self.parser = parser or DefaultQueryParser()
        self.encoder = encoder or HashingEncoder()
        self.feature_extractor = feature_extractor or EmptyFeatureExtractor()
        self.feature_comparator = feature_comparator or DefaultFeatureComparator()
        self.fusion = fusion or JoiningContextFusionProvider()
        self.affinity_extractor = affinity_extractor or DefaultAffinityExtractor()
        self.judge = judge or FakeJudge()
        self.calibration = calibration or CalibrationProfile("uncalibrated", "", self.encoder.fingerprint)
        self.lanes = lanes or LaneManager(self.config.lane_ttl_seconds)
        self.renderers = renderers or ResponseRendererRegistry()
        self.trace = trace or MemoryTraceWriter()
        self.semantic_recaller = SemanticRecaller(self.encoder, self.feature_comparator)
        self.context_recaller = LaneContextRecaller(self.encoder, self.fusion)
        self.scorer = BayesScenarioScorer(self.calibration, self.config.transition_prior, self.config.transition_floor, self.config.availability_alpha, self.config.availability_gamma)

    def handle(self, request: MockRequest) -> MockResponse:
        started = time.perf_counter()
        request_id = f"req_{uuid.uuid4().hex}"
        trace_event: dict[str, Any] = {"request_id": request_id, "timestamp": time.time(), "endpoint_id": request.endpoint_id, "upstream_request_id": request.upstream_request_id, "calibration_version": self.calibration.version, "timing": {}}
        try:
            stage = time.perf_counter()
            query = self.parser.parse(request.body).strip()
            affinity = self.affinity_extractor.extract(request.headers)
            features = self.feature_extractor.extract(query)
            trace_event["query_content"] = query
            trace_event["request_affinity"] = asdict(affinity)
            trace_event["timing"]["parse_feature_ms"] = (time.perf_counter() - stage) * 1000
            samples = self.repository.list_endpoint(request.endpoint_id)
            direct = [sample for sample in samples if sample.mocked_query.strip() == query]
            if len(direct) == 1:
                return self._complete_match(request, request_id, query, direct[0], trace_event, started, "DIRECT_MATCH")
            stage = time.perf_counter()
            local = self.semantic_recaller.recall(query, features, samples, self.config.semantic_recall_k)
            scenario_ids = {candidate.scenario_id for candidate in local}
            active_lanes = self.lanes.active()
            scenario_ids.update(scenario_id for lane in active_lanes for scenario_id in lane.scenario_hypotheses)
            scenarios = {scenario.scenario_id: scenario for scenario in self.repository.get_many(scenario_ids)}
            context = self.context_recaller.recall(query, active_lanes, scenarios, request.endpoint_id, self.config.context_recall_k)
            local, context = fuse_recall(local, context, self.config.recall_fusion_mode, max(self.config.semantic_recall_k, self.config.context_recall_k))
            trace_event["timing"]["recall_ms"] = (time.perf_counter() - stage) * 1000
            scenario_ids = {candidate.scenario_id for candidate in local} | {candidate.scenario_id for candidate in context}
            scenarios = {scenario.scenario_id: scenario for scenario in self.repository.get_many(scenario_ids)}
            local_map = {(item.scenario_id, item.position): item.similarity for item in local}
            context_map = {(item.scenario_id, item.position): item.similarity for item in context}
            candidates: list[JudgeCandidate] = []
            scores = []
            for scenario in scenarios.values():
                lane = self.lanes.find_for_scenario(scenario.scenario_id)
                active_rounds = lane.active_rounds if lane else set()
                relation_by_position = {position.position: self.feature_comparator.compare(features, position.features) for position in scenario.positions}
                for position in scenario.positions:
                    if position.sample.endpoint_id != request.endpoint_id or relation_by_position[position.position][0].value == "CONFLICT":
                        continue
                    semantic = local_map.get((scenario.scenario_id, position.position), 0.0)
                    context_score = context_map.get((scenario.scenario_id, position.position), 0.0)
                    if semantic == 0.0 and context_score == 0.0:
                        continue
                    score = self.scorer.score(scenario.scenario_id, position.position, position.sample.round_id, position.sample.availability, semantic, context_score, relation_by_position[position.position][1], ipv4_affinity(affinity, scenario.registry_affinity_infos), active_rounds)
                    scores.append(score)
                    candidates.append(JudgeCandidate(scenario.scenario_id, position.position, request.endpoint_id, position.sample.mocked_query, score, position.context.fused_context))
            normalize_scores(scores)
            candidates.sort(key=lambda item: item.score.total_log_score, reverse=True)
            candidates = candidates[: self.config.scenario_recall_n]
            trace_event["recall"] = {"local": [asdict(item) | {"sample": None} for item in local], "context": [asdict(item) for item in context], "candidate_scenario_ids": sorted(scenario_ids)}
            trace_event["bayes_scores"] = [asdict(item.score) for item in candidates]
            result = self.judge.judge(query, [interaction.returned_answer for lane in active_lanes for interaction in lane.interactions], candidates)
            trace_event["judge"] = {"model": self.judge.model_name, "prompt_version": self.judge.prompt_version, "decision": result.decision.value, "confidence": result.confidence, "short_reason": result.short_reason, "raw_output": result.raw_output, "candidate_positions": [{"scenario_id": item.scenario_id, "position": item.position, "mocked_query": item.mocked_query} for item in candidates]}
            if result.decision is not MatchDecision.MATCH or result.scenario_id is None or result.position is None:
                return self._failure(request_id, result.decision, trace_event, started, result.short_reason)
            scenario = scenarios.get(result.scenario_id)
            selected = next((position.sample for position in scenario.positions if position.position == result.position and position.sample.endpoint_id == request.endpoint_id), None) if scenario else None
            if selected is None:
                raise ValueError("judge selected an invalid endpoint position")
            return self._complete_match(request, request_id, query, selected, trace_event, started, "JUDGE_MATCH")
        except Exception as error:
            trace_event["error"] = {"error_stage": "runtime", "error_type": type(error).__name__, "error_message": str(error)}
            self._finish_trace(trace_event, started)
            return MockResponse(request_id, MatchDecision.MISS, 500, {"error": "mock runtime failure", "request_id": request_id}, {"content-type": "application/json"})

    def _complete_match(self, request: MockRequest, request_id: str, query: str, sample: Any, event: dict[str, Any], started: float, path: str) -> MockResponse:
        # Fetch after selection so judge candidates can never contain candidate answers.
        stored = self.repository.get_by_hash(sample.sample_hash)
        if stored is None:
            raise KeyError(sample.sample_hash)
        self.repository.increment_invoked(stored.sample_hash)
        scenario = self.repository.get(str(stored.sample_id))
        lane_before = self.lanes.find_for_scenario(str(stored.sample_id))
        before_version = lane_before.state_version if lane_before else None
        lane = self.lanes.update(scenario, RuntimeInteraction(request_id, request.endpoint_id, str(stored.sample_id), stored.position_id, stored.round_id, query, stored.mocked_answer)) if scenario else None
        protocol = request.response_protocol or self.config.default_response_protocol
        body, headers = self.renderers.get(protocol).render(stored.mocked_answer)
        event["selection"] = {"path": path, "selected_scenario_id": str(stored.sample_id), "selected_position": stored.position_id, "selected_sample_hash": stored.sample_hash, "response_protocol": protocol.value, "response_status": 200, "response_size": len(str(body))}
        event["runtime_state"] = {"lane_id": lane.lane_id if lane else None, "state_version_before": before_version, "state_version_after": lane.state_version if lane else None, "active_rounds": sorted(lane.active_rounds) if lane else []}
        self._finish_trace(event, started)
        return MockResponse(request_id, MatchDecision.MATCH, 200, body, headers, str(stored.sample_id), stored.position_id)

    def _failure(self, request_id: str, decision: MatchDecision, event: dict[str, Any], started: float, reason: str) -> MockResponse:
        status = 409 if decision is MatchDecision.AMBIGUOUS else 404
        event["error"] = {"error_stage": "judge", "error_type": decision.value, "error_message": reason}
        self._finish_trace(event, started)
        return MockResponse(request_id, decision, status, {"decision": decision.value, "request_id": request_id, "reason": reason}, {"content-type": "application/json"})

    def _finish_trace(self, event: dict[str, Any], started: float) -> None:
        event["timing"]["total_ms"] = (time.perf_counter() - started) * 1000
        self.trace.write(event)
