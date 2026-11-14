# MOCK_DELIVERY_001 — Panel V1 管理与观测接口

需求：`agent_mock_service/requirements/MOCK_REQUIREMENT_001.md`。协议：`panel.v1`。
交付日期：2026-11-15（UTC+8）。实现范围仅为 agent_mock_service，未修改 Panel 或匹配算法，交付不包含发布或部署。

## 1. 模块边界与 Web 挂载

- `src/agent_mock_service/app_service.py`：可安装、可启动的 Web 入口；提供 `create_app()` 和全局 `mock_service_app`，暴露业务、注册、调试及全部管理路由。
- `src/agent_mock_service/telemetry/http.py`：纯 ASGI 中间件，管理鉴权、HTTP request_id、状态计数、请求记录、流完成/中断观测。响应记录的持久化在线程池执行，避免阻塞 ASGI 事件循环。
- `src/agent_mock_service/telemetry/store.py`：遥测 SQLite、脱敏、运行时 TraceWriter 适配、服务日志和后台导出任务。
- `src/agent_mock_service/telemetry/schemas.py`：管理与调试响应模型，供 OpenAPI 和运行时响应校验复用。

遥测属于可选运维适配，不并入 mock_core。所有 Web 接口按契约挂载于同一个 `mock_service_app`，不另开监听端口。原 app_service.py 的占位注册、拼写错误装饰器及未实现函数已替换；不加载含占位文件依赖的 business/default_components.py。

默认 Web 应用使用框架现有 HashingEncoder、默认业务插件与 neutral calibration，无需下载模型。它提供确定性开发基线，真实语义 embedding/业务模型仍需通过现有运行时注入能力适配。

## 2. 启动、安装与持久化

在 `agent_mock_service` 根目录：

```bash
python -m venv .venv
. .venv/bin/activate
pip install '.[api]'
export MOCK_ADMIN_TOKEN='replace-with-your-admin-token'
# Panel 部署时设置 MOCK_INSTANCE_ID 为该实例的 UUID。
python -m uvicorn agent_mock_service.app_service:mock_service_app --host 0.0.0.0 --port 32882
```

端口通过 uvicorn 的 `--port` 覆盖。一个 Panel 实例对应一个进程（默认一个 worker）；不要用多 worker 共享该实例的内存计数和 boot_id。

| 环境变量 | 默认值 | 语义 |
| --- | --- | --- |
| MOCK_INSTANCE_ID | 首次启动生成 UUID | 首次写入 data/instance_id；后续重启沿用。配置与已有文件不符时拒绝启动，避免悄悄切换实例身份 |
| MOCK_ADMIN_TOKEN | 空 | 空时受保护接口 503；错误凭据 401；公开 health 可用 |
| MOCK_DATA_DIR | ./data | instance_id、mock.sqlite、telemetry.sqlite、exports/ |
| MOCK_LOG_DIR | ./logs | logs.jsonl 服务日志 |
| MOCK_TRACE_DIR | ./traces | traces.jsonl 运行时 trace |
| MOCK_TELEMETRY_BODY_LIMIT | 8192 | 每个请求体/响应体的记录字节上限；超过上限整段省略，truncated=true；0 表示省略所有非空 body |
| MOCK_REDACT_FIELDS | password,token,secret,authorization,cookie,api_key,api-key,set-cookie | 大小写不敏感的字段名子串，递归脱敏；敏感认证/Cookie/API key 头始终脱敏 |
| MOCK_DATABASE_HIDDEN_TABLES | 空 | 逗号分隔的只读查看隐藏表名 |
| MOCK_DATABASE_HIDDEN_COLUMNS | 空 | 逗号分隔的列名或 table.column，控制只读查看 |
| MOCK_EXPORT_TTL_SECONDS | 86400 | 导出任务与产物保留时长；创建、查询或下载导出时清理过期资源 |

启动自动建目录和数据库；退出先停执行资源，再关 Repository，等待导出任务并关闭遥测连接。实例身份和请求/trace/日志跨重启保留；boot_id、metrics 和导出任务索引属于本次进程。重启前的导出 ID 返回 404，可重新创建快照；遗留产物在 TTL 清理时回收。请求/trace/日志历史目前不自动过期，运维应按存储容量管理历史数据。

`/health` 只报告存活；`/ready` 检查应用就绪标志和 Repository 连接，失败返回 not_ready/503。管理接口和调试接口不进入 runtime.handle，不改变 Lane 或调用计数。

