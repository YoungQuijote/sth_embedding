"""ASGI instrumentation captures final status and streaming completion/disconnect."""

import hmac
import logging
import re
import time
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .store import request_context, utc


class ObservationMiddleware:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        supplied = headers.get("x-request-id", "")
        identifier = (
            supplied if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", supplied) else uuid.uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = identifier
        token = request_context.set(identifier)
        start, timestamp = time.perf_counter(), utc()
        status, finished, interrupted, started = 500, False, False, False
        body, response = bytearray(), bytearray()
        total_body = total_response = 0
        store = getattr(self.state, "telemetry", None)
        limit = store.body_limit if store else 0
        path = scope["path"]
        protected = path in (
            "/ready",
            "/openapi.json",
            "/registry",
            "/docs",
            "/redoc",
            "/docs/oauth2-redirect",
        ) or path.startswith(("/management/", "/debug/"))

        async def observed_receive():
            nonlocal total_body, interrupted
            message = await receive()
            if message["type"] == "http.request":
                raw = message.get("body", b"")
                total_body += len(raw)
                body.extend(raw[: max(0, limit + 1 - len(body))])
            elif message["type"] == "http.disconnect":
                interrupted = True
            return message

        async def observed_send(message):
            nonlocal status, finished, total_response, started
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ] + [(b"x-request-id", identifier.encode())]
            elif message["type"] == "http.response.body":
                raw = message.get("body", b"")
                total_response += len(raw)
                response.extend(raw[: max(0, limit + 1 - len(response))])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True

        try:
            expected = getattr(self.state, "admin_token", "")
            if protected and (
                not expected
                or not hmac.compare_digest(
                    headers.get("authorization", "").encode(), ("Bearer " + expected).encode()
                )
            ):
                code = 503 if not expected else 401
                await JSONResponse(
                    {
                        "error": {
                            "code": "admin_unconfigured" if code == 503 else "unauthorized",
                            "message": "Management token is not configured"
                            if code == 503
                            else "Invalid management credentials",
                        },
                        "request_id": identifier,
                    },
                    status_code=code,
                )(scope, observed_receive, observed_send)
            else:
                await self.app(scope, observed_receive, observed_send)
        except Exception:  # noqa: BLE001 - isolate application and optional telemetry failures
            if not started:
                await JSONResponse(
                    {
                        "error": {"code": "internal_error", "message": "Internal server error"},
                        "request_id": identifier,
                    },
                    status_code=500,
                )(scope, observed_receive, observed_send)
            else:
                interrupted = True
        finally:
            if store:
                elapsed = max(0, time.perf_counter() - start)

                def record():
                    try:
                        route = scope.get("route")
                        endpoint = getattr(route, "path", "<unmatched>")
                        if endpoint == "/{endpoint_path:path}":
                            endpoint = (
                                path
                                if path in getattr(self.state, "business_paths", set())
                                else "<unmatched>"
                            )
                        category = (
                            "probe"
                            if path in ("/health", "/ready")
                            else "management"
                            if protected and not path.startswith(("/debug/", "/registry"))
                            else "registry"
                            if path == "/registry"
                            else "debug"
                            if path.startswith("/debug/")
                            or headers.get("x-panel-request-origin") == "console"
                            else "business"
                        )
                        with store.lock:
                            key = (endpoint, scope["method"], category, status)
                            counter = store.counters.setdefault(
                                key,
                                {
                                    "endpoint": endpoint,
                                    "method": scope["method"],
                                    "category": category,
                                    "status_code": status,
                                    "requests_total": 0,
                                    "duration_seconds_sum": 0.0,
                                },
                            )
                            if path not in ("/health", "/ready", "/management/v1/metrics"):
                                counter["requests_total"] += 1
                                counter["duration_seconds_sum"] += elapsed
                            elif counter["requests_total"] == 0:
                                store.counters.pop(key, None)
                        request_body, truncated = store.body(bytes(body))
                        response_body, response_truncated = store.body(bytes(response))
                        traces = store.page("traces", 1, filters={"request_id": identifier})[
                            "items"
                        ]
                        store.add(
                            "requests",
                            identifier,
                            {
                                "request_id": identifier,
                                "timestamp": timestamp,
                                "endpoint": endpoint,
                                "method": scope["method"],
                                "category": category,
                                "status_code": status,
                                "duration_ms": elapsed * 1000,
                                "headers": store.redact(headers),
                                "body": request_body,
                                "response_body": response_body,
                                "trace_id": traces[0]["trace_id"] if traces else None,
                                "truncated": truncated
                                or response_truncated
                                or total_body > limit
                                or total_response > limit,
                                "interrupted": interrupted or not finished,
                                "error": "Stream interrupted"
                                if interrupted or not finished
                                else "HTTP " + str(status)
                                if status >= 400
                                else None,
                            },
                        )
                        store.add(
                            "logs",
                            uuid.uuid4().hex,
                            {
                                "timestamp": utc(),
                                "level": "ERROR"
                                if status >= 500
                                else "WARNING"
                                if status >= 400
                                else "INFO",
                                "message": f"{scope['method']} {endpoint} {status}",
                                "request_id": identifier,
                            },
                        )
                    except Exception:  # noqa: BLE001 - isolate application and optional telemetry failures
                        logging.getLogger(__name__).error("HTTP telemetry persistence failed")

                await run_in_threadpool(record)
            request_context.reset(token)
