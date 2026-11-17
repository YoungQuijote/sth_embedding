import { useState } from "react";
import { post } from "../api";
import { Modal } from "./common";

export function PasswordDialog({
  onClose,
  onChanged,
}: {
  onClose: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal
      title="修改登录密码"
      subtitle="适用于当前管理员的登录密码或访问令牌。"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError("");
          const values = Object.fromEntries(new FormData(e.currentTarget));
          if (values.new_password !== values.confirm_password) {
            setError("两次输入的新密码不一致");
            return;
          }
          setBusy(true);
          try {
            await post("/account/password", values);
            onChanged();
          } catch (err) {
            setError(String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          当前密码 / 访问令牌
          <input
            name="current_password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={4096}
          />
        </label>
        <label>
          新登录密码
          <input
            name="new_password"
            type="password"
            autoComplete="new-password"
            required
            minLength={16}
            maxLength={256}
            pattern="[!-~]+"
          />
        </label>
        <label>
          再次输入新密码
          <input
            name="confirm_password"
            type="password"
            autoComplete="new-password"
            required
            maxLength={256}
          />
        </label>
        <p className="hint">
          使用 16–256
          位英文字母、数字或符号，不含空格。保存后所有使用旧凭据的页面需要重新登录，未完成的后台任务继续执行。
        </p>
        <p className="hint">
          修改会持久保存，Panel 重启后仍有效；原 admin.token 和
          PANEL_ADMIN_TOKEN 不再用于登录。
        </p>
        {error && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}
        <div className="dialog-actions">
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "正在保存…" : "保存新密码"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
