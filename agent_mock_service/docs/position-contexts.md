# Position Context 开发与接口指南

本次实现以 `15dc1f72ec90302e14bb3d4d68a5a3c6e0b5021a` 为基线。优化对象是静态 Position Context 与文档向量；动态 Query、Lane Fuse、Judge 仍按请求计算。跨 Scenario 与跨 Endpoint 的依赖可以读取原始 QA，答案仍必须由请求 Endpoint 的当前事实提供。

## 注册与封存

```python
from agent_mock_service import AgentMockRuntime, MockSample, PositionRef
from agent_mock_service.src.agent_mock_service.mock_core.repository import SQLiteRepository

repository = SQLiteRepository("new-agent-mocks.db")
runtime = AgentMockRuntime(repository)

# 推荐经 Runtime 注册：使用同一 Encoder、Plugin Resolver 和资源池，保存版本化 Feature。
runtime.register(MockSample("/weather", "城市", "罗马", "trip", "00000001", 1))
repository.finalize_round("trip", "00000001", expected_position_count=1)
runtime.register(MockSample("/weather", "明天天气", "晴", "trip", "00000002", 2))

# 允许显式引用其他 Scenario；这里读取 trip/1 的 QA，不物化 trip/1。
runtime.register(MockSample("/packing", "带什么", "带伞", "packing", 1, 1,
                            context_source_positions=[PositionRef("trip", 1)]))
```

`register(sample, affinity=None, *, round_position_completeness=False, force_registry=False,
extractor_fingerprint=None)` 返回 `MockSample`。末尾新增的 `context_source_positions` 和
`fact_revision` 不改变既有位置参数顺序。`fact_revision` 是只读观察值，调用者传入的值不决定版本。

- False 只表示不请求封存。True 在同一个事实事务内执行封存验证。
- 同事实且同依赖声明的重复注册增加 `registry_times`、合并 affinity，保留首次同版本 Feature；SEALED 不重开。
- 新增 SEALED Round 的 Position、替换事实或更改依赖声明需要 `force_registry=True`。
- 实质替换重置 `registry_times=1`、`invoked_times=0`，只保留本次 affinity；相关本地 Round Reopen。
- force 与 True 同次使用时先变更、再验证封存；任一步失败全部回滚。force 不豁免唯一性、环或答案泄漏校验。
- `finalize_round` 必须针对已有 Round，跨该轮全部 Endpoint 计数；重复有效声明幂等。保存过的 expected count 在再次封存时仍会验证，可显式提供新的期望数量。
- `unregister` 只有最后一次才物理删除；仅减计数不改变静态产物。`delete_scenario(force=False)` 是原子逐位置注销，True 是原子物理删除。
- 物理删除只重开被删事实自身的 Round。外部依赖者失效或不完整，但其事实、Round 封存声明不连带删除或重开。

事实提交后才发生模型调用。失败不回滚注册、不要求重试 register：查看
`repository.last_materialization.results`、`last_derived_errors`（Feature/文档向量）和
`last_post_commit_error`（派生基础设施故障），修复后显式 rebuild。不要因派生失败重复增加注册计数。
这些 last_* 属性是最近一次操作的便利观察值，不是并发请求的结果关联通道；并发调用请使用显式 ensure 返回的逐 Ref 批次结果。

注册早于 Runtime 绑定时允许只保存事实与状态；绑定不会隐式全库构建。绑定后主动 rebuild，或由 Query 按实际目标 Lazy Fuse。

## 依赖与状态

| 声明 | 含义 |
| --- | --- |
| `None` | 当前节点自身 Scenario 的全部已声明前序 Round 原始 QA；这些 Round 必须 SEALED 才完整 |
| `[]` | 明确无历史，不受前序 OPEN 阻塞 |
| `[PositionRef(...), ...]` | 递归展开来源声明；按完整 Ref 去重，依赖优先稳定拓扑排序 |

None 不会被中间节点的显式裁剪缩短。Round 数字不必连续；系统不能推断尚未声明的整轮。删除最后一个位置仍保留该轮 OPEN 元数据。`1`、`"1"`、`"00000001"` 统一为内部字符串 `"1"`，比较按数值；不新增 round_order。规范化只影响同一 Round 的表示，sample hash v3 仍不包含 Feature 或依赖声明。旧调用中带零填充的原始 Round 字符串会按新规范化身份重新计算 hash，需随新库重新注册，不能沿用旧库中的 hash 映射。

