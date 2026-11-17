"""Shared feedback board; SQLite owns metadata and image bytes atomically."""

import base64
import binascii
import io
import json
import time
import uuid
import warnings
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

Status = Literal["pending", "developing", "acceptance", "closed"]
Sort = Literal["created_asc", "created_desc", "status_asc", "status_desc"]
IMAGE_LIMIT = 5 * 1024**2
BODY_LIMIT = 36 * 1024**2
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class FeedbackImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=36)
    filename: str = Field(default="image", max_length=200)
    data_base64: str | None = Field(default=None, max_length=((IMAGE_LIMIT + 2) // 3) * 4)

    @model_validator(mode="after")
    def source(self):
        if bool(self.id) == (self.data_base64 is not None):
            raise ValueError("配图须提供已有图片 ID 或新图片内容，不能同时提供")
        return self


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author: str = Field(default="", max_length=16)
    summary: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=20000)
    status: Status = "pending"
    images: list[FeedbackImage] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def meaningful(self):
        if not self.summary.strip():
            raise ValueError("需求简述不能为空白")
        identifiers = [image.id for image in self.images if image.id]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("配图 ID 不可重复")
        return self


class FeedbackUpdate(FeedbackInput):
    revision: int = Field(ge=1)


def parse_body(raw, update=False):
    try:
        data = json.loads(raw)
        return (FeedbackUpdate if update else FeedbackInput).model_validate(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(422, "请求必须为有效 JSON") from error
    except ValidationError as error:
        # Do not echo large base64 bodies or user text in validation errors.
        raise HTTPException(422, "；".join(e["msg"] for e in error.errors())) from error


def prepare_image(image):
    try:
        content = base64.b64decode(image.data_base64, validate=True)
        if not content or len(content) > IMAGE_LIMIT:
            raise ValueError("配图为空或超过 5 MiB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as picture:
                if picture.format not in FORMATS:
                    raise ValueError("配图仅支持 PNG、JPEG、WebP、GIF")
                if picture.width * picture.height > 20_000_000:
                    raise ValueError("配图分辨率超过 2000 万像素")
                mime = FORMATS[picture.format]
                picture.verify()
            with Image.open(io.BytesIO(content)) as picture:
                picture.load()  # Reject truncated pixel data, including JPEGs whose verify is a no-op.
        return {
            "id": str(uuid.uuid4()),
            "filename": image.filename,
            "mime_type": mime,
            "size_bytes": len(content),
            "data": content,
        }
    except (
        binascii.Error,
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise HTTPException(
            422, "无效配图：请使用完整的 PNG/JPEG/WebP/GIF，单张 ≤5 MiB、≤2000 万像素"
        ) from error


class FeedbackStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def detail(db, identifier):
        row = db.execute("SELECT * FROM feedback WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, "需求或反馈不存在，可能已被删除")
        item = dict(row)
        item["images"] = [
            dict(image)
            for image in db.execute(
                "SELECT id,filename,mime_type,size_bytes FROM feedback_images WHERE feedback_id=? ORDER BY position",
                (identifier,),
            )
        ]
        return item

    def get(self, identifier):
        with self.store.connection() as db:
            db.execute("BEGIN")
            return self.detail(db, identifier)

    def list(self, status, sort, limit, offset):
        order = {
            "created_asc": "created_at ASC, id ASC",
            "created_desc": "created_at DESC, id DESC",
            "status_asc": "status_updated_at ASC, id ASC",
            "status_desc": "status_updated_at DESC, id DESC",
        }[sort]
        where, args = ("WHERE status=?", [status]) if status else ("", [])
        with self.store.connection() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM feedback " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT id,author,summary,status,created_at,status_updated_at,revision, "
                "(SELECT COUNT(*) FROM feedback_images WHERE feedback_id=feedback.id) AS image_count "
                f"FROM feedback {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def save(self, raw, identifier=None):
        body = parse_body(raw, update=identifier is not None)
        if identifier is None and any(i.id for i in body.images):
            raise HTTPException(422, "新需求不能引用其他需求的配图")
        images = [({"id": i.id} if i.id else prepare_image(i)) for i in body.images]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if identifier is None:
                identifier = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        body.author,
                        body.summary,
                        body.description,
                        body.status,
                        now,
                        now,
                        1,
                    ),
                )
            else:
                previous = self.detail(db, identifier)
                if body.revision != previous["revision"]:
                    raise HTTPException(409, "此条目已被其他人修改，请重新打开最新详情后再编辑")
                owned = {i["id"] for i in previous["images"]}
                if any("data" not in i and i["id"] not in owned for i in images):
                    raise HTTPException(422, "配图不属于此条目，请重新打开后编辑")
                changed = (
                    now if previous["status"] != body.status else previous["status_updated_at"]
                )
                db.execute(
                    "UPDATE feedback SET author=?,summary=?,description=?,status=?,status_updated_at=?,revision=revision+1 WHERE id=?",
                    (body.author, body.summary, body.description, body.status, changed, identifier),
                )
                keep = {i["id"] for i in images if "data" not in i}
                for removed in owned - keep:
                    db.execute(
                        "DELETE FROM feedback_images WHERE id=? AND feedback_id=?",
                        (removed, identifier),
                    )
            for position, image in enumerate(images):
                if "data" in image:
                    db.execute(
                        "INSERT INTO feedback_images VALUES(?,?,?,?,?,?,?)",
                        (
                            image["id"],
                            identifier,
                            image["filename"],
                            image["mime_type"],
                            image["size_bytes"],
                            position,
                            image["data"],
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE feedback_images SET position=? WHERE id=? AND feedback_id=?",
                        (position, image["id"], identifier),
                    )
            return self.detail(db, identifier)

    def delete(self, identifier, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.detail(db, identifier)
            if current["revision"] != revision:
                raise HTTPException(409, "此条目已被其他人修改，请重新打开后确认删除")
            db.execute("DELETE FROM feedback WHERE id=?", (identifier,))
        return {"deleted": True}


def feedback_router(store):
    router = APIRouter()
    board = FeedbackStore(store)

    async def body_bytes(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "请使用 application/json")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as error:
                raise HTTPException(400, "Content-Length 无效") from error
            if size < 0 or size > BODY_LIMIT:
                raise HTTPException(413, "需求及配图合计请求体超过 36 MiB")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > BODY_LIMIT:
                raise HTTPException(413, "需求及配图合计请求体超过 36 MiB")
            data.extend(chunk)
        return data

    @router.get("/feedback")
    def listing(
        status: Status | None = None,
        sort: Sort = "created_desc",
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return board.list(status, sort, limit, offset)

    @router.post("/feedback", status_code=201)
    async def create(request: Request):
        return await run_in_threadpool(board.save, await body_bytes(request))

    @router.get("/feedback/{identifier}")
    def detail(identifier: str):
        return board.get(identifier)

    @router.put("/feedback/{identifier}")
    async def update(identifier: str, request: Request):
        return await run_in_threadpool(board.save, await body_bytes(request), identifier)

    @router.delete("/feedback/{identifier}")
    def delete(identifier: str, revision: int = Query(ge=1)):
        return board.delete(identifier, revision)

    @router.get("/feedback/{identifier}/images/{image_id}")
    def image(identifier: str, image_id: str):
        with store.connection() as db:
            row = db.execute(
                "SELECT mime_type,data FROM feedback_images WHERE id=? AND feedback_id=?",
                (image_id, identifier),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "配图不存在")
        return Response(
            row["data"],
            media_type=row["mime_type"],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    return router
