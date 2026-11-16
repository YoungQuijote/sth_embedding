from __future__ import annotations

import json
import re
import shlex
import tarfile
import time
from pathlib import Path, PurePosixPath

import yaml

from .deployment_support import (
    BASE,
    LABEL,
    docker_disk_check,
    docker_run_command,
    prepare_directory,
    remote_directory,
)
from .remote import SSH


def validate_source(path):
    with tarfile.open(path) as archive:
        total = 0
        found = False
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError("源码包仅允许普通文件和目录，不允许绝对路径、链接或特殊文件")
            total += member.size
            if total > 4 * 1024**3:
                raise ValueError("源码解包大小超过 4 GiB")
            found |= name.name == "pyproject.toml" and len(name.parts) <= 2
        if not found:
            raise ValueError("源码包根目录或单层顶层目录需要包含 pyproject.toml")
        return total


def validate_manifest(path, instance):
    documents = list(yaml.safe_load_all(Path(path).read_text()))
    deployments = []
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("kind") not in {
            "Deployment",
            "Service",
            "ConfigMap",
            "Secret",
            "PersistentVolumeClaim",
        }:
            raise ValueError("V1 清单仅支持 Deployment、Service、ConfigMap、Secret、PVC")
        meta = doc.get("metadata", {})
        if meta.get("namespace", "default") != instance["namespace"]:
            raise ValueError("清单 namespace 必须与表单一致（未填写视为 default）")
        if doc["kind"] == "Deployment":
            deployments.append(doc)
    if len(deployments) != 1 or deployments[0]["metadata"]["name"] != instance["deployment"]:
        raise ValueError("V1 清单需要且仅允许一个名称匹配的 Deployment")
    spec = deployments[0]["spec"]["template"]["spec"]
    selected = (
        spec.get("nodeName") == instance["node"]
        or spec.get("nodeSelector", {}).get("kubernetes.io/hostname") == instance["node"]
    )
    if not selected:
        raise ValueError(
            "Deployment 必须用 nodeName 或 kubernetes.io/hostname nodeSelector 指向指定节点"
        )
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    if not containers or any(
        c.get("imagePullPolicy") not in {"Never", "IfNotPresent"} for c in containers
    ):
        raise ValueError("每个容器须显式设置 imagePullPolicy: Never 或 IfNotPresent")
    return documents


