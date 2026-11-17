import { useState } from "react";
import { Loader2, ShieldCheck } from "lucide-react";
import { post } from "../api";
import { Modal } from "./common";
export function EnvironmentDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [type, setType] = useState("password");
  return (
    <Modal
      title="新增远程环境"
      subtitle="连接可信的 Linux 主机"
      onClose={onClose}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          const values = Object.fromEntries(new FormData(e.currentTarget));
          try {
            await post("/environments", {
              ...values,
              port: Number(values.port),
              os: "linux",
            });
            onDone();
          } catch (err) {
            setError(String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="segmented">
          <button type="button" className="selected">
            Linux
          </button>
          <button type="button" disabled>
            Windows · 暂不支持
          </button>
          <button type="button" disabled>
            其他系统
          </button>
        </div>
        <label>
          环境名称
          <input name="name" required placeholder="例如：测试环境 · 上海" />
        </label>
        <div className="form-grid">
          <label>
            SSH 主机
            <input name="host" required placeholder="192.168.1.20" />
          </label>
          <label>
            SSH 端口
            <input
              name="port"
              type="number"
              min="1"
              max="65535"
              defaultValue="22"
              required
            />
          </label>
        </div>
        <label>
          SSH 用户
          <input name="username" required placeholder="deploy" />
        </label>
        <label>
          认证方式
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="password">密码</option>
            <option value="private_key">私钥</option>
          </select>
        </label>
        {type === "password" ? (
          <label>
            密码
            <input
              name="password"
              type="password"
              required
              autoComplete="new-password"
            />
          </label>
        ) : (
          <>
            <label>
              私钥
              <textarea
                name="private_key"
                rows={5}
                required
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              />
            </label>
            <label>
              私钥密码（如有）
              <input
                name="password"
                type="password"
                autoComplete="new-password"
              />
            </label>
          </>
        )}
        <label>
          可信主机 SHA256 指纹（可选）
          <input
            name="host_key_sha256"
            placeholder="留空：首次成功登录后自动记住指纹"
          />
        </label>
        <p className="hint">
          可填写已知指纹；留空时自动记录首次成功登录的主机指纹，后续变化会报错。密码和私钥加密保存。
        </p>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onClose}>
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy ? (
              <Loader2 className="spin" size={16} />
            ) : (
              <ShieldCheck size={16} />
            )}
            保存环境
          </button>
        </div>
      </form>
    </Modal>
  );
}
