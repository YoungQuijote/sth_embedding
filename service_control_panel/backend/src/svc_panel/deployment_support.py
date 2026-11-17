import json
import re
import shlex
import time
from uuid import UUID

from .models import DockerOptions, RemoteDirectoryInput
from .remote import SSH

BASE = 'set -euo pipefail\n[ "$(uname -s)" = Linux ] || { echo "仅支持 Linux"; exit 1; }\nSUDO=""; if [ "$(id -u)" != 0 ]; then sudo -n true; SUDO="sudo -n"; fi\n'
LABEL = "io.mockcontrol.instance"
q = shlex.quote


def check_storage(ssh, root, size, mode):
    root = RemoteDirectoryInput(remote_root=root).remote_root
    needed = size + 64 * 1024**2 + (256 * 1024**2 if mode == "bare" else 0)
    output = ssh.run(
        BASE
        + f"""
ROOT={q(root)}
while ! $SUDO test -e "$ROOT"; do ROOT=$(dirname -- "$ROOT"); done
$SUDO test -d "$ROOT" && $SUDO test -w "$ROOT" && $SUDO test -x "$ROOT" || {{ echo '目标目录或其父目录不可写'; exit 1; }}
AVAILABLE=$($SUDO df -PB1 -- "$ROOT" | tail -1 | awk '{{print $4}}')
test "$AVAILABLE" -ge {needed} || {{ echo "远端磁盘空间不足：可用 $AVAILABLE 字节，需要至少 {needed} 字节"; exit 1; }}
printf '%s' "$AVAILABLE"
""",
        timeout=60,
    )
    if mode == "docker":
        docker_disk_check(ssh, size)
    return {"available_bytes": int(output.strip()), "required_bytes": needed}


def remote_directory(instance):
    identifier = instance["id"]
    if not re.fullmatch(r"[A-Za-z0-9-]{3,64}", identifier):
        raise ValueError("无效实例 ID")
    root = RemoteDirectoryInput(
        remote_root=instance.get("remote_root", "/opt/svc-panel")
    ).remote_root
    return instance.get("remote_directory") or root + "/" + identifier


def prepare_directory(ssh, instance, artifact, store):
    root = RemoteDirectoryInput(
        remote_root=instance.get("remote_root", "/opt/svc-panel")
    ).remote_root
    needed = artifact["size_bytes"] + (artifact.get("expanded_bytes") or 0) + 64 * 1024**2
    if instance["mode"] == "bare":
        needed += 256 * 1024**2  # Floor for venv/dependencies, not a promise of final install size.
    script = (
        BASE
        + f"""
ROOT={q(root)}
$SUDO mkdir -p -- "$ROOT"
ROOT=$(readlink -f -- "$ROOT")
test "$ROOT" != / || {{ echo '部署根目录不可解析为 /'; exit 1; }}
DIR="$ROOT/{instance["id"]}"
test ! -e "$DIR" && test ! -L "$DIR" || {{ echo '实例目录已存在，拒绝覆盖'; exit 1; }}
$SUDO test -w "$ROOT" && $SUDO test -x "$ROOT" || {{ echo '部署目录没有写入/访问权限'; exit 1; }}
AVAILABLE=$($SUDO df -PB1 -- "$ROOT" | tail -1 | awk '{{print $4}}')
test "$AVAILABLE" -ge {needed} || {{ echo "远端目录空间不足：可用 $AVAILABLE 字节，需要至少 {needed} 字节（文件及预留空间）"; exit 1; }}
$SUDO mkdir -- "$DIR"
$SUDO chown "$(id -u):$(id -g)" "$DIR"
chmod 700 "$DIR"
printf '%s' {q(instance["id"])} > "$DIR/.panel-owner"
PROBE=$(mktemp "$DIR/.write-test-XXXXXX")
rm -- "$PROBE"
printf 'PANEL_DIR=%s\\nPANEL_AVAILABLE=%s\\n' "$DIR" "$AVAILABLE"
"""
    )
    output = ssh.run(script, timeout=60)
    values = dict(
        line.split("=", 1)
        for line in output.splitlines()
        if line.startswith(("PANEL_DIR=", "PANEL_AVAILABLE="))
    )
    if "PANEL_DIR" not in values:
        raise RuntimeError("无法获取远程目录检查结果")
    directory = values["PANEL_DIR"]
    if not directory.endswith("/" + instance["id"]):
        raise ValueError("远端目录返回值不符合实例目录约定")
    instance.update(remote_directory=directory, resource_version=2)
    store.patch(
        "instance",
        instance["id"],
        remote_directory=directory,
        resource_version=2,
        disk_available_bytes=int(values["PANEL_AVAILABLE"]),
        disk_required_bytes=needed,
    )
    return directory


