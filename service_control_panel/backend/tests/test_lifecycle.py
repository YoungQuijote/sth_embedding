"""Deployment changes: early upload refusal, ownership and monitoring lifecycle."""

import asyncio
import base64
import hashlib
import json
import shlex
import subprocess
from contextlib import contextmanager
from uuid import uuid4

import paramiko
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_panel import MOCK_TOKEN, source_archive

from svc_panel.deployment_support import (
    LABEL,
    check_storage,
    docker_run_command,
    prepare_directory,
    undeploy,
)
from svc_panel.models import DockerOptions, RemoteDirectoryInput
from svc_panel.remote import SSH
from svc_panel.store import Store
from svc_panel.uploads import receive_upload

pytest_plugins = ["test_panel"]


def test_optional_fingerprint_pins_only_successful_login(panel, monkeypatch):
    http, app, _ = panel
    result = http.post(
        "/api/environments",
        json={
            "name": "optional",
            "host": "host",
            "username": "user",
            "password": "pw",
        },
    )
    assert result.status_code == 201
    store = app.state.store
    env = store.get("environment", result.json()["id"])
    key = paramiko.RSAKey.generate(1024)
    success = False

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, *args, **kwargs):
            self.policy.missing_host_key(self, "host", key)
            if not success:
                raise paramiko.AuthenticationException("failed")

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    with pytest.raises(paramiko.AuthenticationException):
        SSH(env, store)
    assert not store.get("environment", env["id"]).get("learned_host_key_sha256")
    success = True
    with SSH(env, store):
        pass
    pinned = store.get("environment", env["id"])
    assert pinned["learned_host_key_sha256"] == "SHA256:" + base64.b64encode(
        hashlib.sha256(key.asbytes()).digest()
    ).decode().rstrip("=")
    key = paramiko.RSAKey.generate(1024)
    with pytest.raises(ValueError, match="指纹不匹配"):
        SSH(pinned, store)


