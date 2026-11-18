# Docker 参数重部署交付

- 需求：[PANEL_REQUIREMENT_005](../requirements/PANEL_REQUIREMENT_005.md)
- 日期：2026-11-18（Asia/Shanghai）
- 分支：`codex/panel-reconfigure`
- 基线：`674a7b5`，main 已合并 PR #15
- 状态：待验收

## 使用方式

点击 Panel 部署的 Docker 实例右侧循环箭头“重新部署实例”，打开回填表单。身份、环境、目录、部署包及镜像置灰；修改启动命令、环境变量/挂载/更多 Docker 参数或“03 服务连接”，点击“保存参数并重新部署”。任务进度在原任务详情中查看；下次打开仍回填上次提交内容，包括部署失败时的提交。

该入口重建容器以应用新参数。裸机/K8s 继续执行原重启逻辑，原 `POST /restart` API 仍保留。

## 实现与恢复

- `GET /api/instances/{id}/configuration`：管理员认证读取回填配置、只读身份及 revision，响应 `Cache-Control: no-store`。
- `POST /api/instances/{id}/redeploy`：只接受可变字段与 revision，保存加密配置后排入异步任务；过期版本/进行中任务/端口或 URL 冲突返回 409，固定属性或参数非法返回 422。
- SQLite `objects` 中实例增加 `requested_configuration`、`configuration_revision`、`loaded_image_id` 及 `redeploy_recovery`；无需建新表。普通实例/任务接口不暴露配置快照、恢复记录或令牌/环境变量值。
- 初次 Docker 部署加载镜像后，先保存不可变镜像 ID，再尝试启动；已有记录没有镜像 ID 时，验证归属后从原容器 `.Image` 读取。不按可变 tag 猜测镜像。
- 先创建候选容器，再停止旧容器并换名保留，然后启动新容器。复用原 data/logs/traces，候选容器使用 `--pull never`。全程不访问本地 artifact 文件、不执行 SFTP 镜像上传或 `docker load`，只写入新的小型配置文件。
- 健康/管理协议检查通过后清理旧容器，替换 `service.env`。创建/启动/健康检查失败时尝试删除候选容器、恢复旧容器及旧连接配置；失败提交仍保留。
- 恢复记录在远端变更前落库；Panel 中断后不会自动重放部署。下次重新部署或卸载先恢复/完成已确认的提交，再进行操作。恢复未完成时普通 restart API 拒绝执行，任务提示检查远端后重试。
- 旧容器与候选容器的 ID/归属必须匹配；远端资源被外部修改或 SSH 不可用时，恢复可能失败，保留日志及记录供处理。恢复是容器/配置恢复，不能撤销新进程已经写入的持久化数据。

## 验证

- 后端完整回归在功能实现后通过（107 passed、1 skipped）；补充任务层健康失败恢复、初次启动失败保存镜像后，部署/重部署定向回归 24 项通过。
- 真实本地 Docker + 实际 mock 镜像：成功更改暴露端口、管理令牌、环境变量和内存限制；确认新旧镜像 ID 相同、容器 ID 改变、持久化文件保留；故意设置不存在的 ENTRYPOINT 后恢复原容器并重新就绪。有限重启及卸载清理用例同时通过。
- TypeScript/Vite 构建通过；浏览器完整回归 11 条流程通过。重部署流程检查固定字段置灰、无文件输入、不请求上传/空间预检 API、提交参数及重新打开回填；包含桌面/390px 窄屏截图。
- Ruff 与 diff 空白检查通过。截图位于忽略的 `frontend/test-results/redeploy-desktop.png`、`redeploy-mobile.png`。
- 浏览器以契约服务及模拟 SSH 执行器验证 UI/API 流程；Docker 测试用本地传输替代 SSH。未对用户实际远程 SSH 主机验收，本次不需要 mock 侧新增接口。

## 升级与限制

重新构建前端、重启后端，或重建 Panel 容器；无新增依赖。旧实例无新增配置字段时按既有参数及默认值回填；旧实例若原容器和可验证的镜像记录均已丢失，无法安全确定原镜像，需恢复资源或重新创建实例。回退 Panel 版本前，应先完成所有重部署任务及恢复清理，避免旧版本无法识别恢复记录。
