# Mock Control · V1

独立运行的 Mock 服务控制台：Python/FastAPI 后端、TypeScript/React 前端、SQLite 数据与内置监控。不依赖 `agent_mock_service` Python 包，不需要 Grafana、Prometheus、Redis 或外部数据库。

mock 接口契约：[MOCK_REQUIREMENT_001](../agent_mock_service/requirements/MOCK_REQUIREMENT_001.md)。当前 Panel 按该契约实现，mock 真实实现完成后需联调；测试目录中的契约模拟服务不会随生产应用启动。

## 启动

从仓库根目录执行：

```bash
cd service_control_panel/frontend
npm ci
npm run build
cd ../backend
uv sync --frozen --extra dev
uv run uvicorn svc_panel.app:create_app --factory --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000`。首次启动生成 `backend/.panel/admin.token`，读取该文件作为登录令牌。也可以预先设置至少 16 字符的 `PANEL_ADMIN_TOKEN`。

开发时两个终端分别运行后端和 `frontend/npm run dev`（5173，`/api` 代理至 8000）。生产构建由后端同源托管。后端必须使用 **单个 worker**，避免重复采集或重复执行任务。

### 配置

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `PANEL_DATA_DIR` | 当前目录 `.panel` | SQLite、加密密钥、上传文件、导出文件 |
| `PANEL_ADMIN_TOKEN` | 自动生成 | 管理员访问令牌 |
| `PANEL_POLL_INTERVAL` | 30 秒，最小 5 秒 | 每轮实例采集间隔；离线实例会增加整轮耗时 |
| `PANEL_FRONTEND_DIST` | 项目 `frontend/dist` | 构建产物路径 |

部署到容器：在本目录运行 `docker compose up --build -d`，默认仅绑定宿主 `127.0.0.1:8000`。使用 `docker compose exec panel cat /data/admin.token` 获取首次令牌。对外访问时配置自己的 TLS 反向代理。

构建网络如使用企业代理 CA，可先执行 `docker build --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem -t svc-panel:local .`。证书仅挂载到构建阶段，未关闭 TLS 校验；普通网络无需此参数。

备份时停止 Panel 后整体备份数据目录，必须同时保留 `credentials.key` 和 SQLite；密钥丢失后无法解密 SSH 凭据与 mock 管理令牌。

## 部署与运行约定

