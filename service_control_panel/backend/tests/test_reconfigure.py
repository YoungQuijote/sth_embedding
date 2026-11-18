import json
import shlex
import subprocess

import pytest
from test_panel import panel as panel

from svc_panel.models import RedeployInput
from svc_panel.reconfigure import DockerReconfigure, configuration, sealed_configuration
from svc_panel.store import Store


def body(**changes):
    return {
        "revision": 0,
        "port": 32900,
        "base_url": "http://127.0.0.1:32900",
        "access": "direct",
        "admin_token": "new-token-123456789",
        "docker": {"environment": {"SECRET": "private-env-value"}, "max_restarts": 0},
        **changes,
    }


def test_config_api_persists_requested_parameters_and_protects_identity(panel, monkeypatch):
    http, app, instance = panel
    store = app.state.store
    store.patch("instance", "i1", mode="docker")
    monkeypatch.setattr(app.state.service.executor, "submit", lambda *a: None)
    path = "/api/instances/i1"
    original = http.get(path + "/configuration")
    assert original.headers["cache-control"] == "no-store"
    assert original.json()["configuration"]["docker"]["max_restarts"] == 3
    assert http.get(path + "/configuration", headers={"Authorization": ""}).status_code == 401
    for forbidden in ("name", "artifact_id", "environment_id", "remote_root", "image_ref", "mode"):
        assert (
            http.post(path + "/redeploy", json=body(**{forbidden: "tampered"})).status_code == 422
        )
    assert http.post(path + "/redeploy", json=body(revision=99)).status_code == 409
    assert (
        http.post(
            path + "/redeploy", json=body(access="ssh", base_url="https://localhost")
        ).status_code
        == 422
    )
    result = http.post(path + "/redeploy", json=body())
    assert result.status_code == 202, result.text
    assert result.json()["operation"] == "redeploy"
    updated = store.get("instance", "i1")
    assert (
        updated["base_url"] == instance["base_url"]
    )  # Effective configuration unchanged until swap.
    assert updated["configuration_revision"] == 1
    assert "private-env-value" not in json.dumps(updated)
    assert "new-token-123456789" not in json.dumps(updated)
    fetched = http.get(path + "/configuration").json()["configuration"]
    assert fetched["docker"]["environment"] == {"SECRET": "private-env-value"}
    assert fetched["port"] == 32900 and fetched["revision"] == 1
    public = http.get("/api/instances").text + http.get("/api/jobs").text
    assert "requested_configuration" not in public and "private-env-value" not in public
    assert "new-token-123456789" not in public
    assert http.post(path + "/redeploy", json=body(revision=1)).status_code == 409
    assert store.get("instance", "i1")["configuration_revision"] == 1
    assert (
        app.state.service.safe_error("new-token-123456789 private-env-value", updated)
        == "[REDACTED] [REDACTED]"
    )


def test_redeploy_rejects_external_undeployed_modes_and_conflicts(panel, monkeypatch):
    http, app, item = panel
    store = app.state.store
    monkeypatch.setattr(app.state.service.executor, "submit", lambda *a: None)
    for fields in (
        {"mode": "bare"},
        {"mode": "docker", "origin": "attached"},
        {"origin": "panel", "state": "undeployed"},
    ):
        store.patch("instance", "i1", **fields)
        assert http.post("/api/instances/i1/redeploy", json=body()).status_code == 409
    store.patch("instance", "i1", mode="docker", state="deployed")
    store.put("instance", {**item, "id": "other", "port": 32900})
    assert http.post("/api/instances/i1/redeploy", json=body()).status_code == 409
    store.patch("instance", "other", port=33000, base_url="http://127.0.0.1:32900")
    assert http.post("/api/instances/i1/redeploy", json=body()).status_code == 409
    store.patch(
        "instance",
        "other",
        base_url="http://127.0.0.1:33000",
        state="redeploying",
        requested_configuration=sealed_configuration(store, RedeployInput(**body())),
    )
    assert http.post("/api/instances/i1/redeploy", json=body()).status_code == 409


@pytest.fixture
def engine(tmp_path, monkeypatch):
    store = Store(tmp_path)
    item = {
        "id": "docker-test",
        "mode": "docker",
        "environment_id": "e",
        "resource_version": 2,
        "admin_token": store.seal("old-token-123456789"),
        "docker_environment": store.seal("{}"),
        "docker": {},
        "port": 32882,
        "base_url": "http://localhost:32882",
        "access": "direct",
        "state": "deployed",
        "container_id": "a" * 64,
    }
    store.put(
        "instance",
        {**item, "requested_configuration": sealed_configuration(store, RedeployInput(**body()))},
    )
    store.put("environment", {"id": "e", "credentials": store.seal("{}")})
    containers = {
        "mock-docker-test": {
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "State": {"Running": True},
            "Config": {"Labels": {"io.mockcontrol.instance": item["id"]}},
        }
    }
    calls, writes, logs = [], {}, []
    fail = set()

    class SSH:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def write(self, path, data):
            writes[path] = data

        def run(self, script, timeout=90):
            assert (
                subprocess.run(
                    ["bash", "-n"], input=script, text=True, capture_output=True
                ).returncode
                == 0
            )
            calls.append(script)
            for marker in fail:
                if marker in script:
                    raise RuntimeError("injected failure: " + marker)
            command = script.splitlines()[-1]
            if not command.startswith("$SUDO docker "):
                return ""
            args = shlex.split(command)[2:]
            if args[0] == "ps":
                return "\n".join(containers)
            if args[0] == "inspect":
                return json.dumps([containers[args[-1]]])
            if args[0] == "image":
                return "sha256:" + "b" * 64
            if args[0] == "create":
                name = args[args.index("--name") + 1]
                containers[name] = {
                    "Id": "c" * 64,
                    "Image": "sha256:" + "b" * 64,
                    "State": {"Running": False},
                    "Config": {"Labels": {"io.mockcontrol.instance": item["id"]}},
                }
                return "c" * 64
            name, info = next(
                (k, v)
                for k, v in containers.items()
                if v["Id"] == args[-1 if args[0] != "rename" else -2]
            )
            if args[0] == "rename":
                containers[args[-1]] = containers.pop(name)
            if args[0] == "rm":
                containers.pop(name)
            if args[0] in {"start", "stop"}:
                info["State"]["Running"] = args[0] == "start"
            return ""

    monkeypatch.setattr("svc_panel.reconfigure.SSH", SSH)
    return store, item, DockerReconfigure(store, logs.append), containers, calls, writes, fail


