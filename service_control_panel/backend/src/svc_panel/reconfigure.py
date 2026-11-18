"""Recreate an owned Docker container from its local, immutable image ID.

The durable journal lets the next redeploy/undeploy reconcile an interrupted swap.
Credentials in saved configurations remain sealed by Store. No archive is opened.
"""

import json
import re
import shlex
from uuid import uuid4

from .deployment_support import BASE, LABEL, docker_run_command, remote_directory
from .models import DockerOptions
from .remote import SSH

FIELDS = ("port", "base_url", "access", "admin_token", "docker", "docker_environment")
PRIVATE_FIELDS = {
    "admin_token",
    "docker_environment",
    "requested_configuration",
    "redeploy_recovery",
}


def configuration(instance):
    return {
        **{key: instance[key] for key in FIELDS if key in instance},
        "docker": instance.get("docker", {}),
        "docker_environment": instance.get("docker_environment"),
    }


def readable_configuration(store, instance):
    saved = instance.get("requested_configuration") or configuration(instance)
    docker = DockerOptions.model_validate(saved.get("docker", {})).model_dump()
    docker["environment"] = (
        json.loads(store.unseal(saved["docker_environment"]))
        if saved.get("docker_environment")
        else {}
    )
    return {
        **{key: saved.get(key, instance.get(key)) for key in ("port", "base_url", "access")},
        "admin_token": store.unseal(saved["admin_token"]),
        "docker": docker,
        "revision": instance.get("configuration_revision", 0),
    }


def sealed_configuration(store, body):
    value = body.model_dump(exclude={"revision"})
    value["admin_token"] = store.seal(value["admin_token"])
    value["docker_environment"] = store.seal(json.dumps(value["docker"].pop("environment")))
    return value


def reserved_instances(store):
    """Include both active and pending addresses when checking conflicts."""
    for instance in store.list("instance"):
        yield instance
        if (
            instance.get("state") == "redeploying" or instance.get("redeploy_recovery")
        ) and instance.get("requested_configuration"):
            yield {**instance, **instance["requested_configuration"]}
        if instance.get("redeploy_recovery"):
            yield {**instance, **instance["redeploy_recovery"]["old_configuration"]}


