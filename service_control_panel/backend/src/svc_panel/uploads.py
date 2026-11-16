"""Raw streaming uploads: validate headers before reading the first network chunk."""

import hashlib
import os
import shutil
import tarfile
import time
import uuid
from pathlib import Path

import anyio
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool


def upload_limits():
    return {
        "source": max(1, int(os.getenv("PANEL_PACKAGE_LIMIT_BYTES", str(2 * 1024**3)))),
        "image": max(1, int(os.getenv("PANEL_PACKAGE_LIMIT_BYTES", str(2 * 1024**3)))),
        "manifest": max(1, int(os.getenv("PANEL_MANIFEST_LIMIT_BYTES", str(2 * 1024**2)))),
    }


async def receive_upload(request: Request, store, kind: str, filename: str):
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(
            415, "请使用二进制流上传（application/octet-stream），不再使用 multipart"
        )
    limit = upload_limits()[kind]
    try:
        expected = int(request.headers["x-upload-size"])
        declared = int(request.headers.get("content-length", expected))
    except (KeyError, ValueError):
        raise HTTPException(400, "需要有效的 X-Upload-Size 文件大小") from None
    if expected > limit or declared > limit:
        raise HTTPException(413, f"{kind} 文件超限：最大 {limit} 字节（{limit / 1024**2:g} MiB）")
    if expected <= 0 or declared != expected:
        raise HTTPException(400, "声明文件大小无效或与 Content-Length 不符")
    folder = store.directory / "artifacts"
    folder.mkdir(exist_ok=True)
    reserve = 64 * 1024**2
    if shutil.disk_usage(folder).free < expected + reserve:
        raise HTTPException(507, "Panel 存储空间不足，文件尚未接收")
    identifier = str(uuid.uuid4())
    path = folder / identifier
    size, digest, started = 0, hashlib.sha256(), time.monotonic()
    try:
        async with await anyio.open_file(path, "xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit or size > expected:
                    raise HTTPException(413, f"{kind} 接收大小超过限制或声明大小")
                if shutil.disk_usage(folder).free < len(chunk) + reserve:
                    raise HTTPException(507, "Panel 存储空间不足，已终止上传")
                digest.update(chunk)
                await stream.write(chunk)
        if size != expected:
            raise HTTPException(400, "上传未完成，实际大小与声明不符")
        received = time.monotonic()
        expanded = None
        if kind == "source":
            from .deploy import validate_source

            expanded = await run_in_threadpool(validate_source, path)
        item = {
            "id": identifier,
            "filename": Path(filename).name[:200],
            "kind": kind,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "path": str(path),
            "created_at": time.time(),
            "expanded_bytes": expanded,
            "upload_seconds": received - started,
            "validation_seconds": time.monotonic() - received,
        }
        await run_in_threadpool(store.put, "artifact", item)
        return {k: v for k, v in item.items() if k != "path"}
    except BaseException as error:
        path.unlink(missing_ok=True)
        if isinstance(error, tarfile.TarError):
            raise HTTPException(400, "无效的源码 tar 包") from error
        if isinstance(error, OSError) and error.errno == 28:
            raise HTTPException(507, "Panel 磁盘已满，上传文件已清理") from error
        raise
