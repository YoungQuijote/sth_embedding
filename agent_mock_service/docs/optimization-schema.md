# SQLite Schema 与重建说明

当前 `repository_metadata.schema_version = 4-position-context-v1`，`sample_hash_version = 3`。
不兼容数据库在创建业务表或修改事实前拒绝打开。应用不迁移、不删除旧库；先保留旧库备份，显式选择新的数据库文件，重新注册事实、依赖与封存声明。旧库不能直接当新库启动。不要只修改 metadata 绕过验证。

## 表字典

原有 `endpoints`、动态 `samples_*`、`scenario_membership` 和 `calibration_profiles` 保留。
Endpoint 分表名由 Repository 生成；membership 的 `(scenario_id,position_id)` 全局唯一。
Round 在 SQLite 中保存规范化数值字符串，身份始终包含 scenario_id。

| 表 | 主键或唯一键 | 内容及类型 |
| --- | --- | --- |
| position_generations | scenario_id TEXT + position_id INTEGER | revision INTEGER；新增/替换递增，删除后保留，防止同 hash 删除重建冒充旧候选 |
| scenario_rounds | scenario_id TEXT + round_id TEXT | completeness TEXT CHECK OPEN/SEALED；expected_position_count INTEGER 可空且非负；revision INTEGER；updated_at REAL |
| position_context_declarations | scenario_id TEXT + position_id INTEGER | mode TEXT CHECK DEFAULT/EXPLICIT；version TEXT；updated_at REAL；DEFAULT 是动态前序 Round 集合声明，不固定展开成边 |
| position_context_sources | target_scenario TEXT + target_position INTEGER + source_scenario TEXT + source_position INTEGER | source_order INTEGER；来源反向索引 idx_context_source；无来源事实级联删除外键 |
| position_context_state | scenario_id TEXT + position_id INTEGER | completeness INTEGER；status TEXT CHECK MISSING/READY/STALE/FAILED；lineage TEXT；last_error TEXT 可空；artifact_fingerprint TEXT 可空；updated_at REAL |
| position_context_artifacts | target Ref + lineage TEXT + fusion TEXT | artifact_fingerprint TEXT UNIQUE；raw_context/fused_context TEXT；created_at REAL |
| context_embeddings | artifact_fingerprint TEXT + encoder TEXT | vector TEXT（JSON 数组）；dimension INTEGER >0；format_version INTEGER=1；created_at REAL |
| sample_embeddings | sample_hash TEXT + encoder TEXT | vector TEXT（JSON 数组）；dimension INTEGER >0；format_version INTEGER=1；created_at REAL |
| sample_features | sample_hash TEXT + extractor TEXT | features TEXT（JSON hard/soft）；input_fingerprint TEXT；created_at REAL |

时间由 SQLite 生成。向量加载还验证 JSON 数组、有限数值、保存维度、Encoder 已知维度与格式版本。Semantic 身份映射由当前 membership+facts 恢复，不另保存 Python 索引对象；当前线性扫描数组在完整构建后一次发布。BM25 仍为进程内索引。

## 有效性与清理

lineage 包括目标 Ref/Query/Round、展开声明、稳定排序的祖先 QA、相关 Round 封存状态与解析版本；排除目标 Answer、计数、affinity 和无关分支。artifact fingerprint 进一步绑定目标 fusor 和具体输出，Embedding 按 artifact+encoder 隔离。

删除事实后旧 Artifact/向量可能仍在历史表中，但不会由当前 membership 或当前有效键选出。
历史清理作业本轮未实现。将来清理时必须保留当前有效状态引用的 Artifact/Embedding、当前样本所需版本及依赖恢复所需声明；不能通过清理源事实来级联删除其他存活节点的引用。并发构建期间不要直接修改这些表，应经 Repository 接口操作。

## 最小重建步骤

1. 停止使用旧 Repository 的业务进程并保存旧库及 WAL 的一致备份。
2. 使用新文件名创建 SQLiteRepository；由 Runtime 绑定同一 Encoder、Plugin 和资源池。
3. 重新注册原始事实与 None/[]/显式依赖声明；完成各 Round 的注册后 finalize_round。
4. 查看 last_materialization/last_derived_errors；必要时显式 rebuild_position_contexts。
5. 关闭并重新打开 Repository/Runtime，验证纯读 hit 与文档向量零重算，再切换业务配置。

本次开发没有迁移或清空用户数据库。数据库备份、重建和切换应由业务方按实际数据来源执行。
