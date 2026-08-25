# AgentMockService

AgentMockService 是一个**确定性的 Stateful Agentic-RAG Mock Runtime**。它使用 endpoint
硬隔离、语义/上下文双路召回、校准后的 evidence 与 runtime prior 来选择预注册的
`MockSample`；最终响应始终从 SQLite 中读取，Judge 不生成、也看不到候选答案。

## 快速开始

```python
from agent_mock_service import AgentMockRuntime, MockRequest, MockSample
from agent_mock_service.context import ScenarioContextBuilder
from agent_mock_service.defaults import HashingEncoder, JoiningContextFusionProvider
from agent_mock_service.repository import SQLiteRepository

builder = ScenarioContextBuilder(JoiningContextFusionProvider(), HashingEncoder())
repository = SQLiteRepository("agent-mocks.db", builder)
repository.register(MockSample("/weather", "罗马未来七天天气", "晴到多云", "trip-1", 1, 1))

runtime = AgentMockRuntime(repository)
response = runtime.handle(MockRequest("/weather", {"query": "罗马未来七天天气"}))
print(response.body)
```

生产环境可通过 Protocol 替换 QueryParser、EmbeddingEncoder、FeatureExtractor、
FeatureComparator、ContextFusionProvider、AffinityExtractor 与 Judge。默认 hashing encoder
仅用于无外部依赖的开发和测试；需要真正语义匹配时应注入业务 embedding encoder。

## v1 能力

* SQLite endpoint 物理分表与 scenario membership 跨 endpoint 聚合；
* 稳定 sample hash、幂等注册计数和原子调用计数；
* 不泄漏当前答案的 scenario context sculpting；
* local semantic / lane context recall 及五种 fusion mode；
* quantile binning、Laplace smoothing、LLR calibration 与 Bayes-style scoring；
* 内存 Lane、当前轮加下一轮滑动窗口和 TTL；
* deterministic FakeJudge 及可替换 LLM Judge contract；
* HTTP JSON / SSE renderer；
* MATCH、MISS、异常路径的 JSONL RuntimeTrace。
