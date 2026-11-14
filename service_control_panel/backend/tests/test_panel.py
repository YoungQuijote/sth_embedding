import base64
import hashlib
import io
import json
import sqlite3
import tarfile
import time
from contextlib import contextmanager

import paramiko
import pytest
from contract_server import MOCK_TOKEN, make_contract_app
from fastapi.testclient import TestClient

from svc_panel.app import create_app
from svc_panel.deploy import validate_source
from svc_panel.remote import FingerprintPolicy
from svc_panel.service import Service

TOKEN = "test-admin-token-123456789"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    app = create_app(tmp_path, admin_token=TOKEN, background=False)
    service = app.state.service
    mock = TestClient(make_contract_app(), headers={"Authorization": "Bearer " + MOCK_TOKEN})

    @contextmanager
    def client(instance, timeout=20):
        yield mock

    monkeypatch.setattr(service, "client", client)
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as http:
        env = http.post(
            "/api/environments",
            json={
                "name": "Test",
                "host": "127.0.0.1",
                "username": "tester",
                "password": "secret-password",
                "host_key_sha256": "SHA256:" + "a" * 43,
            },
        ).json()
        instance = {
            "id": "i1",
            "name": "Fixture",
            "environment_id": env["id"],
            "admin_token": app.state.store.seal(MOCK_TOKEN),
            "mode": "bare",
            "state": "deployed",
            "health": "unknown",
            "health_failures": 0,
            "port": 32882,
            "base_url": "http://127.0.0.1:32882",
            "access": "direct",
        }
        app.state.store.put("instance", instance)
        yield http, app, instance
    mock.close()


def test_authentication_and_encrypted_credentials(panel):
    http, app, instance = panel
    assert http.get("/api/session", headers={"Authorization": ""}).status_code == 401
    assert http.get("/api/session").json()["monitoring"] == "builtin"
    envs = http.get("/api/environments").json()
    assert "credentials" not in envs[0]
    assert "admin_token" not in http.get("/api/instances").json()[0]
    with app.state.store.connection() as db:
        assert "secret-password" not in " ".join(
            r[0] for r in db.execute("SELECT value FROM objects")
        )
    assert (app.state.store.directory / "credentials.key").stat().st_mode & 0o077 == 0


def test_health_metrics_endpoints_and_openapi(panel):
    http, app, _ = panel
    assert http.post("/api/instances/i1/health").json()["health"] == "healthy"
    assert len(http.get("/api/instances/i1/endpoints").json()["items"]) == 3
    schema = http.get("/api/instances/i1/openapi").json()
    assert schema["components"]["schemas"]["QueryBody"]["properties"]["topK"]["default"] == 50
    assert http.get("/api/monitoring?instance_id=missing").status_code == 404
    assert http.get("/api/monitoring?minutes=10081").status_code == 422


