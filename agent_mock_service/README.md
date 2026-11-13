# AgentMockService

AgentMockService 是一个**确定性的 Stateful Agentic-RAG Mock Runtime**。它使用 endpoint
硬隔离、语义/上下文双路召回、校准后的 evidence 与 runtime prior 来选择预注册的
`MockSample`；最终响应始终从 SQLite 中读取，Judge 不生成、也看不到候选答案。

## 快速开始

```python
from agent_mock_service import AgentMockRuntime, MockRequest, MockSample
from agent_mock_service.src.mock_core.repository import SQLiteRepository

repository = SQLiteRepository("agent-mocks.db")
repository.register(MockSample("/weather", "罗马未来七天天气", "晴到多云", "trip-1", 1, 1))

runtime = AgentMockRuntime(repository)
response = runtime.handle(MockRequest("/weather", {"query": "罗马未来七天天气"}))
print(response.body)
```

生产环境可通过 Protocol 替换 QueryParser、EmbeddingEncoder、FeatureExtractor、
FeatureComparator、ContextFusionProvider、AffinityExtractor 与 Judge。默认 hashing encoder
仅用于无外部依赖的开发和测试；需要真正语义匹配时应注入业务 embedding encoder。

可选安装真实语义模型或 FastAPI transport：

```bash
pip install 'agent-mock-service[semantic]'
pip install 'agent-mock-service[api]'
```

通过 `RuntimeConfig(sentence_transformer_encoder="/local/model/path")` 加载本地
SentenceTransformer；路径、依赖或模型不可用时会记录 warning 并安全回退 HashingEncoder。
`create_fastapi_app(runtime)` 提供 JSON 与流式 SSE ASGI transport。

Runtime 是 embedding space 的唯一 owner，并会使用同一个 Encoder 构造 Query、Mocked Query
和 Scenario Context 向量。Bootstrap Calibration 使用独立、相对静态的
`StaticCalibrationCorpusProvider`；未配置 Corpus 时服务使用 neutral profile，Runtime Registry
的增删不会触发 Calibration 重训。

多个业务 Endpoint 应注册到一个共享 Runtime：

```python
from agent_mock_service import BusinessPlugin, BusinessPluginRegistry

plugins = BusinessPluginRegistry(allow_default_plugin=False)
plugins.register(BusinessPlugin(
    endpoint_id="/weather",
    parser=weather_parser,
    feature_extractor=weather_features,
    feature_comparator=weather_comparator,
    fusion=weather_fusion,
    judge=weather_judge,
))
runtime = AgentMockRuntime(repository, business_plugins=plugins)
```

业务组件可用 `execution_resource_id` 显式声明其共享的昂贵后端，并由 Service 级资源池统一治理
进程内并发。使用同一 ID 的 FeatureExtractor、FusionProvider、Judge，以及 Control Plane 调用，
会共享同一个长期存活的 executor；未声明该属性的旧组件仍同步直接执行：

```python
from agent_mock_service import ExecutionResourcePool

resources = ExecutionResourcePool()
resources.register("business-llm", max_concurrency=8)
runtime = AgentMockRuntime(
    repository,
    business_plugins=plugins,
    execution_resources=resources,
)

# 注册阶段也可主动纳入同一容量边界。
features = resources.call_component(extractor, extractor.extract, mocked_query)
```

资源 ID 必须先注册；同一 ID 不允许静默改变容量。该治理仅覆盖当前进程，应用关闭时应调用
`resources.shutdown()`；多进程部署的总并发仍需由外部基础设施约束。
`call_component()` 是“提交并立即等待”的同步 API，适合单次 Feature/Judge 调用；Scenario
Position 和 Lane Context 等批量独立 Fusion 则使用 `submit_component()` 先整体 fan-out、再按原顺序
fan-in。Repository 读取一致事实快照，Position Materializer 只对显式目标进行 Fusion fan-out，
SQLite 操作不进入业务线程池。

Repository、Encoder、Lane、Calibration 和 Trace 保持 Service 级共享；Parser、Feature、Fusion、
Affinity 与 Judge 按请求 Endpoint 路由。SQLite 的 `(scenario_id, position_id)` 现在是跨
Endpoint 唯一键；含有重复位置的旧数据库会在启动时被拒绝，需要先迁移或重建。

## Repository 生命周期

`sample_hash` v3 覆盖 Endpoint、Scenario、Round、Position、Query 和 Answer。Feature 是由业务
Extractor 生成的派生匹配元数据，不参与稳定 Sample Identity；重复注册会保留首次持久化的
Feature，只增加 `registry_times` 并合并 Affinity。
Repository 会在 SQLite `repository_metadata` 中校验 hash version；不兼容
Schema 的旧数据库会 fail fast，需要显式创建新库并重新注册；本版本不自动迁移。

普通业务撤销注册使用引用计数语义：

```python
repository.unregister(sample_hash, affinity=current_affinity)
repository.delete_scenario(scenario_id, force=False, affinity=current_affinity)
```

只有最后一次 `unregister` 才物理删除事实。Admin/Debug 清理可以显式调用
`repository.delete(sample_hash)` 或 `repository.delete_scenario(scenario_id, force=True)`。
跨 Endpoint Scenario 的删除与跨 Scenario 依赖失效在同一 SQLite transaction 中完成。
物理删除自动 Reopen 相关 Round，保留外部引用；文档索引按当前 membership 恢复并只补缺失向量。

