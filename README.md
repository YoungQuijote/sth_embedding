# AgentMockService

AgentMockService 是一个**确定性的 Stateful Agentic-RAG Mock Runtime**。它使用 endpoint
硬隔离、语义/上下文双路召回、校准后的 evidence 与 runtime prior 来选择预注册的
`MockSample`；最终响应始终从 SQLite 中读取，Judge 不生成、也看不到候选答案。

## 快速开始

```python
from agent_mock_service import AgentMockRuntime, MockRequest, MockSample
from agent_mock_service.repository import SQLiteRepository

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

Repository、Encoder、Lane、Calibration 和 Trace 保持 Service 级共享；Parser、Feature、Fusion、
Affinity 与 Judge 按请求 Endpoint 路由。SQLite 的 `(scenario_id, position_id)` 现在是跨
Endpoint 唯一键；含有重复位置的旧数据库会在启动时被拒绝，需要先迁移或重建。

## Repository 生命周期

`sample_hash` v2 覆盖 Endpoint、Scenario、Round、Position、Query、Answer 和静态 Feature。
Repository 会在 SQLite `repository_metadata` 中校验 hash version；已有事实数据但缺少兼容
版本标记的旧 Debug 数据库会 fail fast，需要迁移或重建。

普通业务撤销注册使用引用计数语义：

```python
repository.unregister(sample_hash, affinity=current_affinity)
repository.delete_scenario(scenario_id, force=False, affinity=current_affinity)
```

只有最后一次 `unregister` 才物理删除事实。Admin/Debug 清理可以显式调用
`repository.delete(sample_hash)` 或 `repository.delete_scenario(scenario_id, force=True)`。
跨 Endpoint Scenario 的删除在单一 SQLite transaction 中完成；物理删除会使 Scenario Context
Cache 失效，而 Endpoint Embedding Index 会在下一次 Recall 时根据 sample hash signature 自动收敛。

## v1 能力

* SQLite endpoint 物理分表与 scenario membership 跨 endpoint 聚合；
* 稳定 sample hash、幂等注册计数和原子调用计数；
* 引用计数 unregister、强制 sample delete 和原子跨 Endpoint scenario delete；
* 不泄漏当前答案的 scenario context sculpting；
* local semantic / lane context recall 及五种 fusion mode；
* quantile binning、Laplace smoothing、LLR calibration 与 Bayes-style scoring；
* 内存 Lane、当前轮加下一轮滑动窗口和 TTL；
* 同一 Scenario 的多 Lane runtime path、候选级 Lane context 隔离；
* 多 Endpoint BusinessPlugin 路由与跨 Endpoint Scenario/Lane；
* Endpoint 文档 embedding index 与 lazy Scenario context cache；
* 从独立 Calibration Corpus bootstrap 并由 SQLite 持久化的 Calibration Profile；
* deterministic FakeJudge 及可替换 LLM Judge contract；
* HTTP JSON / SSE renderer；
* MATCH、MISS、异常路径的 JSONL RuntimeTrace。
