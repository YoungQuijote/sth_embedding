"""Opt-in real Docker startup/cleanup check; local transport substitutes SSH.

PANEL_DOCKER_TEST_IMAGE must name a disposable image with Python/uvicorn and the
actual mock package. The test creates/removes only its UUID-named containers and
instance directories; it retains the supplied image. Upload/load/disk preflight
are covered separately and are not part of this rootless-engine test.
"""

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from svc_panel.deployment_support import BASE, docker_run_command, startup_diagnostics, undeploy
from svc_panel.models import RedeployInput
from svc_panel.reconfigure import DockerReconfigure, sealed_configuration
from svc_panel.store import Store


def test_real_docker_custom_startup_bounded_failure_and_cleanup(tmp_path, monkeypatch):
    image = os.getenv("PANEL_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("Set PANEL_DOCKER_TEST_IMAGE to run against a real Docker engine")
    # A separate public temp parent lets a rootless daemon owned by a different
    # local user bind the test files; no real credentials are placed here.
    root = Path(tempfile.mkdtemp(prefix="panel-docker-integration-"))
    root.chmod(0o755)
    store = Store(tmp_path / "panel")
    store.put("environment", {"id": "local", "username": "local"})

    class LocalTransport:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, remote, data):
            Path(remote).write_text(data)

        def run(self, script, timeout=900):
            # The test machine has rootless Docker and no sudo; SSH transport/base
            # privilege gate is the only substitution. Commands and files are real.
            script = script.replace(BASE, 'set -euo pipefail\nSUDO=""\n')
            result = subprocess.run(
                ["bash", "-c", script], text=True, capture_output=True, timeout=timeout
            )
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout + result.stderr

    monkeypatch.setattr("svc_panel.deployment_support.SSH", LocalTransport)
    monkeypatch.setattr("svc_panel.reconfigure.SSH", LocalTransport)
    token = "docker-lifecycle-token-123456"
    created = []
    try:
        for broken in (False, True):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            identifier = str(uuid4())
            created.append(identifier)
            options = {
                "command_mode": "custom",
                "entrypoint": "python",
                "max_restarts": 2,
                "command": (
                    ["-c", "import sys; sys.exit(7)"]
                    if broken
                    else [
                        "-m",
                        "uvicorn",
                        "agent_mock_service.app_service:mock_service_app",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "32882",
                    ]
                ),
            }
            item = {
                "id": identifier,
                "mode": "docker",
                "namespace": "default",
                "deployment": "",
                "environment_id": "local",
                "artifact_id": "image",
                "admin_token": store.seal(token),
                "port": port,
                "image_ref": image,
                "remote_root": str(root),
                "resource_version": 2,
                "docker": options,
                "state": "deploying",
            }
            directory = root / identifier
            directory.mkdir(mode=0o755)
            (directory / ".panel-owner").write_text(identifier)
            for folder in ("data", "logs", "traces"):
                (directory / folder).mkdir()
                (directory / folder).chmod(0o777)  # Disposable rootless-daemon fixture.
            (directory / "service.env").write_text(
                f"MOCK_ADMIN_TOKEN={token}\nMOCK_INSTANCE_ID={identifier}\nMOCK_DATA_DIR=/svc/data\nMOCK_LOG_DIR=/svc/logs\nMOCK_TRACE_DIR=/svc/traces\n"
            )
            item["remote_directory"] = str(directory)
            store.put("instance", item)
            container_id = (
                LocalTransport().run(docker_run_command(item, image, str(directory))).strip()
            )
            store.patch("instance", identifier, container_id=container_id)
            for _ in range(100):
                inspected = json.loads(
                    subprocess.check_output(["docker", "inspect", "mock-" + identifier])
                )[0]
                if broken:
                    if inspected["State"]["Status"] == "exited":
                        break
                else:
                    try:
                        with httpx.Client(trust_env=False) as client:
                            if (
                                client.get(
                                    f"http://127.0.0.1:{port}/ready",
                                    headers={"Authorization": "Bearer " + token},
                                ).status_code
                                == 200
                            ):
                                break
                    except httpx.TransportError:
                        pass
                time.sleep(0.2)
            else:
                pytest.fail("Container did not reach expected terminal/ready state")
            assert inspected["HostConfig"]["RestartPolicy"] == {
                "Name": "on-failure",
                "MaximumRetryCount": 2,
            }
            if broken:
                assert inspected["RestartCount"] == 2 and inspected["State"]["ExitCode"] == 7
                assert "exit=7" in startup_diagnostics(store, item)
            else:
                assert inspected["Config"]["Entrypoint"] == ["python"]
            if not broken:

                def ready(url, credential):
                    for _ in range(80):
                        try:
                            with httpx.Client(trust_env=False) as client:
                                if (
                                    client.get(
                                        url + "/management/v1/info",
                                        headers={"Authorization": "Bearer " + credential},
                                    ).status_code
                                    == 200
                                ):
                                    return
                        except httpx.TransportError:
                            pass
                        time.sleep(0.2)
                    pytest.fail("Reconfigured service did not become ready")

                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    changed_port = sock.getsockname()[1]
                next_token = "reconfigured-token-123456789"
                sentinel = directory / "data" / "retain.txt"
                sentinel.write_text("preserve my database")
                config = RedeployInput(
                    revision=0,
                    port=changed_port,
                    base_url=f"http://127.0.0.1:{changed_port}",
                    access="direct",
                    admin_token=next_token,
                    docker={
                        **options,
                        "environment": {"CUSTOM_OPTION": "reconfigured"},
                        "extra_args": ["--memory", "256m"],
                    },
                )
                store.patch(
                    "instance",
                    identifier,
                    base_url=f"http://127.0.0.1:{port}",
                    access="direct",
                    requested_configuration=sealed_configuration(store, config),
                )
                worker = DockerReconfigure(store, lambda _: None)
                worker.begin(item)
                ready(config.base_url, next_token)
                worker.commit(item)
                effective = json.loads(
                    subprocess.check_output(["docker", "inspect", "mock-" + identifier])
                )[0]
                assert effective["Id"] != container_id
                assert effective["Image"] == inspected["Image"]
                assert "CUSTOM_OPTION=reconfigured" in effective["Config"]["Env"]
                assert effective["HostConfig"]["Memory"] == 256 * 1024**2
                assert effective["HostConfig"]["PortBindings"]["32882/tcp"][0]["HostPort"] == str(
                    changed_port
                )
                assert sentinel.read_text() == "preserve my database"
                assert next_token in (directory / "service.env").read_text()
                # A bad executable must fail, restore the healthy container, and
                # retain the bad submitted parameters for the correction dialog.
                bad = config.model_copy(deep=True)
                bad.docker.entrypoint = "/does-not-exist"
                store.patch(
                    "instance", identifier, requested_configuration=sealed_configuration(store, bad)
                )
                with pytest.raises(RuntimeError):
                    worker.begin(store.get("instance", identifier))
                worker.recover(item)
                ready(config.base_url, next_token)
                restored = json.loads(
                    subprocess.check_output(["docker", "inspect", "mock-" + identifier])
                )[0]
                assert restored["Id"] == effective["Id"]
                assert (
                    store.get("instance", identifier)["requested_configuration"]["docker"][
                        "entrypoint"
                    ]
                    == "/does-not-exist"
                )
                assert sentinel.read_text() == "preserve my database"
            undeploy(store, store.get("instance", identifier), lambda _: None)
            assert not (root / identifier).exists()
            assert (
                subprocess.run(
                    ["docker", "inspect", "mock-" + identifier], capture_output=True
                ).returncode
                != 0
            )
            assert (
                subprocess.run(
                    ["docker", "image", "inspect", image], capture_output=True
                ).returncode
                == 0
            )
    finally:
        for identifier in created:
            ids = subprocess.check_output(
                ["docker", "ps", "-aq", "--filter", "label=io.mockcontrol.instance=" + identifier],
                text=True,
            ).split()
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        import shutil

        shutil.rmtree(root, ignore_errors=True)