def test_upload_oversized_headers_never_read_body(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_PACKAGE_LIMIT_BYTES", "8")
    store = Store(tmp_path)

    async def forbidden_receive():
        pytest.fail("Oversized upload body must not be consumed")

    request = Request(
        {
            "type": "http",
            "headers": [(b"content-type", b"application/octet-stream"), (b"x-upload-size", b"9")],
        },
        forbidden_receive,
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(receive_upload(request, store, "image", "large.tar"))
    assert failure.value.status_code == 413
    assert not list((tmp_path / "artifacts").glob("*"))


def test_chunked_dishonest_upload_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_PACKAGE_LIMIT_BYTES", "8")
    store = Store(tmp_path)
    chunks = iter([b"1234", b"56789"])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request(
        {
            "type": "http",
            "headers": [(b"content-type", b"application/octet-stream"), (b"x-upload-size", b"8")],
        },
        receive,
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(receive_upload(request, store, "image", "large.tar"))
    assert failure.value.status_code == 413
    assert list((tmp_path / "artifacts").iterdir()) == []
    assert store.list("artifact") == []


def test_stream_upload_policy_and_metadata(panel, monkeypatch):
    http, app, _ = panel
    assert http.get("/api/upload-policy").json()["limits"]["image"] == 2 * 1024**3
    content = source_archive()
    headers = {"Content-Type": "application/octet-stream", "X-Upload-Size": str(len(content))}
    r = http.post(
        "/api/artifacts?kind=source&filename=source.tar", content=content, headers=headers
    )
    assert r.status_code == 201, r.text
    assert r.json()["expanded_bytes"] > 0
    assert r.json()["sha256"] == hashlib.sha256(content).hexdigest()
    assert r.json()["upload_seconds"] >= 0
    assert (
        http.post(
            "/api/artifacts?kind=image&filename=image.tar", files={"file": ("x", b"x")}
        ).status_code
        == 415
    )
    assert (
        http.post(
            "/api/artifacts?kind=image&filename=image.tar",
            content=b"x",
            headers={**headers, "X-Upload-Size": "2"},
        ).status_code
        == 400
    )
    monkeypatch.setenv("PANEL_PACKAGE_LIMIT_BYTES", "2")
    assert (
        http.post(
            "/api/artifacts?kind=image&filename=image.tar",
            content=b"abc",
            headers={**headers, "X-Upload-Size": "3"},
        ).status_code
        == 413
    )
    assert len(app.state.store.list("artifact")) == 1


@pytest.mark.parametrize("root", ["/", "/a/../b", "/tmp/$(id)", "relative", "/tmp/x\ny"])
def test_invalid_roots(root):
    with pytest.raises(ValueError):
        RemoteDirectoryInput(remote_root=root)


def test_storage_scripts_and_custom_directory(tmp_path):
    store = Store(tmp_path)
    instance = {"id": str(uuid4()), "mode": "docker", "remote_root": "/home/user/my deployments"}
    store.put("instance", instance)
    scripts = []

    class Fake:
        def run(self, script, **kwargs):
            scripts.append(script)
            result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
            if "PANEL_DIR=" in script:
                return f"PANEL_DIR={instance['remote_root']}/{instance['id']}\nPANEL_AVAILABLE=9000000000"
            return "9000000000" if "printf" in script else ""

    check_storage(Fake(), instance["remote_root"], 1024, "docker")
    assert "df -PB1" in scripts[0] and "test -w" in scripts[0]
    assert "DockerRootDir" in scripts[1]
    directory = prepare_directory(Fake(), instance, {"size_bytes": 1024}, store)
    assert directory == instance["remote_root"] + "/" + instance["id"]
    script = scripts[-1]
    assert script.index('test "$AVAILABLE" -ge') < script.index('$SUDO mkdir -- "$DIR"')
    assert ".panel-owner" in script
    assert store.get("instance", instance["id"])["remote_directory"] == directory


def test_docker_custom_argv_and_finite_restarts():
    malicious = 'echo $(touch /tmp/not-executed); "$TOKEN"'
    instance = {
        "id": str(uuid4()),
        "port": 32999,
        "docker": {
            "command_mode": "custom",
            "entrypoint": "/bin/sh",
            "command": ["-c", malicious],
            "max_restarts": 2,
            "container_port": 8000,
            "extra_args": ["--cpus", "2", "--memory=1g"],
            "mounts": [{"source": "/data/my models", "target": "/models"}],
        },
    }
    script = docker_run_command(instance, "mock:test", "/home/my services/" + instance["id"])
    argv = shlex.split(script.split("$SUDO ", 1)[1])
    assert argv[-3:] == ["mock:test", "-c", malicious]
    assert argv[argv.index("--restart") + 1] == "on-failure:2"
    assert "32999:8000" in argv
    assert "type=bind,source=/data/my models,target=/models,readonly" in argv
    instance["docker"] = {"command_mode": "image", "max_restarts": 0}
    script = docker_run_command(instance, "mock:test", "/data/" + instance["id"])
    assert "--entrypoint" not in script and "--restart no" in script


@pytest.mark.parametrize(
    "options",
    [
        {"extra_args": ["--restart", "always"]},
        {"extra_args": ["--name=x"]},
        {"extra_args": ["--volume", "/:/host"]},
        {"extra_args": ["--memory"]},
        {"environment": {"MOCK_ADMIN_TOKEN": "override"}},
        {"environment": {"TOKEN": "line\nnext"}},
        {"mounts": [{"source": "/data", "target": "/svc/data"}]},
        {"command_mode": "custom"},
    ],
)
def test_docker_reserved_options_rejected(options):
    with pytest.raises(ValueError):
        DockerOptions.model_validate(options)


def test_attach_forget_and_reconnect(panel, monkeypatch):
    http, app, _ = panel
    payload = {
        "name": "External",
        "mode": "docker",
        "base_url": "http://external:32882",
        "admin_token": MOCK_TOKEN,
    }
    response = http.post("/api/instances/attach", json=payload)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["origin"] == "attached" and item["health"] == "healthy"
    assert "admin_token" not in item and item["endpoints"]
    root = "/api/instances/" + item["id"]
    assert http.post("/api/instances/attach", json=payload).status_code == 409
    assert http.post(root + "/restart").status_code == 409
    assert http.post(root + "/undeploy", json={"confirm_name": "External"}).status_code == 409
    app.state.service.check(item["id"])
    # Forget remains available even with a running operation; do not cancel that operation.
    app.state.store.put("job", {"id": "active", "instance_id": item["id"], "status": "running"})
    assert http.delete(root).json()["remote_resources_unchanged"]
    # A late health response must not recreate visibility or monitoring samples.
    app.state.service.check(item["id"])
    assert http.get(root + "/endpoints").status_code == 404
    with app.state.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM samples WHERE instance_id=?", (item["id"],)
            ).fetchone()[0]
            == 0
        )
    assert all(i["id"] != item["id"] for i in http.get("/api/instances").json())
    assert http.post("/api/instances/attach", json=payload).status_code == 201


def test_attach_failure_not_saved(panel, monkeypatch):
    http, app, _ = panel

    @contextmanager
    def fail(*args, **kwargs):
        raise RuntimeError("failed " + MOCK_TOKEN)
        yield

    monkeypatch.setattr(app.state.service, "client", fail)
    r = http.post(
        "/api/instances/attach",
        json={
            "name": "x",
            "mode": "bare",
            "base_url": "http://external",
            "admin_token": MOCK_TOKEN,
        },
    )
    assert r.status_code == 502 and MOCK_TOKEN not in r.text
    assert len(app.state.store.list("instance")) == 1


def owned_fixture(tmp_path, monkeypatch, legacy=False, mode="docker"):
    store = Store(tmp_path)
    identifier = str(uuid4())
    directory = "/opt/svc-panel/" + identifier
    instance = {
        "id": identifier,
        "environment_id": "env",
        "mode": mode,
        "state": "failed",
        "monitoring_enabled": True,
    }
    if not legacy:
        instance.update(resource_version=2, remote_directory=directory)
    store.put("instance", instance)
    scripts = []
    info = {
        "Id": "container-id",
        "Config": {"Labels": {LABEL: identifier}},
        "Mounts": [{"Source": directory + "/data", "Destination": "/svc/data"}],
    }

    class Fake:
        present = True

        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, script, **kwargs):
            scripts.append(script)
            r = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            assert r.returncode == 0, r.stderr
            if "docker ps" in script:
                return "mock-" + identifier if self.present else ""
            if "docker inspect" in script:
                return json.dumps([info])
            return ""

    monkeypatch.setattr("svc_panel.deployment_support.SSH", Fake)
    return store, instance, scripts, info, Fake


