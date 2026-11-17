import { useEffect, useRef, useState } from "react";
import {
  Lightbulb,
  Plus,
  RefreshCw,
  X,
  ImagePlus,
  Pencil,
  Trash2,
} from "lucide-react";
import { api, authHeaders, Data, date, post } from "../api";
import { Empty, Modal } from "./common";

const statuses: Record<string, string> = {
  pending: "待处理",
  developing: "开发中",
  acceptance: "待验收",
  closed: "已闭环",
};
const charCount = (text: string) => Array.from(text).length;
const imageLimit = 5 * 1024 ** 2;
const pageSize = 50;

function FeedbackPicture({ image, parent }: { image: Data; parent?: string }) {
  const [url, setUrl] = useState(""),
    [error, setError] = useState(false);
  useEffect(() => {
    if (image.data_base64) {
      setUrl(`data:${image.mime_type};base64,${image.data_base64}`);
      return;
    }
    const controller = new AbortController();
    let objectUrl = "";
    setError(false);
    setUrl("");
    fetch(`/api/feedback/${parent}/images/${image.id}`, {
      headers: authHeaders(),
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401)
          window.dispatchEvent(new Event("panel-unauthorized"));
        if (!response.ok) throw new Error("image unavailable");
        return response.blob();
      })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [image.id, image.data_base64, parent]);
  if (error) return <span className="hint">配图无法加载，请重新打开详情</span>;
  return url ? (
    <a href={url} target="_blank" rel="noreferrer" title="打开原图">
      <img src={url} alt={image.filename || "需求配图"} />
    </a>
  ) : (
    <span className="hint">配图加载中…</span>
  );
}

