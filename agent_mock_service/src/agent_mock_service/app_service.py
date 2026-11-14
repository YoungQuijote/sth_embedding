"""
Demo AppService
"""

from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from agent_mock_service.mock_core.repository import SQLiteRepository
from agent_mock_service import *
from agent_mock_service.business import *

from loguru import logger


mock_service_app = FastAPI()
app_repository = SQLiteRepository(path="agent-mock.db")
plugin_registry = BusinessPluginRegistry()
execution_resource_pool = ExecutionResourcePool()


# endpint映射
ENDPOINT_MAPPING = {}

# 特征提取插件注册
extracor_route = {}

# bm25分词器注册
bm25_tokenizer_route = {}

# 任务插件注册
plugin_registry.register()

# 执行资源注册
execution_resource_pool.register()

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
) -> Response:
  """反射/直接引用内部embedding模块, 暴露其计算接口, 用于调试/回溯QC相似度计算结果"""
  ...


@mock_service_app.post("/debug/bm25")
def debug_bm25(
  query: str,
  top_k: int
) -> Response:
  """反射/直接引用内部bm25模块, 暴露其计算接口, 用于调试/回溯query-topK检索结果"""
  ...
  
