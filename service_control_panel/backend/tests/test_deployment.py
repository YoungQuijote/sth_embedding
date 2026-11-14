import subprocess
from pathlib import Path

import pytest
import yaml
from test_panel import source_archive

from svc_panel.deploy import Deployer, validate_manifest
from svc_panel.store import Store


def manifest(node="node-a", pull="Never", namespace="default"):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "mock", "namespace": namespace},
        "spec": {
            "template": {
                "spec": {
                    "nodeName": node,
                    "containers": [{"name": "mock", "image": "mock:test", "imagePullPolicy": pull}],
                }
            }
        },
    }


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    source = tmp_path / "source.tar"
    source.write_bytes(source_archive())
    yml = tmp_path / "deployment.yml"
    yml.write_text(yaml.safe_dump(manifest()))
    store.put("environment", {"id": "env", "username": "tester"})
    store.put("artifact", {"id": "a", "path": str(source), "filename": "source.tar"})
    store.put("artifact", {"id": "m", "path": str(yml), "filename": "deployment.yml"})
    instance = {
        "id": "unit-1",
        "environment_id": "env",
        "artifact_id": "a",
        "admin_token": store.seal("mock-test-token-1234"),
        "port": 32882,
        "mode": "bare",
        "namespace": "default",
        "deployment": "mock",
        "manifest_id": "m",
        "node": "node-a",
        "image_loader": "containerd",
        "image_ref": "",
    }
    store.put("instance", instance)
    calls, writes, logs = [], {}, []

    class FakeSSH:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, script, timeout=900):
            checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            assert checked.returncode == 0, checked.stderr
            calls.append(script)
            if script.startswith("cat "):
                return (
                    "venv"
                    if "install-mode" in script
                    else "/opt/svc-panel/unit-1/python-env/bin/python"
                )
            if "docker load" in script:
                return "Loaded image: mock:test\n"
            return ""

        def upload(self, local, remote):
            assert Path(local).exists()

        def write(self, remote, value):
            writes[remote] = value

    monkeypatch.setattr("svc_panel.deploy.SSH", FakeSSH)
    return Deployer(store, logs.append), instance, calls, writes, logs, yml


def test_bare_install_and_systemd_commands(deployment):
    deployer, instance, calls, writes, logs, _ = deployment
    deployer.run(instance)
    scripts = "\n".join(calls)
    assert (
        scripts.index("command -v conda")
        < scripts.index('"$PY" -m venv')
        < scripts.index("INSTALL_MODE=system")
    )
    assert 'pip install "$(cat project-path)[api]"' in scripts
    assert "--break-system-packages" not in scripts
    unit = writes["/opt/svc-panel/unit-1/service.unit"]
    assert "User=tester" in unit and "--port 32882" in unit
    assert "MOCK_ADMIN_TOKEN=mock-test-token-1234" in writes["/opt/svc-panel/unit-1/service.env"]
    assert "mock-test-token-1234" not in scripts


def test_docker_load_and_persistent_mounts(deployment):
    deployer, instance, calls, writes, logs, _ = deployment
    instance["mode"] = "docker"
    deployer.run(instance)
    script = "\n".join(calls)
    assert "docker load" in script and "docker run -d" in script
    assert "32882:32882" in script and "/data:/svc/data" in script
    assert "--env-file" in script and "--restart unless-stopped" in script
    assert "--entrypoint python mock:test" in script


def test_explicit_image_must_be_loaded_from_this_archive(deployment):
    deployer, instance, *_ = deployment
    instance.update(mode="docker", image_ref="another:image")
    with pytest.raises(ValueError, match="本次加载"):
        deployer.run(instance)


@pytest.mark.parametrize(
    "loader,expected",
    [
        ("containerd", "ctr -n k8s.io images import"),
        ("k3s", "k3s ctr -n k8s.io images import"),
        ("docker", "docker load -i"),
    ],
)
def test_k8s_loads_selected_node_runtime_and_applies_manifest(deployment, loader, expected):
    deployer, instance, calls, *_ = deployment
    instance.update(mode="k8s", image_loader=loader)
    deployer.run(instance)
    script = "\n".join(calls)
    assert "hostname -I" in script and "kubectl get node node-a" in script
    assert expected in script and "kubectl apply -f" in script
    assert "rollout status deployment/mock --timeout=180s" in script
    assert "set env" not in script  # User's business manifest is not rewritten.


@pytest.mark.parametrize(
    "mode,command",
    [
        ("bare", "systemctl restart mock-unit-1"),
        ("docker", "docker restart mock-unit-1"),
        ("k8s", "rollout restart deployment/mock"),
    ],
)
def test_restart_uses_deployment_identity(deployment, mode, command):
    deployer, instance, calls, *_ = deployment
    instance["mode"] = mode
    deployer.run(instance, restart=True)
    assert command in "\n".join(calls)
    assert "docker load" not in "\n".join(calls)


@pytest.mark.parametrize(
    "doc",
    [
        manifest(node="wrong"),
        manifest(pull="Always"),
        manifest(namespace="other"),
        {"kind": "ClusterRole"},
    ],
)
def test_invalid_k8s_boundary_rejected(deployment, doc):
    _, instance, _, _, _, path = deployment
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError):
        validate_manifest(path, instance)
