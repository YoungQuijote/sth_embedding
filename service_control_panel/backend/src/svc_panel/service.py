from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .deploy import Deployer
from .monitoring import BuiltinMonitoringProvider
from .remote import api_client, require_json


class Service:
    def __init__(self, store, interval=30):
        self.store = store
        self.interval = interval
        self.monitor = BuiltinMonitoringProvider(store, interval)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="panel-job")
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        for job in store.list("job"):
            if job["status"] in {"queued", "running"}:
                job.update(
                    status="interrupted",
                    error="Panel 重启，任务结果未知；请先检查远程状态",
                    finished_at=time.time(),
                )
                store.put("job", job)
                self.store.alert(job["instance_id"], "operation", job["error"])
                instance = store.get("instance", job["instance_id"])
                if instance:
                    instance["state"] = "unknown"
                    store.put("instance", instance)

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="panel-monitor")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def client(self, instance, timeout=20):
        return api_client(
            instance, self.store.get("environment", instance["environment_id"]), self.store, timeout
        )

    def safe_error(self, error, instance=None):
        message = str(error)
        if instance:
            message = message.replace(self.store.unseal(instance["admin_token"]), "[REDACTED]")
            env = self.store.get("environment", instance["environment_id"])
            if env:
                secrets = json.loads(self.store.unseal(env["credentials"]))
                for secret in secrets.values():
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
        return message[:12000]

    def check(self, identifier, collect=True):
        instance = self.store.get("instance", identifier)
        now = time.time()
        try:
            with self.client(instance) as client:
                ready = require_json(client.get("/ready"))
                if ready.get("status") != "ready":
                    raise ValueError("实例未就绪")
                info = require_json(client.get("/management/v1/info"))
                if info.get("contract_version") != "panel.v1":
                    raise ValueError("实例管理协议不是 panel.v1")
                metrics_error = None
                if collect:
                    try:
                        self.monitor.record(
                            identifier, require_json(client.get("/management/v1/metrics"))
                        )
                    except Exception as error:
                        metrics_error = self.safe_error(error, instance)
                with self.lock:
                    current = self.store.get("instance", identifier)
                    current.update(
                        health="healthy",
                        checked_at=now,
                        health_failures=0,
                        health_error=None,
                        capabilities=info.get("capabilities", []),
                        monitoring_error=metrics_error,
                    )
                    self.store.put("instance", current)
                self.store.resolve(identifier, "health")
                if metrics_error:
                    self.store.alert(identifier, "monitoring", metrics_error)
                else:
                    self.store.resolve(identifier, "monitoring")
                return current
        except Exception as error:
            message = self.safe_error(error, instance)
            with self.lock:
                current = self.store.get("instance", identifier)
                failures = current.get("health_failures", 0) + 1
                current.update(
                    health="unhealthy",
                    checked_at=now,
                    health_failures=failures,
                    health_error=message,
                )
                self.store.put("instance", current)
            if failures >= 3:
                self.store.alert(identifier, "health", message)
            return current

    def loop(self):
        while not self.stop.is_set():
            for instance in self.store.list("instance"):
                if self.stop.is_set():
                    return
                if instance["state"] in {"deployed", "unknown", "failed"}:
                    try:
                        self.check(instance["id"])
                    except Exception:
                        # One corrupt/offline instance must not stop the remaining poller.
                        continue
            cutoff = time.time() - 86400
            for item in self.store.list("download"):
                if item["created_at"] < cutoff and not item.get("expired"):
                    Path(item["path"]).unlink(missing_ok=True)
                    item["expired"] = True
                    self.store.put("download", item)
            self.stop.wait(self.interval)

    def submit(self, instance_id, operation, kind=None):
        with self.lock:
            if any(
                j["instance_id"] == instance_id and j["status"] in {"queued", "running"}
                for j in self.store.list("job")
            ):
                raise ValueError("此实例已有进行中的操作")
            job = {
                "id": str(uuid.uuid4()),
                "instance_id": instance_id,
                "operation": operation,
                "kind": kind,
                "status": "queued",
                "created_at": time.time(),
                "logs": [],
                "error": None,
            }
            self.store.put("job", job)
            if operation in {"deploy", "restart"}:
                instance = self.store.get("instance", instance_id)
                instance["state"] = "deploying" if operation == "deploy" else "restarting"
                self.store.put("instance", instance)
            self.executor.submit(self.execute, job)
        return job

    def execute(self, job):
        instance = self.store.get("instance", job["instance_id"])

        def log(message):
            job["logs"].append(
                {"timestamp": time.time(), "message": self.safe_error(message, instance)}
            )
            self.store.put("job", job)

        job.update(status="running", started_at=time.time())
        self.store.put("job", job)
        try:
            if job["operation"] in {"deploy", "restart"}:
                Deployer(self.store, log).run(instance, restart=job["operation"] == "restart")
                log("等待服务就绪并验证管理协议")
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    current = self.check(instance["id"])
                    if current["health"] == "healthy":
                        break
                    if self.stop.wait(3):
                        raise RuntimeError("Panel 正在关闭")
                else:
                    raise RuntimeError(
                        "远程资源已启动，但健康检查失败：" + current.get("health_error", "unknown")
                    )
                with self.lock:
                    current = self.store.get("instance", instance["id"])
                    current["state"] = "deployed"
                    self.store.put("instance", current)
                self.store.resolve(instance["id"], "operation")
                log("操作完成，实例已就绪")
            else:
                self.export(instance, job, log)
            job["status"] = "succeeded"
        except Exception as error:
            job.update(status="failed", error=self.safe_error(error, instance))
            log(job["error"])
            if job["operation"] in {"deploy", "restart"}:
                with self.lock:
                    current = self.store.get("instance", instance["id"])
                    current["state"] = "failed"
                    self.store.put("instance", current)
            self.store.alert(instance["id"], "operation", job["error"])
        finally:
            job["finished_at"] = time.time()
            self.store.put("job", job)

    def export(self, instance, job, log):
        directory = self.store.directory / "downloads"
        directory.mkdir(exist_ok=True)
        destination = directory / job["id"]
        log("请求远端生成一致性导出")
        try:
            with self.client(instance, timeout=60) as client:
                result = require_json(
                    client.post("/management/v1/exports", json={"kind": job["kind"]})
                )
                identifier = result["export_id"]
                from urllib.parse import quote

                route = "/management/v1/exports/" + quote(identifier, safe="")
                deadline = time.monotonic() + 300
                while result["status"] in {"pending", "running"}:
                    if time.monotonic() > deadline or self.stop.wait(2):
                        raise TimeoutError("导出任务未在 5 分钟内完成")
                    result = require_json(client.get(route))
                if result["status"] != "ready":
                    raise RuntimeError(result.get("error") or "远端导出失败")
                log("下载导出文件到 Panel")
                size = 0
                with client.stream("GET", route + "/download") as response:
                    response.raise_for_status()
                    with destination.open("xb") as stream:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 2 * 1024**3:
                                raise ValueError("导出文件超过 2 GiB 限制")
                            stream.write(chunk)
                if result.get("size_bytes") is not None and size != result["size_bytes"]:
                    raise ValueError("导出文件长度与元数据不符")
                filename = Path(result.get("filename") or job["kind"] + ".bin").name
                self.store.put(
                    "download",
                    {
                        "id": job["id"],
                        "instance_id": instance["id"],
                        "path": str(destination),
                        "filename": filename,
                        "size_bytes": size,
                        "created_at": time.time(),
                        "expired": False,
                    },
                )
                job["download_id"] = job["id"]
                log("回流完成，文件保留 24 小时")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