class Deployer:
    def __init__(self, store, log, progress=None):
        self.store = store
        self.log = log
        self.progress = progress or (lambda **kwargs: None)

    def command(self, ssh, label, script, timeout=900):
        self.log(label)
        started = time.monotonic()
        self.progress(stage=label, stage_started_at=time.time())
        result = ssh.run(script, timeout=timeout)
        self.log(f"{label}完成，耗时 {time.monotonic() - started:.1f} 秒")
        return result

    def transfer(self, ssh, artifact, destination):
        started, last = time.monotonic(), [0.0]
        self.log("Panel → 远端：" + artifact["filename"])

        def report(sent, total):
            now = time.monotonic()
            if now - last[0] >= 1 or sent == total:
                elapsed = max(0.001, now - started)
                self.progress(
                    stage="Panel → 远端传输",
                    transferred_bytes=sent,
                    total_bytes=total,
                    elapsed_seconds=elapsed,
                    rate_bytes_second=sent / elapsed,
                )
                last[0] = now

        ssh.upload(artifact["path"], destination, callback=report)
        self.log(f"远端传输完成，耗时 {time.monotonic() - started:.1f} 秒")

    def run(self, instance, restart=False):
        env = self.store.get("environment", instance["environment_id"])
        artifact = self.store.get("artifact", instance["artifact_id"])
        mode = instance["mode"]
        directory = remote_directory(instance)
        name = "mock-" + instance["id"]
        q = shlex.quote
        self.log("连接 SSH 环境")
        with SSH(env, self.store) as ssh:
            self.log("检查 Linux、操作权限和运行环境")
            base = BASE
            preflight = {
                "bare": 'command -v systemctl >/dev/null; test -d /run/systemd/system; PY=$(command -v python3 || command -v python || true); test -n "$PY" || { echo \'缺少 Python\'; exit 1; }; "$PY" -c \'import sys; assert sys.version_info >= (3,11), "需要 Python >=3.11"\'',
                "docker": "$SUDO docker info >/dev/null",
                "k8s": "command -v kubectl >/dev/null; kubectl cluster-info >/dev/null; kubectl auth can-i create deployments -n "
                + q(instance["namespace"])
                + " | grep -qx yes",
            }[mode]
            ssh.run(base + preflight, timeout=60)
            if restart:
                self.log("执行重启")
                command = {
                    "bare": "$SUDO systemctl restart " + q(name),
                    "docker": "$SUDO docker update --restart "
                    + q(
                        "no"
                        if instance.get("docker", {}).get("max_restarts", 3) == 0
                        else "on-failure:" + str(instance.get("docker", {}).get("max_restarts", 3))
                    )
                    + " "
                    + q(name)
                    + "; $SUDO docker restart "
                    + q(name),
                    "k8s": "kubectl -n "
                    + q(instance["namespace"])
                    + " rollout restart deployment/"
                    + q(instance["deployment"])
                    + "; kubectl -n "
                    + q(instance["namespace"])
                    + " rollout status deployment/"
                    + q(instance["deployment"])
                    + " --timeout=180s",
                }[mode]
                ssh.run(base + command, timeout=240)
                return
            if mode == "bare":
                artifact["expanded_bytes"] = validate_source(artifact["path"])
            if mode == "k8s":
                manifest = self.store.get("artifact", instance["manifest_id"])
                documents = validate_manifest(manifest["path"], instance)
                # Loading a local archive must happen on the selected cluster node.
                node = q(instance["node"])
                ssh.run(
                    base
                    + "NODE_IPS=$(kubectl get node "
                    + node
                    + ' -o jsonpath=\'{.status.addresses[?(@.type=="InternalIP")].address}\'); LOCAL_IPS=$(hostname -I); FOUND=0; for ip in $NODE_IPS; do for local_ip in $LOCAL_IPS; do [ "$ip" != "$local_ip" ] || FOUND=1; done; done; [ $FOUND = 1 ] || { echo \'SSH 目标必须是指定的 K8s 节点\'; exit 1; }',
                    timeout=60,
                )
            self.log("检查远端目录权限与空间")
            directory = prepare_directory(ssh, instance, artifact, self.store)
            if mode == "docker":
                docker_disk_check(ssh, artifact["size_bytes"])
            self.transfer(ssh, artifact, directory + "/artifact.tar")
            if mode == "k8s":
                for doc in documents:
                    doc.setdefault("metadata", {}).setdefault("labels", {})[LABEL] = instance["id"]
                resources = [
                    doc["kind"].lower() + "/" + doc["metadata"]["name"] for doc in documents
                ]
                ssh.write(directory + "/deployment.yml", yaml.safe_dump_all(documents))
                existing = ssh.run(
                    base
                    + "kubectl get -f "
                    + q(directory + "/deployment.yml")
                    + " --ignore-not-found -o name",
                    timeout=60,
                )
                if existing.strip():
                    raise ValueError(
                        "清单包含已存在资源，请使用接入已有实例，避免覆盖资源：" + existing
                    )
                self.store.patch("instance", instance["id"], k8s_resources=resources)

                loader = {
                    "docker": "$SUDO docker load -i ",
                    "containerd": "$SUDO ctr -n k8s.io images import ",
                    "k3s": "$SUDO k3s ctr -n k8s.io images import ",
                }[instance["image_loader"]]
                self.command(
                    ssh, "加载节点镜像", base + loader + q(directory + "/artifact.tar"), timeout=900
                )
                self.command(
                    ssh,
                    "应用 K8s 清单并等待就绪",
                    base
                    + "kubectl apply -f "
                    + q(directory + "/deployment.yml")
                    + "\nkubectl -n "
                    + q(instance["namespace"])
                    + " rollout status deployment/"
                    + q(instance["deployment"])
                    + " --timeout=180s",
                    timeout=240,
                )
                return
            ssh.run(
                base
                + "mkdir -p "
                + " ".join(q(directory + "/" + p) for p in ("data", "logs", "traces"))
            )
            token = self.store.unseal(instance["admin_token"])
            variables = {
                "MOCK_INSTANCE_ID": instance["id"],
                "MOCK_ADMIN_TOKEN": token,
                "MOCK_DATA_DIR": directory + "/data",
                "MOCK_LOG_DIR": directory + "/logs",
                "MOCK_TRACE_DIR": directory + "/traces",
            }
            if mode == "docker":
                variables.update(
                    MOCK_DATA_DIR="/svc/data",
                    MOCK_LOG_DIR="/svc/logs",
                    MOCK_TRACE_DIR="/svc/traces",
                )
            if instance.get("docker_environment"):
                variables.update(json.loads(self.store.unseal(instance["docker_environment"])))
            ssh.write(
                directory + "/service.env",
                "\n".join(f"{k}={v}" for k, v in variables.items()) + "\n",
            )
            if mode == "docker":
                docker_disk_check(ssh, artifact["size_bytes"])
                output = self.command(
                    ssh,
                    "加载 Docker 镜像",
                    base + "$SUDO docker load -i " + q(directory + "/artifact.tar"),
                )
                images = [
                    line.split(": ", 1)[1].strip()
                    for line in output.splitlines()
                    if line.startswith("Loaded image: ") or line.startswith("Loaded image ID: ")
                ]
                image = instance["image_ref"]
                if not image:
                    if len(images) != 1:
                        raise ValueError("镜像包含多个或无法识别的镜像，请明确填写 image reference")
                    image = images[0]
                elif image not in images:
                    raise ValueError("指定镜像不在本次加载结果中")
                output = self.command(
                    ssh, "创建并启动容器", docker_run_command(instance, image, directory)
                )
                identifiers = [
                    line.strip()
                    for line in output.splitlines()
                    if re.fullmatch(r"[a-f0-9]{64}", line.strip())
                ]
                if len(identifiers) != 1:
                    raise RuntimeError("容器已提交启动但未返回唯一 ID，请检查远端状态：" + output)
                container_id = identifiers[0]
                self.store.patch(
                    "instance", instance["id"], container_id=container_id, loaded_image=image
                )
            else:
                self.log("解包源码，选择 conda / venv / 系统 Python 并安装依赖")
                extraction = """import tarfile, pathlib
root=pathlib.Path("source").resolve()
root.mkdir(exist_ok=True)
with tarfile.open("artifact.tar") as archive:
    members=archive.getmembers()
    for m in members:
        p=pathlib.PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()): raise ValueError("unsafe source archive")
    archive.extractall(root, members=members)
projects=list(root.glob("pyproject.toml"))+list(root.glob("*/pyproject.toml"))
if len(projects)!=1: raise ValueError("expected one Python project")
pathlib.Path("project-path").write_text(str(projects[0].parent))
"""
                ssh.write(directory + "/extract.py", extraction)
                setup = (
                    base
                    + "cd "
                    + q(directory)
                    + """
PY=$(command -v python3 || command -v python)
"$PY" extract.py
if command -v conda >/dev/null 2>&1; then
  conda create --yes --prefix "$PWD/python-env" python=3.11
  PY="$PWD/python-env/bin/python"; INSTALL_MODE=conda
elif "$PY" -m venv "$PWD/python-env"; then
  PY="$PWD/python-env/bin/python"; INSTALL_MODE=venv
else
  INSTALL_MODE=system
fi
"$PY" -m pip install "$(cat project-path)[api]"
printf '%s' "$PY" > python-path
printf '%s' "$INSTALL_MODE" > install-mode
"""
                )
                self.command(ssh, "安装 Python 环境及依赖", setup, timeout=1800)
                python = ssh.run("cat " + q(directory + "/python-path")).strip()
                install_mode = ssh.run("cat " + q(directory + "/install-mode")).strip()
                self.log("Python 安装方式：" + install_mode)
                instance["python_environment"] = install_mode
                self.store.patch("instance", instance["id"], python_environment=install_mode)
                unit = f"""[Unit]\nDescription=Mock service {instance["id"]}\nAfter=network.target\nStartLimitIntervalSec=120\nStartLimitBurst=3\n[Service]\nType=simple\nUser={env["username"]}\nWorkingDirectory={directory}\nEnvironmentFile="{directory}/service.env"\nExecStart="{python}" -m uvicorn agent_mock_service.app_service:mock_service_app --host 0.0.0.0 --port {instance["port"]}\nRestart=on-failure\nRestartSec=3\n[Install]\nWantedBy=multi-user.target\n"""
                ssh.write(directory + "/service.unit", unit)
                self.log("注册并启动 systemd 服务")
                ssh.run(
                    base
                    + "$SUDO install -m 644 "
                    + q(directory + "/service.unit")
                    + " /etc/systemd/system/"
                    + q(name + ".service")
                    + "\n$SUDO systemctl daemon-reload\n$SUDO systemctl enable --now "
                    + q(name)
                )
