import re
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class EnvironmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    os: Literal["linux"] = "linux"
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.:_-]+$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(default="", max_length=4096)
    private_key: str = Field(default="", max_length=32768)
    host_key_sha256: str = Field(default="", pattern=r"^(SHA256:[A-Za-z0-9+/]{43}=?)?$")

    @model_validator(mode="after")
    def auth(self):
        if not self.password and not self.private_key:
            raise ValueError("请输入 SSH 密码或私钥")
        return self


class DockerMount(BaseModel):
    source: str
    target: str
    read_only: bool = True

    @model_validator(mode="after")
    def paths(self):
        for path in (self.source, self.target):
            if not re.fullmatch(r"/[A-Za-z0-9_./ -]+", path) or ".." in PurePosixPath(path).parts:
                raise ValueError("挂载路径须为 Linux 绝对路径，不含 .. 或特殊字符")
        self.source = str(PurePosixPath("/" + self.source.lstrip("/")))
        self.target = str(PurePosixPath("/" + self.target.lstrip("/")))
        if (
            str(PurePosixPath(self.target)) == "/"
            or self.target.rstrip("/") == "/svc"
            or self.target.startswith("/svc/")
        ):
            raise ValueError("挂载目标不可覆盖 / 或 Panel 管理的 /svc 目录")
        return self


class DockerOptions(BaseModel):
    command_mode: Literal["service", "image", "custom"] = "service"
    entrypoint: str | None = Field(default=None, max_length=1000)
    command: list[str] = Field(default_factory=list, max_length=100)
    environment: dict[str, str] = Field(default_factory=dict, max_length=100)
    extra_args: list[str] = Field(default_factory=list, max_length=100)
    container_port: int = Field(default=32882, ge=1, le=65535)
    max_restarts: int = Field(default=3, ge=0, le=10)
    mounts: list[DockerMount] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_options(self):
        for key, value in self.environment.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in {
                "MOCK_ADMIN_TOKEN",
                "MOCK_INSTANCE_ID",
                "MOCK_DATA_DIR",
                "MOCK_LOG_DIR",
                "MOCK_TRACE_DIR",
            }:
                raise ValueError(
                    "环境变量名无效，管理令牌、实例 ID 和数据/日志/Trace 目录由 Panel 管理"
                )
            if any(c in value for c in "\r\n\x00"):
                raise ValueError("环境变量值不支持换行或 NUL")
        # Structured argv, never shell text. Lifecycle/network defaults stay owned by Panel.
        allowed = {
            "--cpus",
            "--memory",
            "--memory-swap",
            "--pids-limit",
            "--shm-size",
            "--user",
            "--workdir",
            "--ulimit",
            "--log-driver",
            "--log-opt",
            "--add-host",
            "--dns",
            "--read-only",
            "--tmpfs",
            "--cap-drop",
            "--cap-add",
            "--security-opt",
            "--gpus",
        }
        i = 0
        while i < len(self.extra_args):
            arg = self.extra_args[i]
            flag, sep, value = arg.partition("=")
            if flag not in allowed:
                raise ValueError(
                    f"不支持 Docker 参数 {flag}；名称、端口、挂载、网络与重启策略由专用配置管理"
                )
            if flag == "--read-only":
                if sep and value not in {"true", "false"}:
                    raise ValueError("--read-only 必须为布尔值")
            elif not sep:
                i += 1
                if i >= len(self.extra_args) or self.extra_args[i].startswith("--"):
                    raise ValueError(f"{flag} 缺少参数值")
            elif not value:
                raise ValueError(f"{flag} 缺少参数值")
            i += 1
        for value in [*(self.command), *(self.extra_args), self.entrypoint or ""]:
            if len(value) > 4096 or "\x00" in value:
                raise ValueError("启动参数过长或含 NUL")
        if self.command_mode == "custom" and self.entrypoint is None and not self.command:
            raise ValueError("自定义启动至少填写 entrypoint 或 command")
        return self


class RemoteDirectoryInput(BaseModel):
    remote_root: str = Field(default="/opt/svc-panel", max_length=500)

    @field_validator("remote_root")
    @classmethod
    def directory(cls, value):
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./ -]+", value)
            or ".." in PurePosixPath(value).parts
            or str(PurePosixPath(value)) == "/"
        ):
            raise ValueError("部署根目录须为非根路径的 Linux 绝对路径，不含 .. 或特殊字符")
        return str(PurePosixPath(value))


class InstanceInput(RemoteDirectoryInput):
    name: str = Field(min_length=1, max_length=100)
    environment_id: str
    mode: Literal["bare", "docker", "k8s"]
    artifact_id: str
    port: int = Field(default=32882, ge=1, le=65535)
    base_url: str
    access: Literal["direct", "ssh"] = "direct"
    admin_token: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_.-]+$")
    image_ref: str = Field(default="", max_length=256, pattern=r"^[A-Za-z0-9_./:@-]*$")
    manifest_id: str | None = None
    node: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.-]*$")
    image_loader: Literal["docker", "containerd", "k3s"] = "containerd"
    namespace: str = Field(default="default", max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    deployment: str = Field(default="", max_length=253, pattern=r"^[a-z0-9.-]*$")
    docker: DockerOptions = Field(default_factory=DockerOptions)

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("服务地址必须为 http(s)://主机:端口，不含认证信息或路径")
        _ = parsed.port
        return value.rstrip("/")

    @model_validator(mode="after")
    def kubernetes(self):
        if self.mode == "k8s" and not (self.manifest_id and self.deployment and self.node):
            raise ValueError("K8s 需要清单、Deployment 名称及指定节点")
        if self.access == "ssh" and self.base_url.startswith("https:"):
            raise ValueError("V1 SSH 隧道访问使用 HTTP 服务地址；HTTPS 请使用直连")
        return self


class ConsoleInput(BaseModel):
    endpoint_id: str
    headers: dict[str, str] = Field(default_factory=dict)
    path_params: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: object = None


class AttachInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["bare", "docker", "k8s"]
    base_url: str
    admin_token: str = Field(min_length=1, max_length=4096, pattern=r"^[^\r\n]+$")
    environment_id: str | None = None
    access: Literal["direct", "ssh"] = "direct"
    version: str = Field(default="外部部署", max_length=200)

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        return InstanceInput.url(value)

    @model_validator(mode="after")
    def ssh_environment(self):
        if self.access == "ssh" and (not self.environment_id or self.base_url.startswith("https:")):
            raise ValueError("SSH 访问需选择环境并使用 HTTP 地址")
        return self
