# Mock Control · V1

独立运行的 Mock 服务控制台：Python/FastAPI 后端、TypeScript/React 前端、SQLite 数据与内置监控。不依赖 `agent_mock_service` Python 包，不需要 Grafana、Prometheus、Redis 或外部数据库。

mock 接口契约：[MOCK_REQUIREMENT_001](../agent_mock_service/requirements/MOCK_REQUIREMENT_001.md)。已使用仓内真实 mock 实现完成 ASGI 接口联调；测试目录中的契约模拟服务不会随生产应用启动。

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
| `PANEL_ADMIN_TOKEN` | 自动生成 | 首次管理员凭据；页面改密后使用持久化新密码，不再由此变量覆盖 |
| `PANEL_POLL_INTERVAL` | 30 秒，最小 5 秒 | 每轮实例采集间隔；离线实例会增加整轮耗时 |
| `PANEL_PACKAGE_LIMIT_BYTES` | 2147483648（2 GiB） | 源码/镜像上传上限 |
| `PANEL_MANIFEST_LIMIT_BYTES` | 2097152（2 MiB） | YAML 清单上传上限 |
| `PANEL_FRONTEND_DIST` | 项目 `frontend/dist` | 构建产物路径 |

部署到容器：在本目录运行 `docker compose up --build -d`，默认仅绑定宿主 `127.0.0.1:8000`。使用 `docker compose exec panel cat /data/admin.token` 获取首次令牌。对外访问时配置自己的 TLS 反向代理。

构建网络如使用企业代理 CA，可先执行 `docker build --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem -t svc-panel:local .`。证书仅挂载到构建阶段，未关闭 TLS 校验；普通网络无需此参数。

备份时停止 Panel 后整体备份数据目录，必须同时保留 `credentials.key` 和 SQLite；密钥丢失后无法解密 SSH 凭据与 mock 管理令牌。

## 需求与反馈

右上角刷新、告警后面的**灯泡按钮**打开“需求与反馈”右侧抽屉。清单支持状态筛选，以及提出时间/状态变更时间的升序、降序排序；默认新提出的在前，每页 50 条。

点击“新增需求 / 反馈”，仅需求简述必填（最多 256 字）。提出人选填、最多 16 字，不校验真实身份；描述选填、最多 20,000 字。字符长度按 Unicode 码点计数，emoji 与中文同样计数。四种状态为待处理、开发中、待验收、已闭环，新增默认待处理。

点击清单卡片查看详情，支持编辑文字、状态、配图和确认删除。配图支持 PNG/JPEG/WebP/GIF，最多 5 张、单张 ≤5 MiB、≤2000 万像素；点击图片可打开原图。录入时间只在创建时记录；状态更新时间初始等于录入时间，之后只有状态实际变化才更新。普通文字/配图编辑不会刷新状态时间。

文字和图片 BLOB 分别保存在同一 `panel.sqlite` 的 `feedback` / `feedback_images` 表。保存原子提交，删除级联清理图片，不使用外部图床。图片读取同样需要认证，浏览器以临时 Blob URL 展示；列表不传输图片内容。共享清单无用户隔离，同时编辑采用版本检查，过期编辑/删除返回 409，页面可重新加载最新详情。

接口均位于 `/api/feedback`：

- `GET /api/feedback?status=pending&sort=created_desc&limit=50&offset=0`：清单；状态可省略。排序值为 `created_asc`、`created_desc`、`status_asc`、`status_desc`。
- `POST /api/feedback`：新增 JSON `{summary, author?, description?, status?, images?}`；状态内部值 `pending/developing/acceptance/closed`。
- `GET /api/feedback/{id}`：完整字段、`revision` 和图片元数据。
- `PUT /api/feedback/{id}`：提交完整编辑字段及当前 `revision`；`images` 是要保留的全部配图，已有图填 `{id}`，新图填 `{filename, data_base64}`；省略图片列表视为清空。
- `DELETE /api/feedback/{id}?revision=N`：删除当前版本及其图片。
- `GET /api/feedback/{id}/images/{image_id}`：认证读取原图。

JSON 请求体最大 36 MiB（含 Base64），服务端检查实际图片格式/像素/大小；前端选择时也检查数量和大小。备份仍需停止 Panel 后备份完整数据目录。升级需重新安装后端锁定依赖（新增 Pillow 图片校验库）并构建前端；数据库新增表自动创建，原业务数据不变。

## 环境删除与管理员改密

