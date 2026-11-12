from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from typing import Any

from .bm25 import BM25Recaller, DefaultTokenizer
from .calibration import (
    CalibrationBootstrapper,
    CalibrationConfig,
    CalibrationProfile,
    neutral_profile,
)
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
    Reranker,
    Tokenizer,
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
from .domain import (
    FeatureRelation,
    JudgeCandidate,
    MatchDecision,
    MockRequest,
    MockResponse,
    PositionRef,
    RecallFusionMode,
    RuntimeInteraction,
)
from .execution import ExecutionResourcePool
from .judge import FakeJudge
from .lane import LaneManager
from .plugin import BusinessPlugin, BusinessPluginRegistry
from .recall import LaneContextRecaller, SemanticRecaller, fuse_recall_candidates
from .repository import SQLiteRepository
from .reranker import BiEncoderReranker
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
        tokenizer: Tokenizer | None = None,
        reranker: Reranker | None = None,
        calibration_config: CalibrationConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or RuntimeConfig()
        self.encoder = encoder or create_default_encoder(self.config.sentence_transformer_encoder)
        self.execution_resources = execution_resources or ExecutionResourcePool()
        self.default_tokenizer = tokenizer or DefaultTokenizer()
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
                repository,
                self.encoder,
                calibration_corpus,
                config=calibration_config,
                tokenizer=self.default_tokenizer,
                feature_comparator=(
                    self.business_plugins.default_plugin.feature_comparator
                    if self.business_plugins.default_plugin
                    else None
                ),
                bm25_k1=self.config.bm25_k1,
                bm25_b=self.config.bm25_b,
            )
        else:
            logging.getLogger(__name__).warning(
                "No calibration corpus configured; using neutral calibration profile"
            )
            self.calibration = neutral_profile(self.encoder.fingerprint)
        self.lanes = lanes or LaneManager(self.config.lane_ttl_seconds)
        self.renderers = renderers or ResponseRendererRegistry()
        self.trace = trace or MemoryTraceWriter()
        from .derived import DerivedStore

        self.derived_store = DerivedStore(repository, self.execution_resources)
        self.materializer = repository.materializer
        repository.registration_callback = self._prepare_registered
        self.semantic_recaller = SemanticRecaller(self.encoder, self.derived_store)
        self.bm25_recallers: dict[str, BM25Recaller] = {}
        self.context_recaller = LaneContextRecaller(self.encoder, self.execution_resources)
        if reranker is None and self.config.reranker_type != "bi_encoder":
            raise ValueError("non-default reranker_type requires an explicit Reranker instance")
        self.default_reranker = reranker or BiEncoderReranker(self.encoder, self.derived_store)
        self.evidence_builder = BayesEvidenceBuilder()
        self.scorer = BayesPositionScorer(
            self.calibration,
            self.config.transition_prior,
            self.config.transition_floor,
            self.config.availability_alpha,
            self.config.availability_gamma,
            self.config.availability_prior_weight,
            self.config.runtime_llr_weights,
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
            "calibration_evidences": sorted(self.calibration.distributions),
            "computation": {
                "sample_document_encode": 0,
                "static_context_encode": 0,
                "static_fuse": 0,
                "dynamic_lane_fuse": 0,
            },
            "timing": {
                name: 0.0
                for name in (
                    "facts_read_ms",
                    "semantic_index_load_ms",
                    "semantic_query_encode_ms",
                    "semantic_recall_ms",
                    "bm25_index_load_ms",
                    "bm25_recall_ms",
                    "context_artifact_load_ms",
                    "lane_fusion_ms",
                    "lane_static_materialization_ms",
                    "final_materialization_ms",
                    "rerank_ms",
                    "judge_ms",
                )
            },
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
            facts_started = time.perf_counter()
            samples = self.repository.list_endpoint(request.endpoint_id)
            trace_event["timing"]["facts_read_ms"] = (time.perf_counter() - facts_started) * 1000
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
            from .materialization import component_fingerprint

            feature_fingerprint = component_fingerprint(plugin.feature_extractor)
            samples = self.derived_store.feature_views(samples, plugin.feature_extractor)
            affinity_extractor = plugin.affinity_extractor or self.default_affinity_extractor
            affinity = affinity_extractor.extract(request.headers)
            features = self.execution_resources.call_component(
                plugin.feature_extractor, plugin.feature_extractor.extract, query
            )
            if component_fingerprint(plugin.feature_extractor) != feature_fingerprint:
                raise ValueError("FeatureExtractor changed while preparing the query view")
            trace_event["request_affinity"] = asdict(affinity)
            trace_event["timing"]["parse_feature_ms"] = (time.perf_counter() - stage) * 1000
            stage = time.perf_counter()
            single_source = self.config.single_recall_source
            use_semantic = self.config.recall_fusion_mode not in {
                RecallFusionMode.SINGLE,
                RecallFusionMode.CONTEXT_ONLY,
            } or (
                self.config.recall_fusion_mode is RecallFusionMode.SINGLE
                and single_source == "semantic"
            )
            use_context = self.config.recall_fusion_mode not in {
                RecallFusionMode.SINGLE,
                RecallFusionMode.SEMANTIC_ONLY,
            } or (
                self.config.recall_fusion_mode is RecallFusionMode.SINGLE
                and single_source == "context"
            )
            use_bm25 = self.config.bm25_enabled and (
                self.config.recall_fusion_mode in {RecallFusionMode.UNION, RecallFusionMode.RRF}
                or (
                    self.config.recall_fusion_mode is RecallFusionMode.SINGLE
                    and single_source == "bm25"
                )
            )
            local = (
                self.semantic_recaller.recall(
                    query,
                    features,
                    samples,
                    self.config.semantic_recall_k,
                    plugin.feature_comparator,
                    timings=trace_event["timing"],
                    metrics=trace_event["computation"],
                )
                if use_semantic
                else []
            )
            endpoint_tokenizer = plugin.tokenizer or self.default_tokenizer
            bm25_key = (
                f"{request.endpoint_id}:{endpoint_tokenizer.fingerprint}:"
                f"{self.config.bm25_k1}:{self.config.bm25_b}"
            )
            bm25_recaller = self.bm25_recallers.setdefault(
                bm25_key,
                BM25Recaller(endpoint_tokenizer, self.config.bm25_k1, self.config.bm25_b),
            )
            bm25 = (
                bm25_recaller.recall(
                    query, samples, self.config.bm25_recall_k, timings=trace_event["timing"]
                )
                if use_bm25
                else []
            )
            scenario_ids = {candidate.scenario_id for candidate in local} | {
                candidate.scenario_id for candidate in bm25
            }
            active_lanes = self.lanes.active()
            if use_context:
                scenario_ids.update(
                    scenario_id for lane in active_lanes for scenario_id in lane.scenario_hypotheses
                )
            facts_started = time.perf_counter()
            scenarios = {
                scenario.scenario_id: scenario
                for scenario in self.repository.get_facts_many(scenario_ids)
            }
            trace_event["timing"]["facts_read_ms"] += (time.perf_counter() - facts_started) * 1000
            if use_context:
                self.lanes.reevaluate(scenarios)
                active_lanes = self.lanes.active()
            lane_refs = (
                {
                    PositionRef(p.scenario_id, p.position)
                    for lane in active_lanes
                    for sid in lane.scenario_hypotheses
                    for p in (scenarios[sid].positions if sid in scenarios else [])
                    if p.sample.endpoint_id == request.endpoint_id
                    and int(p.sample.round_id) in {int(r) for r in lane.active_rounds}
                }
                if use_context
                else set()
            )
            self._materialize(scenarios, lane_refs, "lane_recall", trace_event)
            trace_event["computation"]["dynamic_lane_fuse"] = (
                len(active_lanes) if use_context else 0
            )
            lane_started = time.perf_counter()
            context = (
                self.context_recaller.recall(
                    query,
                    active_lanes,
                    scenarios,
                    request.endpoint_id,
                    self.config.context_recall_k,
                    plugin.fusion,
                )
                if use_context
                else []
            )
            trace_event["timing"]["lane_fusion_ms"] = (time.perf_counter() - lane_started) * 1000
            fused = fuse_recall_candidates(
                local,
                context,
                bm25,
                self.config.recall_fusion_mode,
                k=self.config.fused_recall_k
                or max(
                    self.config.semantic_recall_k,
                    self.config.context_recall_k,
                    self.config.bm25_recall_k,
                ),
                rrf_k=self.config.rrf_k,
                single_source=self.config.single_recall_source,
                samples=samples,
            )
            samples_by_position = {
                (str(sample.sample_id), sample.position_id): sample for sample in samples
            }
            hard_conflicts = 0
            eligible = []
            for candidate in fused:
                sample = samples_by_position.get((candidate.scenario_id, candidate.position))
                if sample is None:
                    continue
                relation, _ = plugin.feature_comparator.compare(features, sample.features)
                if relation is FeatureRelation.CONFLICT:
                    hard_conflicts += 1
                    continue
                eligible.append(candidate)
            reranker = plugin.reranker or self.default_reranker
            rerank_started = time.perf_counter()
            fused = self.execution_resources.call_component(
                reranker,
                reranker.rerank,
                query,
                eligible,
                top_n=self.config.reranker_top_n,
                **(
                    {"metrics": trace_event["computation"]}
                    if type(reranker) is BiEncoderReranker
                    else {}
                ),
            )
            trace_event["timing"]["rerank_ms"] = (time.perf_counter() - rerank_started) * 1000
            trace_event["timing"]["recall_ms"] = (time.perf_counter() - stage) * 1000
            scenario_ids = {candidate.scenario_id for candidate in fused}
            facts_started = time.perf_counter()
            scenarios = {
                scenario.scenario_id: scenario
                for scenario in self.repository.get_facts_many(scenario_ids)
            }
            trace_event["timing"]["facts_read_ms"] += (time.perf_counter() - facts_started) * 1000
            final_refs = {PositionRef(c.scenario_id, c.position) for c in fused}
            self._materialize(scenarios, final_refs, "final_candidates", trace_event)
            # Fresh feature view is independent of static Context and fact identity.
            for scenario in scenarios.values():
                for position in scenario.positions:
                    current = samples_by_position.get((position.scenario_id, position.position))
                    if current and current.sample_hash == position.sample.sample_hash:
                        position.features = current.features
            selected_revisions = {
                (c.scenario_id, c.position, c.lane_id): c.fact_revision for c in fused
            }
            selected_hashes = {(c.scenario_id, c.position, c.lane_id): c.sample_hash for c in fused}
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
                bm25,
                fused,
            ):
                candidates.append(
                    JudgeCandidate(
                        evidence.scenario_id,
                        evidence.position,
                        evidence.lane_id,
                        request.endpoint_id,
                        position.sample.mocked_query,
                        self.scorer.score(evidence),
                        position.context.fused_context if position.context else None,
                        runtime_context,
                    )
                )
            scenario_candidates = self.aggregator.aggregate(
                candidates, self.config.scenario_recall_n
            )
            trace_event["recall"] = {
                "local": [
                    asdict(item) | {"sample": None, "rank": rank}
                    for rank, item in enumerate(local, start=1)
                ],
                "context": [
                    asdict(item) | {"rank": rank} for rank, item in enumerate(context, start=1)
                ],
                "bm25": [asdict(item) | {"sample": None} for item in bm25],
                "fusion_mode": self.config.recall_fusion_mode.value,
                "fused": [asdict(item) for item in fused],
                "hard_conflicts": hard_conflicts,
                "candidate_scenario_ids": sorted(scenario_ids),
            }
            trace_event["reranker"] = {
                "fingerprint": reranker.fingerprint,
                "input_count": len(eligible),
                "output_count": len(fused),
            }
            trace_event["bayes_scores"] = [asdict(item.score) for item in candidates]
            judge_started = time.perf_counter()
            result = self.execution_resources.call_component(
                plugin.judge, plugin.judge.judge, query, scenario_candidates
            )
            trace_event["timing"]["judge_ms"] = (time.perf_counter() - judge_started) * 1000
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
            selected_path = (result.scenario_id, result.position, result.lane_id)
            allowed = {
                (p.scenario_id, p.position, p.lane_id)
                for group in scenario_candidates
                for p in group.positions
            }
            if selected_path not in allowed:
                raise ValueError("judge selected a path outside candidates")
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
            if (
                selected is None
                or selected.sample_hash != selected_hashes.get(selected_path)
                or selected.fact_revision != selected_revisions.get(selected_path)
            ):
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
        except Exception as error:  # noqa: BLE001 - report business failures at the service boundary
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

    def _prepare_registered(self, refs):
        self.repository.last_derived_errors = {}
        by_endpoint = {}
        snapshot = self.repository.snapshot()
        for ref in refs:
            fact = snapshot.facts.get(ref)
            if fact:
                sample = self.repository.get_by_hash(fact.sample_hash)
                if sample:
                    by_endpoint.setdefault(fact.endpoint_id, []).append(sample)
        for endpoint, samples in by_endpoint.items():
            try:
                plugin = self.business_plugins.get(endpoint)
                self.derived_store.feature_views(samples, plugin.feature_extractor)
                self.derived_store.document_vectors(samples, self.encoder)
            except Exception as error:  # noqa: BLE001 - report business failures at the service boundary
                self.repository.last_derived_errors[endpoint] = str(error)
                logging.getLogger(__name__).warning(
                    "Committed registration; derived data unavailable for %s: %s", endpoint, error
                )

    def register(self, sample, affinity=None, **kwargs):
        """Versioned registration using the runtime-owned plugin, encoder and resource pool."""
        from dataclasses import replace

        from .materialization import component_fingerprint

        plugin = self.business_plugins.get(sample.endpoint_id)
        fp = component_fingerprint(plugin.feature_extractor)
        features = self.execution_resources.call_component(
            plugin.feature_extractor, plugin.feature_extractor.extract, sample.mocked_query
        )
        return self.repository.register(
            replace(sample, features=features), affinity, extractor_fingerprint=fp, **kwargs
        )

    def _materialize(self, scenarios, refs, reason, event):
        if not refs:
            return
        tick = time.perf_counter()
        batch = self.materializer.ensure_position_contexts(refs, reason=reason)
        event["timing"]["context_artifact_load_ms"] += batch.artifact_load_ms
        event["computation"]["static_fuse"] += sum(r.fuse_count for r in batch.results.values())
        event["computation"]["static_context_encode"] += sum(
            r.embedding_count for r in batch.results.values()
        )
        event.setdefault("materialization", {})[reason] = [
            {key: value for key, value in asdict(result).items() if key != "artifact"}
            for result in batch.results.values()
        ]
        for scenario in scenarios.values():
            for position in scenario.positions:
                result = batch.results.get(PositionRef(position.scenario_id, position.position))
                if result:
                    position.context = result.artifact
        name = (
            "lane_static_materialization_ms"
            if reason == "lane_recall"
            else "final_materialization_ms"
        )
        event["timing"][name] = (time.perf_counter() - tick) * 1000

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
        stored = self.repository.claim_selected_sample(sample.sample_hash, sample.fact_revision)
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