@pytest.mark.parametrize("legacy", [False, True])
def test_undeploy_owned_container_and_directory_retains_shared_image(tmp_path, monkeypatch, legacy):
    store, item, scripts, _, _ = owned_fixture(tmp_path, monkeypatch, legacy)
    undeploy(store, item, lambda _: None)
    joined = "\n".join(scripts)
    assert "docker rm -f mock-" + item["id"] in joined
    assert "$SUDO rm -rf -- /opt/svc-panel/" + item["id"] in joined
    assert "docker rmi" not in joined
    assert store.get("instance", item["id"])["state"] == "undeployed"


def test_undeploy_refuses_changed_container_and_is_idempotent(tmp_path, monkeypatch):
    store, item, scripts, info, fake = owned_fixture(tmp_path, monkeypatch)
    info["Config"]["Labels"][LABEL] = "another-owner"
    with pytest.raises(ValueError, match="归属"):
        undeploy(store, item, lambda _: None)
    assert not any("docker rm" in s or "rm -rf" in s for s in scripts)
    # Container may have been removed manually; owned directory still needs cleanup.
    fake.present = False
    undeploy(store, item, lambda _: None)
    undeploy(store, item, lambda _: None)
    assert store.get("instance", item["id"])["state"] == "undeployed"


def test_undeploy_bare_and_preflight_failure_cleanup(tmp_path, monkeypatch):
    store, item, scripts, _, _ = owned_fixture(tmp_path, monkeypatch, mode="bare")
    undeploy(store, item, lambda _: None)
    assert any("WorkingDirectory=" in s and "systemctl disable" in s for s in scripts)
    assert not any("pip uninstall" in s for s in scripts)
    item.update(mode="k8s", namespace="default")  # Failed before apply: clean only owned directory.
    scripts.clear()
    undeploy(store, item, lambda _: None)
    assert not any("kubectl delete" in s for s in scripts)


def test_k8s_checks_all_owners_before_deleting(tmp_path, monkeypatch):
    store, item, scripts, _, _ = owned_fixture(tmp_path, monkeypatch, mode="k8s")
    item.update(namespace="default", k8s_resources=["deployment/mock", "service/mock"])

    class Fake:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, script, **kwargs):
            scripts.append(script)
            if " get " in script:
                return json.dumps(
                    {
                        "metadata": {
                            "labels": {
                                LABEL: item["id"]
                                if "deployment/mock" in script
                                else "another-owner"
                            }
                        }
                    }
                )
            return ""

    monkeypatch.setattr("svc_panel.deployment_support.SSH", Fake)
    with pytest.raises(ValueError, match="归属"):
        undeploy(store, item, lambda _: None)
    assert not any(" delete " in s or "rm -rf" in s for s in scripts)