def docker_disk_check(ssh, size):
    return ssh.run(
        BASE
        + f"""
ROOT=$($SUDO docker info --format '{{{{.DockerRootDir}}}}')
AVAILABLE=$($SUDO df -PB1 -- "$ROOT" | tail -1 | awk '{{print $4}}')
test "$AVAILABLE" -ge {size + 64 * 1024**2} || {{ echo "Docker 数据目录空间不足：可用 $AVAILABLE 字节"; exit 1; }}
""",
        timeout=60,
    )


def docker_run_command(instance, image, directory):
    options = DockerOptions.model_validate(instance.get("docker", {}))
    restart = "no" if options.max_restarts == 0 else f"on-failure:{options.max_restarts}"
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        "mock-" + instance["id"],
        "--label",
        LABEL + "=" + instance["id"],
        "--restart",
        restart,
        "--env-file",
        directory + "/service.env",
        "-p",
        f"{instance['port']}:{options.container_port}",
    ]
    for folder in ("data", "logs", "traces"):
        args += ["-v", directory + "/" + folder + ":/svc/" + folder]
    args += options.extra_args
    for mount in options.mounts:
        args += [
            "--mount",
            f"type=bind,source={mount.source},target={mount.target}"
            + (",readonly" if mount.read_only else ""),
        ]
    command = []
    if options.command_mode == "service":
        args += ["--entrypoint", "python"]
        command = [
            "-m",
            "uvicorn",
            "agent_mock_service.app_service:mock_service_app",
            "--host",
            "0.0.0.0",
            "--port",
            str(options.container_port),
        ]
    elif options.command_mode == "custom":
        if options.entrypoint is not None:
            args += ["--entrypoint", options.entrypoint]
        command = options.command
    return BASE + "$SUDO " + shlex.join(args + [image] + command)


def inspect_docker(ssh, instance):
    raw = ssh.run(
        BASE + "$SUDO docker inspect --type container " + q("mock-" + instance["id"]), timeout=30
    )
    return json.loads(raw)[0]


def startup_diagnostics(store, instance):
    if instance["mode"] != "docker" or instance.get("origin") == "attached":
        return None
    with SSH(store.get("environment", instance["environment_id"]), store) as ssh:
        info = inspect_docker(ssh, instance)
        state = info["State"]
        if state["Status"] not in {"exited", "dead"}:
            return None
        logs = ssh.run(
            BASE + "$SUDO docker logs --tail 60 " + q("mock-" + instance["id"]), timeout=30
        )
        return f"容器已停止：exit={state.get('ExitCode')}，重启次数={info.get('RestartCount', 0)}，错误={state.get('Error', '')}\n{logs}"


