# MOCK_REQUIREMENT_001 — Control Panel V1 管理与观测接口契约

状态：供 mock 侧并行实现；Panel 将按此契约开发。协议版本：panel.v1。
范围：所有接口在 agent_mock_service/app_service.py 的 mock_service_app 中挂载。保留 mock_core 独立性，具体实现可拆模块。无需实现 Panel、SSH 部署执行器、Grafana 或 Prometheus。
本需求来自用户授权的 Panel V1；请实现后列明不一致项，双方再联调。

## 1. 服务启动和认证
- 启动命令：python -m uvicorn agent_mock_service.app_service:mock_service_app --host 0.0.0.0 --port 32882（实际端口可覆盖）。
- 环境变量：MOCK_INSTANCE_ID（Panel 生成的 UUID）；MOCK_ADMIN_TOKEN（管理 bearer token）；MOCK_DATA_DIR（可写数据目录，默认 ./data）；MOCK_LOG_DIR（默认 ./logs）；MOCK_TRACE_DIR（默认 ./traces）。
- 启动时创建目录，SQLite、日志、trace 持久化在上述目录下。关闭时清理 Repository 和执行资源。
- 裸机源码 tar：顶层单一目录或平铺 Python 项目均可，pyproject.toml 应在项目根目录，支持 pip install '.[api]'，该 extra 必须包含启动 web app 所需依赖。Docker 镜像包含上述模块和 Python，并支持覆盖启动命令。
- /health 为不带敏感信息的公开存活检查；/ready、/openapi.json、/management/v1/*、/registry、/debug/* 使用 Authorization: Bearer <MOCK_ADMIN_TOKEN>。业务 query 认证由业务定义，Panel 试调允许用户填头。
- 未配置管理 token 时管理接口拒绝服务（503），不能默认无认证；认证失败 401/403。
- UTC 时间统一 RFC3339 带 Z；资源 ID 不透明；所有响应默认 JSON。管理接口错误：{"error":{"code":"...","message":"..."},"request_id":"..."}。
- 成功/失败响应都返回 X-Request-ID；输入 X-Request-ID 经长度和字符校验后可沿用，或提供映射。日志、HTTP 请求记录和 runtime trace 使用可关联的 request_id。
- HTTP 中间件记录校验失败、异常和正常响应。Authorization/Cookie/API key 等敏感头必须脱敏，请求体记录限制大小且可配置脱敏。
- 健康检查、监控、管理请求不得修改 mock 样本调用计数和 Lane。

## 2. 健康、能力与发现
GET /health -> 200 {"status":"ok"}；只检查进程存活。
GET /ready -> 200 {"status":"ready","instance_id":"...","boot_id":"...","started_at":"..."}；
依赖未就绪返回 503 {"status":"not_ready","reason":"..."}。
boot_id 每次进程启动变化；instance_id 持久不变。
GET /management/v1/info ->
{"contract_version":"panel.v1","instance_id":"...","boot_id":"...","started_at":"...",
 "capabilities":["metrics","requests","traces","logs","database","exports"]}
GET /management/v1/endpoints ->
{"items":[{"id":"debug.bm25","path":"/debug/bm25","method":"POST",
 "category":"debug","summary":"BM25 调试","operation_id":"debug_bm25"}]}
category 枚举 business/registry/debug/management。列出真实可调用业务 endpoint，不能只返回一个通配路由。
GET /openapi.json：OpenAPI 3.x，覆盖 query、registry、debug、管理接口，包含 JSON Schema、必填项、默认值、示例、路径/查询参数。
例如 POST /debug/bm25 JSON body {"query":"文本","topK":50}；使用明确的 Pydantic 请求模型，不能误定义为 query 参数。
若业务 endpoint 动态注册，其路径须出现在 endpoints，OpenAPI 提供对应路径或可复用请求模型；无模型时 Panel 回退 JSON 编辑。
query / registry / debug 的具体业务协议由 mock 侧定义并在 OpenAPI 完整表达；Panel 不直接调用 Python 内部模块。

## 3. 内置监控（不依赖外部监控服务）
GET /management/v1/metrics ->
{"instance_id":"...","boot_id":"...","sampled_at":"...",
 "counters":[{"endpoint":"/weather","method":"POST","category":"business",
 "status_code":200,"requests_total":12,"duration_seconds_sum":0.36}]}
- counters 为当前 boot_id 内单调累计值；每个 endpoint+method+category+status_code 唯一。
- endpoint 使用规范化路由模板，不能将 query string、request ID 放入维度；未知路由使用受控标签。
- 计数覆盖真实 HTTP 状态码，包括 422、404、500；持续时间在响应结束时记录。SSE 还应在请求明细记录中断信息，不能把流中断伪装成新的 HTTP 状态码。
- category business/registry/debug/management/probe；Panel 试调带 X-Panel-Request-Origin: console，该请求归 debug（或另行返回 origin 并支持隔离），Panel 主概览只默认统计 business。
- metrics/health 请求不计入 business；无业务调用返回 counters:[]。
- 秒数为非负有限数；Panel 用相邻样本增量计算近似时间窗口请求数和 QPS，识别重启及采集中断。无需 mock 侧承担历史趋势聚合。

## 4. 请求记录与 trace
GET /management/v1/requests?limit=50&cursor=...&endpoint=...&method=...&non_200=true&since=...&until=...
-> {"items":[{"request_id":"...","timestamp":"...","endpoint":"/weather","method":"POST",
"status_code":500,"duration_ms":12.4,"category":"business","trace_id":"...",
"error":"错误摘要"}],"next_cursor":null}
GET /management/v1/requests/{request_id} ->
{"request_id":"...","timestamp":"...","endpoint":"...","method":"POST","status_code":500,
"duration_ms":12.4,"headers":{},"body":{},"response_body":{},"trace_id":"...",
"truncated":false,"error":null}
GET /management/v1/traces?limit=50&cursor=...&request_id=...
-> {"items":[{"trace_id":"...","request_id":"...","timestamp":"...","endpoint":"..."}],"next_cursor":null}
GET /management/v1/traces/{trace_id} -> {"trace_id":"...","request_id":"...","event":{原始运行时trace}}
limit 1..200，默认 50；未知 ID 404；列表顺序稳定，最新优先。cursor 不透明，不允许无界全量读取。
保留 JSONL 可以；API 应隔离文件格式，使未来迁移 SQLite 无需 Panel 改动。
无 Runtime trace 的校验失败请求仍有 request 记录，trace_id=null。查询历史记录不得重新执行请求或 debug 运算。

## 5. 日志与数据库只读查看
GET /management/v1/logs?limit=100&cursor=...&level=... ->
{"items":[{"timestamp":"...","level":"INFO","message":"...","request_id":null}],"next_cursor":null}
limit 1..500；只返回服务日志，不接受任意文件路径。
GET /management/v1/database/tables ->
{"items":[{"name":"scenario_membership","columns":[{"name":"scenario_id","type":"TEXT"}]}]}
GET /management/v1/database/tables/{table}/rows?limit=50&offset=0 ->
{"table":"scenario_membership","columns":["scenario_id"],"rows":[{"scenario_id":"s1"}],"next_offset":null}
limit 1..200；offset 非负；只读且表名从服务端已知表集合校验。不得开放任意 SQL 执行，表/列敏感信息可按配置隐藏。

## 6. 文件回流
POST /management/v1/exports JSON {"kind":"database"|"logs"|"traces"}
-> 202 {"export_id":"...","kind":"database","status":"pending","filename":null,"size_bytes":null,"error":null}
GET /management/v1/exports/{export_id} ->
{"export_id":"...","kind":"database","status":"pending"|"running"|"ready"|"failed",
"filename":"database-....sqlite","size_bytes":123,"error":null}
GET /management/v1/exports/{export_id}/download -> 二进制文件 + Content-Disposition。
未就绪 409；不存在或过期 404；失败状态含可读 error。
- 数据库必须使用 SQLite 在线 backup 等一致性快照方式，不能仅复制正在 WAL 写入的主 .db 文件。
- logs/traces 可导出 zip/tar.gz/jsonl；运行期间导出应有明确截止点，不阻塞长时间业务请求。
- filename 仅文件名，不能包含路径；导出为服务端生成的受控文件。
- 导出产物默认保留 24h，可配置；任务和下载均需管理认证。
- Panel 将流式下载到自身存储，不要求 mock 主动回连 Panel。

## 7. 验收
1. 可用约定启动命令从干净的已安装 api extra 环境启动，默认端口可覆盖。
2. 管理鉴权与健康、就绪语义正确，重启后 boot_id 变化。
3. endpoints + OpenAPI 足够生成请求表单，query/registry/debug 可真实调用。
4. 连续调用业务接口成功/失败/校验失败后，metrics 计数、请求明细、X-Request-ID 可以相互核对；探测和试调不污染业务概览。
5. 列表分页有界，非法参数 422，未知资源 404，错误格式统一；认证头与敏感字段不泄露。
6. 正在写入的 SQLite 仍可导出可打开的一致快照；日志和 trace 可查看与导出。
7. 管理接口、metrics 和健康探测不改变样本/Lane，管理失败不拖垮查询服务。
8. 用真实 ASGI 请求验证 JSON、SSE、认证、OpenAPI、分页、异常和导出；更新相关交付文档。
9. 若暂不具备某项 capability，从 info.capabilities 明确缺省并返回 501，不能伪造空数据为实现成功；V1 完整验收仍要求上述能力完成。

## 8. 并行协作约定
Panel 以此文件为契约源；如需更改路径、字段或启动配置，请在此文档记录差异并通知用户安排同步，避免两侧默默偏离。mock 侧负责 agent_mock_service 下实现与交付记录，Panel 侧负责 service_control_panel。本文不要求改动现有匹配算法。
