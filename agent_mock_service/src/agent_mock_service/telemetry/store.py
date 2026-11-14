"""Durable bounded telemetry queries and controlled snapshot exports."""

import base64
import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

request_context = contextvars.ContextVar("telemetry_request", default=None)


def utc():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, data, logs, traces):
        self.lock = threading.RLock()
        self.logs, self.traces = Path(logs), Path(traces)
        self.exports = Path(data) / "exports"
        self.exports.mkdir(exist_ok=True)
        self.db_path = Path(data) / "telemetry.sqlite"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, kind TEXT, id TEXT, payload TEXT)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS events_kind ON events(kind, seq)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_identity ON events(kind,id,seq)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS events_request ON events(kind,json_extract(payload,'$.request_id'),seq)"
        )
        self.db.commit()
        self.counters = {}
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mock-export")
        self.body_limit = max(0, int(os.getenv("MOCK_TELEMETRY_BODY_LIMIT", "8192")))
        self.hidden = {
            x.strip().lower()
            for x in os.getenv(
                "MOCK_REDACT_FIELDS",
                "password,token,secret,authorization,cookie,api_key,api-key,set-cookie",
            ).split(",")
        } | {
            "authorization",
            "cookie",
            "set-cookie",
            "api-key",
            "api_key",
            "apikey",
            "x-api-key",
            "proxy-authorization",
        }

    def redact(self, value):
        if isinstance(value, dict):
            return {
                k: "[REDACTED]" if any(s in k.lower() for s in self.hidden if s) else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            credential = os.getenv("MOCK_ADMIN_TOKEN", "")
            if credential:
                return value.replace(credential, "[REDACTED]")
        return value

    def body(self, raw):
        if not raw:
            return None, False
        if len(raw) > self.body_limit:
            return "[BODY OMITTED: SIZE LIMIT]", True
        try:
            return self.redact(json.loads(raw)), False
        except (ValueError, UnicodeDecodeError):
            return "[NON-JSON BODY OMITTED]", False

    def add(self, kind, identifier, payload):
        payload = self.redact(payload)
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        with self.lock:
            self.db.execute(
                "INSERT INTO events(kind,id,payload) VALUES(?,?,?)", (kind, identifier, encoded)
            )
            self.db.commit()
            if kind in ("logs", "traces"):
                target = self.logs if kind == "logs" else self.traces
                with (target / (kind + ".jsonl")).open("a", encoding="utf-8") as output:
                    output.write(encoded + "\n")

    def write(self, event):
        # Runtime generates an internal request ID; upstream ID is the HTTP correlation ID.
        event = dict(event)
        identifier = uuid.uuid4().hex
        correlation = request_context.get() or event.get("upstream_request_id")
        try:
            self.add(
                "traces",
                identifier,
                {
                    "trace_id": identifier,
                    "request_id": correlation,
                    "timestamp": utc(),
                    "endpoint": event.get("endpoint_id"),
                    "event": event,
                },
            )
        except Exception:  # noqa: BLE001 - optional telemetry and background jobs must isolate failures
            logging.getLogger(__name__).error("Runtime trace persistence failed")

    def get(self, kind, identifier):
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM events WHERE kind=? AND id=? ORDER BY seq DESC LIMIT 1",
                (kind, identifier),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def page(self, kind, limit, cursor=None, filters=None):
        before = 9223372036854775807
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded[0] != kind or not isinstance(decoded[1], int) or decoded[1] < 1:
                    raise ValueError()
                before = decoded[1]
            except Exception as exc:
                raise ValueError("Invalid cursor") from exc
        clauses, args = ["kind=?", "seq<?"], [kind, before]
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key == "non_200":
                if value:
                    clauses.append("json_extract(payload,'$.status_code') != 200")
            elif key in ("since", "until"):
                clauses.append(
                    "json_extract(payload,'$.timestamp') "
                    + (">=" if key == "since" else "<=")
                    + " ?"
                )
                args.append(value)
            else:
                clauses.append(f"json_extract(payload,'$.{key}') = ?")
                args.append(value)
        with self.lock:
            rows = self.db.execute(
                "SELECT seq,payload FROM events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY seq DESC LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
        return {
            "items": [json.loads(r[1]) for r in rows[:limit]],
            "next_cursor": base64.urlsafe_b64encode(
                json.dumps([kind, rows[limit - 1][0]]).encode()
            ).decode()
            if len(rows) > limit
            else None,
        }

    def expire(self):
        cutoff = time.time() - max(1, int(os.getenv("MOCK_EXPORT_TTL_SECONDS", "86400")))
        for path in self.exports.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        for key, job in list(self.jobs.items()):
            if job["created"] < cutoff:
                self.jobs.pop(key, None)

    def export(self, kind, repository_path):
        with self.lock:
            self.expire()
            identifier = uuid.uuid4().hex
            self.jobs[identifier] = {
                "export_id": identifier,
                "kind": kind,
                "status": "pending",
                "filename": None,
                "size_bytes": None,
                "error": None,
                "created": time.time(),
            }
            initial = self.job(identifier)
            self.pool.submit(self._export, identifier, repository_path)
            return initial

    def job(self, identifier):
        with self.lock:
            self.expire()
            value = self.jobs.get(identifier)
            return {k: v for k, v in value.items() if k != "created"} if value else None

    def _export(self, identifier, repository_path):
        job = self.jobs[identifier]
        job["status"] = "running"
        path = self.exports / (
            job["kind"] + "-" + identifier + (".sqlite" if job["kind"] == "database" else ".zip")
        )
        try:
            if job["kind"] == "database":
                source = sqlite3.connect(repository_path)
                target = sqlite3.connect(path)
                try:
                    source.backup(target, pages=128, sleep=0.01)
                finally:
                    target.close()
                    source.close()
            else:
                # Freeze the append-only event sequence, then stream through an independent
                # WAL reader. Compression never holds the request writer's lock.
                with self.lock:
                    cutoff = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[
                        0
                    ]
                source = sqlite3.connect(self.db_path)
                try:
                    with (
                        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive,
                        archive.open(job["kind"] + ".jsonl", "w") as output,
                    ):
                        for row in source.execute(
                            "SELECT payload FROM events WHERE kind=? AND seq<=? ORDER BY seq",
                            (job["kind"], cutoff),
                        ):
                            output.write((row[0] + "\n").encode())
                finally:
                    source.close()
            job.update(status="ready", filename=path.name, size_bytes=path.stat().st_size)
        except Exception:  # noqa: BLE001 - optional telemetry and background jobs must isolate failures
            job.update(
                status="failed", error="Snapshot export failed; check service storage availability"
            )
            path.unlink(missing_ok=True)

    def close(self):
        self.pool.shutdown(wait=True)
        self.db.close()


class ServiceLogHandler(logging.Handler):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def emit(self, record):
        if not record.name.startswith("agent_mock_service"):
            return
        try:
            self.store.add(
                "logs",
                uuid.uuid4().hex,
                {
                    "timestamp": utc(),
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "request_id": request_context.get(),
                },
            )
        except Exception:  # noqa: BLE001 - optional telemetry and background jobs must isolate failures
            return  # Logging failures must not recurse into this handler.
