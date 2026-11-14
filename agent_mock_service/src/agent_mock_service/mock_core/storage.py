"""Transactional lifecycle and derived-data storage shared by repository consumers."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps

from .dag import (
    GraphSnapshot,
    PositionFact,
    affected_positions,
    lineage,
    normalize_round,
    resolve_context,
    validate_graph,
)
from .domain import PositionContextState, PositionRef, ScenarioRound

SCHEMA_VERSION = "4-position-context-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS position_generations(
 scenario_id TEXT NOT NULL, position_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(scenario_id,position_id));
CREATE TABLE IF NOT EXISTS scenario_rounds(
 scenario_id TEXT NOT NULL, round_id TEXT NOT NULL,
 completeness TEXT NOT NULL CHECK(completeness IN ('OPEN','SEALED')),
 expected_position_count INTEGER CHECK(expected_position_count >= 0), revision INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(scenario_id,round_id));
CREATE TABLE IF NOT EXISTS position_context_declarations(
 scenario_id TEXT NOT NULL, position_id INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('DEFAULT','EXPLICIT')), version TEXT NOT NULL,
 updated_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(scenario_id,position_id));
CREATE TABLE IF NOT EXISTS position_context_sources(
 target_scenario TEXT NOT NULL, target_position INTEGER NOT NULL,
 source_scenario TEXT NOT NULL, source_position INTEGER NOT NULL, source_order INTEGER NOT NULL,
 PRIMARY KEY(target_scenario,target_position,source_scenario,source_position));
CREATE INDEX IF NOT EXISTS idx_context_source ON position_context_sources(source_scenario,source_position);
CREATE TABLE IF NOT EXISTS position_context_state(
 scenario_id TEXT NOT NULL, position_id INTEGER NOT NULL, completeness INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('MISSING','READY','STALE','FAILED')),
 lineage TEXT NOT NULL, last_error TEXT, artifact_fingerprint TEXT,
 updated_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(scenario_id,position_id));
CREATE TABLE IF NOT EXISTS position_context_artifacts(
 scenario_id TEXT NOT NULL, position_id INTEGER NOT NULL, lineage TEXT NOT NULL,
 fusion TEXT NOT NULL, artifact_fingerprint TEXT NOT NULL UNIQUE,
 raw_context TEXT NOT NULL, fused_context TEXT NOT NULL,
 created_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(scenario_id,position_id,lineage,fusion));
CREATE TABLE IF NOT EXISTS context_embeddings(
 artifact_fingerprint TEXT NOT NULL, encoder TEXT NOT NULL, vector TEXT NOT NULL,
 dimension INTEGER NOT NULL CHECK(dimension > 0), format_version INTEGER NOT NULL,
 created_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(artifact_fingerprint,encoder));
CREATE TABLE IF NOT EXISTS sample_embeddings(
 sample_hash TEXT NOT NULL, encoder TEXT NOT NULL, vector TEXT NOT NULL,
 dimension INTEGER NOT NULL CHECK(dimension > 0), format_version INTEGER NOT NULL,
 created_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(sample_hash,encoder));
CREATE TABLE IF NOT EXISTS sample_features(
 sample_hash TEXT NOT NULL, extractor TEXT NOT NULL, features TEXT NOT NULL,
 input_fingerprint TEXT NOT NULL, created_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(sample_hash,extractor));
"""


class RoundSealedError(ValueError):
    pass


class RoundCountError(ValueError):
    pass


class PositionConflictError(ValueError):
    pass


