import { useEffect, useRef, useState } from "react";
import { ArrowRight, Loader2, X } from "lucide-react";
import { api, authHeaders, Data, resolveSchema, schemaExample } from "../api";
export function ConsoleDrawer({
  instance,
  endpoint,
  onClose,
}: {
  instance: Data;
  endpoint: Data;
  onClose: () => void;
}) {
  const [document, setDocument] = useState<Data>({}),
    [schema, setSchema] = useState<Data>({}),
    [body, setBody] = useState("{}"),
    [headers, setHeaders] = useState("{}"),
    [query, setQuery] = useState("{}"),
    [path, setPath] = useState("{}"),
    [result, setResult] = useState(""),
    [status, setStatus] = useState<Data | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [editor, setEditor] = useState("form");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    let alive = true;
    api("/instances/" + instance.id + "/openapi")
      .then((doc) => {
        if (!alive) return;
        setDocument(doc);
        const item = doc.paths?.[endpoint.path] || {};
        const op = item[endpoint.method.toLowerCase()] || {};
        const content = op.requestBody?.content?.["application/json"] || {};
        const s = resolveSchema(content.schema || {}, doc);
        setSchema(s);
        setBody(
          JSON.stringify(content.example ?? schemaExample(s, doc), null, 2),
        );
        const parameters = [
          ...(item.parameters || []),
          ...(op.parameters || []),
        ].map((p) => resolveSchema(p, doc));
        for (const kind of ["path", "query"]) {
          const values = Object.fromEntries(
            parameters
              .filter((p) => p.in === kind)
              .map((p) => [
                p.name,
                String(p.example ?? schemaExample(p.schema || {}, doc)),
              ]),
          );
          (kind === "path" ? setPath : setQuery)(
            JSON.stringify(values, null, 2),
          );
        }
        if (!s.properties) setEditor("json");
      })
      .catch((err) => {
        setError("协议未读取成功，可使用 JSON 编辑器：" + String(err));
        setEditor("json");
      });
    return () => {
      alive = false;
      controller.current?.abort();
    };
  }, [instance.id, endpoint.id]);
  function field(key: string, value: any) {
    try {
      const parsed = JSON.parse(body);
      setBody(JSON.stringify({ ...parsed, [key]: value }, null, 2));
    } catch {
      setEditor("json");
    }
  }
  let parsed: Data = {};
  try {
    parsed = JSON.parse(body);
  } catch {
    /* Keep raw editing available */
  }
  async function send() {
    setBusy(true);
    setError("");
    setResult("");
    setStatus(null);
    const started = performance.now();
    controller.current = new AbortController();
    try {
      const payload = {
        endpoint_id: endpoint.id,
        body: body.trim() ? JSON.parse(body) : null,
        headers: JSON.parse(headers),
        query: JSON.parse(query),
        path_params: JSON.parse(path),
      };
      const response = await fetch(
        "/api/instances/" + instance.id + "/console",
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: controller.current.signal,
        },
      );
      setStatus({
        code: response.status,
        requestId: response.headers.get("x-request-id"),
        duration: 0,
      });
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          setResult((old) => old + decoder.decode(value, { stream: true }));
        }
        setResult((old) => old + decoder.decode());
      }
      setStatus({
        code: response.status,
        requestId: response.headers.get("x-request-id"),
        duration: Math.round(performance.now() - started),
      });
    } catch (err) {
      setError(
        err instanceof DOMException && err.name === "AbortError"
          ? "请求已停止"
          : String(err),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="drawer-overlay">
      <aside
        className="drawer console-drawer"
        role="dialog"
        aria-label="接口试调"
        aria-modal="true"
      >
        <div className="dialog-heading">
          <div>
            <div className="eyebrow">{instance.name}</div>
            <h2>
              接口试调 <span className="tag">CONSOLE</span>
            </h2>
          </div>
          <button className="icon" aria-label="关闭" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <div className="drawer-body">
          <div className="request-address">
            <span className={"method " + endpoint.method.toLowerCase()}>
              {endpoint.method}
            </span>
            <code>
              {instance.base_url}
              {endpoint.path}
            </code>
          </div>
          <p className="hint">
            {endpoint.summary} · URL
            与方法来自实例接口清单。试调请求从业务监控中排除。
          </p>
          <details>
            <summary>请求头与参数</summary>
            <label>
              请求头（JSON，可覆盖 Authorization）
              <textarea
                value={headers}
                onChange={(e) => setHeaders(e.target.value)}
                rows={3}
              />
            </label>
            <div className="form-grid">
              <label>
                路径参数（JSON）
                <textarea
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  rows={3}
                />
              </label>
              <label>
                查询参数（JSON）
                <textarea
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  rows={3}
                />
              </label>
            </div>
          </details>
          <div className="section-heading">
            <h3>请求体</h3>
            <div className="segmented compact">
              <button
                className={editor === "form" ? "selected" : ""}
                disabled={!schema.properties}
                onClick={() => setEditor("form")}
              >
                表单
              </button>
              <button
                className={editor === "json" ? "selected" : ""}
                onClick={() => setEditor("json")}
              >
                JSON
              </button>
            </div>
          </div>
          {editor === "json" ? (
            <textarea
              className="code-editor"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={10}
            />
          ) : (
            <div className="schema-fields">
              {Object.entries(schema.properties || {}).map(([key, raw]) => {
                const s = resolveSchema(raw as Data, document);
                return (
                  <label key={key}>
                    {key}{" "}
                    {schema.required?.includes(key) && (
                      <span className="required">*</span>
                    )}
                    <small>{s.description || s.title || s.type}</small>
                    {s.enum ? (
                      <select
                        value={parsed[key] ?? ""}
                        onChange={(e) =>
                          field(
                            key,
                            s.enum.find(
                              (v: any) => String(v) === e.target.value,
                            ),
                          )
                        }
                      >
                        {s.enum.map((v: any) => (
                          <option key={String(v)}>{String(v)}</option>
                        ))}
                      </select>
                    ) : s.type === "boolean" ? (
                      <select
                        value={String(parsed[key] ?? false)}
                        onChange={(e) => field(key, e.target.value === "true")}
                      >
                        <option>true</option>
                        <option>false</option>
                      </select>
                    ) : ["object", "array"].includes(s.type) ? (
                      <textarea
                        value={JSON.stringify(
                          parsed[key] ?? (s.type === "array" ? [] : {}),
                          null,
                          2,
                        )}
                        onChange={(e) => {
                          try {
                            field(key, JSON.parse(e.target.value));
                          } catch {
                            setEditor("json");
                          }
                        }}
                      />
                    ) : (
                      <input
                        type={
                          ["integer", "number"].includes(s.type)
                            ? "number"
                            : "text"
                        }
                        min={s.minimum}
                        max={s.maximum}
                        value={parsed[key] ?? ""}
                        onChange={(e) =>
                          field(
                            key,
                            ["integer", "number"].includes(s.type)
                              ? Number(e.target.value)
                              : e.target.value,
                          )
                        }
                      />
                    )}
                  </label>
                );
              })}
            </div>
          )}
          {error && <div className="form-error">{error}</div>}
          <div className="dialog-actions">
            {busy ? (
              <button
                className="secondary"
                onClick={() => controller.current?.abort()}
              >
                停止请求
              </button>
            ) : (
              <span className="hint">响应最多 2 MiB / 120 秒</span>
            )}
            <button className="primary" onClick={send} disabled={busy}>
              {busy ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <ArrowRight size={16} />
              )}
              发送请求
            </button>
          </div>
          <section className="response-section">
            <div className="section-heading">
              <h3>响应</h3>
              {status && (
                <span>
                  <b className={status.code < 400 ? "green-text" : "amber"}>
                    {status.code}
                  </b>{" "}
                  · {status.duration} ms
                </span>
              )}
            </div>
            {status?.requestId && (
              <small className="mono">Request ID: {status.requestId}</small>
            )}
            <pre>
              {result || "发送请求后，在这里查看响应。SSE 将逐段展示。"}
            </pre>
          </section>
        </div>
      </aside>
    </div>
  );
}
