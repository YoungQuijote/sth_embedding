from __future__ import annotations

import base64
import hashlib
import io
import select
import shlex
import socketserver
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
import paramiko


class FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected.rstrip("=")

    def missing_host_key(self, client, hostname, key):
        actual = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode().rstrip("=")
        if actual != self.expected:
            raise ValueError(f"SSH 主机指纹不匹配，实际指纹 {actual}")


class SSH:
    def __init__(self, environment, store):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(FingerprintPolicy(environment["host_key_sha256"]))
        secret = store.unseal(environment["credentials"])
        import json

        credentials = json.loads(secret)
        key = None
        if credentials.get("private_key"):
            for keytype in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    key = keytype.from_private_key(
                        io.StringIO(credentials["private_key"]),
                        password=credentials.get("password") or None,
                    )
                    break
                except paramiko.SSHException:
                    continue
            if key is None:
                raise ValueError("无法读取 SSH 私钥")
        self.client.connect(
            environment["host"],
            port=environment["port"],
            username=environment["username"],
            password=credentials.get("password") or None,
            pkey=key,
            timeout=15,
            auth_timeout=15,
            banner_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def run(self, script: str, timeout=900) -> str:
        _, stdout, _ = self.client.exec_command("bash -lc " + shlex.quote(script), timeout=timeout)
        channel = stdout.channel
        data = bytearray()
        started = time.monotonic()
        while True:
            for ready, read in (
                (channel.recv_ready, channel.recv),
                (channel.recv_stderr_ready, channel.recv_stderr),
            ):
                while ready():
                    data.extend(read(65536))
                    if len(data) > 256_000:
                        del data[:-256_000]
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            if time.monotonic() - started > timeout:
                channel.close()
                raise TimeoutError("远程命令超时；请检查远程任务状态后重试")
            time.sleep(0.05)
        result = data.decode(errors="replace")
        if channel.recv_exit_status() != 0:
            raise RuntimeError(result[-12000:] or "远程命令失败")
        return result

    def upload(self, local, remote):
        with self.client.open_sftp() as sftp:
            sftp.put(str(local), remote)
            sftp.chmod(remote, 0o600)

    def write(self, remote, data):
        with self.client.open_sftp() as sftp:
            with sftp.file(remote, "w") as stream:
                stream.write(data)
            sftp.chmod(remote, 0o600)


@contextmanager
def api_client(instance, environment, store, timeout=20):
    """All HTTP/console/export access uses the same optional SSH transport."""
    token = store.unseal(instance["admin_token"])
    headers = {"Authorization": "Bearer " + token, "X-Panel-Request-Origin": "probe"}
    if instance["access"] == "direct":
        with httpx.Client(
            base_url=instance["base_url"],
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield client
        return
    parsed = urlsplit(instance["base_url"])
    with SSH(environment, store) as ssh:
        transport = ssh.client.get_transport()

        class Forwarder(socketserver.BaseRequestHandler):
            def handle(self):
                channel = transport.open_channel(
                    "direct-tcpip",
                    (parsed.hostname, parsed.port or 80),
                    self.client_address,
                    timeout=15,
                )
                try:
                    while True:
                        ready, _, _ = select.select([self.request, channel], [], [], 1)
                        if self.request in ready:
                            chunk = self.request.recv(65536)
                            if not chunk:
                                return
                            channel.sendall(chunk)
                        if channel in ready:
                            chunk = channel.recv(65536)
                            if not chunk:
                                return
                            self.request.sendall(chunk)
                finally:
                    channel.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        with Server(("127.0.0.1", 0), Forwarder) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    yield client
            finally:
                server.shutdown()
                thread.join(timeout=2)


def require_json(response):
    if response.is_error:
        raise RuntimeError(f"实例接口返回 {response.status_code}: {response.text[:1500]}")
    return response.json()
