from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import AdminAuth
from .deployment_support import check_storage
from .feedback import feedback_router
from .models import (
    AttachInput,
    ConsoleInput,
    EnvironmentInput,
    InstanceInput,
    PasswordChangeInput,
    RemoteDirectoryInput,
)
from .remote import SSH, require_json
from .service import Service
from .store import Store
from .uploads import receive_upload, upload_limits


def public_instance(instance):
    return {k: v for k, v in instance.items() if k not in {"admin_token", "docker_environment"}}


def create_app(data_dir=None, admin_token=None, background=True):
    store = Store(data_dir or os.environ.get("PANEL_DATA_DIR", ".panel"))
    auth = AdminAuth(store, admin_token)
    interval = max(5, int(os.environ.get("PANEL_POLL_INTERVAL", "30")))
    service = Service(store, interval)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            service.start()
        yield
        service.close()

    app = FastAPI(
        title="Mock Service Control Panel",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.service = store, service

    def authenticated(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or not auth.verify(authorization[7:]):
            raise HTTPException(401, "请使用管理员登录密码或访问令牌登录")

    api = APIRouter(prefix="/api", dependencies=[Depends(authenticated)])

    def get(kind, identifier):
        item = store.get(kind, identifier)
        if not item or (kind == "instance" and not item.get("monitoring_enabled", True)):
            raise HTTPException(404, "资源不存在")
        return item

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/api/account/password":
            # Pydantic's default detail includes input values, including password fields.
            return JSONResponse(
                {"detail": "请检查密码：" + "；".join(e["msg"] for e in error.errors())},
                status_code=422,
            )
        return await request_validation_exception_handler(request, error)

    @api.get("/session")
    def session():
        return {"user": "管理员", "monitoring": "builtin", "poll_interval": interval}

    @api.post("/account/password")
    def change_password(body: PasswordChangeInput):
        with service.lock:
            if not auth.change(body.current_password, body.new_password):
                raise HTTPException(403, "当前登录密码或访问令牌不正确")
        return {"changed": True, "reauthenticate": True}

    @api.get("/environments")
    def environments():
        return [
            {k: v for k, v in e.items() if k != "credentials"} for e in store.list("environment")
        ]

    @api.post("/environments", status_code=201)
    def create_environment(body: EnvironmentInput):
        data = body.model_dump()
        secret = {k: data.pop(k) for k in ("password", "private_key")}
        data.update(
            id=str(uuid.uuid4()), credentials=store.seal(json.dumps(secret)), created_at=time.time()
        )
        store.put("environment", data)
        return {k: v for k, v in data.items() if k != "credentials"}

    @api.delete("/environments/{identifier}")
    def delete_environment(identifier: str):
        with service.lock:
            get("environment", identifier)
            references = [
                i for i in store.list("instance") if i.get("environment_id") == identifier
            ]
            active_ids = {
                j["instance_id"] for j in store.list("job") if j["status"] in {"queued", "running"}
            }
            blockers = [
                i
                for i in references
                if i["id"] in active_ids
                or (i.get("monitoring_enabled", True) and i["state"] != "undeployed")
            ]
            if blockers:
                names = "、".join(i.get("name", i["id"]) for i in blockers)
                raise HTTPException(
                    409,
                    "环境仍被实例或未完成任务使用："
                    + names
                    + "。请先卸载或移除实例监测，并等待相关任务结束。",
                )
            with store.connection() as db:
                db.execute("DELETE FROM objects WHERE kind='environment' AND id=?", (identifier,))
            return {"deleted": True, "remote_resources_unchanged": True}

    @api.post("/environments/{identifier}/check")
    def check_environment(identifier: str):
        env = get("environment", identifier)
        try:
            with SSH(env, store) as ssh:
                output = ssh.run(
                    'set -e; [ "$(uname -s)" = Linux ]; uname -sm; for cmd in python3 python conda systemctl docker kubectl ctr k3s; do command -v "$cmd" || true; done',
                    timeout=30,
                )
            return {"ok": True, "output": output}
        except Exception as error:
            raise HTTPException(502, "SSH 环境检查失败：" + str(error)[:1000]) from error

    @api.get("/upload-policy")
    def upload_policy():
        return {"limits": upload_limits(), "protocol": "raw-stream-v1"}

    @api.post("/environments/{identifier}/storage")
    def storage_check(
        identifier: str,
        body: RemoteDirectoryInput,
        size_bytes: int = Query(gt=0),
        mode: str = Query(pattern="^(bare|docker|k8s)$"),
    ):
        env = get("environment", identifier)
        try:
            with SSH(env, store) as ssh:
                return check_storage(ssh, body.remote_root, size_bytes, mode)
        except Exception as error:
            raise HTTPException(502, "目录检查失败：" + str(error)[:2000]) from error

    @api.post("/artifacts", status_code=201)
    async def upload(
        request: Request,
        kind: str = Query(pattern="^(source|image|manifest)$"),
        filename: str = Query(min_length=1, max_length=200),
    ):
        return await receive_upload(request, store, kind, filename)

    @api.get("/instances")
    def instances():
        return [
            public_instance(i) for i in store.list("instance") if i.get("monitoring_enabled", True)
        ]

    @api.post("/instances", status_code=202)
    def deploy(body: InstanceInput):
        get("environment", body.environment_id)
        artifact = get("artifact", body.artifact_id)
        if artifact["kind"] != ("source" if body.mode == "bare" else "image"):
            raise ValueError("部署文件类型与部署方式不匹配")
        data = body.model_dump()
        data["docker_environment"] = store.seal(json.dumps(data["docker"].pop("environment")))
        if body.mode == "k8s":
            manifest = get("artifact", body.manifest_id)
            if manifest["kind"] != "manifest":
                raise ValueError("需要 deployment YAML 清单")
            from .deploy import validate_manifest

            validate_manifest(manifest["path"], data)
        with service.lock:
            get("environment", body.environment_id)
            existing = store.list("instance")
            for other in existing:
                if not other.get("monitoring_enabled", True) or other["state"] == "undeployed":
                    continue
                if other["base_url"] == body.base_url and (
                    body.access == other.get("access", "direct") == "direct"
                    or other.get("environment_id") == body.environment_id
                ):
                    raise HTTPException(409, "此服务地址已被监测")
                if other["environment_id"] == body.environment_id:
                    if body.mode != "k8s" and other["mode"] != "k8s" and other["port"] == body.port:
                        raise HTTPException(409, "该环境端口已被 Panel 中的实例占用")
                    if body.mode == other["mode"] == "k8s" and (
                        body.namespace,
                        body.deployment,
                    ) == (other["namespace"], other["deployment"]):
                        raise HTTPException(409, "该 Deployment 已被 Panel 管理")
            data.update(
                id=str(uuid.uuid4()),
                version=artifact["filename"],
                artifact_sha256=artifact["sha256"],
                created_at=time.time(),
                state="pending",
                origin="panel",
                resource_version=2,
                monitoring_enabled=True,
                health="unknown",
                health_failures=0,
                checked_at=None,
                admin_token=store.seal(body.admin_token),
            )
            store.put("instance", data)
            job = service.submit(data["id"], "deploy")
        return {"instance": public_instance(data), "job": job}

    @api.post("/instances/attach", status_code=201)
    def attach(body: AttachInput):
        if body.environment_id:
            get("environment", body.environment_id)
        candidate = body.model_dump()
        candidate.update(
            id=str(uuid.uuid4()),
            origin="attached",
            state="deployed",
            health="unknown",
            monitoring_enabled=True,
            created_at=time.time(),
            admin_token=store.seal(body.admin_token),
            port=urlsplit(body.base_url).port
            or (443 if body.base_url.startswith("https:") else 80),
        )
        try:
            with service.client(candidate) as client:
                ready = require_json(client.get("/ready"))
                info = require_json(client.get("/management/v1/info"))
                if ready.get("status") != "ready" or info.get("contract_version") != "panel.v1":
                    raise ValueError("目标地址不是已就绪的 panel.v1 Mock 服务")
                endpoints = require_json(client.get("/management/v1/endpoints"))["items"]
        except Exception as error:
            raise HTTPException(502, service.safe_error(error, candidate)) from error
        with service.lock:
            if body.environment_id:
                get("environment", body.environment_id)
            for other in store.list("instance"):
                if (
                    other.get("monitoring_enabled", True)
                    and other["state"] != "undeployed"
                    and other["base_url"] == body.base_url
                    and (
                        body.access == other.get("access", "direct") == "direct"
                        or other.get("environment_id") == body.environment_id
                    )
                ):
                    raise HTTPException(409, "此地址已被监测")
            candidate.update(
                health="healthy",
                checked_at=time.time(),
                health_failures=0,
                endpoints=endpoints,
                capabilities=info.get("capabilities", []),
                service_instance_id=info.get("instance_id"),
            )
            store.put("instance", candidate)
        return public_instance(candidate)

    @api.delete("/instances/{identifier}")
    def forget(identifier: str):
        get("instance", identifier)
        with service.lock:
            store.patch("instance", identifier, monitoring_enabled=False, removed_at=time.time())
            with store.connection() as db:
                db.execute("DELETE FROM samples WHERE instance_id=?", (identifier,))
        return {"removed": True, "remote_resources_unchanged": True}

    @api.post("/instances/{identifier}/undeploy", status_code=202)
    def remove_deployment(identifier: str, confirmation: dict):
        instance = get("instance", identifier)
        if instance.get("origin") == "attached":
            raise HTTPException(409, "外部实例仅支持移除监测，不允许卸载未知归属资源")
        if confirmation.get("confirm_name") != instance["name"]:
            raise HTTPException(400, "请输入实例名称确认卸载")
        return service.submit(identifier, "undeploy")

    @api.post("/instances/{identifier}/health")
    def health(identifier: str):
        get("instance", identifier)
        return public_instance(service.check(identifier))

    @api.post("/instances/{identifier}/restart", status_code=202)
    def restart(identifier: str):
        instance = get("instance", identifier)
        if instance.get("origin") == "attached" or instance["state"] == "undeployed":
            raise HTTPException(409, "此实例不具备 Panel 可管理的运行资源")
        return service.submit(identifier, "restart")

    @api.get("/jobs")
    def jobs():
        return store.list("job")[:100]

    @api.get("/jobs/{identifier}")
    def job(identifier: str):
        return get("job", identifier)

    @api.get("/alerts")
    def alerts():
        active = {i["id"] for i in store.list("instance") if i.get("monitoring_enabled", True)}
        return [a for a in store.alerts() if a["instance_id"] in active]

    @api.post("/alerts/{identifier}/acknowledge")
    def acknowledge(identifier: int):
        with store.connection() as db:
            count = db.execute(
                "UPDATE alerts SET acknowledged=1 WHERE id=?", (identifier,)
            ).rowcount
        if not count:
            raise HTTPException(404, "告警不存在")
        return {"ok": True}

    @api.get("/monitoring")
    def monitoring(
        instance_id: str | None = None,
        minutes: int = Query(60, ge=1, le=10080),
        endpoint: str | None = None,
        method: str | None = None,
    ):
        if instance_id:
            get("instance", instance_id)
        return service.monitor.summary(instance_id, minutes, endpoint, method)

    def remote_get(identifier, path, params=None):
        instance = get("instance", identifier)
        try:
            with service.client(instance) as client:
                return require_json(client.get(path, params=params))
        except Exception as error:
            raise HTTPException(502, service.safe_error(error, instance)) from error

    @api.get("/instances/{identifier}/endpoints")
    def endpoints(identifier: str):
        result = remote_get(identifier, "/management/v1/endpoints")
        with service.lock:
            get("instance", identifier)
            store.patch("instance", identifier, endpoints=result["items"])
        return result

    @api.get("/instances/{identifier}/openapi")
    def openapi(identifier: str):
        return remote_get(identifier, "/openapi.json")

    @api.get("/instances/{identifier}/logs")
    def logs(
        identifier: str,
        limit: int = Query(100, ge=1, le=500),
        cursor: str | None = None,
        level: str | None = None,
    ):
        return remote_get(
            identifier,
            "/management/v1/logs",
            {
                k: v
                for k, v in {"limit": limit, "cursor": cursor, "level": level}.items()
                if v is not None
            },
        )

    @api.get("/instances/{identifier}/requests")
    def requests(
        identifier: str,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        non_200: bool = True,
        since: str | None = None,
        until: str | None = None,
    ):
        return remote_get(
            identifier,
            "/management/v1/requests",
            {
                k: v
                for k, v in {
                    "limit": limit,
                    "cursor": cursor,
                    "endpoint": endpoint,
                    "method": method,
                    "non_200": non_200,
                    "since": since,
                    "until": until,
                }.items()
                if v is not None
            },
        )

    @api.get("/instances/{identifier}/requests/{request_id}")
    def request_detail(identifier: str, request_id: str):
        return remote_get(identifier, "/management/v1/requests/" + quote(request_id, safe=""))

    @api.get("/instances/{identifier}/traces/{trace_id}")
    def trace(identifier: str, trace_id: str):
        return remote_get(identifier, "/management/v1/traces/" + quote(trace_id, safe=""))

    @api.get("/instances/{identifier}/database/tables")
    def tables(identifier: str):
        return remote_get(identifier, "/management/v1/database/tables")

    @api.get("/instances/{identifier}/database/tables/{table}/rows")
    def rows(
        identifier: str,
        table: str,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return remote_get(
            identifier,
            "/management/v1/database/tables/" + quote(table, safe="") + "/rows",
            {"limit": limit, "offset": offset},
        )

    @api.post("/instances/{identifier}/exports/{kind}", status_code=202)
    def export(identifier: str, kind: str):
        get("instance", identifier)
        if kind not in {"database", "logs", "traces"}:
            raise ValueError("未知导出类型")
        return service.submit(identifier, "export", kind)

    @api.get("/downloads/{identifier}")
    def download(identifier: str):
        item = get("download", identifier)
        if item.get("expired") or not Path(item["path"]).is_file():
            raise HTTPException(410, "文件已过期")
        return FileResponse(
            item["path"], filename=item["filename"], media_type="application/octet-stream"
        )

    @api.post("/downloads/{identifier}/ticket")
    def download_ticket(identifier: str):
        item = get("download", identifier)
        if (
            item.get("expired")
            or time.time() - item["created_at"] > 86400
            or not Path(item["path"]).is_file()
        ):
            raise HTTPException(410, "文件已过期")
        ticket = secrets.token_urlsafe(32)
        store.put(
            "ticket",
            {
                "id": hashlib.sha256(ticket.encode()).hexdigest(),
                "download_id": identifier,
                "expires_at": time.time() + 60,
            },
        )
        return {"url": "/api/files/" + ticket, "expires_in": 60}

    @app.get("/api/files/{ticket}")
    def ticket_download(ticket: str):
        # Single-use capability enables native streaming downloads without exposing admin tokens.
        key = hashlib.sha256(ticket.encode()).hexdigest()
        with service.lock, store.connection() as db:
            row = db.execute(
                "SELECT value FROM objects WHERE kind='ticket' AND id=?", (key,)
            ).fetchone()
            if row:
                db.execute("DELETE FROM objects WHERE kind='ticket' AND id=?", (key,))
        if not row:
            raise HTTPException(404, "下载链接不存在或已使用")
        record = json.loads(row[0])
        if time.time() > record["expires_at"]:
            raise HTTPException(410, "下载链接已过期")
        item = get("download", record["download_id"])
        if (
            item.get("expired")
            or time.time() - item["created_at"] > 86400
            or not Path(item["path"]).is_file()
        ):
            raise HTTPException(410, "文件已过期")
        return FileResponse(
            item["path"],
            filename=item["filename"],
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @api.post("/instances/{identifier}/console")
    def console(identifier: str, body: ConsoleInput):
        instance = get("instance", identifier)
        discovered = remote_get(identifier, "/management/v1/endpoints")["items"]
        endpoint = next((e for e in discovered if e["id"] == body.endpoint_id), None)
        if not endpoint:
            raise HTTPException(404, "接口未出现在实例接口清单中")
        path = endpoint["path"]
        for key, value in body.path_params.items():
            path = path.replace("{" + key + "}", quote(value, safe=""))
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(x in path for x in ("{", "}", "?", "#", "\\"))
        ):
            raise ValueError("接口路径无效或缺少路径参数")
        forbidden = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "proxy-authorization",
            "x-panel-request-origin",
        }
        headers = {k: v for k, v in body.headers.items() if k.lower() not in forbidden}
        headers["X-Panel-Request-Origin"] = "console"
        stack = ExitStack()
        try:
            client = stack.enter_context(service.client(instance, timeout=30))
            response = stack.enter_context(
                client.stream(
                    endpoint["method"], path, params=body.query, headers=headers, json=body.body
                )
            )

            def chunks():
                size, started = 0, time.monotonic()
                try:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2 * 1024**2 or time.monotonic() - started > 120:
                            yield b"\n[Panel: response truncated by size/time limit]\n"
                            break
                        yield chunk
                finally:
                    stack.close()

            return StreamingResponse(
                chunks(),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "text/plain"),
                headers={
                    "X-Request-ID": response.headers.get("x-request-id", ""),
                    "Cache-Control": "no-store",
                },
            )
        except Exception as error:
            stack.close()
            raise HTTPException(502, service.safe_error(error, instance)) from error

    api.include_router(feedback_router(store))
    app.include_router(api)
    frontend = Path(
        os.environ.get(
            "PANEL_FRONTEND_DIST", Path(__file__).resolve().parents[3] / "frontend" / "dist"
        )
    )
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
