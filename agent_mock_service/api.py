from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from .domain import MockRequest, ResponseProtocol
from .runtime import AgentMockRuntime
from .response import sse_event_stream


class WsgiQueryApi:
    """Small dependency-free WSGI adapter; deploy behind any WSGI server."""

    def __init__(self, runtime: AgentMockRuntime) -> None:
        self.runtime = runtime

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[[str, list[tuple[str, str]]], None],
    ) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD") != "POST":
            start_response("405 Method Not Allowed", [("content-type", "application/json")])
            return [b'{"error":"POST required"}']
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(length)
            body = json.loads(raw.decode("utf-8")) if raw else {}
            headers = {
                key[5:].replace("_", "-").lower(): str(value)
                for key, value in environ.items()
                if key.startswith("HTTP_")
            }
            protocol = (
                ResponseProtocol.SSE if "text/event-stream" in headers.get("accept", "") else None
            )
            response = self.runtime.handle(
                MockRequest(
                    endpoint_id=str(environ.get("PATH_INFO", "/")),
                    body=body,
                    headers=headers,
                    upstream_request_id=headers.get("x-request-id"),
                    response_protocol=protocol,
                )
            )
            payload = (
                response.body
                if isinstance(response.body, str)
                else json.dumps(response.body, ensure_ascii=False)
            )
            if response.headers.get("content-type") == "text/event-stream":
                payload = f"data: {payload}\n\n"
            statuses = {200: "OK", 404: "Not Found", 409: "Conflict", 500: "Internal Server Error"}
            start_response(
                f"{response.status_code} {statuses.get(response.status_code, 'Unknown')}",
                list(response.headers.items()),
            )
            return [payload.encode("utf-8")]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            start_response("400 Bad Request", [("content-type", "application/json")])
            return [json.dumps({"error": str(error)}).encode("utf-8")]


def create_fastapi_app(runtime: AgentMockRuntime) -> Any:
    """Create an optional FastAPI adapter with native streaming SSE transport."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, StreamingResponse

        try:
            from fastapi.sse import EventSourceResponse
        except ImportError:
            EventSourceResponse = None
    except ImportError as error:
        raise RuntimeError("Install agent-mock-service[api] to use the FastAPI adapter") from error

    app = FastAPI()

    @app.post("/{endpoint_path:path}")
    async def query(endpoint_path: str, request: Request) -> Any:
        body = await request.json()
        wants_sse = "text/event-stream" in request.headers.get("accept", "")
        result = runtime.handle(
            MockRequest(
                f"/{endpoint_path}",
                body,
                dict(request.headers),
                request.headers.get("x-request-id"),
                ResponseProtocol.SSE if wants_sse else ResponseProtocol.HTTP_JSON,
            )
        )
        if not wants_sse:
            return JSONResponse(result.body, status_code=result.status_code)

        async def events() -> Any:
            yield {"data": json.dumps(result.body, ensure_ascii=False)}

        if EventSourceResponse is not None:
            return EventSourceResponse(events(), status_code=result.status_code)

        return StreamingResponse(
            sse_event_stream(result.body),
            status_code=result.status_code,
            media_type="text/event-stream",
        )

    return app
