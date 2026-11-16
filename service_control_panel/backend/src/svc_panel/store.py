from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "panel.sqlite"
        key = self.directory / "credentials.key"
        try:
            with key.open("xb") as stream:
                os.chmod(key, 0o600)
                stream.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self.cipher = Fernet(key.read_bytes())
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS samples(instance_id TEXT, timestamp REAL, boot_id TEXT, value TEXT);
                CREATE INDEX IF NOT EXISTS samples_time ON samples(instance_id,timestamp);
                CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, instance_id TEXT, kind TEXT, message TEXT,
                    created_at REAL, resolved_at REAL, acknowledged INTEGER DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS active_alert ON alerts(instance_id,kind) WHERE resolved_at IS NULL;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind: str, value: dict):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                (kind, value["id"], json.dumps(value)),
            )
        return value

    def get(self, kind: str, identifier: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM objects WHERE kind=? AND id=?", (kind, identifier)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def patch(self, kind, identifier, **changes):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM objects WHERE kind=? AND id=?", (kind, identifier)
            ).fetchone()
            if row is None:
                raise ValueError("资源不存在")
            value = json.loads(row[0])
            value.update(changes)
            db.execute(
                "UPDATE objects SET value=? WHERE kind=? AND id=?",
                (json.dumps(value), kind, identifier),
            )
        return value

    def list(self, kind: str) -> list[dict]:
        with self.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT value FROM objects WHERE kind=? ORDER BY rowid DESC", (kind,)
                )
            ]

    def seal(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def unseal(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def alert(self, instance: str, kind: str, message: str):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO alerts(instance_id,kind,message,created_at) VALUES(?,?,?,?)",
                (instance, kind, message[:1000], time.time()),
            )

    def resolve(self, instance: str, kind: str):
        with self.connection() as db:
            db.execute(
                "UPDATE alerts SET resolved_at=? WHERE instance_id=? AND kind=? AND resolved_at IS NULL",
                (time.time(), instance, kind),
            )

    def alerts(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 300")
            ]
