import React, { useState, useEffect, useRef } from "react";
import {
  CheckCircle2,
  Globe2,
  Layers3,
  Loader2,
  Plus,
  Rocket,
  Server,
  Terminal,
} from "lucide-react";
import { api, post, upload, Data } from "../api";
import { Modal } from "./common";
export function DeployDialog({
  environments,
  onClose,
  onEnvironment,
  onAttach,
  onDone,
}: {
  environments: Data[];
  onClose: () => void;
  onEnvironment: () => void;
  onAttach: () => void;
  onDone: (job: Data) => void;
}) {
  const [mode, setMode] = useState("bare"),
    [busy, setBusy] = useState(false),
    [progress, setProgress] = useState(""),
    [error, setError] = useState("");
  const [token] = useState(() =>
    Array.from(crypto.getRandomValues(new Uint8Array(24)), (v) =>
      v.toString(16).padStart(2, "0"),
    ).join(""),
  );
  const [limits, setLimits] = useState<Data>({}),
    [fileInfo, setFileInfo] = useState(""),
    [commandMode, setCommandMode] = useState("service");
  const abort = useRef<AbortController | null>(null);
  useEffect(() => {
    api("/upload-policy")
      .then((p) => setLimits(p.limits))
      .catch((e) => setError(String(e)));
    return () => abort.current?.abort();
  }, []);
  return (
    <Modal
      title="部署一个新实例"
      subtitle="选择运行环境与部署方式，剩下的交给控制台。"
      onClose={() => {
        if (!busy) onClose();
      }}
      wide
    >
      <div className="segmented">
        <button className="selected" type="button">
          部署新实例
        </button>
        <button type="button" disabled={busy} onClick={onAttach}>
          监测已有实例
        </button>
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          const form = new FormData(e.currentTarget);
          const values = Object.fromEntries(form);
          try {
            const file = form.get("artifact") as File;
            const kind = mode === "bare" ? "source" : "image";
            if (!file.size || (limits[kind] && file.size > limits[kind]))
              throw new Error(
                `部署包超限或为空，最大 ${limits[kind] / 1024 ** 2} MiB`,
              );
            if (mode === "k8s") {
              const manifestFile = form.get("manifest") as File;
              if (!manifestFile.size || manifestFile.size > limits.manifest)
                throw new Error("部署清单为空或超过大小限制");
            }
            const docker =
              mode === "docker"
                ? {
                    command_mode: commandMode,
                    entrypoint:
                      commandMode === "custom" && values.entrypoint !== ""
                        ? String(values.entrypoint)
                        : null,
                    command: JSON.parse(String(values.command || "[]")),
                    environment: JSON.parse(String(values.environment || "{}")),
                    extra_args: JSON.parse(String(values.extra_args || "[]")),
                    mounts: JSON.parse(String(values.mounts || "[]")),
                    container_port: Number(values.container_port || 32882),
                    max_restarts: Number(values.max_restarts ?? 3),
                  }
                : undefined;
            abort.current = new AbortController();
            setProgress("检查远程目录权限和剩余空间…");
            await post(
              "/environments/" +
                values.environment_id +
                "/storage?size_bytes=" +
                file.size +
                "&mode=" +
                mode,
              { remote_root: values.remote_root },
            );
            if (abort.current.signal.aborted) throw new Error("操作已取消");
            setProgress("浏览器 → Panel：开始上传…");
            const artifact = await upload(
              file,
              mode === "bare" ? "source" : "image",
              (p) =>
                setProgress(
                  p.sent >= p.total
                    ? `已发送 ${(p.total / 1024 ** 2).toFixed(1)} MiB，Panel 正在校验文件…`
                    : `浏览器 → Panel：${((p.sent / p.total) * 100).toFixed(0)}% · ${(p.sent / 1024 ** 2).toFixed(1)} / ${(p.total / 1024 ** 2).toFixed(1)} MiB · ${(p.bytesPerSecond / 1024 ** 2).toFixed(1)} MiB/s · ${p.seconds.toFixed(0)} 秒`,
                ),
              abort.current.signal,
            );
            let manifest;
            if (mode === "k8s") {
              setProgress("正在上传部署清单…");
              manifest = await upload(
                form.get("manifest") as File,
                "manifest",
                undefined,
                abort.current.signal,
              );
            }
            setProgress("正在创建部署任务…");
            const { artifact: _, manifest: __, ...fields } = values;
            const result = await post("/instances", {
              ...fields,
              mode,
              artifact_id: artifact.id,
              manifest_id: manifest?.id,
              port: Number(values.port || 32882),
              docker,
            });
            onDone(result.job);
          } catch (err) {
            setError(String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-section-label">
          <span>01</span>运行环境
        </div>
        <div className="form-grid">
          <label>
            实例名称
            <input
              name="name"
              placeholder="例如：agent-mock-staging"
              required
            />
          </label>
          <label>
            远程环境
            <div className="inline-field">
              <select
                name="environment_id"
                aria-label="远程环境"
                required
                defaultValue=""
              >
                <option value="" disabled>
                  选择 Linux 环境
                </option>
                {environments.map((env) => (
                  <option key={env.id} value={env.id}>
                    {env.name} · {env.host}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="icon bordered"
                title="新增环境"
                onClick={onEnvironment}
              >
                <Plus size={17} />
              </button>
            </div>
          </label>
        </div>
        <div className="form-section-label">
          <span>02</span>部署方式
        </div>
        <div className="deploy-modes">
          {[
            ["bare", "裸机部署", "conda / venv / Python", Terminal],
            ["docker", "Docker", "加载镜像并创建容器", Layers3],
            ["k8s", "Kubernetes", "指定节点 · 应用清单", Globe2],
          ].map(([id, title, sub, Icon]) => (
            <button
              type="button"
              key={String(id)}
              className={mode === id ? "selected" : ""}
              onClick={() => setMode(String(id))}
            >
              {React.createElement(Icon as typeof Server, { size: 21 })}
              <strong>{String(title)}</strong>
              <small>{String(sub)}</small>
              {mode === id && <CheckCircle2 size={16} className="mode-check" />}
            </button>
          ))}
        </div>
        <label>
          远程部署根目录
          <input
            name="remote_root"
            defaultValue="/opt/svc-panel"
            required
            placeholder="/home/deploy/mock-services"
          />
        </label>
        <p className="hint">
          在此目录下创建独立的实例 ID
          子目录。上传前检查写入权限和可用空间；卸载只清理该子目录。
        </p>
        <label>
          {mode === "bare" ? "源码 tar 包" : "镜像 tar 包"}
          <input
            type="file"
            name="artifact"
            accept=".tar,.gz,.tgz"
            required
            key={mode}
            onChange={(e) => {
              const file = e.target.files?.[0];
              setError("");
              if (file) {
                setFileInfo(
                  `${file.name} · ${(file.size / 1024 ** 2).toFixed(1)} MiB`,
                );
                const limit = limits[mode === "bare" ? "source" : "image"];
                if (limit && file.size > limit)
                  setError(
                    `所选部署包超限，最大 ${limit / 1024 ** 2} MiB，尚未上传`,
                  );
              }
            }}
          />
        </label>
        <p className="hint">
          {fileInfo || "选择文件后立即检查大小"} · 部署包上限{" "}
          {limits.image ? limits.image / 1024 ** 2 + " MiB" : "加载中"}
        </p>
        {mode === "bare" && (
          <p className="hint">
            目标机需具备 systemd、Python ≥
            3.11。创建独立环境后访问包源安装依赖；源码包须包含 pyproject.toml。
          </p>
        )}
        {mode === "docker" && (
          <>
            <label>
              镜像名称（单镜像包可自动识别）
              <input name="image_ref" placeholder="mock-service:latest" />
            </label>
            <label>
              启动方式
              <select
                aria-label="启动方式"
                value={commandMode}
                onChange={(e) => setCommandMode(e.target.value)}
              >
                <option value="service">Panel 标准 Mock 启动命令</option>
                <option value="image">使用镜像自身 ENTRYPOINT / CMD</option>
                <option value="custom">自定义 ENTRYPOINT / CMD</option>
              </select>
            </label>
            {commandMode === "custom" && (
              <>
                <label>
                  ENTRYPOINT（可选，可执行文件路径）
                  <input name="entrypoint" placeholder="python 或 /bin/sh" />
                </label>
                <label>
                  启动命令 / 参数（JSON 数组）
                  <textarea
                    name="command"
                    defaultValue={
                      '["-m", "uvicorn", "agent_mock_service.app_service:mock_service_app", "--host", "0.0.0.0", "--port", "32882"]'
                    }
                    rows={4}
                  />
                </label>
                <p className="hint">
                  按参数数组传递，不经过隐式 shell。需要 shell
                  语法时，ENTRYPOINT 填 /bin/sh，参数填 ["-c", "你的命令"]。
                </p>
              </>
            )}
            <div className="form-grid">
              <label>
                容器监听端口
                <input
                  name="container_port"
                  type="number"
                  defaultValue={32882}
                  min={1}
                  max={65535}
                  required
                />
              </label>
              <label>
                失败最多重启次数
                <input
                  name="max_restarts"
                  type="number"
                  defaultValue={3}
                  min={0}
                  max={10}
                  required
                />
              </label>
            </div>
            <p className="hint">
              0 表示不自动重启。非零采用 on-failure:N，避免错误命令无限重启。
            </p>
            <details>
              <summary>环境变量、挂载与更多 Docker 参数</summary>
              <label>
                环境变量（JSON 对象）
                <textarea name="environment" defaultValue="{}" rows={3} />
              </label>
              <label>
                额外挂载（JSON 数组）
                <textarea
                  name="mounts"
                  defaultValue="[]"
                  placeholder={
                    '[{"source":"/data/models","target":"/models","read_only":true}]'
                  }
                  rows={3}
                />
              </label>
              <p className="hint">
                额外挂载由用户管理，卸载不会删除这些目录。示例：[
                {`{"source":"/data/models","target":"/models","read_only":true}`}{" "}
                ]
              </p>
              <label>
                Docker 参数（JSON 数组）
                <textarea
                  name="extra_args"
                  defaultValue="[]"
                  placeholder={'["--cpus", "2", "--memory", "2g"]'}
                  rows={3}
                />
              </label>
              <p className="hint">
                支持资源限制、用户、工作目录、日志选项、DNS、capabilities
                等；名称、主端口、网络和重启策略不允许从这里覆盖。
              </p>
            </details>
            <div className="disabled-feature">
              <span>自定义 Docker bridge</span>
              <span>预留 · 暂不支持</span>
            </div>
          </>
        )}
        {mode === "k8s" && (
          <>
            <div className="segmented">
              <button type="button" className="selected">
                单节点 / 指定节点
              </button>
              <button type="button" disabled>
                多节点 · 暂不支持
              </button>
            </div>
            <label>
              deployment.yml
              <input type="file" name="manifest" accept=".yml,.yaml" required />
            </label>
            <div className="form-grid">
              <label>
                节点名称
                <input name="node" required placeholder="worker-01" />
              </label>
              <label>
                节点镜像加载方式
                <select name="image_loader">
                  <option value="containerd">containerd · ctr</option>
                  <option value="k3s">K3s · k3s ctr</option>
                  <option value="docker">Docker</option>
                </select>
              </label>
              <label>
                Namespace
                <input name="namespace" defaultValue="default" required />
              </label>
              <label>
                Deployment 名称
                <input name="deployment" required />
              </label>
            </div>
            <p className="hint">
              SSH 环境须为指定节点，且具备可用
              kubectl。清单需已配置本地镜像、节点约束、管理 token、Service
              与持久化目录。Panel 保留业务配置，添加资源归属标签后 apply。
            </p>
          </>
        )}
        <div className="form-section-label">
          <span>03</span>服务连接
        </div>
        <div className="form-grid">
          {mode !== "k8s" && (
            <label>
              暴露端口
              <input
                name="port"
                type="number"
                defaultValue="32882"
                min="1"
                max="65535"
                required
              />
            </label>
          )}
          <label>
            访问方式
            <select name="access">
              <option value="direct">Panel 直接访问</option>
              <option value="ssh">通过 SSH 隧道访问 HTTP</option>
            </select>
          </label>
        </div>
        <label>
          服务访问地址
          <input
            name="base_url"
            type="url"
            required
            placeholder={
              mode === "k8s" ? "http://节点IP:NodePort" : "http://主机IP:32882"
            }
          />
        </label>
        <p className="hint">
          隧道模式填写从 SSH 主机可访问的地址，例如 http://127.0.0.1:32882。K8s
          地址由 Service 配置决定。
        </p>
        <label>
          Mock 管理令牌
          <input
            name="admin_token"
            defaultValue={token}
            minLength={16}
            required
            type="text"
            spellCheck={false}
          />
        </label>
        {mode === "k8s" && (
          <p className="hint">请填写与清单中 MOCK_ADMIN_TOKEN 相同的值。</p>
        )}
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions">
          <span className="hint">
            {progress && busy ? progress : "部署进度会在后台持续记录"}
          </span>
          {busy && (
            <button
              className="secondary"
              type="button"
              onClick={() => abort.current?.abort()}
            >
              取消上传
            </button>
          )}
          <button className="primary" disabled={busy || !environments.length}>
            {busy ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <Rocket size={16} />
            )}
            开始部署
          </button>
        </div>
      </form>
    </Modal>
  );
}