## 3. Panel 联调接口

除 `/health` 与业务 query 外，`/ready`、`/openapi.json`、`/registry`、`/debug/*`、`/management/v1/*` 以及文档 UI 均需 `Authorization: Bearer <MOCK_ADMIN_TOKEN>`。管理错误统一为 error.code/message 与 request_id。就绪依赖失败按契约返回 status/reason。

### 注册与真实业务 endpoint

```http
POST /registry
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "endpoint_id": "/weather",
  "mocked_query": "北京今天天气",
  "mocked_answer": "晴",
  "sample_id": "weather-demo-1",
  "round_id": 1,
  "position_id": 1,
  "round_position_completeness": true
}
```

返回 sample_hash、endpoint_id。endpoint 必须是以 `/` 开头、由字母数字、下划线、连字符组成的路径段，不能使用服务保留命名空间；非法输入 422，场景冲突 409。采用默认特征提取器，不开放虚假的业务特征入参。

注册后立即增加真正的 `POST /weather` 路由，并刷新 OpenAPI；重启从 Repository 恢复。重复注册复用同一路由，样本注册语义仍由现有框架决定。需要逐条构造多位置/多轮场景时应先使用 `round_position_completeness=false`，遵循框架已有封口规则。

```http
POST /weather
Content-Type: application/json
X-Request-ID: demo-001

{"query":"北京今天天气"}
```

默认 JSON 响应由 runtime 生成，例如 `{"answer":"晴"}`；请求体 query 为必填字符串。`Accept: text/event-stream` 返回 SSE，状态码保留 runtime 结果，事件 data 中承载 JSON body。业务 query 当前不额外要求管理 token，具体业务鉴权可在业务适配中增加。

- `POST /debug/bm25`：JSON `{ "query": "北京", "topK": 50, "endpoint": "/weather" }`；topK 1..200，endpoint 可省略；返回 sample_hash/query/score，直接使用现有 BM25 算法，不调用 handle。
- `POST /debug/embedding`：JSON `{ "query": "北京", "candidates": ["北京天气"] }`；返回当前 encoder 指纹及各文本向量内积，候选最多 200 条。
- `GET /management/v1/endpoints` 返回真实路由、method/category/operation_id，包含动态业务路径。
- `GET /openapi.json` 为 OpenAPI 3.x，覆盖请求/响应模型、参数范围、默认值、示例、管理 Bearer 安全定义和 SSE 返回格式。Panel 应通过接口发现，不直接导入 Python 组件。

### 观测与历史

- `GET /management/v1/info` 声明全部六种能力：metrics、requests、traces、logs、database、exports。
- `GET /management/v1/metrics` 返回本 boot 内单调累计的 endpoint/method/category/status_code 计数和 duration_seconds_sum。只有规范化路由模板进入维度；未知路由为 `<unmatched>`。health/ready/metrics 轮询不累计 counter，但仍有请求记录。无其它调用时 counters=[]；Panel 默认只汇总 category=business。
- `X-Panel-Request-Origin: console` 的业务试调归 debug。422/404/500 保留实际 HTTP 状态；SSE 中断通过 interrupted/error 表达，已发出的 HTTP 状态不会改写为 500。
- 请求列表支持 limit/cursor/endpoint/method/non_200/since/until；时间过滤要求带时区并转换为 UTC，返回外层时间均为带 Z 的 RFC3339。
- trace 列表支持 limit/cursor/request_id；日志列表支持 limit/cursor/level。请求与 trace 默认 50、上限 200；日志默认 100、上限 500。基于持久化递增序号倒序分页，新记录不造成既有下一页重复；cursor 跨资源复用或非法格式返回 422。
- 请求详情包含脱敏 headers/body/response_body、truncated、trace_id、interrupted。JSON 以外的 body 整段省略；不记录原始凭据。列表不返回敏感 body。所有成功/失败 HTTP 响应带 X-Request-ID。
- 合法 X-Request-ID 沿用：1..128 个 ASCII 字母数字或 `_.:-`；否则生成新 ID，以响应头为准。建议调用方为每次请求生成唯一 ID。
- trace 外层 request_id 与 HTTP ID 一致；event 保留原始运行时事件，其 request_id 是框架内部 ID，upstream_request_id 映射到 HTTP ID。校验失败无运行时 trace，trace_id=null。查看历史不重算业务或调试过程。
- 日志包含该服务命名空间的框架日志、启动记录和每次 HTTP 的结构化摘要；无任意文件路径入参。请求/trace/日志 API 从 SQLite 查询，JSONL 仅为同步的持久化副本。
- 遥测持久化失败被隔离，既有业务查询仍可成功；此时记录可能缺失，不承诺磁盘故障时仍能完整审计。

