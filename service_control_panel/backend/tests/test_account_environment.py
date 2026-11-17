"""Admin rotation and dependency-aware environment deletion regressions."""

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from test_panel import MOCK_TOKEN, TOKEN, source_archive

from svc_panel.app import create_app
from svc_panel.auth import AdminAuth
from svc_panel.store import Store

pytest_plugins = ["test_panel"]
NEW = "new-admin-password-123456"


def change(http, current=TOKEN, new=NEW, confirm=NEW):
    return http.post(
        "/api/account/password",
        json={
            "current_password": current,
            "new_password": new,
            "confirm_password": confirm,
        },
    )


def test_rotation_revokes_old_login_and_download_tickets_and_survives_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PANEL_ADMIN_TOKEN", TOKEN)
    app = create_app(tmp_path, background=False)
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as http:
        app.state.store.put(
            "ticket", {"id": "old-ticket", "download_id": "x", "expires_at": 10**12}
        )
        app.state.store.put(
            "job", {"id": "existing", "instance_id": "not-present", "status": "running"}
        )
        result = change(http)
        assert result.status_code == 200 and result.json()["reauthenticate"]
        assert NEW not in result.text
        assert http.get("/api/session").status_code == 401
        assert (
            http.get("/api/session", headers={"Authorization": "Bearer " + NEW}).status_code == 200
        )
        assert app.state.store.list("ticket") == []
        assert app.state.store.get("job", "existing")["status"] == "running"
        record = json.dumps(app.state.store.get("auth", "admin"))
        assert NEW not in record and TOKEN not in record
        assert "scrypt" in record
    # Persisted changes take precedence over an unchanged deployment env var.
    restarted = create_app(tmp_path, background=False)
    with TestClient(restarted, headers={"Authorization": "Bearer " + TOKEN}) as http:
        assert http.get("/api/session").status_code == 401
        assert (
            http.get("/api/session", headers={"Authorization": "Bearer " + NEW}).status_code == 200
        )


def test_rotation_validates_current_confirmation_and_policy(panel):
    http, app, _ = panel
    assert change(http, current="wrong-current-token").status_code == 403
    invalid = change(http, confirm="does-not-match")
    assert invalid.status_code == 422
    assert NEW not in invalid.text and TOKEN not in invalid.text
    assert change(http, new="short", confirm="short").status_code == 422
    assert change(http, new=TOKEN, confirm=TOKEN).status_code == 422
    assert change(http, new="has space-123456789", confirm="has space-123456789").status_code == 422
    assert (
        change(
            http,
            new="中文密码测试中文密码测试中文密码测试",
            confirm="中文密码测试中文密码测试中文密码测试",
        ).status_code
        == 422
    )
    assert http.get("/api/session").status_code == 200
    assert app.state.store.get("auth", "admin") is None
    assert (
        http.post(
            "/api/account/password",
            headers={"Authorization": ""},
            json={"current_password": TOKEN, "new_password": NEW, "confirm_password": NEW},
        ).status_code
        == 401
    )


def test_bootstrap_file_is_retired_and_failed_save_keeps_current_login(tmp_path, monkeypatch):
    monkeypatch.delenv("PANEL_ADMIN_TOKEN", raising=False)
    store = Store(tmp_path)
    auth = AdminAuth(store)
    token = (tmp_path / "admin.token").read_text()
    connection = store.connection

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "connection", fail)
    with pytest.raises(OSError):
        auth.change(token, NEW)
    assert auth.verify(token) and not auth.verify(NEW)
    assert (tmp_path / "admin.token").exists()
    monkeypatch.setattr(store, "connection", connection)
    assert auth.change(token, NEW)
    assert not (tmp_path / "admin.token").exists()
    assert not auth.change(token, "another-password-123456")
    reloaded = AdminAuth(Store(tmp_path))
    assert reloaded.verify(NEW) and not reloaded.verify(token)


