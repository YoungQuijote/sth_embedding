from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from typing import Any

from .calibration import CalibrationBootstrapper, CalibrationProfile, neutral_profile
from .config import RuntimeConfig
from .context import ScenarioContextBuilder
from .contracts import (
    AffinityExtractor,
    BusinessFeatureExtractor,
    CalibrationCorpusProvider,
    ContextFusionProvider,
    EmbeddingEncoder,
    FeatureComparator,
    Judge,
    QueryParser,
    TraceWriter,
)
from .defaults import (
    DefaultAffinityExtractor,
    DefaultFeatureComparator,
    DefaultQueryParser,
    EmptyFeatureExtractor,
    JoiningContextFusionProvider,
    create_default_encoder,
)
from .domain import JudgeCandidate, MatchDecision, MockRequest, MockResponse, RuntimeInteraction
from .execution import ExecutionResourcePool
from .judge import FakeJudge
from .lane import LaneManager
from .plugin import BusinessPlugin, BusinessPluginRegistry
from .recall import LaneContextRecaller, SemanticRecaller, fuse_recall
from .repository import SQLiteRepository
from .response import ResponseRendererRegistry
from .scoring import BayesEvidenceBuilder, BayesPositionScorer, ScenarioScoreAggregator
from .trace import MemoryTraceWriter


