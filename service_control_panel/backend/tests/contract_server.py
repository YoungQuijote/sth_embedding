"""Test-only panel.v1 implementation. Never mounted in the production app."""

import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

MOCK_TOKEN = "contract-mock-token-123456789"
CAPABILITIES = ["metrics", "requests", "traces", "logs", "database", "exports"]


class QueryBody(BaseModel):
    query: str
    topK: int = 50


def make_contract_app():
    app = FastAPI()
    app.state.requests = 0

    @app.middleware("http")
    async def check_auth(request: Request, call_next):
        if (
            request.url.path != "/health"
            and request.headers.get("authorization") != "Bearer " + MOCK_TOKEN
        ):
            return Response(status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        return {
            "status": "ready",
            "instance_id": "contract-instance",
            "boot_id": "boot-test",
            "started_at": "2026-11-14T00:00:00Z",
        }

    @app.get("/management/v1/info")
    def info():
        return {
            "contract_version": "panel.v1",
            "instance_id": "contract-instance",
            "boot_id": "boot-test",
            "capabilities": CAPABILITIES,
        }

    @app.get("/management/v1/endpoints")
    def endpoints():
        return {
            "items": [
                {
                    "id": "weather",
                    "path": "/weather",
                    "method": "POST",
                    "category": "business",
                    "summary": "天气查询",
                },
                {
                    "id": "bm25",
                    "path": "/debug/bm25",
                    "method": "POST",
                    "category": "debug",
                    "summary": "BM25 检索调试",
                },
                {
                    "id": "stream",
                    "path": "/stream",
                    "method": "POST",
                    "category": "business",
                    "summary": "SSE 流式查询",
                },
            ]
        }

    @app.post("/weather")
    @app.post("/debug/bm25")
    def query(body: QueryBody, request: Request):
        app.state.requests += 1
        return {
            "query": body.query,
            "topK": body.topK,
            "origin": request.headers.get("x-panel-request-origin"),
            "answer": "晴",
        }

    @app.post("/stream")
    def stream():
        return StreamingResponse(
            iter([b'data: {"answer":"hello"}\n\n', b'data: {"done":true}\n\n']),
            media_type="text/event-stream",
            headers={"X-Request-ID": "stream-1"},
        )

    @app.get("/management/v1/metrics")
    def metrics():
        return {
            "instance_id": "contract-instance",
            "boot_id": "boot-test",
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "counters": [
                {
                    "endpoint": "/weather",
                    "method": "POST",
                    "category": "business",
                    "status_code": 200,
                    "requests_total": app.state.requests,
                    "duration_seconds_sum": app.state.requests * 0.01,
                }
            ],
        }

    @app.get("/management/v1/logs")
    def logs(limit: int = 100, cursor: str | None = None, level: str | None = None):
        return {
            "items": [
                {
                    "timestamp": "2026-11-14T00:00:00Z",
                    "level": "INFO",
                    "message": "Contract fixture is ready",
                    "request_id": None,
                }
            ],
            "next_cursor": "page-2" if not cursor else None,
        }

    @app.get("/management/v1/database/tables")
    def tables():
        return {"items": [{"name": "samples", "columns": [{"name": "query", "type": "TEXT"}]}]}

    @app.get("/management/v1/database/tables/{table}/rows")
    def rows(table: str, offset: int = 0, limit: int = 50):
        if table != "samples":
            raise HTTPException(404)
        return {
            "table": table,
            "columns": ["query"],
            "rows": [{"query": "天气如何"}],
            "next_offset": None,
        }

    @app.get("/management/v1/requests")
    def requests(endpoint: str | None = None, method: str | None = None, non_200: bool = False):
        return {
            "items": [
                {
                    "request_id": "r1",
                    "timestamp": "2026-11-14T00:00:00Z",
                    "endpoint": endpoint or "/weather",
                    "method": method or "POST",
                    "status_code": 500,
                    "duration_ms": 12,
                    "category": "business",
                    "trace_id": "t1",
                    "error": "Fixture failure",
                }
            ],
            "next_cursor": None,
        }

    @app.get("/management/v1/requests/{request_id}")
    def request_detail(request_id: str):
        return {
            "request_id": request_id,
            "body": {"query": "weather"},
            "status_code": 500,
            "trace_id": "t1",
            "headers": {"authorization": "[REDACTED]"},
        }

    @app.get("/management/v1/traces/{trace_id}")
    def trace(trace_id: str):
        return {"trace_id": trace_id, "request_id": "r1", "event": {"error": "fixture"}}

    @app.post("/management/v1/exports", status_code=202)
    def export(body: dict):
        return {
            "export_id": body["kind"],
            "kind": body["kind"],
            "status": "pending",
            "filename": None,
            "size_bytes": None,
            "error": None,
        }

    def exported(kind):
        if kind == "database":
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE samples(query TEXT)")
            db.execute("INSERT INTO samples VALUES('weather')")
            data = db.serialize()
            db.close()
            return data, "snapshot.sqlite"
        return b'{"request_id":"r1","message":"fixture"}\n', kind + ".jsonl"

    @app.get("/management/v1/exports/{export_id}")
    def export_status(export_id: str):
        data, filename = exported(export_id)
        return {
            "export_id": export_id,
            "kind": export_id,
            "status": "ready",
            "filename": filename,
            "size_bytes": len(data),
            "error": None,
        }

    @app.get("/management/v1/exports/{export_id}/download")
    def export_download(export_id: str):
        data, filename = exported(export_id)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


app = make_contract_app()
