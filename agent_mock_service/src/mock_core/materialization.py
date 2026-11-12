"""Explicit, version-checked Position materialization; SQL never runs on model executors."""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import Future
from dataclasses import dataclass

from .context import PositionContextCache, serialize_context_messages
from .dag import digest, lineage, resolve_context
from .domain import ContextMessage, ContextReadResult, MaterializationBatchResult, ScenarioContext

logger = logging.getLogger(__name__)


def component_fingerprint(component):
    value = getattr(component, "fingerprint", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{type(component).__name__} requires a reliable fingerprint for persistence"
        )
    return value


def valid_vector(raw, dimension, expected=None):
    try:
        vector = json.loads(raw)
        if not isinstance(vector, list) or not vector or len(vector) != dimension:
            return None
        if expected is not None and dimension != expected:
            return None
        if any(
            not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
            for x in vector
        ):
            return None
        return vector
    except (ValueError, TypeError):
        return None


@dataclass
class Plan:
    ref: object
    resolution: object
    lineage: str
    fusion: object
    fusion_fp: str
    encoder_fp: str
    inputs: list
    raw: str
    query: str
    endpoint: str
    fact_revision: int


class PositionContextMaterializer:
    def __init__(self, repository, builder):
        self.repository = repository
        self.builder = builder
        self.encoder = builder.encoder
        self.resources = builder.execution_resources
        self.cache = PositionContextCache()

    def _plan(self, ref, snapshot):
        resolution = resolve_context(ref, snapshot)
        fact = snapshot.facts.get(ref)
        if fact is None:
            return None
        fusion = self.builder._resolve_fusion(fact.endpoint_id)
        registry_get = getattr(self.builder.fusion, "get", None)
        fp = (
            registry_get(fact.endpoint_id).fusion_fingerprint()
            if registry_get
            else component_fingerprint(fusion)
        )
        messages = []
        for ancestor in resolution.ancestors:
            source = snapshot.facts.get(ancestor)
            if source:
                messages.extend(
                    [
                        ContextMessage("question", source.query),
                        ContextMessage("answer", source.answer),
                    ]
                )
        messages.append(ContextMessage("question", fact.query))
        return Plan(
            ref,
            resolution,
            lineage(ref, snapshot),
            fusion,
            fp,
            component_fingerprint(self.encoder),
            messages,
            serialize_context_messages(messages),
            fact.query,
            fact.endpoint_id,
            fact.fact_revision,
        )

    def _read_plan(self, plan):
        ref = plan.ref
        result = ContextReadResult(
            ref,
            plan.resolution.position_context_completeness,
            "miss",
            lineage_fingerprint=plan.lineage,
            fusion_fingerprint=plan.fusion_fp,
            encoder_fingerprint=plan.encoder_fp,
            missing_refs=plan.resolution.missing_refs,
            incomplete_rounds=plan.resolution.incomplete_rounds,
        )
        if not result.completeness:
            result.status = "incomplete"
            return result, None
        if not self._current(plan):
            result.status, result.stale_discard = "stale", True
            return result, None
        connection = self.repository._connection
        row = connection.execute(
            "SELECT * FROM position_context_artifacts WHERE scenario_id=? AND position_id=? AND lineage=? AND fusion=?",
            (ref.scenario_id, ref.position_id, plan.lineage, plan.fusion_fp),
        ).fetchone()
        if row is None:
            state = connection.execute(
                "SELECT status FROM position_context_state WHERE scenario_id=? AND position_id=?",
                (ref.scenario_id, ref.position_id),
            ).fetchone()
            result.status = "stale" if state and state[0] == "STALE" else "miss"
            return result, None
        embedding = connection.execute(
            "SELECT * FROM context_embeddings WHERE artifact_fingerprint=? AND encoder=?",
            (row["artifact_fingerprint"], plan.encoder_fp),
        ).fetchone()
        cache_key = (
            ref,
            plan.lineage,
            plan.fusion_fp,
            plan.encoder_fp,
            row["artifact_fingerprint"],
            row["raw_context"],
            row["fused_context"],
            tuple(embedding) if embedding else None,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            result.status, result.cache_hit, result.artifact = "hit", "memory", cached
            return result, row
        vector = (
            valid_vector(
                embedding["vector"],
                embedding["dimension"],
                getattr(self.encoder, "dimensions", None),
            )
            if embedding and embedding["format_version"] == 1
            else None
        )
        if vector is not None:
            result.status = "hit"
            result.cache_hit = "sqlite"
            result.artifact = ScenarioContext(row["raw_context"], row["fused_context"], vector)
            self.cache.put(cache_key, result.artifact)
        else:
            result.status = "stale"
        return result, row

    def read(self, refs):
        results = {}
        snapshot = self.repository.snapshot()
        for ref in dict.fromkeys(refs):
            plan = self._plan(ref, snapshot)
            with self.repository._read():
                results[ref] = (
                    self._read_plan(plan)[0]
                    if plan
                    else ContextReadResult(ref, False, "incomplete", missing_refs=(ref,))
                )
        return results

    def _current(self, plan):
        # Lifecycle transactions atomically maintain this exact lineage for every affected
        # target (including external dependents). It is a validation token, not a lease.
        connection = self.repository._connection
        state = connection.execute(
            "SELECT completeness,lineage FROM position_context_state WHERE scenario_id=? AND position_id=?",
            (plan.ref.scenario_id, plan.ref.position_id),
        ).fetchone()
        member = connection.execute(
            "SELECT endpoint_id FROM scenario_membership WHERE scenario_id=? AND position_id=?",
            (plan.ref.scenario_id, plan.ref.position_id),
        ).fetchone()
        if (
            state is None
            or member is None
            or not state["completeness"]
            or state["lineage"] != plan.lineage
            or member["endpoint_id"] != plan.endpoint
            or self.repository._generation(plan.ref.scenario_id, plan.ref.position_id)
            != plan.fact_revision
            or self.repository.context_builder is not self.builder
        ):
            return False
        try:
            resolver = getattr(self.builder.fusion, "get", None)
            fusion_fp = (
                resolver(plan.endpoint).fusion_fingerprint()
                if resolver
                else component_fingerprint(self.builder._resolve_fusion(plan.endpoint))
            )
            return (
                self.builder._resolve_fusion(plan.endpoint) is plan.fusion
                and fusion_fp == plan.fusion_fp
                and component_fingerprint(self.encoder) == plan.encoder_fp
            )
        except (ValueError, KeyError):
            return False

    def ensure_position_contexts(self, refs, *, reason):
        tick = time.perf_counter()
        results, plans = {}, []
        snapshot = self.repository.snapshot()
        for ref in sorted(set(refs)):
            try:
                plan = self._plan(ref, snapshot)
                if plan is None:
                    results[ref] = ContextReadResult(ref, False, "incomplete", missing_refs=(ref,))
                    continue
                with self.repository._read():
                    result, row = self._read_plan(plan)
                results[ref] = result
                if result.completeness and result.artifact is None and not result.stale_discard:
                    plans.append((plan, row))
            except Exception as error:  # noqa: BLE001 - isolate arbitrary business component failures
                resolution = resolve_context(ref, snapshot)
                results[ref] = ContextReadResult(
                    ref,
                    resolution.position_context_completeness,
                    "FAILED",
                    error=str(error),
                    lineage_fingerprint=lineage(ref, snapshot),
                    missing_refs=resolution.missing_refs,
                    incomplete_rounds=resolution.incomplete_rounds,
                )
        for ref, result in results.items():
            if result.status != "FAILED":
                continue
            with self.repository._write():
                current_snapshot = self.repository._snapshot_locked()
                if (
                    ref not in current_snapshot.facts
                    or lineage(ref, current_snapshot) != result.lineage_fingerprint
                    or self.repository.context_builder is not self.builder
                ):
                    result.stale_discard = True
                    continue
                try:
                    self._plan(ref, current_snapshot)
                except (ValueError, KeyError) as current_error:
                    # Configuration is still invalid for the same current inputs.
                    self.repository._connection.execute(
                        "UPDATE position_context_state SET status='FAILED',last_error=?,updated_at=unixepoch() "
                        "WHERE scenario_id=? AND position_id=? AND lineage=?",
                        (
                            str(current_error),
                            ref.scenario_id,
                            ref.position_id,
                            result.lineage_fingerprint,
                        ),
                    )
                else:
                    result.stale_discard = True
        artifact_load_ms = (time.perf_counter() - tick) * 1000
        # Submit all independent fusions before waiting. Encoding and SQL are on the caller.
        pending = []
        for plan, row in plans:
            result = results[plan.ref]
            if reason in {"lane_recall", "final_candidates", "query"}:
                logger.warning(
                    "Lazy Fuse/Embedding target=%s reason=%s lineage=%s fusion=%s",
                    plan.ref,
                    reason,
                    plan.lineage,
                    plan.fusion_fp,
                )
            try:
                if row:
                    value = row["fused_context"]
                elif not plan.resolution.ancestors:
                    value = plan.query
                else:
                    result.fuse_count = 1
                    value = self.resources.submit_component(
                        plan.fusion, plan.fusion.fuse, plan.inputs
                    )
                    if value is None:
                        value = plan.fusion.fuse(plan.inputs)
                pending.append((plan, value, None))
            except Exception as error:  # noqa: BLE001 - isolate arbitrary business component failures
                pending.append((plan, None, error))
        for plan, value, error in pending:
            result = results[plan.ref]
            try:
                if error:
                    raise error
                fused = value.result() if isinstance(value, Future) else value
                result.embedding_count = 1
                vector = self.resources.call_component(
                    self.encoder, self.encoder.encode_document, fused
                )
                if (
                    valid_vector(
                        json.dumps(vector), len(vector), getattr(self.encoder, "dimensions", None)
                    )
                    is None
                ):
                    raise ValueError("invalid context embedding")
                artifact_fp = digest(
                    [
                        plan.ref.scenario_id,
                        plan.ref.position_id,
                        plan.lineage,
                        plan.fusion_fp,
                        plan.raw,
                        fused,
                    ]
                )
                with self.repository._write():
                    if not self._current(plan):
                        result.status, result.stale_discard = "stale", True
                        continue
                    connection = self.repository._connection
                    connection.execute(
                        "INSERT OR IGNORE INTO position_context_artifacts(scenario_id,position_id,lineage,fusion,artifact_fingerprint,raw_context,fused_context) VALUES(?,?,?,?,?,?,?)",
                        (
                            plan.ref.scenario_id,
                            plan.ref.position_id,
                            plan.lineage,
                            plan.fusion_fp,
                            artifact_fp,
                            plan.raw,
                            fused,
                        ),
                    )
                    winner = connection.execute(
                        "SELECT artifact_fingerprint FROM position_context_artifacts WHERE scenario_id=? AND position_id=? AND lineage=? AND fusion=?",
                        (plan.ref.scenario_id, plan.ref.position_id, plan.lineage, plan.fusion_fp),
                    ).fetchone()[0]
                    if winner != artifact_fp:
                        # A concurrent nondeterministic output won. Never pair its text with our vector.
                        chosen, _ = self._read_plan(plan)
                        if chosen.artifact:
                            chosen.fuse_count = result.fuse_count
                            chosen.embedding_count = result.embedding_count
                            results[plan.ref] = chosen
                        else:
                            result.status, result.stale_discard = "stale", True
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO context_embeddings(artifact_fingerprint,encoder,vector,dimension,format_version) VALUES(?,?,?,?,1)",
                        (artifact_fp, plan.encoder_fp, json.dumps(vector), len(vector)),
                    )
                    connection.execute(
                        "UPDATE position_context_state SET status='READY',last_error=NULL,artifact_fingerprint=?,updated_at=unixepoch() WHERE scenario_id=? AND position_id=? AND lineage=?",
                        (artifact_fp, plan.ref.scenario_id, plan.ref.position_id, plan.lineage),
                    )
                    result.artifact = ScenarioContext(plan.raw, fused, vector)
                    result.status = "READY"
            except Exception as error:  # noqa: BLE001 - isolate arbitrary business component failures
                result.error = f"{type(error).__name__}: {error}"
                result.status = "FAILED"
                with self.repository._write():
                    if self._current(plan):
                        hit, _ = self._read_plan(plan)
                        if hit.artifact is None:
                            self.repository._connection.execute(
                                "UPDATE position_context_state SET status='FAILED',last_error=?,updated_at=unixepoch() WHERE scenario_id=? AND position_id=? AND lineage=?",
                                (
                                    result.error,
                                    plan.ref.scenario_id,
                                    plan.ref.position_id,
                                    plan.lineage,
                                ),
                            )
                        else:
                            hit.fuse_count = result.fuse_count
                            hit.embedding_count = result.embedding_count
                            results[plan.ref] = hit
                    else:
                        result.stale_discard = True
        return MaterializationBatchResult(results, artifact_load_ms)
