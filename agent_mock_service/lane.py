from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .domain import LaneRuntimeState, RuntimeInteraction, Scenario, ScenarioHypothesisState


def _round_number(value: str | int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("lane sliding windows require integer-like round_id values") from error


class LaneManager:
    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lanes: dict[str, LaneRuntimeState] = {}
        self._lock = threading.RLock()

    def active(self, now: float | None = None) -> list[LaneRuntimeState]:
        current = time.time() if now is None else now
        with self._lock:
            expired = [lane_id for lane_id, lane in self._lanes.items() if current - lane.last_active_at > self.ttl_seconds]
            for lane_id in expired:
                del self._lanes[lane_id]
            return list(self._lanes.values())

    def find_for_scenario(self, scenario_id: str) -> LaneRuntimeState | None:
        return next((lane for lane in self.active() if scenario_id in lane.scenario_hypotheses), None)

    def update(self, scenario: Scenario[Any, Any], interaction: RuntimeInteraction, now: float | None = None) -> LaneRuntimeState | None:
        rounds = sorted({_round_number(position.sample.round_id) for position in scenario.positions})
        matched_round = _round_number(interaction.round_id)
        # No lane for a single-round scenario or after the terminal round completes.
        if len(rounds) <= 1:
            return None
        current = time.time() if now is None else now
        with self._lock:
            lane = self.find_for_scenario(scenario.scenario_id)
            if lane is None:
                lane = LaneRuntimeState(f"lane_{uuid.uuid4().hex}", created_at=current, last_active_at=current)
                lane.scenario_hypotheses[scenario.scenario_id] = ScenarioHypothesisState(scenario.scenario_id)
                self._lanes[lane.lane_id] = lane
            hypothesis = lane.scenario_hypotheses[scenario.scenario_id]
            previous_min = min((_round_number(value) for value in lane.active_rounds), default=matched_round)
            hypothesis.matched_positions.append(interaction.position)
            lane.interactions.append(interaction)
            positions_in_previous = {position.position for position in scenario.positions if _round_number(position.sample.round_id) == previous_min}
            matched_in_previous = {item.position for item in lane.interactions if item.scenario_id == scenario.scenario_id and _round_number(item.round_id) == previous_min}
            base = matched_round if matched_round > previous_min or positions_in_previous <= matched_in_previous else previous_min
            if base >= rounds[-1] and positions_in_previous <= matched_in_previous:
                self._lanes.pop(lane.lane_id, None)
                return None
            lane.active_rounds = {round_value for round_value in rounds if base <= round_value <= base + 1}
            lane.last_active_at = current
            lane.state_version += 1
            return lane
