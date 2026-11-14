"""Pure dependency resolution. No repository, model, or execution-pool dependencies."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .domain import DependencyResolution, PositionRef, ScenarioRound

RESOLUTION_VERSION = "position-dag-v1"


class DependencyError(ValueError):
    pass


class DependencyCycleError(DependencyError):
    pass


def normalize_round(value: str | int) -> str:
    try:
        return str(int(value))
    except (ValueError, TypeError) as error:
        raise ValueError("round_id must be integer-like") from error


def round_key(value: str | int) -> int:
    return int(normalize_round(value))


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PositionFact:
    ref: PositionRef
    endpoint_id: str
    round_id: str
    query: str
    answer: str
    sample_hash: str
    sources: tuple[PositionRef, ...] | None
    fact_revision: int = 0


@dataclass(frozen=True)
class GraphSnapshot:
    facts: Mapping[PositionRef, PositionFact]
    rounds: Mapping[tuple[str, str], ScenarioRound]

    def __post_init__(self):
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
        object.__setattr__(self, "rounds", MappingProxyType(dict(self.rounds)))


def resolve_context(target: PositionRef, snapshot: GraphSnapshot) -> DependencyResolution:
    edges: dict[PositionRef, tuple[PositionRef, ...]] = {}
    missing: set[PositionRef] = set()
    required_rounds: set[tuple[str, str]] = set()
    visiting: list[PositionRef] = []

    def visit(ref):
        if ref in visiting:
            path = visiting[visiting.index(ref) :] + [ref]
            raise DependencyCycleError(f"dependency cycle: {path}")
        if ref in edges:
            return
        fact = snapshot.facts.get(ref)
        if fact is None:
            missing.add(ref)
            edges[ref] = ()
            return
        visiting.append(ref)
        if fact.sources is None:
            rounds = {
                key
                for key in snapshot.rounds
                if key[0] == ref.scenario_id and round_key(key[1]) < round_key(fact.round_id)
            }
            required_rounds.update(rounds)
            sources = tuple(
                other.ref
                for other in snapshot.facts.values()
                if (other.ref.scenario_id, other.round_id) in rounds
            )
        else:
            sources = fact.sources
        for source in sources:
            source_fact = snapshot.facts.get(source)
            if source == ref:
                raise DependencyCycleError(f"self dependency: {ref}")
            if (
                source_fact
                and source.scenario_id == ref.scenario_id
                and (round_key(source_fact.round_id) >= round_key(fact.round_id))
            ):
                raise DependencyError(f"same/future round dependency: {ref} -> {source}")
            visit(source)
        visiting.pop()
        edges[ref] = tuple(set(sources))

    visit(target)
    target_fact = snapshot.facts.get(target)
    if target_fact:
        for ref in edges:
            fact = snapshot.facts.get(ref)
            if (
                ref != target
                and fact
                and ref.scenario_id == target.scenario_id
                and (round_key(fact.round_id) >= round_key(target_fact.round_id))
            ):
                raise DependencyError(f"indirect answer leakage: {target} -> {ref}")

    def order(ref):
        fact = snapshot.facts.get(ref)
        return (round_key(fact.round_id) if fact else -1, ref.scenario_id, ref.position_id)

    counts = {ref: len(sources) for ref, sources in edges.items()}
    reverse: dict[PositionRef, list[PositionRef]] = {}
    for ref, sources in edges.items():
        for source in sources:
            reverse.setdefault(source, []).append(ref)
    ready = [(order(ref), ref) for ref, count in counts.items() if not count]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, ref = heapq.heappop(ready)
        if ref != target:
            ordered.append(ref)
        for dependent in reverse.get(ref, []):
            counts[dependent] -= 1
            if counts[dependent] == 0:
                heapq.heappush(ready, (order(dependent), dependent))
    incomplete = tuple(
        sorted(
            key
            for key in required_rounds
            if snapshot.rounds[key].round_position_completeness != "SEALED"
        )
    )
    return DependencyResolution(
        target,
        tuple(ordered),
        tuple(sorted(missing)),
        incomplete,
        not missing and not incomplete,
        RESOLUTION_VERSION,
        tuple(sorted(required_rounds)),
    )


def validate_graph(snapshot: GraphSnapshot) -> None:
    for ref in snapshot.facts:
        resolve_context(ref, snapshot)


def lineage(target: PositionRef, snapshot: GraphSnapshot) -> str:
    resolution = resolve_context(target, snapshot)
    fact = snapshot.facts.get(target)
    return digest(
        {
            "version": RESOLUTION_VERSION,
            "target": (target.scenario_id, target.position_id),
            "query": fact.query if fact else None,
            "round": fact.round_id if fact else None,
            "declarations": [
                (
                    ref.scenario_id,
                    ref.position_id,
                    None
                    if snapshot.facts[ref].sources is None
                    else [(s.scenario_id, s.position_id) for s in snapshot.facts[ref].sources],
                )
                for ref in (target, *resolution.ancestors)
                if ref in snapshot.facts
            ],
            "qa": [
                (
                    ref.scenario_id,
                    ref.position_id,
                    snapshot.facts[ref].round_id,
                    snapshot.facts[ref].query,
                    snapshot.facts[ref].answer,
                )
                for ref in resolution.ancestors
                if ref in snapshot.facts
            ],
            "rounds": [
                (s, r, snapshot.rounds[s, r].round_position_completeness)
                for s, r in resolution.required_rounds
            ],
            "missing": [(r.scenario_id, r.position_id) for r in resolution.missing_refs],
        }
    )


def affected_positions(changes, before: GraphSnapshot, after: GraphSnapshot) -> set[PositionRef]:
    """Compare effective inputs on both graphs; counters/affinity are deliberately absent."""
    return {
        ref
        for ref in before.facts.keys() | after.facts.keys()
        if ref not in before.facts
        or ref not in after.facts
        or lineage(ref, before) != lineage(ref, after)
    }
