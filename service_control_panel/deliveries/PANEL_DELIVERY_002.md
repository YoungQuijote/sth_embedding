# Panel 部署与实例生命周期改进交付

- 需求：[PANEL_REQUIREMENT_002](../requirements/PANEL_REQUIREMENT_002.md)
- 分支：`codex/panel-deployment-lifecycle`
- 日期：2026-11-16 UTC
- 状态：待用户目标环境验收

## 实现与验收对应

| 需求 | 实现 | 本次验证 |
| --- | --- | --- |
| R1 指纹可选 | 留空先连接，成功认证后原子记住指纹；以后严格匹配 | 成功登录持久化、失败登录不记录、密钥变化拒绝；浏览器空指纹保存 |
| R2 目录与空间 | Docker/裸机/K8s 使用自定义根目录的 UUID 子目录；浏览器上传前预检、远程传输前再次检查；Docker 数据目录另查 | 目录验证、带空格路径、权限/df 与创建顺序、归属标记及 shell 语法检查；真实目标权限/容量尚待验收 |
| R3 Docker 参数 | 三种入口模式、argv、加密环境变量、独立容器端口、挂载和受支持的额外参数；默认 on-failure:3 | argv 保真与保留参数拒绝；真实 Docker 覆盖错误镜像入口成功就绪，故意 exit(7) 重启 2 次后停止；日志诊断及 UI 表单 |
| R4 双重删除 | 移除监测逻辑隐藏并停止采集；独立卸载任务清理所属容器/systemd/K8s 及实例目录 | 隐藏后迟到响应不恢复 UI/样本、已有任务保留；旧/新容器归属、已人工删除容器、重复清理、错误归属拒绝；真实 Docker 容器和目录清理，镜像保留 |
| R5 接入已有服务 | URL、方式、管理 token；验证 ready、panel.v1 及接口发现；直连无须 SSH | 真实 mock token 错误拒绝、接入、试调、日志/数据库/请求/trace 及三类回流；浏览器接入与移除监测 |
| R6 上传提前拒绝 | 二进制流、前端文件大小检查、后端请求头检查、实际累计字节及空间检查；分阶段进度 | 超限请求头不触发 body receive、伪报大小清理、multipart 拒绝、成功流 hash/解包大小/耗时；浏览器真实上传至 Panel |
| R7 裸机一致性 | 共用目录/传输/清理逻辑；计入源码展开量与安装最低预留；systemd 限制连续启动失败 | conda→venv→系统 Python 命令顺序、systemd 命令及所属单元清理；真实 systemd/依赖安装尚待环境验收 |

## 变更组织

- `uploads.py`：原始流上传，移除 multipart 预缓存导致的迟到大小判断。
- `deployment_support.py`：目录/空间检查、Docker argv、退出诊断与所属资源卸载。
- `models.py` / `app.py`：可选指纹、Docker/目录配置、接入/移除/卸载接口、敏感环境变量加密保存。
- `remote.py` / `deploy.py` / `service.py` / `store.py`：首次信任、传输进度、有限重启、原子更新、防止任务与采集覆盖生命周期字段。
- 前端：扩展部署表单、新接入表单、两种移除确认、任务阶段与传输进度。
- 数据库不增加表，通过 JSON 对象追加字段兼容旧数据。生产 Panel 仍不依赖对侧 Python 包及外部监控服务。

## 测试记录

- 后端：`PANEL_DOCKER_TEST_IMAGE=mock-panel-lifecycle:test backend/.venv/bin/pytest backend/tests`：66 passed；包含真实 mock ASGI 联调、真实 Docker 启动/有限重启/卸载测试。
- Ruff：`ruff check backend` 与 `ruff format --check backend` 通过。
- 前端：TypeScript/Vite 构建通过；Playwright 6 条流程通过。一次与容器构建并行的运行触发 30 秒超时，停止并行构建后重跑验证。
- Panel Dockerfile 构建通过；构建使用可选 CA secret，没有关闭 TLS 验证。
- mock 实现为 `main` 已合入版本，本次未修改 `agent_mock_service`。

### 测试边界

浏览器流程使用契约模拟服务与模拟远程执行器，上传、后端状态及前端交互是真实路径。真实 mock 测试使用 ASGI 传输、真实管理接口和 SQLite；不使用契约模拟器。

Docker 测试使用含真实 mock 包且故意错误 ENTRYPOINT 的临时镜像。SSH 替换为本地命令传输，真实 Docker 命令验证启动、有限重启、诊断与卸载；rootless daemon 测试目录权限为临时适配。未将此结果视为 SSH/SFTP、镜像传输/加载或真实目标磁盘检查的端到端验收。镜像上传与目录检查由单元/脚本测试覆盖。裸机安装、远程网络、K8s 仍需目标环境验证。

## 上传延迟结论

旧版 `UploadFile` 进入业务 handler 前，框架已接收/缓存完整 multipart，因此业务循环的大小判断发生在网络上传结束之后。不是循环逐块读取速度本身能够解释全部等待。

原仓库阈值为部署包 1 GiB、清单 2 MiB，错误消息同时写两种限制，可能造成误解；无法确定用户现场的 2 MB 错误来源。新版错误仅显示当前文件类型的限额，默认部署包 2 GiB、清单 2 MiB。反向代理若缓冲请求或有更小 body 限额，仍可能先被代理拦截，需按 README 配置。

## 使用与升级

1. 停止 Panel 并备份完整数据目录（包括 `credentials.key`、SQLite）；前后端一起更新，重新构建前端/Panel 镜像。
2. 环境指纹可以留空。新增实例表单可改远程根目录；Docker 高级配置在部署方式下方。
3. 已有服务从“新增实例 → 监测已有实例”接入。已有 token 按原值填写，不要求改成 Panel 新部署生成的格式。
4. 实例“更多操作”中分别提供“移除监测”和“卸载实例”。若要清理部署，请先卸载，再移除监测。
5. 新增配置 `PANEL_PACKAGE_LIMIT_BYTES` 与 `PANEL_MANIFEST_LIMIT_BYTES`；API 上传从 multipart 改为 raw-stream-v1，旧调用方必须同步修改。

卸载包含独立目录的持久数据及所属 K8s PVC，请先回流所需数据。镜像、额外挂载、系统 Python 共享包和 Panel 本地缓存保留。外部接入实例无可信运行资源身份，重启/卸载置灰。旧 K8s 无归属记录不自动删除。

回滚需同时恢复旧应用和升级前备份；旧应用不识别新的隐藏/外部接入等字段。回滚 Panel 数据不能恢复已卸载的远程数据，需使用事先回流备份。
