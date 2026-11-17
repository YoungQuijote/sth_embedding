import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from test_panel import TOKEN

from svc_panel.app import create_app
from svc_panel.feedback import BODY_LIMIT, FeedbackStore

pytest_plugins = ["test_panel"]


def image_payload(fmt="PNG", color="green"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format=fmt)
    content = buffer.getvalue()
    return {
        "filename": "示意图." + fmt.lower(),
        "data_base64": base64.b64encode(content).decode(),
    }, content


def update_payload(item, **changes):
    return {
        **{k: item[k] for k in ("author", "summary", "description", "status", "revision")},
        "images": [{"id": i["id"]} for i in item["images"]],
        **changes,
    }


def test_minimal_create_persists_and_defaults(panel):
    http, app, _ = panel
    before = time.time()
    result = http.post("/api/feedback", json={"summary": "增加一个统计入口"})
    assert result.status_code == 201, result.text
    item = result.json()
    assert item["status"] == "pending" and item["author"] == "" and item["description"] == ""
    assert before <= item["created_at"] <= time.time()
    assert item["created_at"] == item["status_updated_at"]
    assert item["images"] == []
    reopened = create_app(app.state.store.directory, admin_token=TOKEN, background=False)
    with TestClient(reopened, headers={"Authorization": "Bearer " + TOKEN}) as other:
        assert other.get("/api/feedback/" + item["id"]).json() == item
        assert other.get("/api/feedback").json()["total"] == 1


def test_time_changes_only_when_status_changes(panel, monkeypatch):
    http, _, _ = panel
    monkeypatch.setattr("svc_panel.feedback.time.time", lambda: 1000)
    item = http.post("/api/feedback", json={"summary": "需求", "author": "🚀" * 16}).json()
    assert item["author"] == "🚀" * 16
    monkeypatch.setattr("svc_panel.feedback.time.time", lambda: 2000)
    route = "/api/feedback/" + item["id"]
    item = http.put(route, json=update_payload(item, description="详细说明", author="a\n b")).json()
    assert item["created_at"] == item["status_updated_at"] == 1000
    assert item["author"] == "a\n b"
    item = http.put(route, json=update_payload(item, status="developing")).json()
    assert item["created_at"] == 1000 and item["status_updated_at"] == 2000
    monkeypatch.setattr("svc_panel.feedback.time.time", lambda: 3000)
    item = http.put(route, json=update_payload(item, summary="修改简述")).json()
    assert item["status_updated_at"] == 2000
    item = http.put(route, json=update_payload(item, status="acceptance")).json()
    assert item["status_updated_at"] == 3000
    item = http.put(route, json=update_payload(item, status="closed")).json()
    assert item["status"] == "closed"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"summary": ""},
        {"summary": " \n "},
        {"summary": "a" * 257},
        {"summary": "ok", "author": "🙂" * 17},
        {"summary": "ok", "description": "x" * 20001},
        {"summary": "ok", "status": "done"},
        {"summary": "ok", "created_at": 1},
        {"summary": "ok", "images": [{"id": "x"}] * 6},
    ],
)
def test_input_boundaries(panel, payload):
    http, _, _ = panel
    assert http.post("/api/feedback", json=payload).status_code == 422
    assert http.get("/api/feedback").json()["total"] == 0


def test_maximum_text_and_unrestricted_proposer_characters(panel):
    http, _, _ = panel
    payload = {
        "summary": "😀" * 256,
        "author": "<a> 空格&\n🚀",
        "description": "详" * 20000,
        "status": "acceptance",
    }
    result = http.post("/api/feedback", json=payload)
    assert result.status_code == 201
    assert all(result.json()[k] == v for k, v in payload.items())


def test_status_filter_all_four_orders_and_pagination(panel, monkeypatch):
    http, _, _ = panel
    items = []
    for index in range(3):
        monkeypatch.setattr("svc_panel.feedback.time.time", lambda index=index: 1000 + index)
        items.append(http.post("/api/feedback", json={"summary": str(index)}).json())
    monkeypatch.setattr("svc_panel.feedback.time.time", lambda: 5000)
    items[0] = http.put(
        "/api/feedback/" + items[0]["id"], json=update_payload(items[0], status="closed")
    ).json()
    for sort, expected in [
        ("created_asc", ["0", "1", "2"]),
        ("created_desc", ["2", "1", "0"]),
        ("status_asc", ["1", "2", "0"]),
        ("status_desc", ["0", "2", "1"]),
    ]:
        result = http.get("/api/feedback", params={"sort": sort}).json()
        assert [i["summary"] for i in result["items"]] == expected
    result = http.get("/api/feedback?status=pending&sort=created_asc&limit=1&offset=1").json()
    assert result["total"] == 2 and result["items"][0]["summary"] == "2"
    assert "description" not in result["items"][0] and "images" not in result["items"][0]
    assert http.get("/api/feedback?status=developing").json()["total"] == 0
    assert http.get("/api/feedback?sort=DROP%20TABLE").status_code == 422
    assert http.get("/api/feedback?offset=-1").status_code == 422


