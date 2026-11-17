import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  CheckCircle2,
  ChevronRight,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { api, download, Data, date, count } from "../api";
import { jobNames, statusNames, enc } from "../labels";
import { Empty, TrafficChart } from "./common";
export function DataDrawer({
  target,
  onClose,
  onError,
}: {
  target: Data;
  onClose: () => void;
  onError: (m: string) => void;
}) {
  const [data, setData] = useState<any>(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [table, setTable] = useState(""),
    [rowData, setRowData] = useState<Data | null>(null),
    [detail, setDetail] = useState<any>(null),
    [level, setLevel] = useState("");
  const serial = useRef(0);
  const root = "/instances/" + target.instance?.id;
  const titles: Data = {
    logs: "运行日志",
    database: "数据库 · 只读",
    requests: "非 200 请求",
    metrics: "接口统计",
    job: "任务详情",
    text: target.title,
  };
  async function load(cursor?: string) {
    const id = ++serial.current;
    setLoading(true);
    setError("");
    try {
      let result;
      const params = new URLSearchParams();
      if (cursor) params.set("cursor", cursor);
      if (level) params.set("level", level);
      if (target.endpoint) {
        params.set("endpoint", target.endpoint.path);
        params.set("method", target.endpoint.method);
      }
      if (target.type === "requests" && target.minutes)
        params.set(
          "since",
          new Date(Date.now() - target.minutes * 60000).toISOString(),
        );
      if (target.type === "job") result = await api("/jobs/" + target.jobId);
      else if (target.type === "text") result = target.data;
      else if (target.type === "metrics")
        result = await api(
          "/monitoring?instance_id=" +
            target.instance.id +
            "&minutes=" +
            target.minutes +
            "&endpoint=" +
            enc(target.endpoint.path) +
            "&method=" +
            enc(target.endpoint.method),
        );
      else
        result = await api(
          root +
            (target.type === "database"
              ? "/database/tables"
              : "/" + target.type) +
            "?" +
            params,
        );
      if (id === serial.current) setData(result);
    } catch (err) {
      if (id === serial.current) setError(String(err));
    } finally {
      if (id === serial.current) setLoading(false);
    }
  }
  useEffect(() => {
    setData(null);
    setDetail(null);
    setTable("");
    setRowData(null);
    load();
    const timer =
      target.type === "job" ? setInterval(() => load(), 2000) : null;
    return () => {
      serial.current++;
      if (timer) clearInterval(timer);
    };
  }, [target, level]);
  async function tableRows(name: string, offset = 0) {
    setTable(name);
    setLoading(true);
    try {
      setRowData(
        await api(
          root + "/database/tables/" + enc(name) + "/rows?offset=" + offset,
        ),
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }
  async function requestDetail(requestId: string) {
    setLoading(true);
    try {
      const item = await api(root + "/requests/" + enc(requestId));
      setDetail(item);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }
  return (
    <div className="drawer-overlay">
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={titles[target.type]}
      >
        <div className="dialog-heading">
          <div>
            <div className="eyebrow">
              {target.instance?.name || "OPERATIONS"}
            </div>
            <h2>{titles[target.type]}</h2>
          </div>
          <button className="icon" aria-label="关闭" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <div className="drawer-body">
          <div className="drawer-toolbar">
            {target.type === "logs" && (
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                <option value="">全部级别</option>
                {["DEBUG", "INFO", "WARNING", "ERROR"].map((v) => (
                  <option key={v}>{v}</option>
                ))}
              </select>
            )}
            <button
              className="secondary"
              onClick={() => load()}
              disabled={loading}
            >
              <RefreshCw size={14} className={loading ? "spin" : ""} />
              刷新
            </button>
            {target.endpoint && (
              <code>
                {target.endpoint.method} {target.endpoint.path}
              </code>
            )}
          </div>
          {error && <div className="form-error">{error}</div>}
          {loading && !data && (
            <div className="loading">
              <Loader2 className="spin" size={18} />
              加载中…
            </div>
          )}
          {target.type === "job" && data && (
            <>
              <div className="job-summary">
                <span className={"badge " + data.status}>
                  {statusNames[data.status]}
                </span>
                <h3>{jobNames[data.operation]}</h3>
                <p>{date(data.created_at)}</p>
                <code>{data.id}</code>
              </div>
              {data.progress && (
                <div className="job-summary">
                  <strong>{data.progress.stage}</strong>
                  <p>
                    本阶段已用{" "}
                    {Math.max(
                      0,
                      (data.finished_at || Date.now() / 1000) -
                        data.progress.stage_started_at,
                    ).toFixed(0)}{" "}
                    秒
                  </p>
                  {data.progress.total_bytes > 0 && (
                    <>
                      <progress
                        value={data.progress.transferred_bytes}
                        max={data.progress.total_bytes}
                      />
                      <p>
                        {(data.progress.transferred_bytes / 1024 ** 2).toFixed(
                          1,
                        )}{" "}
                        / {(data.progress.total_bytes / 1024 ** 2).toFixed(1)}{" "}
                        MiB ·{" "}
                        {(data.progress.rate_bytes_second / 1024 ** 2).toFixed(
                          1,
                        )}{" "}
                        MiB/s
                      </p>
                    </>
                  )}
                </div>
              )}
              <div className="task-log">
                {data.logs.map((entry: Data, index: number) => (
                  <div key={index}>
                    <span>
                      {new Date(entry.timestamp * 1000).toLocaleTimeString()}
                    </span>
                    <p>{entry.message}</p>
                  </div>
                ))}
                {!data.logs.length && <p>任务排队中…</p>}
              </div>
              {data.error && <div className="form-error">{data.error}</div>}
              {data.download_id && (
                <button
                  className="primary"
                  onClick={() =>
                    download(data.download_id).catch((e) => onError(String(e)))
                  }
                >
                  <ArrowDownToLine size={16} />
                  下载回流文件
                </button>
              )}
            </>
          )}
          {target.type === "logs" && data && (
            <>
              <div className="log-view">
                {data.items.length ? (
                  data.items.map((line: Data, i: number) => (
                    <div key={i}>
                      <span>
                        {line.timestamp} [{line.level}]
                      </span>
                      <p>{line.message}</p>
                      {line.request_id && <small>{line.request_id}</small>}
                    </div>
                  ))
                ) : (
                  <p>没有符合条件的日志。</p>
                )}
              </div>
              {data.next_cursor && (
                <button
                  className="secondary"
                  onClick={() => load(data.next_cursor)}
                >
                  下一页
                </button>
              )}
            </>
          )}
          {target.type === "database" && data && (
            <>
              <label>
                数据表
                <select
                  aria-label="数据表"
                  value={table}
                  onChange={(e) => tableRows(e.target.value)}
                >
                  <option value="" disabled>
                    选择数据表
                  </option>
                  {data.items.map((item: Data) => (
                    <option key={item.name}>{item.name}</option>
                  ))}
                </select>
              </label>
              {rowData && (
                <>
                  <div className="data-table">
                    <table>
                      <thead>
                        <tr>
                          {rowData.columns.map((c: string) => (
                            <th key={c}>{c}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rowData.rows.map((row: Data, i: number) => (
                          <tr key={i}>
                            {rowData.columns.map((c: string) => (
                              <td key={c}>
                                {typeof row[c] === "object"
                                  ? JSON.stringify(row[c])
                                  : String(row[c] ?? "")}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {!rowData.rows.length && (
                      <p className="empty-inline">表中暂无数据。</p>
                    )}
                  </div>
                  <div className="dialog-actions">
                    <button
                      className="secondary"
                      onClick={() => tableRows(table, 0)}
                    >
                      回到首页
                    </button>
                    {rowData.next_offset !== null &&
                      rowData.next_offset !== undefined && (
                        <button
                          className="secondary"
                          onClick={() => tableRows(table, rowData.next_offset)}
                        >
                          下一页
                        </button>
                      )}
                  </div>
                </>
              )}
            </>
          )}
          {target.type === "requests" && data && (
            <>
              <div className="request-list">
                {data.items.length ? (
                  data.items.map((item: Data) => (
                    <button
                      key={item.request_id}
                      onClick={() => requestDetail(item.request_id)}
                    >
                      <span className="badge failed">{item.status_code}</span>
                      <div>
                        <code>
                          {item.method} {item.endpoint}
                        </code>
                        <small>
                          {item.timestamp} · {item.duration_ms} ms
                        </small>
                        <p>{item.error || item.request_id}</p>
                      </div>
                      <ChevronRight size={16} />
                    </button>
                  ))
                ) : (
                  <Empty
                    icon={CheckCircle2}
                    title="没有非 200 请求"
                    text="当前查询范围内未发现相关记录。"
                  />
                )}
              </div>
              {data.next_cursor && (
                <button
                  className="secondary"
                  onClick={() => load(data.next_cursor)}
                >
                  下一页
                </button>
              )}
              {detail && (
                <section className="detail-block">
                  <div className="section-heading">
                    <h3>请求详情</h3>
                    {detail.trace_id && (
                      <button
                        className="secondary"
                        onClick={async () => {
                          try {
                            setDetail({
                              ...detail,
                              trace: await api(
                                root + "/traces/" + enc(detail.trace_id),
                              ),
                            });
                          } catch (err) {
                            setError(String(err));
                          }
                        }}
                      >
                        读取 Trace
                      </button>
                    )}
                  </div>
                  <pre>{JSON.stringify(detail, null, 2)}</pre>
                </section>
              )}
            </>
          )}
          {target.type === "metrics" && data && (
            <>
              <div className="mini-stats">
                <div>
                  200 响应
                  <strong>{data.available ? count(data.ok_200) : "—"}</strong>
                </div>
                <div>
                  非 200 响应
                  <strong>{data.available ? count(data.non_200) : "—"}</strong>
                </div>
                <div>
                  平均耗时
                  <strong>
                    {data.available ? count(data.mean_duration_ms) : "—"}{" "}
                    <small>ms</small>
                  </strong>
                </div>
              </div>
              <TrafficChart metrics={data} />
              <p className="hint">
                最近 {data.minutes}{" "}
                分钟的业务请求窗口估算；没有连续采集数据时不显示计数。
              </p>
            </>
          )}
          {target.type === "text" && (
            <pre>
              {typeof data === "string" ? data : JSON.stringify(data, null, 2)}
            </pre>
          )}
        </div>
      </aside>
    </div>
  );
}
