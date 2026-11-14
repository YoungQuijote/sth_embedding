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
