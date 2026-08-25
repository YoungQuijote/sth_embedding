from __future__ import annotations

import json
from typing import Any, Protocol

from .domain import ResponseProtocol


class ResponseRenderer(Protocol):
    protocol: ResponseProtocol
    def render(self, answer: str) -> tuple[Any, dict[str, str]]: ...


class HttpJsonRenderer:
    protocol = ResponseProtocol.HTTP_JSON
    def render(self, answer: str) -> tuple[Any, dict[str, str]]:
        try:
            value = json.loads(answer)
        except json.JSONDecodeError:
            value = {"answer": answer}
        return value, {"content-type": "application/json"}


class SseRenderer:
    protocol = ResponseProtocol.SSE
    def render(self, answer: str) -> tuple[str, dict[str, str]]:
        return f"data: {json.dumps({'answer': answer}, ensure_ascii=False)}\n\n", {"content-type": "text/event-stream", "cache-control": "no-cache"}


class ResponseRendererRegistry:
    def __init__(self) -> None:
        self._renderers: dict[ResponseProtocol, ResponseRenderer] = {}
        self.register(HttpJsonRenderer())
        self.register(SseRenderer())

    def register(self, renderer: ResponseRenderer) -> None:
        self._renderers[renderer.protocol] = renderer

    def get(self, protocol: ResponseProtocol) -> ResponseRenderer:
        try:
            return self._renderers[protocol]
        except KeyError as error:
            raise ValueError(f"no renderer registered for {protocol}") from error
