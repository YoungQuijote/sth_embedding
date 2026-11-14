"""Panel contract tests through actual ASGI transport and persistent SQLite."""

import asyncio
import io
import json
import sqlite3
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from agent_mock_service.app_service import create_app


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for key, folder in (("DATA", "data"), ("LOG", "logs"), ("TRACE", "traces")):
        monkeypatch.setenv(f"MOCK_{key}_DIR", str(tmp_path / folder))
    monkeypatch.setenv("MOCK_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("MOCK_INSTANCE_ID", "36c67189-94c3-4dca-8ea1-50528670d85b")
    return tmp_path


@pytest.fixture
def client(configured):
    with TestClient(create_app()) as value:
        value.headers["Authorization"] = "Bearer admin-secret"
        yield value


def register(client):
    response = client.post(
        "/registry",
        json={
            "endpoint_id": "/weather",
            "mocked_query": "weather today",
            "mocked_answer": "sunny",
            "sample_id": "weather-1",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["sample_hash"]


def test_auth_identity_restart(configured, monkeypatch):
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        for path in (
            "/ready",
            "/openapi.json",
            "/registry",
            "/debug/bm25",
            "/management/v1/info",
            "/docs",
        ):
            r = client.get(path)
            assert r.status_code == 401
            assert r.headers["x-request-id"] == r.json()["request_id"]
        first = client.get("/ready", headers={"Authorization": "Bearer admin-secret"}).json()
    with TestClient(create_app()) as client:
        second = client.get("/ready", headers={"Authorization": "Bearer admin-secret"}).json()
        assert first["instance_id"] == second["instance_id"]
        assert first["boot_id"] != second["boot_id"]
    monkeypatch.delenv("MOCK_ADMIN_TOKEN")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


def test_discovery_json_sse_and_observation(client):
    key = register(client)
    schema = client.get("/openapi.json").json()
    for path in ("/weather", "/registry", "/debug/bm25", "/management/v1/exports"):
        assert "requestBody" in schema["paths"][path]["post"]
    assert any(
        x["path"] == "/weather" for x in client.get("/management/v1/endpoints").json()["items"]
    )
    assert (
        client.post("/debug/bm25", json={"query": "weather", "topK": 50}).json()["items"][0][
            "sample_hash"
        ]
        == key
    )
    assert (
        client.post(
            "/debug/embedding", json={"query": "weather", "candidates": ["weather"]}
        ).status_code
        == 200
    )
    r = client.post(
        "/weather",
        json={"query": "weather today", "password": "body-secret"},
        headers={"X-Request-ID": "http-123", "Cookie": "secret-cookie", "X-API-Key": "api-secret"},
    )
    assert r.status_code == 200 and r.json() == {"answer": "sunny"}
    assert r.headers["X-Request-ID"] == "http-123"
    detail = client.get("/management/v1/requests/http-123").json()
    assert detail["headers"]["authorization"] == "[REDACTED]"
    assert detail["headers"]["cookie"] == "[REDACTED]"
    assert detail["body"]["password"] == "[REDACTED]"
    trace = client.get("/management/v1/traces/" + detail["trace_id"]).json()
    assert trace["request_id"] == "http-123" and trace["event"]["upstream_request_id"] == "http-123"
    bad = client.post("/weather", json={"query": 42}, headers={"X-Request-ID": "invalid-123"})
    assert bad.status_code == 422
    assert client.get("/management/v1/requests/invalid-123").json()["trace_id"] is None
    with client.stream(
        "POST", "/weather", json={"query": "weather today"}, headers={"Accept": "text/event-stream"}
    ) as stream:
        assert stream.status_code == 200
        assert "data:" in stream.read().decode()
    client.post(
        "/weather", json={"query": "weather today"}, headers={"X-Panel-Request-Origin": "console"}
    )
    counters = client.get("/management/v1/metrics").json()["counters"]
    business = [c for c in counters if c["category"] == "business" and c["endpoint"] == "/weather"]
    assert sum(c["requests_total"] for c in business) == 3
    assert {c["status_code"] for c in business} == {200, 422}
    assert all(c["duration_seconds_sum"] >= 0 for c in counters)
    before = client.app.state.repository.get_by_hash(key).availability.invoked_times
    for path in (
        "/health",
        "/ready",
        "/management/v1/metrics",
        "/management/v1/traces",
        "/management/v1/database/tables",
    ):
        assert client.get(path).status_code == 200
    assert client.app.state.repository.get_by_hash(key).availability.invoked_times == before


def test_pages_errors_and_redaction(client, monkeypatch):
    register(client)
    for i in range(5):
        client.post(
            "/weather", json={"query": "weather today"}, headers={"X-Request-ID": f"page-{i}"}
        )
    r = client.get("/management/v1/requests", params={"limit": 2, "endpoint": "/weather"}).json()
    second = client.get(
        "/management/v1/requests",
        params={"limit": 2, "endpoint": "/weather", "cursor": r["next_cursor"]},
    ).json()
    assert len(r["items"]) == 2 and len(second["items"]) == 2
    assert not {x["request_id"] for x in r["items"]} & {x["request_id"] for x in second["items"]}
    for path in (
        "/management/v1/requests?limit=201",
        "/management/v1/requests?cursor=bad",
        "/management/v1/logs?limit=501",
        "/management/v1/database/tables/endpoints/rows?offset=-1",
        "/management/v1/requests?since=oops",
    ):
        assert client.get(path).status_code == 422
    for path in (
        "/missing",
        "/management/v1/requests/no",
        "/management/v1/traces/no",
        "/management/v1/database/tables/evil/rows",
        "/management/v1/exports/no/download",
    ):
        r = client.get(path)
        assert r.status_code == 404 and "error" in r.json() and r.headers["x-request-id"]
    monkeypatch.setenv("MOCK_DATABASE_HIDDEN_COLUMNS", "endpoint_id")
    assert (
        "endpoint_id"
        not in client.get("/management/v1/database/tables/endpoints/rows").json()["columns"]
    )
    client.app.state.telemetry.body_limit = 16
    r = client.post("/weather", json={"query": "weather today", "token": "secret-long"})
    record = client.get("/management/v1/requests/" + r.headers["x-request-id"]).json()
    assert record["truncated"] and "secret-long" not in json.dumps(record)


def test_exports(client, configured):
    register(client)
    client.post("/weather", json={"query": "weather today"})
    for kind in ("database", "logs", "traces"):
        r = client.post("/management/v1/exports", json={"kind": kind})
        assert r.status_code == 202 and r.json()["status"] == "pending"
        identifier = r.json()["export_id"]
        for _ in range(200):
            job = client.get("/management/v1/exports/" + identifier).json()
            if job["status"] in ("ready", "failed"):
                break
            time.sleep(0.005)
        assert job["status"] == "ready", job
        download = client.get("/management/v1/exports/" + identifier + "/download")
        assert (
            download.status_code == 200 and "attachment" in download.headers["content-disposition"]
        )
        assert len(download.content) == job["size_bytes"]
        if kind == "database":
            target = configured / "snapshot.sqlite"
            target.write_bytes(download.content)
            with sqlite3.connect(target) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert db.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 1
        else:
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                assert archive.read(kind + ".jsonl")


def test_runtime_exception_and_telemetry_failure(client, monkeypatch):
    register(client)

    def fail(*args):
        raise RuntimeError("private error with secret")

    monkeypatch.setattr(client.app.state.runtime, "handle", fail)
    r = client.post("/weather", json={"query": "weather today"})
    assert r.status_code == 500 and "secret" not in r.text
    detail = client.get("/management/v1/requests/" + r.headers["x-request-id"]).json()
    assert detail["status_code"] == 500
    assert any(
        c["status_code"] == 500 for c in client.get("/management/v1/metrics").json()["counters"]
    )


def test_telemetry_fault_does_not_break_query(client, monkeypatch):
    register(client)

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(client.app.state.telemetry, "add", fail)
    r = client.post("/weather", json={"query": "weather today"})
    assert r.status_code == 200 and r.json() == {"answer": "sunny"}


def test_invalid_id_readiness_and_no_business_counters(client):
    assert client.get("/health").status_code == 200
    assert client.get("/management/v1/metrics").json()["counters"] == []
    r = client.get("/health", headers={"X-Request-ID": "invalid id " + "x" * 200})
    assert len(r.headers["x-request-id"]) <= 128 and " " not in r.headers["x-request-id"]
    client.app.state.ready = False
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["status"] == "not_ready"


def test_restore_discovery_and_telemetry(configured):
    with TestClient(create_app(), headers={"Authorization": "Bearer admin-secret"}) as client:
        register(client)
        r = client.post("/weather", json={"query": "weather today"})
        identifier = r.headers["x-request-id"]
    with TestClient(create_app(), headers={"Authorization": "Bearer admin-secret"}) as client:
        assert "/weather" in client.get("/openapi.json").json()["paths"]
        assert client.get("/management/v1/requests/" + identifier).status_code == 200
        assert client.post("/weather", json={"query": "weather today"}).status_code == 200


def test_export_pending_failure_expiry_and_online_writes(client, configured, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = client.app.state.telemetry
    blocked, resume = threading.Event(), threading.Event()
    original = store._export

    def delayed(*args):
        blocked.set()
        assert resume.wait(5)
        original(*args)

    monkeypatch.setattr(store, "_export", delayed)
    identifier = client.post("/management/v1/exports", json={"kind": "database"}).json()[
        "export_id"
    ]
    assert blocked.wait(5)
    assert client.get(f"/management/v1/exports/{identifier}/download").status_code == 409
    resume.set()
    for _ in range(200):
        if store.job(identifier)["status"] in ("ready", "failed"):
            break
        time.sleep(0.005)
    assert store.job(identifier)["status"] == "ready"
    # A concurrent independent writer keeps the WAL active while backup executes.
    register(client)
    stop = threading.Event()

    def write():
        with sqlite3.connect(client.app.state.repository.path) as db:
            while not stop.is_set():
                db.execute(
                    "INSERT OR REPLACE INTO repository_metadata(key,value) VALUES('backup_test','active')"
                )
                db.commit()
                time.sleep(0.001)

    monkeypatch.setattr(store, "_export", original)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        try:
            identifier = client.post("/management/v1/exports", json={"kind": "database"}).json()[
                "export_id"
            ]
            for _ in range(200):
                if store.job(identifier)["status"] in ("ready", "failed"):
                    break
                time.sleep(0.005)
            assert store.job(identifier)["status"] == "ready"
            target = configured / "online.sqlite"
            target.write_bytes(client.get(f"/management/v1/exports/{identifier}/download").content)
            with sqlite3.connect(target) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            stop.set()
            writer.result()
    with store.lock:
        store.jobs[identifier]["created"] -= 90000
    assert client.get(f"/management/v1/exports/{identifier}").status_code == 404
    # Snapshot failure is reported with a readable error and a conflict on download.
    monkeypatch.setattr(client.app.state.repository, "path", "/does/not/exist/database.sqlite")
    identifier = client.post("/management/v1/exports", json={"kind": "database"}).json()[
        "export_id"
    ]
    for _ in range(200):
        job = store.job(identifier)
        if job["status"] == "failed":
            break
        time.sleep(0.005)
    assert job["status"] == "failed" and job["error"]
    assert client.get(f"/management/v1/exports/{identifier}/download").status_code == 409


def test_stream_interruption_records_original_status(client):
    from starlette.datastructures import State

    from agent_mock_service.telemetry.http import ObservationMiddleware

    class Route:
        path = "/stream"

    async def stream(scope, receive, send):
        scope["route"] = Route()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: test\n\n", "more_body": True})
        raise RuntimeError("broken stream")

    async def receive():
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    state = State({"telemetry": client.app.state.telemetry})
    middleware = ObservationMiddleware(stream, state)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/stream",
                "headers": [(b"x-request-id", b"stream-break")],
            },
            receive,
            send,
        )
    )
    assert len([m for m in messages if m["type"] == "http.response.start"]) == 1
    detail = client.get("/management/v1/requests/stream-break").json()
    assert detail["status_code"] == 200 and detail["interrupted"] and detail["error"]