- 只支持 Linux；UI 中其他操作系统、多节点 K8s、Docker bridge 均禁用。
- 新增环境需填写 SSH 主机、用户、密码或私钥以及可信 SHA256 主机指纹。目标主机可通过 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256` 查看指纹，经可信渠道核对后填写。不会自动信任首次连接的未知密钥。
- 部署账户需要 root 或非交互 `sudo -n` 权限；基础环境检查失败会结束任务。Panel 不安装 systemd、Docker 或 Kubernetes。
- 远端实例目录为 `/opt/svc-panel/<instance-id>`，systemd 单元或容器名为 `mock-<instance-id>`；上传内容与配置仅部署账户可读。数据库、日志和 trace 使用单独持久化目录。
- 裸机：目标需 Python ≥3.11 与 systemd。源码 tar 须在根目录或唯一顶层目录含 `pyproject.toml`，支持 `pip install '.[api]'`。先检查 conda；有则创建独立环境，无则尝试 venv，venv 创建失败才尝试系统 Python。包安装失败直接报告；不绕过发行版 PEP 668 保护。
- Docker：需要运行中的 Docker daemon；上传 `docker save` 镜像包，单镜像可自动解析，多个镜像需指定本次加载结果中的 image reference。宿主默认端口 32882 映射容器 32882，使用契约启动入口覆盖镜像 entrypoint。
- K8s：SSH 环境必须是指定节点，且已有可用 kubeconfig/kubectl；校验节点 InternalIP 与主机地址匹配。支持 `docker load`、`ctr -n k8s.io images import`、`k3s ctr -n k8s.io images import`。多节点自动分发暂不支持。
- K8s 清单只接受一个目标 Deployment，以及同 namespace 的 Service/ConfigMap/Secret/PVC；需要显式 nodeName 或 hostname nodeSelector、显式 Never/IfNotPresent 拉取策略。用户需配置业务参数、管理 token、数据持久化与访问入口，Panel 不改写原清单。表单管理 token 必须与清单一致；K8s 中 MOCK_INSTANCE_ID 由清单指定，可与 Panel 的登记 ID 不同。
- 访问地址单独填写；直连支持 HTTP/HTTPS，SSH 隧道 V1 支持 HTTP（可用目标主机 localhost 地址）。K8s 的 NodePort/Ingress 地址由清单决定，不自动沿用 32882。
- 每实例同时只执行一个部署、重启或导出任务。失败时保留任务日志和远端资源用于诊断，不自动删除镜像、目录或 K8s 资源。Panel 重启后未完成任务标为“已中断”，不会自动重放可能已经执行的远程操作。

## 内置监控与诊断

- 保存每个实例的累计计数，按 `boot_id`、路由、方法、分类、状态码计算增量；默认只统计 business。
- 历史指标保留 7 天。跨窗口边界按时间比例估算；200 与其他 HTTP 状态码分开统计，非 200 不直接等同业务失败。
- 进程重启、计数回退及大于 2.5 个采集间隔的缺口不会推算请求量；图表缺失区间留空。QPS 按每实例有效采集时段计算后求和，避免将断连时间算作零流量。
- 三次连续就绪检查失败创建健康告警，恢复自动关闭；监控采集失败和操作失败另行记录。告警可确认，确认不等于恢复。
- 接口清单来自实例；请求模型来自 OpenAPI，支持 JSON 表单/原文、headers、query/path 参数以及 JSON/SSE 响应。试调通过后端代理，URL/方法限定为实例发现的接口；最多读取 2 MiB 或 120 秒，单次无响应超时 30 秒。
- 日志分页、失败请求→详情→trace、数据库表与分页行查看均走管理 API，不开放任意 SQL 或任意远程文件读取。
- 日志/数据库/trace 回流为后台任务：请求实例生成导出，等待 ready，流式下载到 Panel。数据库一致性由 mock 的在线备份接口保障。下载产物保留 24 小时，最大 2 GiB；源码/镜像上传最大 1 GiB，清单最大 2 MiB。
- 浏览器下载使用经管理员认证签发的 60 秒一次性链接，浏览器原生流式接收文件，不把管理员令牌放入 URL，也不先将整个文件加载为内存 Blob。
- `MonitoringProvider` 是外部监控扩展边界，V1 仅实现 `BuiltinMonitoringProvider`。没有自动探测 Grafana，也没有运行时切换数据源。

## 验证

```bash
cd service_control_panel/backend
uv run --extra dev pytest
uv run --extra dev ruff check src tests
cd ../frontend
npm run build
npm run test:e2e
```

浏览器测试使用系统 `/usr/bin/chromium`，可用 `CHROMIUM_PATH` 覆盖；需已安装 backend dev 环境。测试自动启动临时 Panel 和契约模拟服务，使用 8000 / 32883 端口；不要同时启动正式实例占用这些端口。截图与失败 trace 在 `frontend/test-results/`。

模拟预览：构建前端后，在 backend 执行 `.venv/bin/python tests/preview_server.py`，访问 8000，令牌为 `browser-test-token-123456789`。这只用于开发：数据临时生成、SSH 部署被明确替换成模拟执行器，不能作为真实部署验收。

## 尚需实际联调

mock 的真实管理接口、真实 Linux/systemd/conda/venv/系统 Python、Docker 镜像、K8s 节点运行时与用户清单。当前自动化覆盖管理契约、数据库快照下载、统计、后台任务、部署命令构造及浏览器操作，不把模拟测试声明为真实远程部署成功。
