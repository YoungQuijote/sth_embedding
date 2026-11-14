import React, { useState } from "react";
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
import { post, upload, Data } from "../api";
import { Modal } from "./common";
export function DeployDialog({
  environments,
  onClose,
  onEnvironment,
  onDone,
}: {
  environments: Data[];
  onClose: () => void;
  onEnvironment: () => void;
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
  return (
    <Modal
      title="部署一个新实例"
      subtitle="选择运行环境与部署方式，剩下的交给控制台。"
      onClose={() => {
        if (!busy) onClose();
      }}
      wide
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          const form = new FormData(e.currentTarget);
          const values = Object.fromEntries(form);
          try {
            setProgress("正在上传部署文件…");
            const artifact = await upload(
              form.get("artifact") as File,
              mode === "bare" ? "source" : "image",
            );
            let manifest;
            if (mode === "k8s") {
              setProgress("正在上传部署清单…");
              manifest = await upload(form.get("manifest") as File, "manifest");
            }
            setProgress("正在创建部署任务…");
            const { artifact: _, manifest: __, ...fields } = values;
            const result = await post("/instances", {
              ...fields,
              mode,
              artifact_id: artifact.id,
              manifest_id: manifest?.id,
              port: Number(values.port || 32882),
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
          {mode === "bare" ? "源码 tar 包" : "镜像 tar 包"}
          <input
            type="file"
            name="artifact"
            accept=".tar,.gz,.tgz"
            required
            key={mode}
          />
        </label>
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
              与持久化目录。Panel 按原清单 apply。
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
