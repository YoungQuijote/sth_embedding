"""Disposable browser-test workspace backed by the panel.v1 fixture, no SSH execution."""

import importlib
import json
import os
import tempfile
import threading
import time
from contextlib import nullcontext

import uvicorn
from contract_server import MOCK_TOKEN
from contract_server import app as mock_app

from svc_panel.app import create_app
from svc_panel.deploy import Deployer


def main():
    directory = tempfile.mkdtemp(
        prefix="panel-browser-", dir=os.environ.get("PANEL_TEST_TMP", "/tmp")
    )
    os.environ["PANEL_POLL_INTERVAL"] = "30"
    app = create_app(directory, admin_token="browser-test-token-123456789")
    store = app.state.store
    store.put(
        "environment",
        {
            "id": "fixture-env",
            "name": "本地契约测试环境",
            "os": "linux",
            "host": "127.0.0.1",
            "port": 22,
            "username": "fixture",
            "host_key_sha256": "SHA256:" + "a" * 43,
            "credentials": store.seal(json.dumps({"password": "fixture-only", "private_key": ""})),
        },
    )
    for index, name in enumerate(["Staging · 契约测试", "QA · 契约测试"]):
        identifier = "fixture-" + str(index)
        store.put(
            "instance",
            {
                "id": identifier,
                "name": name,
                "environment_id": "fixture-env",
                "mode": "docker",
                "artifact_id": "test-artifact",
                "port": 32883 + index,
                "base_url": "http://127.0.0.1:32883",
                "access": "direct",
                "admin_token": store.seal(MOCK_TOKEN),
                "version": "agent-mock-v1.tar",
                "state": "deployed",
                "health": "healthy",
                "health_failures": 0,
                "checked_at": time.time(),
                "created_at": time.time(),
            },
        )
        with store.connection() as db:
            total, failed = 0, 0
            for n in range(120):
                total += 12 + (n % 7) * 5 + index
                failed += int(n % 9 == 0)
                counters = [
                    {
                        "endpoint": "/weather",
                        "method": "POST",
                        "category": "business",
                        "status_code": status,
                        "requests_total": number,
                        "duration_seconds_sum": number * 0.037,
                    }
                    for status, number in [(200, total), (500, failed)]
                ]
                db.execute(
                    "INSERT INTO samples VALUES(?,?,?,?)",
                    (identifier, time.time() - (120 - n) * 30, "seed", json.dumps(counters)),
                )

    def simulate(self, instance, restart=False):
        self.log("契约测试执行器：模拟远程" + ("重启" if restart else "部署"))
        time.sleep(0.3)

    Deployer.run = simulate
    module = importlib.import_module("svc_panel.app")
    module.SSH = lambda *args: nullcontext(None)
    module.check_storage = lambda *args: {"available_bytes": 10**12, "required_bytes": 1024}
    # Use a second loopback alias for a newly deployed instance to exercise URL uniqueness.

    thread = threading.Thread(
        target=lambda: uvicorn.run(mock_app, host="0.0.0.0", port=32883, log_level="warning"),
        daemon=True,
    )
    thread.start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