唯一字符串 Exact Match 是最短路径：完成 Query Parse 和 Endpoint Sample 查询后直接返回，
不会执行 Affinity/Feature Extraction、Semantic Encoding、Context Fusion 或 Judge。多个相同
`mocked_query` 仍会进入完整 Recall/Judge 链路。

## Hybrid Retrieval 与 Reranker

默认 Recall 使用 `UNION` 合并 Endpoint 范围内的 Semantic、Lane Context 和 BM25 Top-K，且不会
因为不同来源的原始分数量纲不同而做隐式截断。也可以通过 `RuntimeConfig` 选择确定性的 RRF，
或用 `SINGLE` + `single_recall_source` 执行 semantic/context/bm25 消融测试；原有
`SCORE_FUSION`、`HALF`、`DOUBLE` 等模式继续兼容。

BM25 使用按 Endpoint 隔离的内存索引，并根据 Sample Hash、Tokenizer fingerprint 和参数签名
惰性重建。默认 `DefaultTokenizer` 无外部依赖；可安装 `agent-mock-service[tokenize]` 使用
`JiebaTokenizer` 和业务词典，也可在 `BusinessPlugin.tokenizer` 注入自定义 Tokenizer。

```python
config = RuntimeConfig(
    recall_fusion_mode=RecallFusionMode.RRF,
    bm25_recall_k=50,
    bm25_k1=1.5,
    bm25_b=0.75,
    rrf_k=60,
    reranker_top_n=None,  # 默认不因精排降低 Union 覆盖率
    runtime_llr_weights={
        "semantic": 1.0,
        "context": 0.0,
        "bm25": 0.0,
        "feature": 0.0,
    },
)
```

默认 `BiEncoderReranker` 复用 Dense Recall 分数；显式设置 Top-N 时才为缺少 Dense 分数的候选
补算向量。可选 `CrossEncoderReranker` 仅接收 `(Agent Query, mocked_query)`，支持 batch、候选上限、
ExecutionResourcePool 与故障 fallback，不接收 `mocked_answer`，也不会替代最终 Judge。

`CalibrationBootstrapper` 现在通过 `CalibrationConfig.enabled_evidences` 调度独立的 Semantic、
Context、BM25 和 Feature Calibrator。默认仍只构建 Semantic；缺少可靠正负样本的 Evidence 会记录
为 unavailable 并保持中性。Calibration 构建开关与 `runtime_llr_weights` 完全分离，调整运行时
权重不会重新训练 Profile，未校准 Evidence 也不会把 Raw Score 直接当作 LLR。

## v1 能力

* SQLite endpoint 物理分表与 scenario membership 跨 endpoint 聚合；
* 稳定 sample hash、幂等注册计数和原子调用计数；
* 引用计数 unregister、强制 sample delete 和原子跨 Endpoint scenario delete；
* 不泄漏当前答案的 scenario context sculpting；
* Semantic / Lane Context / BM25 三路 Recall，以及 Union、RRF、Single 与 Legacy fusion；
* quantile binning、Laplace smoothing、LLR calibration 与 Bayes-style scoring；
* Availability raw signal 与加权 log-score contribution 分离（默认最大 bonus 为 `ln(1.2)`）；
* Service 级、跨 Query/Plugin/Stage 共享的进程内 ExecutionResourcePool；
* 内存 Lane、当前轮加下一轮滑动窗口和 TTL；
* 同一 Scenario 的多 Lane runtime path、候选级 Lane context 隔离；
* 多 Endpoint BusinessPlugin 路由与跨 Endpoint Scenario/Lane；
* 持久化 Endpoint 文档向量、Position Context Artifact 与 Context Embedding；
* Round OPEN/SEALED、跨 Scenario 依赖 DAG、精细失效与版本条件写回；
* 从独立 Calibration Corpus bootstrap 并由 SQLite 持久化的 Calibration Profile；
* deterministic FakeJudge 及可替换 LLM Judge contract；
* HTTP JSON / SSE renderer；
* MATCH、MISS、异常路径的 JSONL RuntimeTrace。


## Position Context 优化版本

完整接口、兼容变化和示例见 [Position Context 指南](docs/position-contexts.md)，
表结构与重建步骤见 [Schema 字典](docs/optimization-schema.md)，
验收和实测结果见 [六阶段交付报告](docs/optimization-delivery.md)。

多轮默认历史需要显式 `finalize_round`；未封存或缺失的历史不构造完整 Context。
推荐通过 `runtime.register(sample)` 进行带 Feature 生产者版本的注册。
Repository 的 `get_facts_many` / `get_position_contexts` 是纯读，
`runtime.materializer.ensure_position_contexts(refs, reason="rebuild")` 是显式物化入口。
唯一 Exact Match 仍绕过模型；非 Exact Match 仅为 Lane 目标与 Reranker 保留的 Position 准备静态 Context。

开发验证：
```
cd agent_mock_service

python -m pytest

ruff check src tests benchmarks
ruff format --check src tests benchmarks
```
性能脚本：`python benchmarks/materialization.py`；基线对照：`python benchmarks/compare_baseline.py`。
