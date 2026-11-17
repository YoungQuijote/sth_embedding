"""Optional sibling-service integration; install agent_mock_service[api] to run."""

import io
import sqlite3
import time
import zipfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from svc_panel.app import create_app


def test_actual_mock_attach_console_monitoring_database_and_exports(tmp_path, monkeypatch):
    mock_module = pytest.importorskip("agent_mock_service.app_service")
    token = "admin-secret"
    for key in ("DATA", "LOG", "TRACE"):
        monkeypatch.setenv("MOCK_" + key + "_DIR", str(tmp_path / key.lower()))
    monkeypatch.setenv("MOCK_ADMIN_TOKEN", token)
    monkeypatch.delenv("MOCK_INSTANCE_ID", raising=False)
    with TestClient(mock_module.create_app(), headers={"Authorization": "Bearer " + token}) as mock:
        registered = mock.post(
            "/registry",
            json={
                "endpoint_id": "/weather",
                "mocked_query": "weather today",
                "mocked_answer": "sunny",
                "sample_id": "integration-1",
            },
        )
        assert registered.status_code == 200, registered.text
        app = create_app(
            tmp_path / "panel", admin_token="panel-integration-token", background=False
        )

        @contextmanager
        def client(instance, timeout=20):
            original = dict(mock.headers)
            mock.headers.update(
                {
                    "Authorization": "Bearer " + app.state.store.unseal(instance["admin_token"]),
                    "X-Panel-Request-Origin": "probe",
                }
            )
            try:
                yield mock
            finally:
                mock.headers.clear()
                mock.headers.update(original)

        monkeypatch.setattr(app.state.service, "client", client)
        with TestClient(app, headers={"Authorization": "Bearer panel-integration-token"}) as http:
            payload = {
                "name": "Actual mock",
                "mode": "docker",
                "base_url": "http://mock:32882",
                "admin_token": token,
            }
            bad = http.post(
                "/api/instances/attach", json={**payload, "admin_token": "wrong-token-1234567"}
            )
            assert bad.status_code == 502 and not app.state.store.list("instance")
            attached = http.post("/api/instances/attach", json=payload)
            assert attached.status_code == 201, attached.text
            root = "/api/instances/" + attached.json()["id"]
            endpoints = http.get(root + "/endpoints").json()["items"]
            weather = next(e for e in endpoints if e["path"] == "/weather")
            assert http.get(root + "/openapi").json()["paths"]["/debug/bm25"]["post"]["requestBody"]
            response = http.post(
                root + "/console",
                json={"endpoint_id": weather["id"], "body": {"query": "weather today"}},
            )
            assert response.json() == {"answer": "sunny"}
            request_id = response.headers["x-request-id"]
            detail = http.get(root + "/requests/" + request_id).json()
            assert http.get(root + "/traces/" + detail["trace_id"]).status_code == 200
            assert http.get(root + "/logs").json()["items"]
            assert http.post(root + "/health").json()["health"] == "healthy"
            assert http.get(root + "/database/tables/endpoints/rows").json()["rows"]
            mock.post(
                "/weather", json={"query": 42}, headers={"X-Request-ID": "invalid-integration"}
            )
            failures = http.get(root + "/requests?non_200=true").json()["items"]
            assert any(x["request_id"] == "invalid-integration" for x in failures)
            for kind in ("database", "logs", "traces"):
                job = http.post(root + "/exports/" + kind).json()
                for _ in range(150):
                    result = http.get("/api/jobs/" + job["id"]).json()
                    if result["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.05)
                assert result["status"] == "succeeded", result
                artifact = http.get("/api/downloads/" + result["download_id"])
                assert artifact.status_code == 200
                if kind == "database":
                    snapshot = tmp_path / "downloaded.sqlite"
                    snapshot.write_bytes(artifact.content)
                    db = sqlite3.connect(snapshot)
                    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert db.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 1
                    db.close()
                else:
                    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                        assert archive.read(kind + ".jsonl")
            assert http.delete(root).status_code == 200
            assert mock.get("/health").status_code == 200