def test_unused_environment_deletion_removes_credentials_without_remote_calls(panel, monkeypatch):
    http, app, _ = panel
    env = http.post(
        "/api/environments",
        json={"name": "Disposable", "host": "host", "username": "user", "password": "ssh-secret"},
    ).json()

    def forbidden(*args, **kwargs):
        pytest.fail("Environment deletion must not contact a remote host")

    monkeypatch.setattr("svc_panel.app.SSH", forbidden)
    result = http.delete("/api/environments/" + env["id"])
    assert result.status_code == 200 and result.json()["remote_resources_unchanged"]
    assert app.state.store.get("environment", env["id"]) is None
    assert all(e["id"] != env["id"] for e in http.get("/api/environments").json())
    assert http.delete("/api/environments/" + env["id"]).status_code == 404
    assert http.post("/api/environments/" + env["id"] + "/check").status_code == 404


def test_environment_in_use_then_forgotten_job_finishes(panel):
    http, app, item = panel
    path = "/api/environments/" + item["environment_id"]
    blocked = http.delete(path)
    assert blocked.status_code == 409 and "Fixture" in blocked.text
    app.state.store.put("job", {"id": "running", "instance_id": item["id"], "status": "running"})
    http.delete("/api/instances/" + item["id"])
    assert http.delete(path).status_code == 409  # Hidden instance still has an active task.
    app.state.store.patch("job", "running", status="succeeded")
    assert http.delete(path).status_code == 200
    assert app.state.store.get("instance", item["id"])  # Keep operation/audit history.
    assert http.get("/api/jobs/running").json()["status"] == "succeeded"


def test_environment_for_undeployed_instance_is_deletable(panel):
    http, app, item = panel
    app.state.store.patch("instance", item["id"], state="undeployed")
    assert http.delete("/api/environments/" + item["environment_id"]).status_code == 200
    assert http.get("/api/instances").json()[0]["state"] == "undeployed"


def test_deleted_environment_cannot_be_used_to_deploy_or_attach(panel, monkeypatch):
    http, app, item = panel
    http.delete("/api/instances/" + item["id"])
    assert http.delete("/api/environments/" + item["environment_id"]).status_code == 200
    response = http.post(
        "/api/instances/attach",
        json={
            "name": "External",
            "mode": "docker",
            "base_url": "http://example:32882",
            "admin_token": MOCK_TOKEN,
            "environment_id": item["environment_id"],
            "access": "ssh",
        },
    )
    assert response.status_code == 404
    content = source_archive()
    artifact = http.post(
        "/api/artifacts?kind=source&filename=test.tar",
        content=content,
        headers={"Content-Type": "application/octet-stream", "X-Upload-Size": str(len(content))},
    ).json()
    assert (
        http.post(
            "/api/instances",
            json={
                "name": "new",
                "environment_id": item["environment_id"],
                "mode": "bare",
                "artifact_id": artifact["id"],
                "base_url": "http://example:32882",
                "admin_token": MOCK_TOKEN,
            },
        ).status_code
        == 404
    )


def test_environment_deleted_during_attach_probe_cannot_leave_dangling_reference(
    panel, monkeypatch
):
    http, app, item = panel
    http.delete("/api/instances/" + item["id"])
    original = app.state.service.client

    @contextmanager
    def during_probe(*args, **kwargs):
        with original(*args, **kwargs) as client:
            yield client
        assert http.delete("/api/environments/" + item["environment_id"]).status_code == 200

    monkeypatch.setattr(app.state.service, "client", during_probe)
    result = http.post(
        "/api/instances/attach",
        json={
            "name": "Race",
            "mode": "bare",
            "base_url": "http://remote:32882",
            "admin_token": MOCK_TOKEN,
            "environment_id": item["environment_id"],
        },
    )
    assert result.status_code == 404
    assert http.get("/api/instances").json() == []


def test_rotation_rolls_back_if_ticket_revocation_fails(tmp_path):
    import sqlite3

    store = Store(tmp_path)
    auth = AdminAuth(store, TOKEN)
    store.put("ticket", {"id": "old", "expires_at": 10**12})
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_ticket_delete BEFORE DELETE ON objects WHEN OLD.kind='ticket' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        auth.change(TOKEN, NEW)
    assert auth.verify(TOKEN) and not auth.verify(NEW)
    assert store.get("auth", "admin") is None
    assert store.get("ticket", "old") is not None