不同 Scenario 的轮号不存在全局时序。同 Scenario 自引、同轮、未来轮、跨 Scenario 环，以及绕回目标 Scenario 的同轮/未来答案均拒绝。缺失 Ref 保留为占位，补入时校验整个受影响闭包；非法补入原子回滚。删除目标可清除自己的出边，其他存活节点的入边保留；重注册采用本次声明。

`position_context_completeness` 仅描述原始依赖；与 `MISSING/READY/STALE/FAILED` 正交。
祖先 Fuse FAILED 不会使拥有完整原始 QA 的后继变成 incomplete。无历史目标直接使用 mocked_query，静态 Fuse 为零；Round 1 如有外部历史则正常 Fuse。目标自己的 Answer 永不进入静态 Context。

| 事件 | Round | 派生状态 |
| --- | --- | --- |
| 新事实 | OPEN，或同次成功封存后 SEALED | 完整目标可主动物化；否则 MISSING/incomplete |
| finalize | SEALED | 重新评估依赖该 Round 的目标 |
| 实质替换或物理删除 | 被修改事实的 Round OPEN | 精细更新受影响 lineage，已有产物标 STALE |
| 计数或 affinity 更新 | 不变 | 不失效静态产物 |
| ensure 成功且版本仍有效 | 不变 | READY |
| 模型失败 | 不变 | 条件写 FAILED，可重试 |
| 构建结果过时 | 不变 | 不发布；返回 stale_discard，不覆盖新状态 |

## 纯读和显式物化

```python
ref = PositionRef("trip", 2)
facts = repository.get_facts_many(["trip"])             # 无模型 IO
reads = repository.get_position_contexts([ref])          # 无模型 IO，当前指纹校验
batch = runtime.materializer.ensure_position_contexts([ref], reason="rebuild")
result = batch.results[ref]
state = repository.get_position_state(ref)
# 等价的 Repository 便利入口：
batch = repository.rebuild_position_contexts([ref])
```

纯事实对象的 `ScenarioPosition.context` 为 None。纯读结果包括 hit/miss/stale/incomplete；配置错误会明确抛出，ensure 则隔离到逐 Ref FAILED 结果。ensure 返回完整性、缺失原因、Artifact、错误、stale_discard、缓存层级及本次 Fuse/Embedding 次数。不要仅根据历史 READY 字段使用产物。

`get/get_many` 保留为弃用兼容外观，可能触发模型 IO，但与 ensure 共用 Materializer。新 Runtime 不调用它们。`ScenarioContextBuilder.build/build_many` 仅保留为旧离线构建工具，不承担数据库 DAG、封存或持久化语义；服务侧使用显式 Materializer。

SQLite 是权威派生存储。Position 内存缓存只复用当前 SQLite 行匹配的版本，并复制返回值；Encoder 改变仅重新编码有效文本，不重 Fuse。重启后不依赖进程内缓存。损坏、格式不匹配或维度不符的向量视为缺失。

## 指纹和 Feature

Encoder、FeatureExtractor、持久化 Fusion 需可靠 fingerprint。自定义 Fusion 也可通过
`BusinessPlugin.context_fingerprint` 声明模型、prompt、算法和所有影响输出的推理参数的语义版本，例如：

```python
plugin.context_fingerprint = "model-A:weights-2026-11:prompt-v3:join-v2:temperature-0"
# 自定义提取器明确标识生产者；比较器参与 Feature Calibration 时也需要版本。
extractor.fingerprint = "business-feature:model-B:prompt-v5:schema-v2"
comparator.fingerprint = "business-hard-key-comparison-v2"
```

实际使用时这些值必须随输出相关配置变化，不能复制示例字符串冒充真实版本。
`execution_resource_id` 是容量归属，不是算法身份。未托管的同步组件仍可用，但同样需要可靠指纹。
HashingEncoder 指纹包含真实维度；SentenceTransformer 的路径+包版本策略及加载失败 fallback 保持不变。

