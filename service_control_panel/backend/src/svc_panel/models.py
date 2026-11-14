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
    host_key_sha256: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}=?$")

    @model_validator(mode="after")
    def auth(self):
        if not self.password and not self.private_key:
            raise ValueError("请输入 SSH 密码或私钥")
        return self


class InstanceInput(BaseModel):
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
