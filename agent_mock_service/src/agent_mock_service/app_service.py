"""Panel V1 ASGI entrypoint. Install with pip install '.[api]'."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .mock_core.bm25 import DefaultTokenizer, EndpointBM25Index
from .mock_core.defaults import HashingEncoder
from .mock_core.domain import FeatureSet, MockRequest, MockSample, ResponseProtocol
from .mock_core.repository import SQLiteRepository
from .mock_core.runtime import AgentMockRuntime
from .telemetry import schemas
from .telemetry.http import ObservationMiddleware
from .telemetry.store import ServiceLogHandler, Store, utc


class QueryBody(BaseModel):
    query: str = Field(examples=["天气如何"])


class Registration(BaseModel):
    endpoint_id: str = Field(
        pattern=r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$", examples=["/weather"]
    )
    mocked_query: str
    mocked_answer: str
    sample_id: str
    round_id: str | int = 1
    position_id: int = Field(default=1, ge=0)
    round_position_completeness: bool = True

    @field_validator("endpoint_id")
    @classmethod
    def available_path(cls, value):
        if value.split("/")[1] in {
            "management",
            "debug",
            "registry",
            "health",
            "ready",
            "docs",
            "redoc",
        }:
            raise ValueError("Reserved service path")
        return value


class BM25Body(QueryBody):
    topK: int = Field(default=50, ge=1, le=200)
    endpoint: str | None = None


class EmbeddingBody(QueryBody):
    candidates: list[str] = Field(max_length=200)


class ExportBody(BaseModel):
    kind: Literal["database", "logs", "traces"]


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        state = app.state
        state.ready = False
        data = Path(os.getenv("MOCK_DATA_DIR", "./data"))
        logs = Path(os.getenv("MOCK_LOG_DIR", "./logs"))
        traces = Path(os.getenv("MOCK_TRACE_DIR", "./traces"))
        for path in (data, logs, traces):
            path.mkdir(parents=True, exist_ok=True)
        state.admin_token = os.getenv("MOCK_ADMIN_TOKEN", "")
        identity = data / "instance_id"
        if not identity.exists():
            identity.write_text(os.getenv("MOCK_INSTANCE_ID") or str(uuid4()))
        state.instance_id = identity.read_text().strip()
        configured = os.getenv("MOCK_INSTANCE_ID")
        if configured and configured != state.instance_id:
            raise RuntimeError("MOCK_INSTANCE_ID differs from persisted identity")
        state.boot_id, state.started_at = str(uuid4()), utc()
        state.telemetry = Store(data, logs, traces)
        handler = ServiceLogHandler(state.telemetry)
        logging.getLogger().addHandler(handler)
        state.repository = None
        state.runtime = None
        try:
            state.repository = SQLiteRepository(data / "mock.sqlite")
            state.runtime = AgentMockRuntime(
                state.repository, encoder=HashingEncoder(), trace=state.telemetry
            )
            state.business_paths = {s.endpoint_id for s in state.repository.list_all()}
            for path in sorted(state.business_paths):
                add_business(app, path)
            state.ready = True
            state.telemetry.add(
                "logs",
                uuid4().hex,
                {
                    "timestamp": utc(),
                    "level": "INFO",
                    "message": "Service ready",
                    "request_id": None,
                },
            )
            yield
        finally:
            state.ready = False
            if state.runtime:
                state.runtime.execution_resources.shutdown()
            if state.repository:
                state.repository.close()
            logging.getLogger().removeHandler(handler)
            state.telemetry.close()

    app = FastAPI(
        title="Agent Mock Service",
        version="panel.v1",
        lifespan=lifespan,
        responses={code: {"model": schemas.Error} for code in (401, 404, 409, 422, 500, 503)},
    )
    app.add_middleware(ObservationMiddleware, state=app.state)

    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(RequestValidationError)
    async def error(request, exc):
        status = 422 if isinstance(exc, RequestValidationError) else exc.status_code
        message = "Request validation failed" if status == 422 else str(exc.detail)
        return JSONResponse(
            {
                "error": {"code": f"http_{status}", "message": message},
                "request_id": request.state.request_id,
            },
            status_code=status,
        )

    @app.get("/health", response_model=schemas.Health)
    def health():
        return {"status": "ok"}

    @app.get("/ready", response_model=schemas.Ready)
    def ready():
        if not getattr(app.state, "ready", False):
            return JSONResponse(
                {"status": "not_ready", "reason": "Repository is not ready"}, status_code=503
            )
        try:
            with app.state.repository._lock:
                app.state.repository._connection.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001 - readiness must describe unavailable dependencies
            return JSONResponse(
                {"status": "not_ready", "reason": "Repository is unavailable"}, status_code=503
            )
        return {"status": "ready", **identity()}

    def identity():
        return {k: getattr(app.state, k) for k in ("instance_id", "boot_id", "started_at")}

    @app.get("/management/v1/info", response_model=schemas.Info)
    def info():
        return {
            "contract_version": "panel.v1",
            **identity(),
            "capabilities": ["metrics", "requests", "traces", "logs", "database", "exports"],
        }

    @app.get("/management/v1/endpoints", response_model=schemas.Endpoints)
    def endpoints():
        items = []
        for route in app.routes:
            path = getattr(route, "path", "")
            if (
                not getattr(route, "include_in_schema", False)
                or "{" in path
                and path == "/{endpoint_path:path}"
            ):
                continue
            category = (
                "management"
                if path.startswith("/management/") or path in ("/health", "/ready")
                else "debug"
                if path.startswith("/debug/")
                else "registry"
                if path == "/registry"
                else "business"
            )
            for method in sorted(route.methods or []):
                items.append(
                    {
                        "id": route.name,
                        "path": path,
                        "method": method,
                        "category": category,
                        "summary": route.summary or route.name,
                        "operation_id": route.operation_id or route.unique_id,
                    }
                )
        return {"items": items}

    @app.post("/registry", response_model=schemas.Registered)
    def registry(body: Registration):
        sample = MockSample(
            body.endpoint_id,
            body.mocked_query,
            body.mocked_answer,
            body.sample_id,
            body.round_id,
            body.position_id,
            FeatureSet({}, {}),
        )
        try:
            result = app.state.runtime.register(
                sample, round_position_completeness=body.round_position_completeness
            )
        except ValueError:
            raise HTTPException(409, "Sample conflicts with stored scenario")
        app.state.business_paths.add(body.endpoint_id)
        add_business(app, body.endpoint_id)
        return {"sample_hash": result.sample_hash, "endpoint_id": body.endpoint_id}

    @app.post("/debug/embedding", response_model=schemas.EmbeddingResult)
    def debug_embedding(body: EmbeddingBody):
        encoder = app.state.runtime.encoder
        q = encoder.encode_query(body.query)
        return {
            "encoder": encoder.fingerprint,
            "items": [
                {
                    "text": text,
                    "score": sum(a * b for a, b in zip(q, encoder.encode_document(text))),
                }
                for text in body.candidates
            ],
        }

    @app.post("/debug/bm25", response_model=schemas.BM25Result)
    def debug_bm25(body: BM25Body):
        samples = (
            app.state.repository.list_endpoint(body.endpoint)
            if body.endpoint
            else app.state.repository.list_all()
        )
        index = EndpointBM25Index(DefaultTokenizer())
        index.refresh_if_changed(samples)
        return {
            "items": [
                {"sample_hash": s.sample_hash, "query": s.mocked_query, "score": score}
                for s, score in index.recall(body.query, body.topK)
            ]
        }

    @app.get("/management/v1/metrics", response_model=schemas.Metrics)
    def metrics():
        with app.state.telemetry.lock:
            counters = [dict(v) for v in app.state.telemetry.counters.values()]
        return {
            "instance_id": app.state.instance_id,
            "boot_id": app.state.boot_id,
            "sampled_at": utc(),
            "counters": counters,
        }

    def date_filter(value):
        if value is None:
            return None
        if value.tzinfo is None:
            raise HTTPException(422, "Time filters require a timezone")
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def page(kind, limit, cursor, filters=None):
        try:
            return app.state.telemetry.page(kind, limit, cursor, filters)
        except ValueError:
            raise HTTPException(422, "Invalid cursor")

    @app.get("/management/v1/requests", response_model=schemas.Requests)
    def requests(
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        non_200: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
    ):
        return page(
            "requests",
            limit,
            cursor,
            {
                "endpoint": endpoint,
                "method": method,
                "non_200": non_200,
                "since": date_filter(since),
                "until": date_filter(until),
            },
        )

    def resource(kind, identifier):
        value = app.state.telemetry.get(kind, identifier)
        if value is None:
            raise HTTPException(404, "Resource not found")
        return value

    @app.get("/management/v1/requests/{request_id}", response_model=schemas.RequestDetail)
    def request_detail(request_id: str):
        return resource("requests", request_id)

    @app.get("/management/v1/traces", response_model=schemas.Traces)
    def traces(
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
        request_id: str | None = None,
    ):
        return page("traces", limit, cursor, {"request_id": request_id})

    @app.get("/management/v1/traces/{trace_id}", response_model=schemas.TraceDetail)
    def trace_detail(trace_id: str):
        return resource("traces", trace_id)

    @app.get("/management/v1/logs", response_model=schemas.Logs)
    def logs(
        limit: int = Query(100, ge=1, le=500), cursor: str | None = None, level: str | None = None
    ):
        return page("logs", limit, cursor, {"level": level})

    def tables():
        repo = app.state.repository
        hidden = set(os.getenv("MOCK_DATABASE_HIDDEN_TABLES", "").split(","))
        with repo._lock:
            return [
                r[0]
                for r in repo._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                if r[0] not in hidden
            ]

    def columns(table):
        if table not in tables():
            raise HTTPException(404, "Table not found")
        hidden = set(os.getenv("MOCK_DATABASE_HIDDEN_COLUMNS", "").split(","))
        with app.state.repository._lock:
            return [
                {"name": r[1], "type": r[2]}
                for r in app.state.repository._connection.execute(
                    'PRAGMA table_info("' + table.replace('"', '""') + '")'
                )
                if r[1] not in hidden and table + "." + r[1] not in hidden
            ]

    @app.get("/management/v1/database/tables", response_model=schemas.Tables)
    def database_tables():
        return {"items": [{"name": t, "columns": columns(t)} for t in tables()]}

    @app.get("/management/v1/database/tables/{table}/rows", response_model=schemas.Rows)
    def rows(table: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        names = [c["name"] for c in columns(table)]
        if not names:
            return {"table": table, "columns": [], "rows": [], "next_offset": None}
        selected = ",".join('"' + n.replace('"', '""') + '"' for n in names)
        with app.state.repository._lock:
            data = app.state.repository._connection.execute(
                "SELECT "
                + selected
                + ' FROM "'
                + table.replace('"', '""')
                + '" ORDER BY rowid LIMIT ? OFFSET ?',
                (limit + 1, offset),
            ).fetchall()
        return {
            "table": table,
            "columns": names,
            "rows": [dict(r) for r in data[:limit]],
            "next_offset": offset + limit if len(data) > limit else None,
        }

    @app.post("/management/v1/exports", response_model=schemas.Export, status_code=202)
    def export(body: ExportBody):
        return app.state.telemetry.export(body.kind, app.state.repository.path)

    @app.get("/management/v1/exports/{export_id}", response_model=schemas.Export)
    def export_detail(export_id: str):
        job = app.state.telemetry.job(export_id)
        if job is None:
            raise HTTPException(404, "Export not found or expired")
        return job

    @app.get(
        "/management/v1/exports/{export_id}/download",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
    )
    def download(export_id: str):
        job = export_detail(export_id)
        if job["status"] != "ready":
            raise HTTPException(409, "Export is not ready")
        return FileResponse(app.state.telemetry.exports / job["filename"], filename=job["filename"])

    from fastapi.openapi.utils import get_openapi

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})["AdminBearer"] = {
                "type": "http",
                "scheme": "bearer",
            }
            for path, operations in schema["paths"].items():
                if (
                    path == "/ready"
                    or path == "/registry"
                    or path.startswith(("/management/", "/debug/"))
                ):
                    for operation in operations.values():
                        if isinstance(operation, dict):
                            operation["security"] = [{"AdminBearer": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app


def add_business(app, path):
    if any(getattr(r, "path", None) == path for r in app.routes):
        return

    async def query(body: QueryBody, request: Request):
        result = await run_in_threadpool(
            app.state.runtime.handle,
            MockRequest(
                path,
                body.model_dump(),
                dict(request.headers),
                request.state.request_id,
                ResponseProtocol.SSE
                if "text/event-stream" in request.headers.get("accept", "")
                else ResponseProtocol.HTTP_JSON,
            ),
        )
        if "text/event-stream" in request.headers.get("accept", ""):

            async def stream():
                yield "data: " + json.dumps(result.body, ensure_ascii=False) + "\n\n"

            return StreamingResponse(
                stream(), status_code=result.status_code, media_type="text/event-stream"
            )
        return JSONResponse(result.body, status_code=result.status_code, headers=result.headers)

    app.add_api_route(
        path,
        query,
        methods=["POST"],
        name="query_" + path.strip("/").replace("/", "_"),
        summary="Mock query",
        responses={
            200: {
                "content": {
                    "application/json": {"schema": {"type": "object"}},
                    "text/event-stream": {"schema": {"type": "string"}},
                }
            }
        },
    )
    app.openapi_schema = None


mock_service_app = create_app()