直接 `repository.register(sample, extractor_fingerprint="producer-v1")` 表示调用者负责所给 Feature 的生产者身份；缺少版本的历史/直接输入不被认作当前版本，进入严格过滤前由当前 Endpoint Extractor 重提取。同 hash+producer 保留第一份 Feature，新 producer 建立新视图而不改计数或 sample hash。

独立 Calibration Corpus 可在 `CalibrationSample.feature_fingerprint` 中声明 Feature 生产者。仅启用 Feature evidence 时纳入该版本及 Feature 内容。未启用 evidence 的 tokenizer/comparator 配置不触发无谓训练；Runtime registry 与 runtime LLR weights 不进入 Corpus 训练键。LLR 公式、默认权重、runtime prior 和 Context probes 要求不变。

## 并发与 Runtime

事实、membership、Round、声明和跨 Scenario 失效同一短事务提交；首次读取不可变图快照后在锁外解析物化计划。Fuse/Embedding/Future 等待不持 SQLite 锁。发布事务原子校验目标存活、fact_revision、完整性、精细 lineage 和当前组件指纹，再写文本、向量和 READY。lineage 状态由每次事实事务原子维护，不是 BUILDING 租约。

同键重复构建允许发生。不可变文本版本先到者获选；其 fingerprint 绑定输出，另一输出的向量不会附到获选文本。失败写回同样检查版本，不能覆盖新 READY。业务 Fusion 先 fan-out，再 fan-in；编码、数据库操作由调用线程编排，避免在 Fusion executor 内嵌套提交后同步等待。

Runtime 顺序：唯一 Exact Match → 原始 Query 召回 → Lane 目标静态准备 → 动态 Lane Recall → Fusion/硬冲突/Rerank → 最终候选 Position 静态准备 → Evidence/Score/Judge → 当前事实答案。Exact Match 后处理只读事实/Round，不引入模型调用。

Lane 静态目标仅来自 hypotheses、当前 Endpoint 和 active_rounds；外部祖先不会自动成为候选。最终目标是 Reranker 输出，不以 scenario_recall_n 代替 Position 截断。静态 Ref 去重，`(scenario, position, lane)` 路径保留；缺失 Context 为 None、中性 Context LLR，原始 Query 路径可继续。Context-only 不自动开启其他召回源。

完整 Scenario 元数据仍用于 scenario-max、affinity 和 ordinal distance。OPEN 即使可见位置全部匹配也不因“完成”关闭或推进；随后 Finalize 会在下次 Lane 评估重新检查。最后读答案与调用计数增加在同一短事务线性化，并校验选中事实版本；删除后相同 hash 重注册也不会冒充旧候选。

## Trace 与诊断

MemoryTraceWriter 仍为默认；JsonlTraceWriter 的启用方式不变。

- `timing`：facts_read_ms、semantic_index_load_ms、semantic_query_encode_ms、semantic_recall_ms、bm25_index_load_ms、bm25_recall_ms、context_artifact_load_ms、lane_static_materialization_ms、lane_fusion_ms、rerank_ms、final_materialization_ms、judge_ms、total_ms。
- `materialization.lane_recall/final_candidates`：逐目标 Ref、完整性、status、cache_hit（memory/sqlite）、缺失引用/未封存 Round、lineage/fusion/encoder 指纹、失败原因、stale_discard、fuse_count、embedding_count。无目标时可以省略相应数组。
- `computation`：sample_document_encode、static_context_encode、static_fuse、dynamic_lane_fuse，区分静态和动态工作。

`context_artifact_load_ms` 包含快照/计划/读取验证，是静态物化计时的子项；lane_fusion_ms 包含动态 Lane 编码与比较，recall_ms 包含 Rerank 等子阶段。不能把嵌套项相加当 total_ms。BM25 load 指内存准备，不是磁盘加载。

示例 warning：`Lazy Fuse/Embedding target=PositionRef(...) reason=final_candidates lineage=<hash> fusion=<version>`，不输出整段 QA。FAILED 修复后再次 ensure；stale_discard 表示输入已变，调用者可重新 ensure 当前版本，不无限自旋。失败的部分目标不丢失同批其他目标结果。