class DockerReconfigure:
    def __init__(self, store, log):
        self.store, self.log = store, log

    def remote(self, instance):
        return SSH(self.store.get("environment", instance["environment_id"]), self.store)

    def command(self, ssh, *args, timeout=90):
        return ssh.run(BASE + "$SUDO " + shlex.join(args), timeout=timeout)

    def inspect(self, ssh, name):
        names = self.command(ssh, "docker", "ps", "-a", "--format", "{{.Names}}")
        if name not in names.splitlines():
            return None
        return json.loads(self.command(ssh, "docker", "inspect", "--type", "container", name))[0]

    def owned(self, info, instance, expected=None):
        if info is None:
            return
        owned = (info.get("Config", {}).get("Labels") or {}).get(LABEL) == instance["id"]
        if not instance.get("resource_version"):
            owned = owned or any(
                m.get("Source") == remote_directory(instance) + "/data"
                and m.get("Destination") == "/svc/data"
                for m in info.get("Mounts", [])
            )
        if not owned or (expected and info["Id"] != expected):
            raise ValueError("容器归属或 ID 已变化，拒绝重新部署/恢复")

    def guard(self, ssh, instance):
        directory = remote_directory(instance)
        if not instance.get("resource_version") and directory != "/opt/svc-panel/" + instance["id"]:
            raise ValueError("旧实例目录无法确认归属")
        q = shlex.quote
        ssh.run(
            BASE
            + f"""
DIR={q(directory)}
test -d "$DIR" && test ! -L "$DIR" && test "$(readlink -f -- "$DIR")" = "$DIR" || {{ echo '原实例目录缺失或已重定向'; exit 1; }}
{":" if not instance.get("resource_version") else 'test "$(cat "$DIR/.panel-owner")" = ' + q(instance["id"])}
for folder in data logs traces; do test -d "$DIR/$folder" || {{ echo '持久化目录缺失'; exit 1; }}; done
""",
            timeout=30,
        )
        return directory

    def begin(self, instance):
        # A prior process may have died between remote mutations. Reconcile first.
        self.recover(instance)
        instance = self.store.get("instance", instance["id"])
        desired = {**instance, **instance["requested_configuration"]}
        with self.remote(instance) as ssh:
            directory = self.guard(ssh, instance)
            name = "mock-" + instance["id"]
            old = self.inspect(ssh, name)
            self.owned(old, instance, instance.get("container_id"))
            image = instance.get("loaded_image_id") or (old or {}).get("Image")
            if not image or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise ValueError("没有可复用的原镜像 ID；请检查原容器或重新创建实例")
            actual = self.command(
                ssh, "docker", "image", "inspect", "--format", "{{.Id}}", image
            ).strip()
            if actual != image:
                raise ValueError("原镜像已不可用，请恢复该镜像后重试")
            suffix = uuid4().hex[:12]
            candidate, backup = name + "-next-" + suffix, name + "-previous-" + suffix
            if self.inspect(ssh, candidate) or self.inspect(ssh, backup):
                raise ValueError("临时容器名称冲突")
            journal = {
                "old_configuration": configuration(instance),
                "old_id": old["Id"] if old else None,
                "old_running": bool(old and old["State"].get("Running")),
                "candidate": candidate,
                "backup": backup,
                "new_id": None,
                "env_file": directory + "/service.env." + suffix,
                "stage": "preparing",
            }
            self.store.patch(
                "instance", instance["id"], redeploy_recovery=journal, loaded_image_id=image
            )
            variables = {
                "MOCK_INSTANCE_ID": instance["id"],
                "MOCK_ADMIN_TOKEN": self.store.unseal(desired["admin_token"]),
                "MOCK_DATA_DIR": "/svc/data",
                "MOCK_LOG_DIR": "/svc/logs",
                "MOCK_TRACE_DIR": "/svc/traces",
                **json.loads(self.store.unseal(desired["docker_environment"])),
            }
            ssh.write(
                journal["env_file"], "\n".join(f"{k}={v}" for k, v in variables.items()) + "\n"
            )
            self.log("复用远端原镜像和持久化目录，创建替代容器（不上传、不加载镜像包）")
            ssh.run(
                docker_run_command(
                    desired,
                    image,
                    directory,
                    name=candidate,
                    create=True,
                    env_file=journal["env_file"],
                )
            )
            new = self.inspect(ssh, candidate)
            if not new:
                raise RuntimeError("替代容器创建后无法读取")
            self.owned(new, instance)
            journal.update(new_id=new["Id"], stage="swapping")
            self.store.patch("instance", instance["id"], redeploy_recovery=journal)
            if old:
                self.log("停止旧容器并保留，准备切换")
                self.command(ssh, "docker", "stop", old["Id"])
                self.command(ssh, "docker", "rename", old["Id"], backup)
            self.command(ssh, "docker", "rename", new["Id"], name)
            self.store.patch(
                "instance", instance["id"], **configuration(desired), container_id=new["Id"]
            )
            self.command(ssh, "docker", "start", new["Id"])

    def commit(self, instance):
        instance = self.store.get("instance", instance["id"])
        journal = instance.get("redeploy_recovery")
        if not journal:
            return
        journal["stage"] = "committing"
        self.store.patch("instance", instance["id"], redeploy_recovery=journal)
        with self.remote(instance) as ssh:
            directory = self.guard(ssh, instance)
            new = self.inspect(ssh, "mock-" + instance["id"])
            if not new:
                raise RuntimeError("新容器已缺失，无法完成提交")
            self.owned(new, instance, journal["new_id"])
            old = self.inspect(ssh, journal["backup"])
            self.owned(old, instance, journal["old_id"])
            if old:
                self.command(ssh, "docker", "rm", old["Id"])
            # Idempotent if Panel stopped after the move and before journal cleanup.
            ssh.run(
                BASE
                + f"if test -f {shlex.quote(journal['env_file'])}; then mv -- {shlex.quote(journal['env_file'])} {shlex.quote(directory + '/service.env')}; fi"
            )
        self.store.patch("instance", instance["id"], redeploy_recovery=None)
        self.log("新配置已生效，旧容器已清理；原镜像和持久化数据保留")

    def recover(self, instance):
        instance = self.store.get("instance", instance["id"])
        journal = instance.get("redeploy_recovery")
        if not journal:
            return False
        if journal["stage"] == "committing":
            self.commit(instance)
            return True
        with self.remote(instance) as ssh:
            self.guard(ssh, instance)
            name = "mock-" + instance["id"]
            canonical = self.inspect(ssh, name)
            candidate = self.inspect(ssh, journal["candidate"])
            backup = self.inspect(ssh, journal["backup"])
            # Validate every target before removing any resource.
            self.owned(
                canonical,
                instance,
                journal["old_id"]
                if canonical and canonical["Id"] == journal["old_id"]
                else journal["new_id"],
            )
            self.owned(candidate, instance, journal["new_id"])
            self.owned(backup, instance, journal["old_id"])
            if canonical and canonical["Id"] not in {journal["old_id"], journal["new_id"]}:
                raise ValueError("实例名称已被其他容器占用，无法恢复")
            if canonical and canonical["Id"] == journal["new_id"]:
                self.command(ssh, "docker", "rm", "-f", canonical["Id"])
            if candidate:
                self.command(ssh, "docker", "rm", "-f", candidate["Id"])
            if backup:
                self.command(ssh, "docker", "rename", backup["Id"], name)
            old = backup or (
                canonical if canonical and canonical["Id"] == journal["old_id"] else None
            )
            if journal["old_id"] and not old:
                raise RuntimeError("旧容器已缺失，无法自动恢复")
            if old and journal["old_running"]:
                self.command(ssh, "docker", "start", old["Id"])
            self.command(ssh, "rm", "-f", "--", journal["env_file"])
        self.store.patch(
            "instance",
            instance["id"],
            **journal["old_configuration"],
            container_id=journal["old_id"],
            redeploy_recovery=None,
            health="unknown",
        )
        self.log("已恢复原容器及生效配置；本次提交的参数仍保留供下次修正")
        return True
