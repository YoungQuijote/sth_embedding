from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .context import ScenarioContextBuilder
from .domain import FeatureSet, InvokeAvailability, MockSample, RequestAffinityInfo, Scenario, ScenarioContext, ScenarioPosition


def compute_sample_hash(sample: MockSample) -> str:
    payload = {"endpoint_id": sample.endpoint_id.strip(), "mocked_query": sample.mocked_query.strip(), "mocked_answer": sample.mocked_answer.strip(), "sample_id": str(sample.sample_id).strip(), "round_id": str(sample.round_id).strip()}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SQLiteRepository:
    """SQLite fact store with endpoint tables hidden behind repository methods."""

    def __init__(self, path: str | Path, context_builder: ScenarioContextBuilder | None = None) -> None:
        self.path = str(path)
        self.context_builder = context_builder
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self._connection.close()

    def _init_schema(self) -> None:
        self._connection.executescript("""
        CREATE TABLE IF NOT EXISTS endpoints(endpoint_id TEXT PRIMARY KEY, table_name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS scenario_membership(
          scenario_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, sample_hash TEXT PRIMARY KEY,
          position_id INTEGER NOT NULL, affinity_json TEXT NOT NULL DEFAULT '[]');
        CREATE INDEX IF NOT EXISTS idx_membership_scenario ON scenario_membership(scenario_id);
        CREATE TABLE IF NOT EXISTS calibration_profiles(
          version TEXT PRIMARY KEY, profile_json TEXT NOT NULL, created_at REAL NOT NULL);
        """)
        self._connection.commit()

    def _table(self, endpoint_id: str, create: bool = False) -> str:
        row = self._connection.execute("SELECT table_name FROM endpoints WHERE endpoint_id=?", (endpoint_id,)).fetchone()
        if row:
            return str(row[0])
        if not create:
            return ""
        base = re.sub(r"[^a-zA-Z0-9_]", "_", endpoint_id).strip("_") or "root"
        suffix = hashlib.sha256(endpoint_id.encode()).hexdigest()[:10]
        table = f"samples_{base[:30]}_{suffix}"
        self._connection.execute("INSERT INTO endpoints VALUES(?, ?)", (endpoint_id, table))
        self._connection.execute(f'''CREATE TABLE "{table}"(
          sample_hash TEXT PRIMARY KEY, sample_id TEXT NOT NULL, round_id TEXT NOT NULL,
          position_id INTEGER NOT NULL, endpoint_id TEXT NOT NULL, mocked_query TEXT NOT NULL,
          mocked_answer TEXT NOT NULL, features TEXT NOT NULL, registry_times INTEGER NOT NULL,
          invoked_times INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(sample_id, position_id))''')
        return table

    def register(self, sample: MockSample, affinity: RequestAffinityInfo | None = None) -> MockSample:
        sample_hash = compute_sample_hash(sample)
        now = time.time()
        with self._lock, self._connection:
            table = self._table(sample.endpoint_id, create=True)
            existing_position = self._connection.execute(f'SELECT sample_hash FROM "{table}" WHERE sample_id=? AND position_id=?', (str(sample.sample_id), sample.position_id)).fetchone()
            if existing_position and existing_position[0] != sample_hash:
                raise ValueError("scenario_id + position_id must identify one stable sample")
            features = json.dumps({"hard": sample.features.hard_features, "soft": sample.features.soft_features}, ensure_ascii=False, default=str)
            self._connection.execute(f'''INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(sample_hash) DO UPDATE SET registry_times=registry_times+1, updated_at=excluded.updated_at''',
              (sample_hash, str(sample.sample_id), str(sample.round_id), sample.position_id, sample.endpoint_id, sample.mocked_query, sample.mocked_answer, features, 1, 0, now, now))
            affinities = [] if affinity is None else [affinity.__dict__ if hasattr(affinity, "__dict__") else {name: getattr(affinity, name) for name in affinity.__slots__}]
            self._connection.execute("INSERT INTO scenario_membership VALUES(?,?,?,?,?) ON CONFLICT(sample_hash) DO NOTHING", (str(sample.sample_id), sample.endpoint_id, sample_hash, sample.position_id, json.dumps(affinities)))
        result = self.get_by_hash(sample_hash)
        assert result is not None
        return result

    def _row_to_sample(self, row: sqlite3.Row) -> MockSample:
        features = json.loads(row["features"])
        return MockSample(row["endpoint_id"], row["mocked_query"], row["mocked_answer"], row["sample_id"], row["round_id"], row["position_id"], FeatureSet(features["hard"], features["soft"]), row["sample_hash"], InvokeAvailability(row["registry_times"], row["invoked_times"]))

    def list_endpoint(self, endpoint_id: str) -> list[MockSample]:
        table = self._table(endpoint_id)
        if not table:
            return []
        return [self._row_to_sample(row) for row in self._connection.execute(f'SELECT * FROM "{table}" ORDER BY sample_id, position_id')]

    def get_by_hash(self, sample_hash: str) -> MockSample | None:
        member = self._connection.execute("SELECT endpoint_id FROM scenario_membership WHERE sample_hash=?", (sample_hash,)).fetchone()
        if not member:
            return None
        table = self._table(member[0])
        row = self._connection.execute(f'SELECT * FROM "{table}" WHERE sample_hash=?', (sample_hash,)).fetchone()
        return self._row_to_sample(row) if row else None

    def increment_invoked(self, sample_hash: str) -> None:
        with self._lock, self._connection:
            member = self._connection.execute("SELECT endpoint_id FROM scenario_membership WHERE sample_hash=?", (sample_hash,)).fetchone()
            if not member:
                raise KeyError(sample_hash)
            table = self._table(member[0])
            cursor = self._connection.execute(f'UPDATE "{table}" SET invoked_times=invoked_times+1, updated_at=? WHERE sample_hash=?', (time.time(), sample_hash))
            if cursor.rowcount != 1:
                raise KeyError(sample_hash)

    def get(self, scenario_id: str) -> Scenario[Any, Any] | None:
        members = self._connection.execute("SELECT * FROM scenario_membership WHERE scenario_id=? ORDER BY position_id", (str(scenario_id),)).fetchall()
        if not members:
            return None
        positions = []
        affinities: list[RequestAffinityInfo] = []
        for member in members:
            sample = self.get_by_hash(member["sample_hash"])
            if sample is None:
                continue
            positions.append(ScenarioPosition(str(scenario_id), sample.position_id, sample, ScenarioContext(sample.mocked_query, sample.mocked_query), sample.features))
            affinities.extend(RequestAffinityInfo(**item) for item in json.loads(member["affinity_json"]))
        scenario = Scenario(str(scenario_id), positions, affinities)
        return self.context_builder.build(scenario) if self.context_builder else scenario

    def get_many(self, scenario_ids: Iterable[str]) -> list[Scenario[Any, Any]]:
        return [scenario for scenario_id in scenario_ids if (scenario := self.get(scenario_id)) is not None]
