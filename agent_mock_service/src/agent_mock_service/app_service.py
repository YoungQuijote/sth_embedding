"""
Demo AppService
"""

from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from loguru import logger


mock_service_app = FastAPI()

@mock_service_app.exception_hanlder()
async def log_validation_error(
  request: Request,
  exc: RequestValidationError
):
  ...


@mock_service_app.get("/health")
def health() -> Response:
  ...


@mock_service_app.post("/registry")
def registry(
  raw_input: dict,
  request: Request
) -> None:
  ...


@mock_service_app.post("/debug/embedding")
def debug_embedding(
  query: str,
  candidates: list[str]
) -> Respones:
  """反射/直接引用内部embedding模块, 暴露其计算接口, 用于调试/回溯QC相似度计算结果"""
  ...


@mock_service_app.post("/debug/bm25")
def debug_bm25(
  query: str,
  top_k: int
) -> Response:
  """反射/直接引用内部bm25模块, 暴露其计算接口, 用于调试/回溯query-topK检索结果"""
  ...
  
