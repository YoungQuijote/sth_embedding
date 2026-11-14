# 该文件从语义级别记录需求录入, 代码变更, 接口变更.

# 该文件只增不减

---

## 记录格式及字段语义说明

### 需求标题: 填写需求的简述标题，可以是commit时的短文本描述

**需求文档**: 填写此次变更是为了解决哪篇录入的需求文档
如果需求文档记录在/agent_mock_service/requirements/目录下，则填写对应路径，例如：/agent_mock_service/requirements/MOCK_REQUIREMENT_XXX.md；
如果需求文档不记录在/agent_mock_service/requirements/目录下，则填写`cloud`；
如果没有对应需求文档，则填写`null`

**解决方案**: 简述语义级别的解决方案，例如：新增dag模块提供图构建能力，提升预置用例节点间的关联关系；优化fuse流程，以提升query侧检索性能；etc.

**交付文档**: 填写`详细的变更说明/交付文档`所在路径
约定将`详细文档`记录在/agent_mock_service/deliveries目录下，
`详细文档`的命名格式为`MOCK_DELIVERY_XXX.md`，其中MOCK_DELIVERY_为固定前缀，如果有对应的需求文档，则XXX与对应需求文档的编号一致；
如果没有对应的需求文档或对应需求文档没有编号，则XXX为`yyyyMMddHHmm`的(UTC+8)时间字符串，例如`202611130600`；时间以你追加变更记录前调用的时间工具所给时间为准

**变更时间**: 提交代码的日期(yyyyMMdd)

**代码变更**: 记录发生了变更的代码文件，只记录文件路径，不做row级别的细节变更记录
约定如下记录符号：
`+ <文件路径>`: 新增<文件路径> ;
`- <文件路径>`: 删除<文件路径> ;
`<文件路径> +N`: <文件路径>新增N行代码，N为整数类型 ;
`<文件路径> -N`: <文件路径>删除N行代码，N为整数类型 ;
`<文件路径> +N/-M`: <文件路径>新增N行代码，同时删除M行代码，N和M为整数类型 ;
`<文件路径A> > <文件路径B>`: <文件路径A>变更为了<文件路径B>，用于描述重命名或文件移动 ;

**接口变更**: 记录发生了变化的功能接口
约定如下记录符号：
`+ package.module.fn(args); <fn功能简述>`: 新增功能为<fn功能简述>的功能接口package.module.fn ;
`- package.module.fn(args)`: 删除功能接口fn ;
`packageA.moduleA.fnA(argsA) > packageB.moduleB.fnB(argsB)`: 功能接口 packageA.moduleA.fnA 变更为了功能接口 packageB.moduleB.fnB；该记录一般用于表示同功能(或同性质)的接口发生了移动、归属变更或参数变更 

(写完后，用`\n + --- + \n`和下一段分隔)

---

### 需求标题: 变更填写示例

**需求文档**：/agent_mock_service/requirements/MOCK_REQUIREMENT_000.md

**解决方案**: 追加了一段变更记录，示例变更记录如何填写

**交付文档**: /agent_mock_service/deliveries/MOCK_DELIVERY_000.md

**代码变更**: null

**接口变更**: null

---

### 需求标题: 实现 Panel V1 管理、观测和文件回流契约

**需求文档**: /agent_mock_service/requirements/MOCK_REQUIREMENT_001.md

**解决方案**: 将占位 Web app 实现为可安装运行的统一 FastAPI 入口；新增 telemetry 适配层承载管理鉴权、HTTP 关联、监控、持久化请求/trace/日志、后台一致性导出和 OpenAPI 响应模型。业务注册生成真实可发现路由，提供 JSON/SSE 与只读调试接口；保留 mock_core 算法独立性。补充 Docker 构建入口，修正目录迁移后两处回归测试导入。

**交付文档**: /agent_mock_service/deliveries/MOCK_DELIVERY_001.md

**变更时间**: 20261115

**代码变更**:

- /agent_mock_service/src/agent_mock_service/app_service.py +465/-55
- + /agent_mock_service/src/agent_mock_service/telemetry/__init__.py
- + /agent_mock_service/src/agent_mock_service/telemetry/http.py
- + /agent_mock_service/src/agent_mock_service/telemetry/store.py
- + /agent_mock_service/src/agent_mock_service/telemetry/schemas.py
- + /agent_mock_service/tests/test_panel_v1.py
- /agent_mock_service/tests/test_materialization_persistence.py +5/-5
- /agent_mock_service/tests/test_v11_gaps.py +1/-1
- + /agent_mock_service/Dockerfile
- + /agent_mock_service/.dockerignore
- /agent_mock_service/README.md +16/-1
- + /agent_mock_service/deliveries/MOCK_DELIVERY_001.md

**接口变更**:

- + agent_mock_service.app_service.create_app(); 创建完整 Panel V1 Web app，生命周期治理 Repository、运行时资源和遥测
- + agent_mock_service.app_service.add_business(app, path); 注册真实业务 HTTP 路由并刷新 OpenAPI
- + agent_mock_service.telemetry.http.ObservationMiddleware(app, state); 管理鉴权、HTTP 关联、状态计数和流完成观测
- + agent_mock_service.telemetry.store.Store(data, logs, traces); 持久化、脱敏、分页查询、TraceWriter 适配及后台导出
- + agent_mock_service.telemetry.store.ServiceLogHandler(store); 捕获服务命名空间日志并关联 request_id
- agent_mock_service.app_service.health() > GET /health; 补全公开存活检查
- agent_mock_service.app_service.registry(body) > POST /registry; 补全样本注册与动态业务路由发现
- agent_mock_service.app_service.debug_embedding(body) > POST /debug/embedding; JSON 请求模型及只读向量调试
- agent_mock_service.app_service.debug_bm25(body) > POST /debug/bm25; JSON 请求模型及只读 BM25 调试
- + GET /ready; 受保护的依赖就绪检查
- + GET /management/v1/info; 协议、身份及能力声明
- + GET /management/v1/endpoints; 真实接口发现
- + GET /management/v1/metrics; 本 boot 的 HTTP 单调累计指标
- + GET /management/v1/requests; 有界筛选分页
- + GET /management/v1/requests/{request_id}; 脱敏请求详情
- + GET /management/v1/traces; 有界筛选分页
- + GET /management/v1/traces/{trace_id}; 原始运行时事件及 HTTP 关联
- + GET /management/v1/logs; 服务日志有界查看
- + GET /management/v1/database/tables; 只读表结构发现
- + GET /management/v1/database/tables/{table}/rows; 受控表的只读分页
- + POST /management/v1/exports; 创建数据库/日志/trace 导出任务
- + GET /management/v1/exports/{export_id}; 导出状态及错误
- + GET /management/v1/exports/{export_id}/download; 认证后的受控二进制下载
- + GET /openapi.json; 受保护的完整 OpenAPI，含动态业务路径和模型

---