def test_console_json_sse_and_route_allowlist(panel):
    http, _, _ = panel
    response = http.post(
        "/api/instances/i1/console",
        json={
            "endpoint_id": "bm25",
            "body": {"query": "hello", "topK": 5},
            "headers": {"X-Panel-Request-Origin": "business", "Host": "evil.example"},
        },
    )
    assert response.status_code == 200
    assert response.json() == {"query": "hello", "topK": 5, "origin": "console", "answer": "晴"}
    response = http.post("/api/instances/i1/console", json={"endpoint_id": "stream"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"done":true' in response.text
    assert response.headers["x-request-id"] == "stream-1"
    assert (
        http.post("/api/instances/i1/console", json={"endpoint_id": "http://evil"}).status_code
        == 404
    )
    response = http.post("/api/instances/i1/console", json={"endpoint_id": "weather", "body": {}})
    assert response.status_code == 422  # Preserve upstream validation status.


def test_logs_database_and_request_trace_drilldown(panel):
    http, _, _ = panel
    logs = http.get("/api/instances/i1/logs").json()
    assert logs["next_cursor"] == "page-2"
    assert http.get("/api/instances/i1/logs?cursor=page-2").json()["next_cursor"] is None
    assert http.get("/api/instances/i1/logs?limit=10000").status_code == 422
    assert http.get("/api/instances/i1/database/tables").json()["items"][0]["name"] == "samples"
    assert http.get("/api/instances/i1/database/tables/samples/rows").json()["rows"] == [
        {"query": "天气如何"}
    ]
    assert http.get("/api/instances/i1/database/tables/samples/rows?offset=-1").status_code == 422
    item = http.get("/api/instances/i1/requests?endpoint=/weather&method=POST&non_200=true").json()[
        "items"
    ][0]
    detail = http.get("/api/instances/i1/requests/" + item["request_id"]).json()
    assert (
        http.get("/api/instances/i1/traces/" + detail["trace_id"]).json()["event"]["error"]
        == "fixture"
    )


@pytest.mark.parametrize("kind", ["logs", "database", "traces"])
def test_export_jobs_download_real_artifacts(panel, kind):
    http, app, _ = panel
    response = http.post("/api/instances/i1/exports/" + kind)
    assert response.status_code == 202
    identifier = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = http.get("/api/jobs/" + identifier).json()
        if job["status"] in {"failed", "succeeded"}:
            break
        time.sleep(0.05)
    assert job["status"] == "succeeded", job
    download = http.get("/api/downloads/" + job["download_id"])
    assert download.status_code == 200
    if kind == "database":
        db = sqlite3.connect(":memory:")
        db.deserialize(download.content)
        assert db.execute("SELECT query FROM samples").fetchone()[0] == "weather"
        db.close()
    else:
        assert json.loads(download.text)["request_id"] == "r1"
    ticket = http.post("/api/downloads/" + identifier + "/ticket").json()
    assert http.get(ticket["url"], headers={"Authorization": ""}).content == download.content
    assert http.get(ticket["url"]).status_code == 404
    assert (
        http.post(
            "/api/downloads/" + identifier + "/ticket", headers={"Authorization": ""}
        ).status_code
        == 401
    )
    record = app.state.store.get("download", identifier)
    record["expired"] = True
    app.state.store.put("download", record)
    assert http.get("/api/downloads/" + identifier).status_code == 410


def test_failure_alert_threshold_recovery_and_redaction(panel, monkeypatch):
    http, app, instance = panel
    original = app.state.service.client

    @contextmanager
    def offline(*args, **kwargs):
        raise RuntimeError("offline " + MOCK_TOKEN + " secret-password")
        yield

    monkeypatch.setattr(app.state.service, "client", offline)
    for _ in range(2):
        assert http.post("/api/instances/i1/health").json()["health"] == "unhealthy"
    assert http.get("/api/alerts").json() == []
    response = http.post("/api/instances/i1/health")
    assert MOCK_TOKEN not in response.text and "secret-password" not in response.text
    alerts = http.get("/api/alerts").json()
    assert len(alerts) == 1
    assert http.post(f"/api/alerts/{alerts[0]['id']}/acknowledge").status_code == 200
    monkeypatch.setattr(app.state.service, "client", original)
    http.post("/api/instances/i1/health")
    assert http.get("/api/alerts").json()[0]["resolved_at"] is not None


def source_archive(name="project/pyproject.toml", link=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(name)
        data = b'[project]\nname="test"\nversion="1"\n'
        member.size = len(data)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            member.size = 0
        archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "name,link",
    [("../../pyproject.toml", False), ("/pyproject.toml", False), ("project/pyproject.toml", True)],
)
def test_reject_unsafe_source_archive(tmp_path, name, link):
    path = tmp_path / "source.tar"
    path.write_bytes(source_archive(name, link))
    with pytest.raises(ValueError):
        validate_source(path)


def test_upload_deploy_and_duplicate_port(panel, monkeypatch):
    http, app, existing = panel
    response = http.post(
        "/api/artifacts?kind=source",
        files={"file": ("service.tar", source_archive(), "application/x-tar")},
    )
    assert response.status_code == 201
    artifact = response.json()
    assert artifact["sha256"] == hashlib.sha256(source_archive()).hexdigest()
    assert "path" not in artifact
    monkeypatch.setattr(app.state.service.executor, "submit", lambda *args: None)
    payload = {
        "name": "new",
        "environment_id": existing["environment_id"],
        "mode": "bare",
        "artifact_id": artifact["id"],
        "port": 32883,
        "base_url": "http://127.0.0.1:32883",
        "admin_token": MOCK_TOKEN,
    }
    response = http.post("/api/instances", json=payload)
    assert response.status_code == 202
    assert MOCK_TOKEN not in response.text
    assert http.post("/api/instances", json=payload).status_code == 409
    identifier = response.json()["instance"]["id"]
    assert http.post(f"/api/instances/{identifier}/restart").status_code == 400


def test_interrupted_task_recovery(panel):
    http, app, instance = panel
    app.state.store.put("job", {"id": "unfinished", "instance_id": "i1", "status": "running"})
    recovered = Service(app.state.store)
    try:
        assert app.state.store.get("job", "unfinished")["status"] == "interrupted"
        assert app.state.store.get("instance", "i1")["state"] == "unknown"
    finally:
        recovered.close()


def test_host_fingerprint_must_match():
    key = paramiko.RSAKey.generate(1024)
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(key.asbytes()).digest()
    ).decode().rstrip("=")
    FingerprintPolicy(fingerprint).missing_host_key(None, "host", key)
    with pytest.raises(ValueError, match="指纹不匹配"):
        FingerprintPolicy("SHA256:wrong").missing_host_key(None, "host", key)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://user:pass@localhost", "http://host/path", "http://host?token=x"],
)
def test_service_base_url_constraints(url):
    from svc_panel.models import InstanceInput

    with pytest.raises(ValueError):
        InstanceInput(
            name="x",
            environment_id="e",
            mode="bare",
            artifact_id="a",
            base_url=url,
            admin_token=MOCK_TOKEN,
        )