- **远程环境 → 环境卡片 → 删除环境**：确认后移除 Panel 保存的连接、加密 SSH 凭据和指纹，不连接远端，不删除远端服务/文件。
- 环境仍关联可见且未卸载的实例时，返回引用实例名称并阻止删除。请先卸载实例，或仅移除监测。隐藏实例若仍有排队/执行中的任务，也需等待任务结束。已卸载记录及结束的任务历史保留。
- **设置与帮助 → 修改登录密码**：输入当前密码或原访问令牌、新密码和确认密码。新密码为 16–256 位英文字母、数字或符号，不含空格，可使用密码管理器生成。
- 保存后退出当前页面；其他页面在下次请求时也必须用新密码登录。旧凭据和已签发的下载票据失效，已开始的请求/后台任务继续，不影响 SSH 和 Mock 管理令牌。登录页原“访问令牌”输入框同样接受新密码。
- 新密码保存为带随机盐的 scrypt 校验值，**重启后优先于 `PANEL_ADMIN_TOKEN` 和旧 `admin.token`**。旧 bootstrap 文件会尝试删除；即使因文件权限留下，也不再生效。程序依然要求单进程、单 worker。
- 接口：`DELETE /api/environments/{id}`；`POST /api/account/password`，JSON 字段为 `current_password`、`new_password`、`confirm_password`。当前凭据错误返回 403，字段错误 422；仍需有效 Bearer 认证。

如果遗失新密码，由具有数据目录访问权限的主机管理员停机并备份完整数据目录，然后从 `panel.sqlite` 删除 `objects` 中 `kind='auth' AND id='admin'` 的记录，同时移除旧 `admin.token`；配置新的 `PANEL_ADMIN_TOKEN` 后启动，或清除此环境变量让程序重新生成 `admin.token`。这是离线恢复操作，不能在运行中的 Panel 上直接修改数据库。回退到不支持改密的旧版会忽略新校验记录，需重新配置可信的管理员令牌，不能依赖新密码仍生效。

## 部署与运行约定

