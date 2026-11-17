# 该文件从语义级别记录需求录入, 代码变更, 接口变更.

# 该文件只增不减

---

## 记录格式及字段语义说明

### 需求标题: 填写需求的简述标题，可以是commit时的短文本描述

**需求文档**: 填写此次变更是为了解决哪篇录入的需求文档
如果需求文档记录在/service_control_panel/requirements/目录下，则填写对应路径，例如：/service_control_panel/requirements/PANEL_REQUIREMENT_XXX.md；
如果需求文档不记录在/service_control_panel/requirements/目录下，则填写`cloud`；
如果没有对应需求文档，则填写`null`

**解决方案**: 简述语义级别的解决方案，例如：新增deploy模块，并提供前端一键部署功能；优化UI排版；etc.

**交付文档**: 填写`详细的变更说明/交付文档`所在路径
约定将`详细文档`记录在/service_control_panel/deliveries目录下，
`详细文档`的命名格式为`PANEL_DELIVERY_XXX.md`，其中PANEL_DELIVERY_为固定前缀，如果有对应的需求文档，则XXX与对应需求文档的编号一致；
如果没有对应的需求文档或对应需求文档没有编号，则XXX为`yyyyMMddHHmm`的(UTC+8)时间字符串，例如`202611130600`；时间以你追加变更记录前调用的时间工具所给时间为准

**变更时间**: 提交代码的日期(yyyyMMdd)

**代码变更**: 记录发生了变更的代码文件，只记录文件路径，不做row级别的细节变更记录
约定如下记录符号：
`+ <文件路径>`: 新增<文件路径> ;
`- <文件路径>`: 删除<文件路径> ;
`<文件路径> +N`: <文件路径>新增N行代码，N为整数类型 ;
`<文件路径> -N`: <文件路径>删除N行代码，N为整数类型 ;
`<文件路径> +N/-M`: <文件路径>新增N行代码，同时删除M行代码，N和M为整数类型 ;
`<文件路径A> > <文件路径B>`: <文件路径A>变更为了<文件路径B>，用于描述重命名或文件移动 ;

**接口变更**: 记录发生了变化的功能接口
约定如下记录符号：
`+ package.module.fn(args); <fn功能简述>`: 新增功能为<fn功能简述>的功能接口package.module.fn ;
`- package.module.fn(args)`: 删除功能接口fn ;
`packageA.moduleA.fnA(argsA) > packageB.moduleB.fnB(argsB)`: 功能接口 packageA.moduleA.fnA 变更为了功能接口 packageB.moduleB.fnB；该记录一般用于表示同功能(或同性质)的接口发生了移动、归属变更或参数变更 

(写完后，用`\n + --- + \n`和下一段分隔)

---

### 需求标题: 变更填写示例

**需求文档**：/service_contol_panel/requirements/PANEL_REQUIREMENT_000.md

**解决方案**: 追加了一段变更记录，示例变更记录如何填写

**交付文档**: /service_contol_panel/deliveries/PANEL_DELIVERY_000.md

**代码变更**: null

**接口变更**: null

---

### 需求标题: 实现内置监控与远程部署管理控制台 V1

**需求文档**: /service_control_panel/requirements/PANEL_REQUIREMENT_001.md

**解决方案**: 增加独立 Python 后端和 TypeScript 前端，提供 Linux 裸机/Docker/指定节点 K8s 部署、实例健康与重启、接口发现和试调、内置监控与告警、日志/数据库/trace 查看及回流。mock 管理接口以已推送的 MOCK_REQUIREMENT_001 为契约，等待真实联调。

**交付文档**: /service_control_panel/deliveries/PANEL_DELIVERY_001.md

**变更时间**: 20261115

**代码变更**:

/service_control_panel/README.md +80/-2
+ /service_control_panel/.dockerignore
+ /service_control_panel/.gitignore
+ /service_control_panel/Dockerfile
+ /service_control_panel/backend/pyproject.toml
+ /service_control_panel/backend/src/svc_panel/__init__.py
+ /service_control_panel/backend/src/svc_panel/app.py
+ /service_control_panel/backend/src/svc_panel/deploy.py
+ /service_control_panel/backend/src/svc_panel/models.py
+ /service_control_panel/backend/src/svc_panel/monitoring.py
+ /service_control_panel/backend/src/svc_panel/remote.py
+ /service_control_panel/backend/src/svc_panel/service.py
+ /service_control_panel/backend/src/svc_panel/store.py
+ /service_control_panel/backend/tests/contract_server.py
+ /service_control_panel/backend/tests/preview_server.py
+ /service_control_panel/backend/tests/test_deployment.py
+ /service_control_panel/backend/tests/test_monitoring.py
+ /service_control_panel/backend/tests/test_panel.py
+ /service_control_panel/backend/uv.lock
+ /service_control_panel/compose.yml
+ /service_control_panel/deliveries/PANEL_DELIVERY_001.md
+ /service_control_panel/frontend/e2e/panel.spec.ts
+ /service_control_panel/frontend/index.html
+ /service_control_panel/frontend/package-lock.json
+ /service_control_panel/frontend/package.json
+ /service_control_panel/frontend/playwright.config.ts
+ /service_control_panel/frontend/src/api.ts
+ /service_control_panel/frontend/src/components/ConsoleDrawer.tsx
+ /service_control_panel/frontend/src/components/DataDrawer.tsx
+ /service_control_panel/frontend/src/components/DeployDialog.tsx
+ /service_control_panel/frontend/src/components/EnvironmentDialog.tsx
+ /service_control_panel/frontend/src/components/common.tsx
+ /service_control_panel/frontend/src/labels.ts
+ /service_control_panel/frontend/src/main.tsx
+ /service_control_panel/frontend/src/style.css
+ /service_control_panel/frontend/tsconfig.json
+ /service_control_panel/frontend/vite.config.ts
+ /service_control_panel/requirements/PANEL_REQUIREMENT_001.md

**接口变更**:

+ svc_panel.app.create_app(data_dir, admin_token, background); 创建控制台API及同源前端应用
+ svc_panel.remote.SSH; SSH指纹校验、命令执行与SFTP上传
+ svc_panel.remote.api_client(instance, environment, store, timeout); 直连或SSH隧道访问实例管理API
+ svc_panel.deploy.Deployer.run(instance, restart); 按部署方式执行生命周期操作
+ svc_panel.monitoring.MonitoringProvider.summary(instance_id, minutes, endpoint, method); 监控数据源扩展协议
+ /api/environments、/api/artifacts、/api/instances、/api/jobs、/api/monitoring、/api/alerts; 管理REST接口
+ /api/instances/{id}/console、/logs、/requests、/database、/exports; 实例诊断与回流接口
+ /api/downloads/{id}/ticket、/api/files/{ticket}; 认证签发的短时一次性文件下载

---

### 需求标题: 部署目录与启动参数可配置，补齐实例接入、移除监测和卸载

**需求文档**: /service_control_panel/requirements/PANEL_REQUIREMENT_002.md

**解决方案**: SSH 指纹改为可选并持久记住首次成功连接；统一远程目录/容量检查；Docker 提供入口、参数、环境变量、挂载和有限重启；区分移除监测与按资源归属卸载；支持接入已有服务；原始流上传提前验证限额，展示两段传输及部署阶段进度。复用已实现的 panel.v1 接口，完成真实 mock 与本地 Docker 引擎联调。

**交付文档**: /service_control_panel/deliveries/PANEL_DELIVERY_002.md

**变更时间**: 20261116

**代码变更**:

/service_control_panel/README.md +54/-9
/service_control_panel/backend/src/svc_panel/app.py +117/-49
/service_control_panel/backend/src/svc_panel/deploy.py +110/-48
/service_control_panel/backend/src/svc_panel/models.py +134/-2
/service_control_panel/backend/src/svc_panel/remote.py +46/-19
/service_control_panel/backend/src/svc_panel/service.py +62/-21
/service_control_panel/backend/src/svc_panel/store.py +16/-0
/service_control_panel/backend/tests/preview_server.py +8/-1
/service_control_panel/backend/tests/test_deployment.py +15/-3
/service_control_panel/backend/tests/test_panel.py +6/-2
/service_control_panel/frontend/e2e/panel.spec.ts +78/-4
/service_control_panel/frontend/src/api.ts +68/-4
/service_control_panel/frontend/src/components/DataDrawer.tsx +33/-0
/service_control_panel/frontend/src/components/DeployDialog.tsx +215/-6
/service_control_panel/frontend/src/components/EnvironmentDialog.tsx +6/-3
/service_control_panel/frontend/src/labels.ts +3/-0
/service_control_panel/frontend/src/main.tsx +123/-4
+ /service_control_panel/backend/src/svc_panel/deployment_support.py
+ /service_control_panel/backend/src/svc_panel/uploads.py
+ /service_control_panel/backend/tests/test_docker_integration.py
+ /service_control_panel/backend/tests/test_lifecycle.py
+ /service_control_panel/backend/tests/test_mock_integration.py
+ /service_control_panel/deliveries/PANEL_DELIVERY_002.md
+ /service_control_panel/frontend/src/components/AttachDialog.tsx
+ /service_control_panel/requirements/PANEL_REQUIREMENT_002.md

**接口变更**:

+ GET /api/upload-policy; 查询按文件类型的上传上限及 raw-stream-v1 协议
+ POST /api/environments/{id}/storage; 检查部署根目录权限、空间及 Docker 存储容量
+ POST /api/instances/attach; 验证就绪/管理协议并接入已有服务
+ DELETE /api/instances/{id}; 停止监测并隐藏记录，不触碰远程资源
+ POST /api/instances/{id}/undeploy; 按名称确认后提交所属资源卸载任务
POST /api/artifacts; multipart 改为 application/octet-stream，需 filename 和 X-Upload-Size，进入正文前检查限额
POST /api/instances; 新增 remote_root 和 docker 启动配置；环境变量加密保存
POST /api/environments; host_key_sha256 改为可选，首次成功登录记录 learned_host_key_sha256
GET /api/jobs/{id}; 增加阶段、传输字节、速度与耗时信息

---

### 需求标题: 支持删除远程环境与管理员修改登录密码

**需求文档**: /service_control_panel/requirements/PANEL_REQUIREMENT_003.md

**解决方案**: 环境删除检查可见实例和全部未完成任务依赖，只移除本地连接/凭据；实例登记在锁内重新确认环境。设置页新增改密入口，兼容现有访问令牌，持久化带盐 scrypt 校验值，原子撤销下载票据，修改后重新登录。新增后端和浏览器回归测试及离线恢复说明。

**交付文档**: /service_control_panel/deliveries/PANEL_DELIVERY_003.md

**变更时间**: 20261117

**代码变更**:

/service_control_panel/README.md +12/-1
/service_control_panel/backend/src/svc_panel/app.py +62/-15
/service_control_panel/backend/src/svc_panel/models.py +14/-0
/service_control_panel/backend/src/svc_panel/remote.py +2/-0
/service_control_panel/frontend/e2e/panel.spec.ts +100/-0
/service_control_panel/frontend/src/main.tsx +97/-2
+ /service_control_panel/backend/src/svc_panel/auth.py
+ /service_control_panel/backend/tests/test_account_environment.py
+ /service_control_panel/deliveries/PANEL_DELIVERY_003.md
+ /service_control_panel/frontend/src/components/PasswordDialog.tsx
+ /service_control_panel/requirements/PANEL_REQUIREMENT_003.md

**接口变更**:

+ DELETE /api/environments/{id}; 删除无活动依赖的环境记录与凭据，不操作远端
+ POST /api/account/password; 校验当前凭据和新密码，持久改密并撤销旧下载票据
+ svc_panel.auth.AdminAuth; 兼容 bootstrap 令牌、scrypt 密码校验和原子轮换
POST /api/instances、POST /api/instances/attach; 登记锁内重新校验环境存在

---