class AgentMockRuntime:
    """Thin orchestrator; answers are loaded only after a position has been selected."""

    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        config: RuntimeConfig | None = None,
        parser: QueryParser[Any] | None = None,
        encoder: EmbeddingEncoder | None = None,
        feature_extractor: BusinessFeatureExtractor | None = None,
        feature_comparator: FeatureComparator | None = None,
        fusion: ContextFusionProvider | None = None,
        affinity_extractor: AffinityExtractor | None = None,
        judge: Judge | None = None,
        calibration: CalibrationProfile | None = None,
        calibration_corpus: CalibrationCorpusProvider | None = None,
        lanes: LaneManager | None = None,
        renderers: ResponseRendererRegistry | None = None,
        trace: TraceWriter | None = None,
        business_plugins: BusinessPluginRegistry | None = None,
        execution_resources: ExecutionResourcePool | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or RuntimeConfig()
        self.encoder = encoder or create_default_encoder(self.config.sentence_transformer_encoder)
        self.execution_resources = execution_resources or ExecutionResourcePool()
        self.default_affinity_extractor = affinity_extractor or DefaultAffinityExtractor()
        if business_plugins is None:
            default_plugin = BusinessPlugin(
                "*",
                parser or DefaultQueryParser(),
                feature_extractor or EmptyFeatureExtractor(),
                feature_comparator or DefaultFeatureComparator(),
                fusion or JoiningContextFusionProvider(),
                judge or FakeJudge(),
                affinity_extractor,
            )
            business_plugins = BusinessPluginRegistry(
                default_plugin, allow_default_plugin=self.config.allow_default_plugin
            )
            if any(
                value is not None
                for value in (
                    parser,
                    feature_extractor,
                    feature_comparator,
                    fusion,
                    affinity_extractor,
                    judge,
                )
            ):
                logging.getLogger(__name__).warning(
                    "legacy per-runtime business components are deprecated; "
                    "use BusinessPluginRegistry"
                )
        self.business_plugins = business_plugins
        default = self.business_plugins.default_plugin
        self.parser = default.parser if default else None
        self.feature_extractor = default.feature_extractor if default else None
        self.feature_comparator = default.feature_comparator if default else None
        self.fusion = default.fusion if default else None
        self.judge = default.judge if default else None
        if calibration is not None and calibration.encoder_fingerprint != self.encoder.fingerprint:
            raise ValueError("calibration and runtime encoder fingerprints differ")
        self.repository.bind_context_builder(
            ScenarioContextBuilder(self.business_plugins, self.encoder, self.execution_resources)
        )
        if calibration is not None:
            self.calibration = calibration
        elif calibration_corpus is not None:
            bootstrapper = CalibrationBootstrapper(
                self.config.bootstrap_hard_negative_k,
                self.config.bootstrap_easy_negative_k,
                self.config.bootstrap_random_seed,
            )
            self.calibration = bootstrapper.load_or_bootstrap(
                repository, self.encoder, calibration_corpus
            )
        else:
            logging.getLogger(__name__).warning(
                "No calibration corpus configured; using neutral calibration profile"
            )
            self.calibration = neutral_profile(self.encoder.fingerprint)
        self.lanes = lanes or LaneManager(self.config.lane_ttl_seconds)
        self.renderers = renderers or ResponseRendererRegistry()
        self.trace = trace or MemoryTraceWriter()
        self.semantic_recaller = SemanticRecaller(self.encoder)
        self.context_recaller = LaneContextRecaller(self.encoder, self.execution_resources)
        self.evidence_builder = BayesEvidenceBuilder()
        self.scorer = BayesPositionScorer(
            self.calibration,
            self.config.transition_prior,
            self.config.transition_floor,
            self.config.availability_alpha,
            self.config.availability_gamma,
            self.config.availability_prior_weight,
        )
        self.aggregator = ScenarioScoreAggregator()

    def handle(self, request: MockRequest) -> MockResponse:
        started = time.perf_counter()
        request_id = f"req_{uuid.uuid4().hex}"
        trace_event: dict[str, Any] = {
            "request_id": request_id,
            "timestamp": time.time(),
            "endpoint_id": request.endpoint_id,
            "upstream_request_id": request.upstream_request_id,
            "calibration_version": self.calibration.version,
            "timing": {},
        }
        try:
            try:
                plugin = self.business_plugins.get(request.endpoint_id)
            except KeyError as error:
                return self._failure(
                    request_id, MatchDecision.MISS, trace_event, started, str(error)
                )
            stage = time.perf_counter()
            query = plugin.parser.parse(request.body).strip()
            trace_event["query_content"] = query
            samples = self.repository.list_endpoint(request.endpoint_id)
            direct = [sample for sample in samples if sample.mocked_query.strip() == query]
            if len(direct) == 1:
                trace_event["timing"]["parse_ms"] = (time.perf_counter() - stage) * 1000
                lane_id = self.lanes.select_direct_lane(
                    str(direct[0].sample_id), direct[0].round_id
                )
                return self._complete_match(
                    request,
                    request_id,
                    query,
                    direct[0],
                    trace_event,
                    started,
                    "DIRECT_MATCH",
                    lane_id,
                )
            affinity_extractor = plugin.affinity_extractor or self.default_affinity_extractor
            affinity = affinity_extractor.extract(request.headers)
            features = self.execution_resources.call_component(
                plugin.feature_extractor, plugin.feature_extractor.extract, query
            )
            trace_event["request_affinity"] = asdict(affinity)
            trace_event["timing"]["parse_feature_ms"] = (time.perf_counter() - stage) * 1000
            stage = time.perf_counter()
            local = self.semantic_recaller.recall(
                query,
                features,
                samples,
                self.config.semantic_recall_k,
                plugin.feature_comparator,
            )
            scenario_ids = {candidate.scenario_id for candidate in local}
            active_lanes = self.lanes.active()
            scenario_ids.update(
                scenario_id for lane in active_lanes for scenario_id in lane.scenario_hypotheses
            )
            scenarios = {
                scenario.scenario_id: scenario
                for scenario in self.repository.get_many(scenario_ids)
            }
            context = self.context_recaller.recall(
                query,
                active_lanes,
                scenarios,
                request.endpoint_id,
                self.config.context_recall_k,
                plugin.fusion,
            )
            local, context = fuse_recall(
                local,
                context,
                self.config.recall_fusion_mode,
                max(self.config.semantic_recall_k, self.config.context_recall_k),
            )
            trace_event["timing"]["recall_ms"] = (time.perf_counter() - stage) * 1000
            scenario_ids = {candidate.scenario_id for candidate in local} | {
                candidate.scenario_id for candidate in context
            }
            scenarios = {
                scenario.scenario_id: scenario
                for scenario in self.repository.get_many(scenario_ids)
            }
            lanes_by_id = {lane.lane_id: lane for lane in active_lanes}
            candidates: list[JudgeCandidate] = []
            for evidence, position, runtime_context in self.evidence_builder.build(
                list(scenarios.values()),
                request.endpoint_id,
                features,
                affinity,
                local,
                context,
                lanes_by_id,
                plugin.feature_comparator,
            ):
                candidates.append(
                    JudgeCandidate(
                        evidence.scenario_id,
                        evidence.position,
                        evidence.lane_id,
                        request.endpoint_id,
                        position.sample.mocked_query,
                        self.scorer.score(evidence),
                        position.context.fused_context,
                        runtime_context,
                    )
                )
            scenario_candidates = self.aggregator.aggregate(
                candidates, self.config.scenario_recall_n
            )
            trace_event["recall"] = {
                "local": [asdict(item) | {"sample": None} for item in local],
                "context": [asdict(item) for item in context],
                "candidate_scenario_ids": sorted(scenario_ids),
            }
            trace_event["bayes_scores"] = [asdict(item.score) for item in candidates]
            result = self.execution_resources.call_component(
                plugin.judge, plugin.judge.judge, query, scenario_candidates
            )
            trace_event["judge"] = {
                "model": plugin.judge.model_name,
                "prompt_version": plugin.judge.prompt_version,
                "decision": result.decision.value,
                "confidence": result.confidence,
                "short_reason": result.short_reason,
                "raw_output": result.raw_output,
                "candidate_scenarios": [
                    {
                        "scenario_id": item.scenario_id,
                        "score": item.score,
                        "posterior": item.posterior,
                        "positions": [
                            {
                                "position": position.position,
                                "lane_id": position.lane_id,
                                "mocked_query": position.mocked_query,
                                "scenario_context": position.scenario_context,
                                "runtime_context": position.runtime_context,
                            }
                            for position in item.positions
                        ],
                    }
                    for item in scenario_candidates
                ],
            }
            if (
                result.decision is not MatchDecision.MATCH
                or result.scenario_id is None
                or result.position is None
            ):
                return self._failure(
                    request_id, result.decision, trace_event, started, result.short_reason
                )
            scenario = scenarios.get(result.scenario_id)
            selected = (
                next(
                    (
                        position.sample
                        for position in scenario.positions
                        if position.position == result.position
                        and position.sample.endpoint_id == request.endpoint_id
                    ),
                    None,
                )
                if scenario
                else None
            )
            if selected is None:
                raise ValueError("judge selected an invalid endpoint position")
            return self._complete_match(
                request,
                request_id,
                query,
                selected,
                trace_event,
                started,
                "JUDGE_MATCH",
                result.lane_id,
            )
        except Exception as error:
            trace_event["error"] = {
                "error_stage": "runtime",
                "error_type": type(error).__name__,
                "error_message": str(error),
            }
            self._finish_trace(trace_event, started)
            return MockResponse(
                request_id,
                MatchDecision.MISS,
                500,
                {"error": "mock runtime failure", "request_id": request_id},
                {"content-type": "application/json"},
            )

    def _complete_match(
        self,
        request: MockRequest,
        request_id: str,
        query: str,
        sample: Any,
        event: dict[str, Any],
        started: float,
        path: str,
        lane_id: str | None,
    ) -> MockResponse:
        # Fetch after selection so judge candidates can never contain candidate answers.
        stored = self.repository.get_by_hash(sample.sample_hash)
        if stored is None:
            raise KeyError(sample.sample_hash)
        self.repository.increment_invoked(stored.sample_hash)
        scenario = self.repository.get_facts(str(stored.sample_id))
        lane_before = self.lanes.get(lane_id) if lane_id else None
        before_version = lane_before.state_version if lane_before else None
        lane = (
            self.lanes.update(
                scenario,
                RuntimeInteraction(
                    request_id,
                    request.endpoint_id,
                    str(stored.sample_id),
                    stored.position_id,
                    stored.round_id,
                    query,
                    stored.mocked_answer,
                ),
                lane_id=lane_id,
            )
            if scenario
            else None
        )
        protocol = request.response_protocol or self.config.default_response_protocol
        body, headers = self.renderers.get(protocol).render(stored.mocked_answer)
        event["selection"] = {
            "path": path,
            "selected_scenario_id": str(stored.sample_id),
            "selected_position": stored.position_id,
            "selected_sample_hash": stored.sample_hash,
            "response_protocol": protocol.value,
            "response_status": 200,
            "response_size": len(str(body)),
        }
        event["runtime_state"] = {
            "lane_id": lane.lane_id if lane else None,
            "state_version_before": before_version,
            "state_version_after": lane.state_version if lane else None,
            "active_rounds": sorted(lane.active_rounds) if lane else [],
        }
        self._finish_trace(event, started)
        return MockResponse(
            request_id,
            MatchDecision.MATCH,
            200,
            body,
            headers,
            str(stored.sample_id),
            stored.position_id,
        )

    def _failure(
        self,
        request_id: str,
        decision: MatchDecision,
        event: dict[str, Any],
        started: float,
        reason: str,
    ) -> MockResponse:
        status = 409 if decision is MatchDecision.AMBIGUOUS else 404
        event["error"] = {
            "error_stage": "judge",
            "error_type": decision.value,
            "error_message": reason,
        }
        self._finish_trace(event, started)
        return MockResponse(
            request_id,
            decision,
            status,
            {"decision": decision.value, "request_id": request_id, "reason": reason},
            {"content-type": "application/json"},
        )

    def _finish_trace(self, event: dict[str, Any], started: float) -> None:
        event["timing"]["total_ms"] = (time.perf_counter() - started) * 1000
        self.trace.write(event)