- 只支持 Linux；UI 中其他操作系统、多节点 K8s、Docker bridge 均禁用。
- 新增环境填写 SSH 主机、用户、密码或私钥；SHA256 主机指纹可选。目标主机可通过 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256` 查看指纹，经可信渠道核对后填写。留空时采用首次使用信任：首次成功认证后持久记住指纹，后续变化拒绝连接。已填写的指纹仍严格校验。
- 部署账户需要 root 或非交互 `sudo -n` 权限；基础环境检查失败会结束任务。Panel 不安装 systemd、Docker 或 Kubernetes。
- 远端根目录可配置，默认 `/opt/svc-panel`；实际实例目录为 `<remote_root>/<instance-id>`。上传前检查目录权限/容量，源码还计入解包大小与环境安装最低预留空间。systemd 单元或容器名为 `mock-<instance-id>`；上传内容与配置仅部署账户可读。数据库、日志和 trace 使用单独持久化目录。
- 裸机：目标需 Python ≥3.11 与 systemd。源码 tar 须在根目录或唯一顶层目录含 `pyproject.toml`，支持 `pip install '.[api]'`。先检查 conda；有则创建独立环境，无则尝试 venv，venv 创建失败才尝试系统 Python。包安装失败直接报告；不绕过发行版 PEP 668 保护。
- Docker：需要运行中的 Docker daemon；上传 `docker save` 镜像包，单镜像可自动解析，多个镜像需指定本次加载结果中的 image reference。宿主与容器端口默认均为 32882，可分别配置。支持标准 Mock 入口、镜像自身 ENTRYPOINT/CMD、自定义 ENTRYPOINT/CMD；默认标准入口会覆盖镜像 entrypoint。额外环境变量加密保存，`MOCK_ADMIN_TOKEN`、`MOCK_INSTANCE_ID`、`MOCK_DATA_DIR`、`MOCK_LOG_DIR`、`MOCK_TRACE_DIR` 由 Panel 管理。支持额外挂载及常见 Docker argv 参数。默认失败最多重启 3 次，0 表示不自动重启；检测容器退出时返回退出码、重启次数和尾部日志。加载前另检查 Docker 数据目录剩余空间。
- K8s：SSH 环境必须是指定节点，且已有可用 kubeconfig/kubectl；校验节点 InternalIP 与主机地址匹配。支持 `docker load`、`ctr -n k8s.io images import`、`k3s ctr -n k8s.io images import`。多节点自动分发暂不支持。
- K8s 清单只接受一个目标 Deployment，以及同 namespace 的 Service/ConfigMap/Secret/PVC；需要显式 nodeName 或 hostname nodeSelector、显式 Never/IfNotPresent 拉取策略。用户需配置业务参数、管理 token、数据持久化与访问入口，Panel 保留业务配置，添加实例归属标签；为保证卸载边界，拒绝覆盖已有同名对象。表单管理 token 必须与清单一致；K8s 中 MOCK_INSTANCE_ID 由清单指定，可与 Panel 的登记 ID 不同。
- 访问地址单独填写；直连支持 HTTP/HTTPS，SSH 隧道 V1 支持 HTTP（可用目标主机 localhost 地址）。K8s 的 NodePort/Ingress 地址由清单决定，不自动沿用 32882。
- 每实例同时只执行一个部署、重启、重新部署、卸载或导出任务。失败时保留任务日志和远端资源用于诊断，不自动删除镜像、目录或 K8s 资源。Panel 重启后未完成任务标为“已中断”，不会自动重放可能已经执行的远程操作。

## Docker 免传包重新部署

点击 Docker 实例右侧循环箭头“重新部署实例”，会打开与新增实例相同的表单，并回填上次提交的参数：

- 名称、环境、部署方式、目录、原部署包和镜像固定。
- 可修改启动方式/ENTRYPOINT/CMD、容器监听端口、失败重启次数、环境变量、额外挂载、更多 Docker 参数，以及“03 服务连接”的暴露端口、访问方式、URL 和管理令牌。
- 点击“保存参数并重新部署”，复用远端原镜像的不可变 ID 及 data/logs/traces，跳过部署包上传和镜像加载，也不从仓库拉取镜像。修改暴露端口后需同步修改服务访问 URL。
- 新容器健康检查通过才删除旧容器；失败尝试恢复原容器和原生效配置，同时保留本次提交参数，方便再次修正。持久化目录保留，但不回滚新进程已经写入的数据；切换会短暂中断服务、清空内存会话。
- 若 Panel 中断或 SSH 故障留下恢复记录，下次重新部署/卸载先处理恢复，再继续操作。远端资源归属发生变化时拒绝自动处理并在任务中报错。
- 初次启动失败也可重部署，前提是原镜像 ID 与持久化目录可用；旧版本实例可从仍存在且归属正确的原容器获取镜像 ID。缺少资源时报错，不自动重新传包。

本轮参数重部署支持 Panel 部署的 Docker 实例；裸机和 K8s 保留原重启行为，外部接入实例不开放远端生命周期操作。配置回填 API 仅管理员可用、禁止缓存；令牌和额外环境变量继续加密保存。旧 `POST /api/instances/{id}/restart` API 仍可用于不改配置的普通重启。

## 新增、移除与卸载

- **新增实例 → 部署新实例**：填写目录及启动参数后先检查远程目录，再上传。UI 显示浏览器 → Panel 的进度；任务详情显示 Panel → 远端的字节、速率和耗时，以及镜像加载、创建容器、等待就绪等阶段。可取消浏览器上传；已提交的远端任务不因此取消。
- **新增实例 → 监测已有实例**：填写部署方式、URL 和管理 token，验证 `/ready`、管理协议及接口清单后接入。直连不需要 SSH；隧道访问需选择环境。外部实例不具备可信的容器/systemd/K8s 资源归属，重启和卸载按钮置灰。
- **实例更多操作 → 移除监测**：移除列表和采集，清除该实例的监控样本，远端完全不动，已有任务继续。后台记录及任务保留。再次接入会成为外部实例，不能自动恢复原部署资源管理权；需要卸载时应先卸载再移除监测。
- **实例更多操作 → 卸载实例**：输入实例名确认。停止并删除所属容器/systemd/K8s 资源及独立实例目录，**包含该目录内数据库、日志、trace**；先回流需要保留的数据。保留共享镜像、用户额外挂载、系统 Python 包及 Panel 本地上传缓存。完成后保留“已卸载”记录，可再移除监测。
- 旧 Docker/裸机实例兼容默认目录的清理：Docker 校验原持久化挂载，systemd 校验 WorkingDirectory。新实例使用归属标签/标记及记录的容器 ID。目标已被人工删除时，仍清理其余所属文件。资源归属不符则拒绝清理。旧 K8s 无归属记录须人工清理后移除监测。
- K8s 卸载包含清单中 PVC，PVC 对应数据是否回收取决于集群策略；额外 hostPath 目录不会递归清理。

### Docker 参数示例

自定义 ENTRYPOINT 填 `/bin/sh`，CMD 参数填 `["-c", "exec python -m uvicorn agent_mock_service.app_service:mock_service_app --host 0.0.0.0 --port 32882"]`。不需要 shell 时直接使用可执行文件和参数数组。

```json
{
  "command_mode": "custom",
  "entrypoint": "python",
  "command": ["-m", "uvicorn", "agent_mock_service.app_service:mock_service_app", "--host", "0.0.0.0", "--port", "8000"],
  "container_port": 8000,
  "max_restarts": 3,
  "environment": {"MY_SETTING": "value"},
  "extra_args": ["--cpus", "2", "--memory", "2g"],
  "mounts": [{"source": "/data/models", "target": "/models", "read_only": true}]
}
```

`command_mode=image` 保留镜像自身入口。自定义模式下 `entrypoint=null` 保留原入口，空字符串可清除入口（API）；命令和 Docker 参数按 argv 传递，不自动解释 shell。额外挂载目录必须已存在；自定义用户需具备持久化挂载目录权限。不要在命令参数中放密钥，密钥使用环境变量字段。支持的 argv 选项见 `DockerOptions.validate_options`，未支持的选项返回明确校验错误；`--name/--restart/-p/-v/--network` 使用专门配置，不能在额外参数中覆盖。

### 上传慢的定位与协议变化

旧版 `UploadFile` 会先由框架接收/缓存完整 multipart，再进入代码中的分块读取，因此“循环累计超过限制”并不等于“网络传输途中立即拒绝”。旧仓库实际阈值是部署包 1 GiB、清单 2 MiB；无法仅凭反馈确定用户现场 2 MB 错误是面板、代理还是本地修改产生。

新版 `GET /api/upload-policy` 提供当前上限，前端选文件时检查；`POST /api/artifacts?kind=image&filename=image.tar` 使用 `Content-Type: application/octet-stream`、`X-Upload-Size: <字节数>` 和文件二进制正文。后端读取正文前检查声明大小及本地空间，传输中继续累计实际大小，失败/取消清理临时文件。返回 `upload_seconds`、`validation_seconds`。**旧 multipart 上传客户端需同步升级**。反向代理需同步配置允许的 body 大小；若代理缓冲完整请求，关闭此路由的请求缓冲才能保留流式提前拒绝行为。

## 内置监控与诊断

- 保存每个实例的累计计数，按 `boot_id`、路由、方法、分类、状态码计算增量；默认只统计 business。
- 历史指标保留 7 天。跨窗口边界按时间比例估算；200 与其他 HTTP 状态码分开统计，非 200 不直接等同业务失败。
- 进程重启、计数回退及大于 2.5 个采集间隔的缺口不会推算请求量；图表缺失区间留空。QPS 按每实例有效采集时段计算后求和，避免将断连时间算作零流量。
- 三次连续就绪检查失败创建健康告警，恢复自动关闭；监控采集失败和操作失败另行记录。告警可确认，确认不等于恢复。
- 接口清单来自实例；请求模型来自 OpenAPI，支持 JSON 表单/原文、headers、query/path 参数以及 JSON/SSE 响应。试调通过后端代理，URL/方法限定为实例发现的接口；最多读取 2 MiB 或 120 秒，单次无响应超时 30 秒。
- 日志分页、失败请求→详情→trace、数据库表与分页行查看均走管理 API，不开放任意 SQL 或任意远程文件读取。
- 日志/数据库/trace 回流为后台任务：请求实例生成导出，等待 ready，流式下载到 Panel。数据库一致性由 mock 的在线备份接口保障。下载产物保留 24 小时，最大 2 GiB；源码/镜像上传默认最大 2 GiB，清单默认最大 2 MiB，见配置项。
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

真实 mock 接口集成测试可独立运行（仅测试安装对侧包，生产 Panel 不依赖它）：

```bash
uv pip install --python service_control_panel/backend/.venv/bin/python -e './agent_mock_service[api]'
service_control_panel/backend/.venv/bin/pytest service_control_panel/backend/tests/test_mock_integration.py
```

可选 Docker 引擎测试：设置 `PANEL_DOCKER_TEST_IMAGE=<含实际 mock 包、Python、uvicorn 的测试镜像>`，运行 `backend/tests/test_docker_integration.py`；需要可用 Docker，且 daemon 能访问本机临时目录。测试使用真实启动/检查/停止/删除命令，本地传输替代 SSH；为兼容不同用户运行的 rootless daemon，临时持久化目录设置可写权限，仅写入测试数据。验证自定义入口、有限次重启、免传包参数重部署、错误命令恢复、数据保留和归属清理；不验证 SSH/SFTP、镜像传输/加载及 Docker 数据目录容量。测试创建并清理 UUID 容器/目录，保留指定镜像。未设置时跳过。

## 尚需环境验收

真实 SSH/SFTP 网络、Linux/systemd/conda/venv/系统 Python 与 K8s 节点运行时/用户清单仍需目标环境验收。实际 mock ASGI 联调与本地 Docker 引擎测试不等于远程主机端到端部署成功。
