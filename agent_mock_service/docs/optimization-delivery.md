# 六阶段优化交付报告

核对日期：2026-11-12（Asia/Shanghai）。起始 HEAD 与需求基线
`15dc1f72ec90302e14bb3d4d68a5a3c6e0b5021a` 完全一致。修改保存在当前工作区；未推送、未发布，也没有迁移或清空业务数据库。

## 实施与兼容性

| 阶段 | 实际变更 | 验证与兼容说明 |
| --- | --- | --- |
| 一 领域与 DAG | PositionRef、Round、正交完整性/物化状态；独立 schema_version；纯 DAG；数值 Round 规范化；跨 Scenario 拓扑闭包、环和间接泄漏检查 | `test_position_dag.py` 覆盖三态往返、方案 A、菱形/外部同轮、补回非法原子回滚、旧库不破坏。MockSample 新字段放末尾；旧库明确拒绝 |
| 二 生命周期 | register/finalize/force/unregister/delete 的短事务；精细 lineage 同事务失效；跨 Scenario 原子回滚；fact_revision 防同 hash 替换；入边保留 | `test_position_dag.py`、既有 `test_v14_repository_lifecycle.py` 与新增故障测试保留计数、affinity、唯一性。False 不 Reopen；返回 MockSample 和删除语义保留 |
| 三 物化 | 统一 PositionContextMaterializer；目标 fusor；Artifact/Context Embedding 分表；SQLite+Position 内存缓存；fan-out/fan-in；原子发布和失败条件写回 | `test_materialization_persistence.py`、迁移后的 `test_v161_fanout.py`。独立 Python 子进程命中零模型调用；Encoder-only、目标 fusor-only、跨 Scenario barrier、同键不同输出、容量 1、损坏向量及失败重试通过 |
| 四 派生索引 | 版本化 Feature；原始 Query 文档向量落盘；索引完整发布；当前 membership/计数更新；BiEncoder Top-N 补算复用相同存储 | `test_derived_persistence.py`。未版本化输入重提取；同 producer 保留首份 Feature；重启、只补一条、删除交错、BM25-only Top-N 持久化复用通过。BM25 不落盘 |
| 五 Runtime 与 Lane | 纯事实读取；Lane 精确目标；Rerank 后仅最终 Ref ensure；缺失 Context 降级；路径与评分元数据保留；SEALED 完成条件；详细计时/计数 | `test_runtime_materialization.py` 和既有 Exact Match、Recall、Lane、评分测试。100→2、5∪2=6、两个 Lane 共享静态但不合并路径、Finalize 后重新评估、恶意 Judge/过时选择拒绝通过 |
| 六 校准与交付 | 保留 LLR/prior/Calibration 分离；Feature producer 进入启用的 Feature Corpus key；未启用 evidence 配置不驱动训练；文档与基线实测 | `test_optimization_calibration.py`、`test_hybrid_retrieval.py`、既有 v11–v16 测试。独立 Corpus、None/0、默认权重、原子计数、资源上限和答案隔离均保留 |

所有现有测试均完整阅读并纳入回归。旧测试只按本包明确的新契约修改：历史先 Finalize、空历史直接 Query、纯事实 Context=None、显式生产者指纹、物理删除后不再把残缺历史 Fuse 成完整 Context，以及把 Repository 隐式批构建测试移到显式 Materializer。并发容量、Endpoint 唯一性、答案隔离和原子性断言未删除。

实际执行命令使用 `/workspace/venvs/sth_embedding/bin/python` 与同目录 `ruff`，工作目录 `/workspace/sth_embedding`：

```bash
python -m pytest
ruff check agent_mock_service tests benchmarks
ruff format --check agent_mock_service tests benchmarks
git diff --check
python benchmarks/materialization.py
python benchmarks/compare_baseline.py
```

最终完整 pytest：**133 passed**；无 failed、skipped 或 xfailed。相较基线的 95 项，新增 38 个测试用例（含参数化实例）。上述六阶段的测试均包含在这次实际完整运行中，不把推荐的聚焦命令当作独立通过报告。Ruff 检查通过，40 个 Python 文件格式检查通过，diff 空白检查通过。

开发期间另实际运行过 `pytest tests/test_position_dag.py -x`、
`pytest tests/test_materialization_persistence.py -x`、
`pytest tests/test_derived_persistence.py tests/test_runtime_materialization.py -x` 和
`pytest tests/test_materialization_persistence.py tests/test_position_dag.py -x`；初始测试暴露的旧契约冲突及 Lane 重评估扩大目标集合问题已修正，以上最终完整结果覆盖修正后的状态。

## 验收追踪