### 数据库与导出

- `GET /management/v1/database/tables`、`GET /management/v1/database/tables/{table}/rows` 仅 SELECT/PRAGMA，表名来自 sqlite_master 的受控集合；limit 1..200、offset 非负，未知表 404，不提供 SQL 执行入口。
- `POST /management/v1/exports` 的 JSON 为 `{ "kind": "database" }`（或 logs/traces），总是先返回 202/pending；通过 `GET /management/v1/exports/{id}` 轮询 pending/running/ready/failed；下载路由为同路径追加 `/download`。
- 数据库使用独立 SQLite 连接的在线 backup，包含已提交 WAL 数据，是可独立打开的一致快照，不复制裸 .db。
- logs/traces 导出 ZIP，内部文件为 logs.jsonl/traces.jsonl；截止点为后台任务开始读取时的最高持久化事件序号。压缩通过独立 WAL 读连接流式完成，不占用业务写锁，也不把全部历史读入内存。
- 下载返回二进制及 Content-Disposition；未 ready 为 409，未知/过期为 404。filename 仅服务端生成的文件名，失败给出可读 error。
- 表/列隐藏配置只作用于在线只读查看；database 导出是完整业务库备份。导出任务索引不跨进程保留，见启动章节。

路径、启动参数、必需响应字段未改变，未改写需求文档。新增 interrupted 字段用于表达契约要求的 SSE 中断信息；上述业务请求/注册协议是本次 mock 侧明确的业务适配协议，Panel 可直接依 OpenAPI 生成表单。

## 4. Docker

在仓根构建（仅本地构建，不推送镜像）：

```bash
docker build -t agent-mock-service:panel-v1 agent_mock_service
docker run --rm -p 32882:32882 -e MOCK_ADMIN_TOKEN="$MOCK_ADMIN_TOKEN" \
  -v mock-data:/data -v mock-logs:/logs -v mock-traces:/traces \
  agent-mock-service:panel-v1
```

镜像包含 Python、源码和已安装 api extra，CMD 为约定 uvicorn 命令，支持直接追加命令覆盖端口和启动配置。持久化需挂载上述卷。

本执行环境存在 HTTPS 代理自签 CA，普通构建首次因证书校验失败；验证时以 BuildKit secret 临时提供 CA，证书不写入镜像，不关闭 TLS 校验：

```bash
docker build --secret id=pip_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t agent-mock-service:panel-v1-local agent_mock_service
```

## 5. 验证结果

- 干净虚拟环境仅 `pip install './agent_mock_service[api]'`，非 editable wheel 安装成功；在源码目录以外启动约定模块、覆盖端口，经真实 HTTP 验证 health/ready/info、registry、业务 query、OpenAPI 成功。
- Docker 本地构建成功；默认 CMD/32882 和覆盖 CMD/32991 均运行真实 HTTP 冒烟测试成功，测试容器已停止。未发布镜像。
- `pytest agent_mock_service/tests -o addopts='' -q`：143 passed。新增 10 项 ASGI/持久化契约测试，覆盖管理拒绝服务/鉴权、重启身份、发现/模型、JSON/SSE、脱敏、422/404/500、分页、未知资源、超限 body、历史恢复、遥测故障隔离、导出 pending/failed/过期、持续 WAL 写入下的在线备份，以及流中断不篡改状态码。
- 修正两处既有回归测试的目录迁移残留：sse_event_stream 从 mock_core.response 导入；跨进程测试脚本从 mock_core 下导入 repository/context/defaults/domain/derived。核心实现未修改。
- 新增/修改的服务模块及新增测试通过 Ruff 检查和格式化；git diff --check 通过。
- 测试环境 FastAPI 0.163.0 / Starlette 1.10.0 / Python 3.12。Starlette 对 httpx TestClient 有一条弃用提示，不影响通过。

复现测试：

```bash
pip install -e './agent_mock_service[api]' pytest httpx
pytest agent_mock_service/tests -o addopts='' -q
```

Panel 集成尚未做两侧端到端联调；可据本说明及在线 OpenAPI 开始联调。