function FeedbackDetail({
  identifier,
  onClose,
  onChanged,
}: {
  identifier: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [entry, setEntry] = useState<Data | null>(
    identifier === "new"
      ? {
          author: "",
          summary: "",
          description: "",
          status: "pending",
          images: [],
        }
      : null,
  );
  const [draft, setDraft] = useState<Data>({
    author: "",
    summary: "",
    description: "",
    status: "pending",
    images: [],
  });
  const [editing, setEditing] = useState(identifier === "new"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [confirmDelete, setConfirmDelete] = useState(false),
    [conflict, setConflict] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (identifier === "new") return;
    const controller = new AbortController();
    api(`/feedback/${identifier}`, { signal: controller.signal })
      .then(setEntry)
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e));
      });
    return () => controller.abort();
  }, [identifier]);
  const close = () => {
    if (!busy) onClose();
  };
  async function reload() {
    if (!entry?.id) return;
    setBusy(true);
    setError("");
    try {
      const latest = await api(`/feedback/${entry.id}`);
      setEntry(latest);
      setDraft(latest);
      setEditing(false);
      setConflict(false);
      setConfirmDelete(false);
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function addImages(files: FileList | null) {
    if (!files?.length) return;
    setError("");
    if (draft.images.length + files.length > 5) {
      setError("最多添加 5 张配图，请先移除不需要的图片");
      return;
    }
    setBusy(true);
    try {
      const images: Data[] = [];
      for (const file of Array.from(files)) {
        if (
          !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(
            file.type,
          )
        )
          throw new Error("配图仅支持 PNG、JPEG、WebP、GIF");
        if (!file.size || file.size > imageLimit)
          throw new Error(`${file.name} 为空或超过 5 MiB`);
        const encoded = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
          reader.onerror = () => reject(new Error("读取配图失败"));
          reader.readAsDataURL(file);
        });
        images.push({
          filename: Array.from(file.name).slice(0, 200).join(""),
          mime_type: file.type,
          data_base64: encoded,
          key: crypto.randomUUID(),
        });
      }
      if (mounted.current)
        setDraft((previous) => ({
          ...previous,
          images: [...previous.images, ...images],
        }));
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  const editingExisting = !!entry?.id;
  return (
    <Modal
      title={
        editing
          ? editingExisting
            ? "编辑需求与反馈"
            : "新增需求与反馈"
          : "需求与反馈详情"
      }
      subtitle={
        entry?.id
          ? `录入于 ${date(entry.created_at)}`
          : "记录想法、问题与改进建议。"
      }
      onClose={close}
      wide
    >
      {!entry && !error && <div className="loading">正在读取详情…</div>}
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {conflict && (
        <button className="secondary" disabled={busy} onClick={reload}>
          重新加载最新详情
        </button>
      )}
      {entry &&
        (editing ? (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setError("");
              setConflict(false);
              if (!draft.summary.trim()) {
                setError("请填写需求简述");
                return;
              }
              if (
                charCount(draft.author) > 16 ||
                charCount(draft.summary) > 256 ||
                charCount(draft.description) > 20000
              ) {
                setError(
                  "提出人最多 16 字，简述最多 256 字，详细描述最多 20,000 字",
                );
                return;
              }
              setBusy(true);
              const payload = {
                author: draft.author,
                summary: draft.summary,
                description: draft.description,
                status: draft.status,
                images: draft.images.map((image: Data) =>
                  image.id
                    ? { id: image.id }
                    : {
                        filename: image.filename,
                        data_base64: image.data_base64,
                      },
                ),
              };
              try {
                const saved = editingExisting
                  ? await api(`/feedback/${entry.id}`, {
                      method: "PUT",
                      body: JSON.stringify({
                        ...payload,
                        revision: entry.revision,
                      }),
                    })
                  : await post("/feedback", payload);
                setEntry(saved);
                setEditing(false);
                onChanged();
              } catch (e) {
                const message = String(e);
                setError(message);
                setConflict(message.includes("其他人修改"));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label>
              需求简述（必填）
              <textarea
                value={draft.summary}
                onChange={(e) =>
                  setDraft({ ...draft, summary: e.target.value })
                }
                rows={3}
                required
                disabled={busy}
              />
              <small className="hint">
                {charCount(draft.summary)} / 256 字
              </small>
            </label>
            <div className="form-grid">
              <label>
                提出人（选填）
                <input
                  value={draft.author}
                  onChange={(e) =>
                    setDraft({ ...draft, author: e.target.value })
                  }
                  disabled={busy}
                />
                <small className="hint">
                  {charCount(draft.author)} / 16 字
                </small>
              </label>
              <label>
                处理状态
                <select
                  aria-label="处理状态"
                  value={draft.status}
                  onChange={(e) =>
                    setDraft({ ...draft, status: e.target.value })
                  }
                  disabled={busy}
                >
                  {Object.entries(statuses).map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label>
              详细描述（选填）
              <textarea
                value={draft.description}
                onChange={(e) =>
                  setDraft({ ...draft, description: e.target.value })
                }
                rows={8}
                disabled={busy}
              />
              <small className="hint">
                {charCount(draft.description)} / 20,000 字
              </small>
            </label>
            <label>
              添加配图（选填）
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                disabled={busy || draft.images.length >= 5}
                onChange={(e) => {
                  const files = e.target.files;
                  void addImages(files);
                  e.target.value = "";
                }}
              />
            </label>
            <p className="hint">
              最多 5 张，每张 ≤5 MiB、≤2000 万像素。PNG / JPEG / WebP /
              GIF，配图与内容一起保存。
            </p>
            <div className="feedback-gallery">
              {draft.images.map((image: Data) => (
                <figure key={image.id || image.key}>
                  <FeedbackPicture image={image} parent={entry.id} />
                  <figcaption>
                    {image.filename}
                    <button
                      type="button"
                      className="icon"
                      title={`移除配图 ${image.filename}`}
                      disabled={busy}
                      onClick={() =>
                        setDraft({
                          ...draft,
                          images: draft.images.filter(
                            (other: Data) => other !== image,
                          ),
                        })
                      }
                    >
                      <X size={15} />
                    </button>
                  </figcaption>
                </figure>
              ))}
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => {
                  if (editingExisting) {
                    setEditing(false);
                    setError("");
                    setConflict(false);
                  } else onClose();
                }}
              >
                取消
              </button>
              <button className="primary" disabled={busy || conflict}>
                {busy ? "正在保存…" : "保存需求与反馈"}
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="feedback-detail-heading">
              <span className={`feedback-status ${entry.status}`}>
                {statuses[entry.status]}
              </span>
              <h3>{entry.summary}</h3>
            </div>
            <dl className="feedback-meta">
              <div>
                <dt>提出人</dt>
                <dd>{entry.author || "未填写"}</dd>
              </div>
              <div>
                <dt>录入时间</dt>
                <dd>{date(entry.created_at)}</dd>
              </div>
              <div>
                <dt>状态更新时间</dt>
                <dd>{date(entry.status_updated_at)}</dd>
              </div>
            </dl>
            <h3 className="feedback-section-title">详细描述</h3>
            <p className="feedback-description">
              {entry.description || "暂未填写详细描述"}
            </p>
            {!!entry.images.length && (
              <>
                <h3 className="feedback-section-title">
                  配图 · {entry.images.length}
                </h3>
                <div className="feedback-gallery">
                  {entry.images.map((image: Data) => (
                    <figure key={image.id}>
                      <FeedbackPicture image={image} parent={entry.id} />
                      <figcaption>{image.filename}</figcaption>
                    </figure>
                  ))}
                </div>
              </>
            )}
            {confirmDelete ? (
              <div className="feedback-delete">
                <p>
                  确定删除这条需求与反馈？文字和全部配图会一起删除，无法恢复。
                </p>
                <div className="dialog-actions">
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => setConfirmDelete(false)}
                  >
                    取消删除
                  </button>
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={async () => {
                      setBusy(true);
                      setError("");
                      try {
                        await api(
                          `/feedback/${entry.id}?revision=${entry.revision}`,
                          { method: "DELETE" },
                        );
                        onChanged();
                        onClose();
                      } catch (e) {
                        const message = String(e);
                        setError(message);
                        setConflict(message.includes("其他人修改"));
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    确认删除
                  </button>
                </div>
              </div>
            ) : (
              <div className="dialog-actions">
                <button
                  className="subtle"
                  onClick={() => setConfirmDelete(true)}
                >
                  <Trash2 size={15} />
                  删除
                </button>
                <button
                  className="primary"
                  onClick={() => {
                    setDraft({ ...entry, images: [...entry.images] });
                    setError("");
                    setEditing(true);
                  }}
                >
                  <Pencil size={15} />
                  编辑
                </button>
              </div>
            )}
          </>
        ))}
    </Modal>
  );
}

export function FeedbackDrawer({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState(""),
    [sort, setSort] = useState("created_desc"),
    [offset, setOffset] = useState(0),
    [version, setVersion] = useState(0);
  const [data, setData] = useState<Data>({ items: [], total: 0 }),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [selected, setSelected] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null),
    closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    closeButton.current?.focus();
    return () => previous?.focus();
  }, []);
  useEffect(() => {
    if (selected) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [selected, onClose]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      sort,
      offset: String(offset),
      limit: String(pageSize),
    });
    if (status) params.set("status", status);
    api(`/feedback?${params}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) {
          setData(result);
          if (!result.items.length && offset > 0)
            setOffset(Math.max(0, offset - pageSize));
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [status, sort, offset, version]);
  const open = (identifier: string, button: HTMLButtonElement) => {
    trigger.current = button;
    setSelected(identifier);
  };
  return (
    <>
      <div
        className="drawer-overlay feedback-overlay"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget && !selected) onClose();
        }}
      >
        <aside
          className="drawer feedback-drawer"
          role="dialog"
          aria-modal={!selected}
          aria-label="需求与反馈"
          inert={selected !== null}
        >
          <div className="dialog-heading">
            <div>
              <div className="eyebrow">IDEAS & FEEDBACK</div>
              <h2>
                <Lightbulb size={20} />
                需求与反馈
              </h2>
              <p>记录每一个建议，跟进每一次改进。</p>
            </div>
            <button
              ref={closeButton}
              className="icon"
              aria-label="关闭需求与反馈"
              onClick={onClose}
            >
              <X size={20} />
            </button>
          </div>
          <div className="drawer-body">
            <div className="feedback-tools">
              <span className="muted">
                {loading ? "正在加载…" : `${data.total} 条记录`}
              </span>
              <button
                className="icon"
                title="刷新需求清单"
                onClick={() => setVersion((v) => v + 1)}
                disabled={loading}
              >
                <RefreshCw size={16} />
              </button>
              <button
                className="primary"
                onClick={(e) => open("new", e.currentTarget)}
              >
                <Plus size={16} />
                新增需求 / 反馈
              </button>
            </div>
            <div className="feedback-filters">
              <label>
                筛选状态
                <select
                  aria-label="筛选状态"
                  value={status}
                  onChange={(e) => {
                    setStatus(e.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">全部状态</option>
                  {Object.entries(statuses).map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                排序方式
                <select
                  aria-label="排序方式"
                  value={sort}
                  onChange={(e) => {
                    setSort(e.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="created_desc">提出时间 · 从新到旧</option>
                  <option value="created_asc">提出时间 · 从旧到新</option>
                  <option value="status_desc">状态变更时间 · 从新到旧</option>
                  <option value="status_asc">状态变更时间 · 从旧到新</option>
                </select>
              </label>
            </div>
            {error && (
              <div className="form-error" role="alert">
                {error}
              </div>
            )}
            {!loading && !error && !data.items.length ? (
              <Empty
                icon={Lightbulb}
                title={status ? "此状态下暂无记录" : "还没有需求或反馈"}
                text="点击新增，记录你的想法或遇到的问题。"
              />
            ) : (
              <div className="feedback-list" aria-busy={loading}>
                {!loading &&
                  data.items.map((item: Data) => (
                    <button
                      key={item.id}
                      className="feedback-card"
                      onClick={(e) => open(item.id, e.currentTarget)}
                    >
                      <div className="feedback-card-top">
                        <span className={`feedback-status ${item.status}`}>
                          {statuses[item.status]}
                        </span>
                        {item.image_count > 0 && (
                          <small>
                            <ImagePlus size={13} />
                            {item.image_count} 张配图
                          </small>
                        )}
                      </div>
                      <h3>{item.summary}</h3>
                      <p>提出人：{item.author || "未填写"}</p>
                      <div className="feedback-card-times">
                        <span>录入：{date(item.created_at)}</span>
                        <span>状态更新：{date(item.status_updated_at)}</span>
                      </div>
                    </button>
                  ))}
              </div>
            )}
            {data.total > pageSize && (
              <div className="pagination">
                <button
                  className="secondary"
                  disabled={loading || offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - pageSize))}
                >
                  上一页
                </button>
                <span>
                  {Math.floor(offset / pageSize) + 1} /{" "}
                  {Math.ceil(data.total / pageSize)}
                </span>
                <button
                  className="secondary"
                  disabled={loading || offset + pageSize >= data.total}
                  onClick={() => setOffset(offset + pageSize)}
                >
                  下一页
                </button>
              </div>
            )}
          </div>
        </aside>
      </div>
      {selected && (
        <FeedbackDetail
          key={selected}
          identifier={selected}
          onClose={() => {
            setSelected(null);
            setTimeout(() => trigger.current?.focus(), 0);
          }}
          onChanged={() => setVersion((v) => v + 1)}
        />
      )}
    </>
  );
}