def consistent_read(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._read():
            return fn(self, *args, **kwargs)

    return wrapped


class LifecycleStore:
    @contextmanager
    def _write(self):
        with self._lock:
            outer = not self._connection.in_transaction
            if outer:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                if outer:
                    self._connection.commit()
            except BaseException:
                if outer:
                    self._connection.rollback()
                raise

    @contextmanager
    def _read(self):
        with self._lock:
            outer = not self._connection.in_transaction
            if outer:
                self._connection.execute("BEGIN")
            try:
                yield
            finally:
                if outer:
                    self._connection.rollback()

    def snapshot(self):
        with self._read():
            return self._snapshot_locked()

    def _snapshot_locked(self):
        facts = {}
        for sample in self.list_all():
            ref = PositionRef(str(sample.sample_id), sample.position_id)
            facts[ref] = PositionFact(
                ref,
                sample.endpoint_id,
                normalize_round(sample.round_id),
                sample.mocked_query,
                sample.mocked_answer,
                sample.sample_hash,
                None
                if sample.context_source_positions is None
                else tuple(sample.context_source_positions),
                sample.fact_revision,
            )
        rounds = {
            (row["scenario_id"], row["round_id"]): ScenarioRound(
                row["scenario_id"],
                row["round_id"],
                row["completeness"],
                row["expected_position_count"],
                row["revision"],
            )
            for row in self._connection.execute("SELECT * FROM scenario_rounds")
        }
        return GraphSnapshot(facts, rounds)

    def _generation(self, scenario_id, position_id):
        row = self._connection.execute(
            "SELECT revision FROM position_generations WHERE scenario_id=? AND position_id=?",
            (scenario_id, position_id),
        ).fetchone()
        return row[0] if row else 0

    def _sources(self, scenario_id, position_id):
        row = self._connection.execute(
            "SELECT mode FROM position_context_declarations WHERE scenario_id=? AND position_id=?",
            (scenario_id, position_id),
        ).fetchone()
        if row is None or row[0] == "DEFAULT":
            return None
        return [
            PositionRef(r[0], r[1])
            for r in self._connection.execute(
                "SELECT source_scenario,source_position FROM position_context_sources "
                "WHERE target_scenario=? AND target_position=? ORDER BY source_order",
                (scenario_id, position_id),
            )
        ]

    def _synchronize(self, before):
        after = self._snapshot_locked()
        validate_graph(after)
        affected = affected_positions(None, before, after)
        for ref in affected:
            if ref not in after.facts:
                self._connection.execute(
                    "DELETE FROM position_context_state WHERE scenario_id=? AND position_id=?",
                    (ref.scenario_id, ref.position_id),
                )
                continue
            resolution = resolve_context(ref, after)
            key = lineage(ref, after)
            self._connection.execute(
                """INSERT INTO position_context_state(scenario_id,position_id,completeness,status,lineage,last_error,artifact_fingerprint)
                VALUES(?,?,?,'MISSING',?,NULL,NULL) ON CONFLICT(scenario_id,position_id)
                DO UPDATE SET completeness=excluded.completeness,status='STALE',
                lineage=excluded.lineage,last_error=NULL,artifact_fingerprint=NULL,updated_at=unixepoch()""",
                (
                    ref.scenario_id,
                    ref.position_id,
                    int(resolution.position_context_completeness),
                    key,
                ),
            )
        return affected

    def register(
        self,
        sample,
        affinity=None,
        *,
        round_position_completeness=False,
        force_registry=False,
        extractor_fingerprint=None,
    ):
        from .repository import compute_sample_hash

        sample = replace(
            sample, round_id=normalize_round(sample.round_id), sample_id=str(sample.sample_id)
        )
        sources = sample.context_source_positions
        if sources is not None:
            sample = replace(sample, context_source_positions=list(dict.fromkeys(sources)))
        ref = PositionRef(sample.sample_id, sample.position_id)
        with self._write():
            before = self._snapshot_locked()
            old = before.facts.get(ref)
            changed = old is not None and (
                old.sample_hash != compute_sample_hash(sample)
                or old.sources
                != (None if sources is None else tuple(sample.context_source_positions))
            )
            round_state = before.rounds.get((sample.sample_id, sample.round_id))
            if changed and not force_registry:
                raise PositionConflictError(
                    "scenario_id + position_id must uniquely identify one ScenarioPosition; use force_registry for replacement"
                )
            if (
                old is None
                and round_state
                and round_state.round_position_completeness == "SEALED"
                and not force_registry
            ):
                raise RoundSealedError("Round is SEALED; new Position requires force_registry")
            if changed:
                self._delete_locked(old.sample_hash)
            self._connection.execute(
                "INSERT OR IGNORE INTO scenario_rounds(scenario_id,round_id,completeness,expected_position_count,revision) VALUES(?,?,'OPEN',NULL,0)",
                (sample.sample_id, sample.round_id),
            )
            if changed or old is None:
                self._connection.execute(
                    "INSERT INTO position_generations VALUES(?,?,1) ON CONFLICT(scenario_id,position_id) "
                    "DO UPDATE SET revision=revision+1",
                    (sample.sample_id, sample.position_id),
                )
                self._connection.execute(
                    "UPDATE scenario_rounds SET completeness='OPEN', revision=revision+1,updated_at=unixepoch() WHERE scenario_id=? AND round_id=?",
                    (sample.sample_id, sample.round_id),
                )
                self._connection.execute(
                    "INSERT OR REPLACE INTO position_context_declarations(scenario_id,position_id,mode,version) VALUES(?,?,?,?)",
                    (
                        sample.sample_id,
                        sample.position_id,
                        "DEFAULT" if sources is None else "EXPLICIT",
                        "v1",
                    ),
                )
                self._connection.execute(
                    "DELETE FROM position_context_sources WHERE target_scenario=? AND target_position=?",
                    (sample.sample_id, sample.position_id),
                )
                for index, source in enumerate(sample.context_source_positions or []):
                    self._connection.execute(
                        "INSERT INTO position_context_sources VALUES(?,?,?,?,?)",
                        (
                            sample.sample_id,
                            sample.position_id,
                            source.scenario_id,
                            source.position_id,
                            index,
                        ),
                    )
            result = self._register_fact(sample, affinity)
            if extractor_fingerprint:
                self._save_feature_locked(result, extractor_fingerprint, sample.features)
            if round_position_completeness:
                self._finalize_locked(sample.sample_id, sample.round_id, None)
            affected = self._synchronize(before)
        self._materialize_after(affected | {ref})
        return result

    def _finalize_locked(self, scenario_id, round_id, expected):
        row = self._connection.execute(
            "SELECT * FROM scenario_rounds WHERE scenario_id=? AND round_id=?",
            (scenario_id, round_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Round: {scenario_id}/{round_id}")
        snapshot = self._snapshot_locked()
        count = sum(
            f.ref.scenario_id == scenario_id and f.round_id == round_id
            for f in snapshot.facts.values()
        )
        expected = row["expected_position_count"] if expected is None else expected
        if expected is not None and (expected < 0 or expected != count):
            raise RoundCountError(f"expected_position_count={expected}, actual={count}")
        if (
            row["completeness"] == "SEALED"
            and row["expected_position_count"] is not None
            and expected != row["expected_position_count"]
        ):
            raise RoundCountError("contradictory sealed Round count")
        if row["completeness"] == "SEALED" and row["expected_position_count"] == expected:
            return
        self._connection.execute(
            "UPDATE scenario_rounds SET completeness='SEALED',expected_position_count=?,revision=revision+1,updated_at=unixepoch() WHERE scenario_id=? AND round_id=?",
            (expected, scenario_id, round_id),
        )

    def finalize_round(self, scenario_id, round_id, *, expected_position_count=None):
        scenario_id, round_id = str(scenario_id), normalize_round(round_id)
        with self._write():
            before = self._snapshot_locked()
            self._finalize_locked(scenario_id, round_id, expected_position_count)
            affected = self._synchronize(before)
            result = self._snapshot_locked().rounds[scenario_id, round_id]
        self._materialize_after(affected)
        return result

    def _after_delete_locked(self, sample):
        self._connection.execute(
            "UPDATE scenario_rounds SET completeness='OPEN',revision=revision+1,updated_at=unixepoch() WHERE scenario_id=? AND round_id=?",
            (sample.sample_id, normalize_round(sample.round_id)),
        )
        # Only outgoing declarations belong to the removed target. Incoming edges survive.
        self._connection.execute(
            "DELETE FROM position_context_declarations WHERE scenario_id=? AND position_id=?",
            (sample.sample_id, sample.position_id),
        )
        self._connection.execute(
            "DELETE FROM position_context_sources WHERE target_scenario=? AND target_position=?",
            (sample.sample_id, sample.position_id),
        )

    def _mutate_delete(self, method, *args, **kwargs):
        with self._write():
            before = self._snapshot_locked()
            result = method(*args, **kwargs)
            affected = self._synchronize(before)
        self._materialize_after(affected)
        return result

    def unregister(self, sample_hash, affinity=None):
        return self._mutate_delete(self._unregister_fact, sample_hash, affinity)

    def delete(self, sample_hash):
        return self._mutate_delete(self._delete_fact, sample_hash)

    def delete_scenario(self, scenario_id, *, force=False, affinity=None):
        return self._mutate_delete(
            self._delete_scenario_facts, scenario_id, force=force, affinity=affinity
        )

    def _materialize_after(self, refs):
        materializer = getattr(self, "materializer", None)
        callback = getattr(self, "registration_callback", None)
        self.last_post_commit_error = None
        self.last_materialization = None
        try:
            if callback and refs:
                callback(refs)
            if materializer and refs:
                self.last_materialization = materializer.ensure_position_contexts(
                    refs, reason="registration"
                )
        except Exception as error:
            # Facts already committed. Surface infrastructure failure separately; never invite
            # an implicit registration retry which would double registry_times.
            self.last_post_commit_error = f"{type(error).__name__}: {error}"
            logging.getLogger(__name__).exception(
                "Committed facts; post-commit materialization failed"
            )

    def rebuild_position_contexts(self, refs):
        if not getattr(self, "materializer", None):
            raise RuntimeError("bind runtime encoder/plugins before materialization")
        return self.materializer.ensure_position_contexts(refs, reason="rebuild")

    def get_position_contexts(self, refs):
        if not getattr(self, "materializer", None):
            from .domain import ContextReadResult

            snapshot = self.snapshot()
            return {
                ref: ContextReadResult(
                    ref,
                    resolve_context(ref, snapshot).position_context_completeness,
                    "miss",
                    error="runtime not bound",
                )
                for ref in refs
            }
        return self.materializer.read(refs)

    def claim_selected_sample(self, sample_hash, expected_revision=None):
        """Linearize final identity validation and invocation count in one short transaction."""
        with self._write():
            sample = self.get_by_hash(sample_hash)
            if sample is None or (
                expected_revision is not None and sample.fact_revision != expected_revision
            ):
                raise KeyError(f"stale selected sample: {sample_hash}")
            self.increment_invoked(sample_hash)
            return sample

    def get_position_state(self, ref):
        with self._read():
            row = self._connection.execute(
                "SELECT * FROM position_context_state WHERE scenario_id=? AND position_id=?",
                (ref.scenario_id, ref.position_id),
            ).fetchone()
            if row is None:
                return None
            status, error = row["status"], row["last_error"]
            if getattr(self, "materializer", None):
                try:
                    current = self.materializer.read([ref])[ref]
                except (ValueError, KeyError) as config_error:
                    status, error = "FAILED", str(config_error)
                else:
                    if current.artifact is not None:
                        status, error = "READY", None
                    elif status == "READY":
                        status = "STALE"
            return PositionContextState(
                ref, bool(row["completeness"]), status, row["lineage"], error
            )

    def _save_feature_locked(self, sample, fingerprint, features):
        from .dag import digest

        self._connection.execute(
            "INSERT OR IGNORE INTO sample_features(sample_hash,extractor,features,input_fingerprint) VALUES(?,?,?,?)",
            (
                sample.sample_hash,
                fingerprint,
                json.dumps({"hard": features.hard_features, "soft": features.soft_features}),
                digest(sample.mocked_query),
            ),
        )