| 验收编号 | 证据 |
| --- | --- |
| A01 | 重开实例测试、独立子进程测试、持久化基准的 restart 行 |
| A02–A03 | 100 个原始候选只请求最终 2 Ref；Lane 5 与最终 2 重叠 1，仅 6 次静态 Fuse |
| A04–A06 | 祖先 FAILED 后继独立成功；缺失/删除/补回及重开库；None 完整历史反例 |
| A07–A08 | force/SEALED/计数矩阵；跨 Endpoint 删除中途注入异常，外部状态与事实一起回滚 |
| A09–A11 | 五种 barrier 交错；Encoder-only；目标 fusor 与不相关 Plugin；Feature producer 新视图 |
| A12 | 新增仅补一条、删除不再召回、计数刷新不重复 encode、编码期间删除不发布 |
| A13 | OPEN 匹配不完成、Finalize 后重评估、末轮关闭测试 |
| A14–A16 | 既有 None/0/LLR 测试；权重变化与禁用 evidence 不训练；独立 Corpus 变化重建 |
| A17 | Exact Match 零昂贵调用；Judge 候选路径校验；CrossEncoder 仅接 Query 对；同 hash 重建拒绝旧选择 |
| A18 | 既有资源池测试、显式批物化容量测试、容量 1 注册/Query 共享及同键并发结果配对 |
| A19–A22 | 跨 Scenario 菱形/递归闭包、缺失占位、环和间接同轮/未来泄漏拒绝；外部删除恢复与 barrier 过时结果拒绝 |

## 实测结果与限制

完整原始输出保存在 [基线对照](optimization-baseline.json) 和 [运行场景测量](optimization-performance.json)。两份脚本可以复现。

基线对照实际从 Git 固定提交导出源码到临时目录，用独立 Python 进程执行；没有重置工作区。两边使用相同的 100 个双轮 Scenario、100 条 Endpoint Query 文档、Top-N=2、无活动 Lane、HashingEncoder(32) 和确定性 Joining Fusion。

| 冷请求指标 | 基线 | 当前 |
| --- | ---: | ---: |
| 静态 Fuse 次数 | 200 | 2 |
| 文档编码条数（Query 文档 + Context） | 300 | 102 |
| 实测单次延迟 ms | 27.625 | 75.280 |
| 热请求 p50 ms（20 次） | 9.181 | 22.010 |
| 热请求 p95 ms（20 次） | 12.593 | 27.183 |

静态模型调用减少已经由实际调用计数证明；**本地轻量模型的端到端延迟没有下降**。
当前一致快照、持久化查询、依赖和版本校验增加了 Python/SQLite 开销；图失效目前通过前后全图有效输入对比得到精确影响集合，模型计算只限受影响目标，但事实操作的 CPU 成本仍随图规模增长。不能把调用量收益解释为本地 Hashing 模型的速度提升，更不能据此承诺真实模型固定秒数收益。后续若业务主要使用这种廉价模型，可再针对快照批量读取及图解析开销优化。

单独的运行场景测量使用 100 个目标、一个共享外部祖先：

| 场景 | 请求数 | 静态 Fuse 总次数 | 文档编码总条数 | p50 / p95 ms |
| --- | ---: | ---: | ---: | ---: |
| 全新数据冷请求 | 1 | 2 | 102 | 184.109 / 184.109 |
| 热请求 | 20 | 0 | 0 | 29.900 / 47.975 |
| 预物化后重开 Repository/Runtime | 20 | 0 | 0 | 27.155 / 34.496 |
| 局部变更后首个请求 | 1 | 0 | 0 | 18.278 / 18.278 |
| 缺失依赖降级 | 20 | 0 | 0 | 20.394 / 31.240 |
| Fuse 故障降级 | 20 | 40 次失败尝试 | 0 | 22.178 / 31.167 |

局部变更的注册阶段另计算 1 次 Fuse 和 2 条文档向量，未隐藏在“请求零计算”中。动态 Query 编码仍存在；这里无活动 Lane，动态 Lane Fuse=0。动态 Lane 的并发和路径隔离由独立测试覆盖。冷请求和局部变更只有 1 次观测，其 p50/p95 不是统计稳定的服务承诺。实际云主机调度及负载也会影响数字。

## 使用变化与遗留边界

接口示例、失败诊断、指纹配置、计时包含关系见 [使用指南](position-contexts.md)；
表字典、版本、重建和历史清理原则见 [Schema 说明](optimization-schema.md)。

仍按需求延期：同 Ref 重复构建合并、多进程协调、BM25 落盘、细分 Fuse/Embedding 失败阶段、force_finalize、round_order、SentenceTransformer 模型内容指纹、未确认的 Lazy Fuse 数量/超时预算；未新增持久任务队列或 BUILDING 租约。动态 Lane 不跨重启保存，历史 Artifact 清理作业本轮不实现。

未执行可选真实 SentenceTransformer/CrossEncoder 模型 smoke，未下载模型；主验收使用确定性替身。原 FastAPI transport 的 Request 注解问题属于环境设置时发现的既有独立缺陷，本次优化没有修改其业务实现或将该接口报告为可用。本次交付的验证范围是 Runtime/Repository 库工作流。