@pytest.mark.parametrize(
    "fmt,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_images_auth_persistence_and_cascade_delete(panel, fmt, mime):
    http, app, _ = panel
    payload, content = image_payload(fmt)
    item = http.post("/api/feedback", json={"summary": "截图反馈", "images": [payload]}).json()
    route = "/api/feedback/" + item["id"]
    image_route = route + "/images/" + item["images"][0]["id"]
    response = http.get(image_route)
    assert response.status_code == 200 and response.content == content
    assert (
        response.headers["content-type"] == mime
        and response.headers["x-content-type-options"] == "nosniff"
    )
    assert http.get(image_route, headers={"Authorization": ""}).status_code == 401
    assert http.get("/api/feedback/other/images/" + item["images"][0]["id"]).status_code == 404
    assert "data_base64" not in json.dumps(item)
    with app.state.store.connection() as db:
        assert db.execute("SELECT data FROM feedback_images").fetchone()[0] == content
    assert http.delete(route + "?revision=" + str(item["revision"])).status_code == 200
    assert http.get(route).status_code == 404 and http.get(image_route).status_code == 404
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_images").fetchone()[0] == 0


def test_replace_remove_images_and_reject_cross_entry_references(panel):
    http, app, _ = panel
    green, _ = image_payload()
    red, red_bytes = image_payload(color="red")
    a = http.post("/api/feedback", json={"summary": "A", "images": [green]}).json()
    b = http.post("/api/feedback", json={"summary": "B"}).json()
    image_id = a["images"][0]["id"]
    assert (
        http.put(
            "/api/feedback/" + b["id"], json=update_payload(b, images=[{"id": image_id}])
        ).status_code
        == 422
    )
    assert (
        http.post("/api/feedback", json={"summary": "C", "images": [{"id": image_id}]}).status_code
        == 422
    )
    route = "/api/feedback/" + a["id"]
    a = http.put(route, json=update_payload(a, images=[red])).json()
    assert http.get(route + "/images/" + image_id).status_code == 404
    assert http.get(route + "/images/" + a["images"][0]["id"]).content == red_bytes
    unchanged = a["status_updated_at"]
    a = http.put(route, json=update_payload(a, images=[])).json()
    assert a["images"] == [] and a["status_updated_at"] == unchanged
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_images").fetchone()[0] == 0


@pytest.mark.parametrize("data", [b'<svg onload="alert(1)"></svg>', b"not an image", b"GIF89a"])
def test_invalid_images_do_not_save_partial_records(panel, data):
    http, app, _ = panel
    valid, _ = image_payload()
    r = http.post(
        "/api/feedback",
        json={
            "summary": "invalid",
            "images": [valid, {"data_base64": base64.b64encode(data).decode()}],
        },
    )
    assert r.status_code == 422
    assert http.get("/api/feedback").json()["total"] == 0
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_images").fetchone()[0] == 0


def test_oversize_body_rejected_before_reading(panel):
    http, _, _ = panel
    response = http.post(
        "/api/feedback",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(BODY_LIMIT + 1)},
    )
    assert response.status_code == 413
    assert (
        http.post(
            "/api/feedback", content=b"{bad", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/api/feedback", content=b"{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        http.post("/api/feedback", json={"summary": "x"}, headers={"Authorization": ""}).status_code
        == 401
    )


def test_concurrent_edit_and_stale_delete_are_rejected(panel):
    http, app, _ = panel
    item = http.post("/api/feedback", json={"summary": "original"}).json()
    board = FeedbackStore(app.state.store)

    def update(summary):
        try:
            return board.save(
                json.dumps(update_payload(item, summary=summary)).encode(), item["id"]
            )
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["edit A", "edit B"]))
    assert sum(isinstance(result, dict) for result in results) == 1 and 409 in results
    assert http.delete("/api/feedback/" + item["id"] + "?revision=1").status_code == 409
    latest = board.get(item["id"])
    assert latest["revision"] == 2
    assert http.delete("/api/feedback/" + item["id"] + "?revision=2").status_code == 200
    assert http.put("/api/feedback/" + item["id"], json=update_payload(latest)).status_code == 404


def test_image_size_and_count_boundaries(panel):
    from svc_panel.feedback import IMAGE_LIMIT

    http, _, _ = panel
    valid, _ = image_payload()
    accepted = http.post("/api/feedback", json={"summary": "五张图", "images": [valid] * 5})
    assert accepted.status_code == 201 and len(accepted.json()["images"]) == 5
    assert (
        http.post("/api/feedback", json={"summary": "六张图", "images": [valid] * 6}).status_code
        == 422
    )
    too_big = base64.b64encode(b"x" * (IMAGE_LIMIT + 1)).decode()
    assert (
        http.post(
            "/api/feedback", json={"summary": "太大", "images": [{"data_base64": too_big}]}
        ).status_code
        == 422
    )
    assert http.get("/api/feedback").json()["total"] == 1


def test_failed_image_insert_rolls_back_text_and_image_changes(panel):
    import sqlite3

    http, app, _ = panel
    board = FeedbackStore(app.state.store)
    valid, _ = image_payload()
    item = http.post("/api/feedback", json={"summary": "原文", "images": [valid]}).json()
    with app.state.store.connection() as db:
        db.execute(
            "CREATE TRIGGER feedback_insert_failure BEFORE INSERT ON feedback_images BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        board.save(
            json.dumps(
                update_payload(item, summary="不应保存", status="closed", images=[valid])
            ).encode(),
            item["id"],
        )
    assert board.get(item["id"]) == item
