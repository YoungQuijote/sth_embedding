# PANEL_DELIVERY_001 — Mock Control V1

实现与验证：2026-11-14；交付整理：2026-11-15（UTC+8）
状态：Panel 实现与契约验证完成，等待 mock 侧真实实现及远程环境联调。
需求：[PANEL_REQUIREMENT_001](../requirements/PANEL_REQUIREMENT_001.md)
依赖：[MOCK_REQUIREMENT_001](../../agent_mock_service/requirements/MOCK_REQUIREMENT_001.md)，已于 main 提交 `26a92de1153ef23a4440c2f52332f7a86b58050c` 推送，供 mock 侧并行实现。

## 交付内容

- Python/FastAPI 后端独立打包，TS/React/Vite 前端独立构建；uv/npm 锁文件、运行文档、Dockerfile 与 Compose。
- SSH 环境管理、指纹校验与凭据加密；裸机、Docker、指定节点 K8s 部署执行器。
- SQLite 实例登记、后台任务、重启、健康检查、接口发现、OpenAPI 试调与 SSE 透传。
- 内置监控采集、7天趋势、按实例/接口区分200与非200、告警确认/恢复；外部监控保留 Protocol。
- 分页日志、请求详情与 trace 关联、只读数据库浏览、一致性导出任务及原生浏览器下载。
- 中文响应式控制台；非Linux、Docker bridge、多节点K8s、外部监控功能明确禁用。

## 需求对应与证据

| 需求 | 实现 | 验证 |
| --- | --- | --- |
| P01/P02 | models、remote、store、环境表单 | 认证、加密、指纹匹配测试；浏览器新增环境 |
| P03 | deploy.py 裸机执行器 | conda/venv/system Python 顺序、systemd配置、bash语法检查；未接入真实远程主机 |
| P04 | deploy.py Docker执行器 | 加载镜像选择、端口/持久化/环境配置测试；未部署真实mock镜像 |
| P05 | manifest校验与节点镜像加载 | 三类runtime命令、节点/namespace/拉取策略边界、apply/rollout测试；未接入真实K8s |
| P06/P07 | Store/Service及任务页 | 端口冲突、并发操作拒绝、中断恢复、导出任务；浏览器模拟部署闭环 |
| P08 | 总览、实例列表、健康与重启入口 | 健康失败阈值与恢复测试；浏览器筛选与页面展示 |
| P09 | endpoints/OpenAPI/Console | JSON默认值、路由白名单、状态码保留、试调分类、SSE；真实Chromium操作 |
| P10 | BuiltinMonitoringProvider | 实例隔离、probe排除、重启/断连、窗口裁剪、非200含201、非法计数测试 |
| P11 | 管理客户端与详情抽屉 | 日志分页、数据库行、请求→trace契约测试及浏览器流程 |
| P12 | 导出任务、受控下载 | logs/database/traces三类下载、SQLite可打开、一次性链接、过期与认证检查 |
| P13 | alerts表与告警页 | 连续三次失败触发、恢复关闭、确认、敏感信息脱敏 |
| P14 | tests与e2e | 38项后端测试、4项浏览器测试通过；前端构建、Ruff、容器启动检查通过 |

## 实际执行的验证

1. `backend/.venv/bin/pytest`：**38 passed**。使用与文档路径/字段一致的独立ASGI契约模拟服务；不会导入mock实现。Starlette对其httpx测试适配器有一条弃用提示，不影响结果。
2. `backend/.venv/bin/ruff check src tests`：通过。
3. `frontend/npm run build`：TypeScript类型检查与Vite生产构建通过。
4. `frontend/npm run test:e2e`：**4 passed**。覆盖桌面总览/筛选、OpenAPI表单、JSON/SSE、日志翻页、数据库表、失败请求和trace、数据库文件下载、部署表单与后台任务、移动端无横向溢出、环境创建。截图在被忽略的test-results目录。
5. `docker compose config --quiet`：通过。
6. Docker构建、独立容器启动检查：页面200、未认证API401、认证后的空实例列表200、没有虚构指标。测试容器验证后已停止删除。
7. 构建环境存在代理CA：首次默认构建因容器不信任代理证书而失败；增加可选BuildKit构建期CA挂载后成功，未禁用TLS。

浏览器测试中的部署执行器被显式替换为测试实现；远程执行器另用命令构造和bash语法测试验证。两者均不等同真实主机部署验收。

## 设计选择与限制

- 后端单进程、单worker。任务记录持久化，进程重启后的进行中操作标为interrupted，由操作者核查远端状态后处理，避免重复创建资源。
- 远程基础依赖由用户提供，Panel不安装操作系统组件。包源故障或依赖安装失败直接保留失败日志，不自动绕过系统包保护。
- SSH协议使用SFTP传输文件，提供与用户所述scp相同的上传用途，统一凭据与主机指纹校验。
- K8s原清单直接应用，不注入业务配置；SSH主机需就是所选节点，且具备可用kubectl context。用户配置mock token和Service地址。
- 流量是有效采集区间的近似统计。每实例有效区间QPS求和；间断和重启不会伪造零流量。没有精确账单计数、P95/P99、自定义规则编辑器。
- 当前监控轮询为单后台线程，适合V1少量实例；大量离线主机会延长轮询周期，长缺口会显示缺失。
- 失败远程资源保留，不实现自动删除、回滚或升级。源码和镜像上传文件留在Panel用于记录与排障；导出文件保留24h。
- 管理员凭据在服务端加密，部署令牌与SSH密码不出现在列表或任务日志；生产环境需由部署方配置HTTPS入口并妥善备份数据目录和加密密钥。
- JSON/OpenAPI请求协议为试调主路径；复杂multipart/file上传或任意自定义编辑器不在V1范围。

## 后续联调清单

1. mock侧实现panel.v1，核对启动命令、环境变量、鉴权及错误响应。
2. 用真实实例验证ready/info/endpoints/OpenAPI和三类业务接口。
3. 连续产生200/非200/校验失败，核对HTTP计数、分类与request_id/trace关联。
4. 在业务写入期间导出SQLite，打开快照核对一致性；验证日志和trace导出大小、下载、过期。
5. 各准备一个Linux裸机、Docker与指定节点K8s环境，验证部署、就绪、重启、端口和数据持久化；检查失败日志。
6. 如mock契约发生变化，先更新需求文档并同步Panel客户端，再追加真实联调结果。

部署命令、配置与恢复说明见 [README](../README.md)。本交付不声称mock依赖已真实实现或远程部署已验收。
