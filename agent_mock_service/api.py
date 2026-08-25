from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from .domain import MockRequest, ResponseProtocol
from .runtime import AgentMockRuntime


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
            protocol = ResponseProtocol.SSE if "text/event-stream" in headers.get("accept", "") else None
            response = self.runtime.handle(
                MockRequest(
                    endpoint_id=str(environ.get("PATH_INFO", "/")),
                    body=body,
                    headers=headers,
                    upstream_request_id=headers.get("x-request-id"),
                    response_protocol=protocol,
                )
            )
            payload = response.body if isinstance(response.body, str) else json.dumps(response.body, ensure_ascii=False)
            statuses = {200: "OK", 404: "Not Found", 409: "Conflict", 500: "Internal Server Error"}
            start_response(f"{response.status_code} {statuses.get(response.status_code, 'Unknown')}", list(response.headers.items()))
            return [payload.encode("utf-8")]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            start_response("400 Bad Request", [("content-type", "application/json")])
            return [json.dumps({"error": str(error)}).encode("utf-8")]
