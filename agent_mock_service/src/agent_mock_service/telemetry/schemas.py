"""Panel response schemas, shared by HTTP discovery and generated OpenAPI."""

from typing import Any, Literal

from pydantic import BaseModel

Category = Literal["business", "registry", "debug", "management", "probe"]


class Health(BaseModel):
    status: Literal["ok"]


class Identity(BaseModel):
    instance_id: str
    boot_id: str
    started_at: str


class Ready(Identity):
    status: Literal["ready"]


class Info(Identity):
    contract_version: Literal["panel.v1"]
    capabilities: list[str]


class Endpoint(BaseModel):
    id: str
    path: str
    method: str
    category: Literal["business", "registry", "debug", "management"]
    summary: str
    operation_id: str


class Endpoints(BaseModel):
    items: list[Endpoint]


class Counter(BaseModel):
    endpoint: str
    method: str
    category: Category
    status_code: int
    requests_total: int
    duration_seconds_sum: float


class Metrics(BaseModel):
    instance_id: str
    boot_id: str
    sampled_at: str
    counters: list[Counter]


class RequestSummary(BaseModel):
    request_id: str
    timestamp: str
    endpoint: str
    method: str
    category: Category
    status_code: int
    duration_ms: float
    trace_id: str | None
    error: str | None
    interrupted: bool


class RequestDetail(RequestSummary):
    headers: dict[str, str]
    body: Any
    response_body: Any
    truncated: bool


class Requests(BaseModel):
    items: list[RequestSummary]
    next_cursor: str | None


class TraceSummary(BaseModel):
    trace_id: str
    request_id: str | None
    timestamp: str
    endpoint: str | None


class TraceDetail(TraceSummary):
    event: dict[str, Any]


class Traces(BaseModel):
    items: list[TraceSummary]
    next_cursor: str | None


class Log(BaseModel):
    timestamp: str
    level: str
    message: str
    request_id: str | None


class Logs(BaseModel):
    items: list[Log]
    next_cursor: str | None


class Column(BaseModel):
    name: str
    type: str


class Table(BaseModel):
    name: str
    columns: list[Column]


class Tables(BaseModel):
    items: list[Table]


class Rows(BaseModel):
    table: str
    columns: list[str]
    rows: list[dict[str, Any]]
    next_offset: int | None


class Export(BaseModel):
    export_id: str
    kind: Literal["database", "logs", "traces"]
    status: Literal["pending", "running", "ready", "failed"]
    filename: str | None
    size_bytes: int | None
    error: str | None


class Registered(BaseModel):
    sample_hash: str
    endpoint_id: str


class BM25Item(BaseModel):
    sample_hash: str
    query: str
    score: float


class BM25Result(BaseModel):
    items: list[BM25Item]


class EmbeddingItem(BaseModel):
    text: str
    score: float


class EmbeddingResult(BaseModel):
    encoder: str
    items: list[EmbeddingItem]


class ErrorDetail(BaseModel):
    code: str
    message: str


class Error(BaseModel):
    error: ErrorDetail
    request_id: str
