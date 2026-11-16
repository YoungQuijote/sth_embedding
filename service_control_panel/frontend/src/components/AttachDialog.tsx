import { useState } from "react";
import { Data, post } from "../api";
import { Modal } from "./common";

export function AttachDialog({
  environments,
  onClose,
  onDone,
}: {
  environments: Data[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [access, setAccess] = useState("direct");
  return (
    <Modal
      title="监测已有实例"
      subtitle="接入正在运行的 Mock 服务，无需重新部署。"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          const values = Object.fromEntries(new FormData(e.currentTarget));
          try {
            await post("/instances/attach", {
              ...values,
              environment_id: values.environment_id || null,
            });
            onDone();
          } catch (err) {
            setError(String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          实例名称
          <input name="name" required maxLength={100} />
        </label>
        <label>
          部署方式
          <select name="mode">
            <option value="docker">Docker</option>
            <option value="bare">裸机</option>
            <option value="k8s">Kubernetes</option>
          </select>
        </label>
        <label>
          服务访问地址
          <input
            name="base_url"
            type="url"
            placeholder="http://192.168.1.10:32882"
            required
          />
        </label>
        <label>
          Mock 管理令牌
          <input name="admin_token" type="password" required />
        </label>
        <label>
          访问方式
          <select
            name="access"
            value={access}
            onChange={(e) => setAccess(e.target.value)}
          >
            <option value="direct">Panel 直接访问</option>
            <option value="ssh">通过 SSH 隧道访问 HTTP</option>
          </select>
        </label>
        {access === "ssh" && (
          <label>
            远程环境
            <select name="environment_id" required defaultValue="">
              <option value="" disabled>
                选择 Linux 环境
              </option>
              {environments.map((env) => (
                <option key={env.id} value={env.id}>
                  {env.name} · {env.host}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          镜像 / 源码版本（备注）
          <input name="version" defaultValue="外部部署" />
        </label>
        <p className="hint">
          连接时检查就绪状态、管理令牌和 panel.v1
          接口。接入后可健康检查、试调接口、查看及回流日志与数据库。外部实例支持移除监测；重启和卸载需由原部署方操作。
        </p>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions">
          <span className="hint">
            {busy ? "正在验证服务连接…" : "直接访问无需 SSH 环境"}
          </span>
          <button className="primary" disabled={busy}>
            验证并接入
          </button>
        </div>
      </form>
    </Modal>
  );
}