def undeploy(store, instance, log):
    if instance.get("origin") == "attached":
        raise ValueError("接入的外部实例不归 Panel 部署所有，仅支持移除监测")
    directory = remote_directory(instance)
    # Only an exact per-instance leaf is ever removed, never a caller-supplied root.
    identifier = str(UUID(instance["id"]))
    if (
        not directory.startswith("/")
        or not directory.endswith("/" + identifier)
        or "/../" in directory
    ):
        raise ValueError("实例目录校验失败")
    legacy = not instance.get("resource_version")
    if legacy and directory != "/opt/svc-panel/" + identifier:
        raise ValueError("旧实例只支持清理其默认独立目录")
    name = "mock-" + identifier
    with SSH(store.get("environment", instance["environment_id"]), store) as ssh:
        log("检查实例资源归属")
        guard = (
            BASE
            + f"""
DIR={q(directory)}
test ! -L "$DIR" || {{ echo '实例目录已变为符号链接，拒绝清理'; exit 1; }}
if test -e "$DIR"; then
  test "$(readlink -f -- "$DIR")" = "$DIR" || {{ echo '实例目录已重定向'; exit 1; }}
  {":" if legacy else '$SUDO test -f "$DIR/.panel-owner" && test "$($SUDO cat "$DIR/.panel-owner")" = ' + q(identifier)}
fi
"""
        )
        ssh.run(guard, timeout=60)
        if instance["mode"] == "docker":
            output = ssh.run(BASE + "$SUDO docker ps -a --format '{{.Names}}'", timeout=30)
            if name in output.splitlines():
                info = inspect_docker(ssh, instance)
                owned = (info.get("Config", {}).get("Labels") or {}).get(LABEL) == identifier
                if legacy:
                    owned = any(
                        m.get("Source") == directory + "/data"
                        and m.get("Destination") == "/svc/data"
                        for m in info.get("Mounts", [])
                    )
                if not owned or (
                    instance.get("container_id") and info["Id"] != instance["container_id"]
                ):
                    raise ValueError("容器归属或 ID 已变化，拒绝卸载")
                log("停止并删除实例容器；保留可共享的镜像")
                ssh.run(BASE + "$SUDO docker rm -f " + q(name), timeout=90)
        elif instance["mode"] == "bare":
            unit = "/etc/systemd/system/" + name + ".service"
            log("停用并移除 systemd 单元")
            ssh.run(
                BASE
                + f"""
if $SUDO test -f {q(unit)}; then
  $SUDO grep -Fx -- {q("WorkingDirectory=" + directory)} {q(unit)} >/dev/null || {{ echo 'systemd 单元不属于该实例'; exit 1; }}
  $SUDO systemctl stop {q(name)}
  $SUDO systemctl disable {q(name)}
  $SUDO rm -- {q(unit)}
  $SUDO systemctl daemon-reload
fi
""",
                timeout=90,
            )
            log("保留系统 Python 的共享包；独立 conda/venv 随实例目录清理")
        else:
            resources = instance.get("k8s_resources")
            if not resources and legacy:
                raise ValueError("此旧 K8s 实例未记录资源归属，请手动清理后移除监测")
            owned_resources = []
            for resource in resources or []:
                route = q(resource)
                namespace = q(instance["namespace"])
                raw = ssh.run(
                    BASE + f"kubectl -n {namespace} get {route} --ignore-not-found -o json",
                    timeout=30,
                )
                if raw.strip():
                    value = json.loads(raw)
                    if value.get("metadata", {}).get("labels", {}).get(LABEL) != identifier:
                        raise ValueError("K8s 资源归属已变化：" + resource)
                    owned_resources.append(resource)
            for resource in owned_resources:
                log("删除 K8s 资源 " + resource)
                ssh.run(
                    BASE
                    + f"kubectl -n {q(instance['namespace'])} delete {q(resource)} --ignore-not-found --wait=true --timeout=90s",
                    timeout=100,
                )
        log("清理实例独立目录（上传包、配置、数据、日志和 trace）")
        ssh.run(guard + "\n$SUDO rm -rf -- " + q(directory), timeout=120)
        store.patch(
            "instance",
            identifier,
            state="undeployed",
            health="unknown",
            undeployed_at=time.time(),
            health_error=None,
        )
        store.resolve(identifier, "health")
        store.resolve(identifier, "monitoring")