def test_swap_and_commit_reuses_image_without_artifact_access(engine):
    store, item, worker, containers, calls, writes, fail = engine
    worker.begin(item)
    current = store.get("instance", item["id"])
    assert current["port"] == 32900 and len(containers) == 2
    assert not containers[current["redeploy_recovery"]["backup"]]["State"]["Running"]
    assert containers["mock-docker-test"]["State"]["Running"]
    assert "private-env-value" in next(iter(writes.values()))
    assert "private-env-value" not in "\n".join(calls)
    worker.commit(current)
    assert list(containers) == ["mock-docker-test"]
    assert containers["mock-docker-test"]["Id"] == "c" * 64
    assert store.get("instance", item["id"])["redeploy_recovery"] is None
    assert not any("docker load" in c or "artifact.tar" in c for c in calls)


@pytest.mark.parametrize("failure", ["docker create", "docker start"])
def test_failed_swap_recovers_original_and_keeps_requested_parameters(engine, failure):
    store, item, worker, containers, calls, writes, fail = engine
    fail.add(failure)
    with pytest.raises(RuntimeError):
        worker.begin(item)
    fail.clear()
    # A new worker represents recovery after an interrupted process as well.
    assert DockerReconfigure(store, lambda _: None).recover(item)
    current = store.get("instance", item["id"])
    assert configuration(current) == configuration(item)
    assert current["requested_configuration"]["port"] == 32900
    assert list(containers) == ["mock-docker-test"]
    assert containers["mock-docker-test"]["Id"] == "a" * 64
    assert containers["mock-docker-test"]["State"]["Running"]


def test_preflight_image_and_ownership_fail_before_stopping_old(engine):
    store, item, worker, containers, calls, writes, fail = engine
    fail.add("docker image inspect")
    with pytest.raises(RuntimeError):
        worker.begin(item)
    assert not any("docker stop" in c or "docker create" in c for c in calls)
    fail.clear()
    containers["mock-docker-test"]["Config"]["Labels"] = {}
    with pytest.raises(ValueError, match="归属"):
        worker.begin(item)
    assert not any("docker stop" in c for c in calls)


def test_commit_interruption_finishes_cleanup_without_rollback(engine):
    store, item, worker, containers, calls, writes, fail = engine
    worker.begin(item)
    fail.add("docker rm")
    with pytest.raises(RuntimeError):
        worker.commit(item)
    fail.clear()
    worker.recover(item)
    assert containers["mock-docker-test"]["Id"] == "c" * 64
    assert len(containers) == 1
    assert store.get("instance", item["id"])["port"] == 32900


def test_failed_initial_container_can_redeploy_with_pinned_image(engine):
    store, item, worker, containers, calls, writes, fail = engine
    containers.clear()
    store.patch("instance", item["id"], loaded_image_id="sha256:" + "b" * 64, container_id=None)
    worker.begin(item)
    worker.commit(item)
    assert list(containers) == ["mock-docker-test"]


@pytest.mark.parametrize("healthy", [True, False])
def test_job_waits_for_health_and_recovers_on_failure(engine, monkeypatch, healthy):
    from svc_panel.service import Service

    store, item, worker, containers, calls, writes, fail = engine
    service = Service(store)
    monkeypatch.setattr(
        service, "check", lambda _: {"health": "healthy" if healthy else "unhealthy"}
    )
    monkeypatch.setattr(
        "svc_panel.service.startup_diagnostics",
        lambda *_: "startup failed private-env-value new-token-123456789",
    )
    job = {
        "id": "job-test",
        "instance_id": item["id"],
        "operation": "redeploy",
        "status": "queued",
        "logs": [],
    }
    try:
        service.execute(job)
        assert job["status"] == ("succeeded" if healthy else "failed")
        current = store.get("instance", item["id"])
        assert current["port"] == (32900 if healthy else 32882)
        assert current["state"] == ("deployed" if healthy else "failed")
        assert current["requested_configuration"]["port"] == 32900
        assert current["redeploy_recovery"] is None
        assert len(containers) == 1
        assert containers["mock-docker-test"]["State"]["Running"]
        assert "private-env-value" not in json.dumps(job)
        assert "new-token-123456789" not in json.dumps(job)
    finally:
        service.close()
