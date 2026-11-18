import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Bell,
  Lightbulb,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  Globe2,
  Layers3,
  LayoutDashboard,
  Loader2,
  LogOut,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  ShieldCheck,
  Terminal,
  Zap,
} from "lucide-react";
import { api, post, Data, date, count } from "./api";
import {
  modeNames,
  stateNames,
  categoryNames,
  jobNames,
  statusNames,
} from "./labels";
import { createRoot } from "react-dom/client";
import "./style.css";
import {
  Brand,
  Toast,
  Health,
  Empty,
  Modal,
  TrafficChart,
} from "./components/common";
import { FeedbackDrawer } from "./components/FeedbackDrawer";
import { PasswordDialog } from "./components/PasswordDialog";
import { EnvironmentDialog } from "./components/EnvironmentDialog";
import { AttachDialog } from "./components/AttachDialog";
import { DeployDialog } from "./components/DeployDialog";
import { DataDrawer } from "./components/DataDrawer";
import { ConsoleDrawer } from "./components/ConsoleDrawer";
function App() {
  const [authenticated, setAuthenticated] = useState(false),
    [loginToken, setLoginToken] = useState("");
  const [page, setPage] = useState("overview"),
    [instances, setInstances] = useState<Data[]>([]),
    [environments, setEnvironments] = useState<Data[]>([]),
    [jobs, setJobs] = useState<Data[]>([]),
    [alerts, setAlerts] = useState<Data[]>([]),
    [metrics, setMetrics] = useState<Data>({});
  const [minutes, setMinutes] = useState(60),
    [search, setSearch] = useState(""),
    [filter, setFilter] = useState("all"),
    [busy, setBusy] = useState(false),
    [toast, setToast] = useState<{ message: string; error: boolean } | null>(
      null,
    );
  const [drawer, setDrawer] = useState<Data | null>(null),
    [deployOpen, setDeployOpen] = useState(false),
    [attachOpen, setAttachOpen] = useState(false),
    [envOpen, setEnvOpen] = useState(false),
    [passwordOpen, setPasswordOpen] = useState(false),
    [feedbackOpen, setFeedbackOpen] = useState(false),
    [deleteEnv, setDeleteEnv] = useState<Data | null>(null),
    [deleteEnvError, setDeleteEnvError] = useState(""),
    [deletingEnv, setDeletingEnv] = useState(false),
    [expanded, setExpanded] = useState<string | null>(null),
    [endpoints, setEndpoints] = useState<Data[]>([]),
    [loadingEndpoints, setLoadingEndpoints] = useState(false),
    [endpointMetrics, setEndpointMetrics] = useState<Data>({});
  const [consoleTarget, setConsoleTarget] = useState<{
      instance: Data;
      endpoint: Data;
    } | null>(null),
    [confirm, setConfirm] = useState<Data | null>(null),
    [reconfigure, setReconfigure] = useState<Data | null>(null),
    [remove, setRemove] = useState<Data | null>(null),
    [removeName, setRemoveName] = useState("");
  const refreshRef = useRef(0),
    endpointRef = useRef(0);
  function notify(message: string, error = false) {
    setToast({ message, error });
  }
  async function refresh() {
    const id = ++refreshRef.current;
    try {
      const [i, e, j, a, m] = await Promise.all([
        api("/instances"),
        api("/environments"),
        api("/jobs"),
        api("/alerts"),
        api("/monitoring?minutes=" + minutes),
      ]);
      if (id !== refreshRef.current) return;
      setInstances(i);
      setEnvironments(e);
      setJobs(j);
      setAlerts(a);
      setMetrics(m);
    } catch (error) {
      notify(String(error), true);
    }
  }
  useEffect(() => {
    api("/session")
      .then(() => setAuthenticated(true))
      .catch(() => {});
    const logout = () => setAuthenticated(false);
    window.addEventListener("panel-unauthorized", logout);
    return () => window.removeEventListener("panel-unauthorized", logout);
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    refresh();
    const timer = setInterval(refresh, 10000);
    return () => {
      clearInterval(timer);
      refreshRef.current++;
    };
  }, [authenticated, minutes]);
  useEffect(() => {
    if (!expanded) return;
    let active = true;
    const load = () =>
      api("/monitoring?instance_id=" + expanded + "&minutes=" + minutes)
        .then((m) => {
          if (active) setEndpointMetrics(m);
        })
        .catch(() => {});
    setEndpointMetrics({});
    load();
    const t = setInterval(load, 10000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [expanded, minutes]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 7000);
    return () => clearTimeout(t);
  }, [toast]);
  async function action(fn: () => Promise<any>, message?: string) {
    setBusy(true);
    try {
      const result = await fn();
      if (message) notify(message);
      await refresh();
      return result;
    } catch (error) {
      notify(String(error), true);
    } finally {
      setBusy(false);
    }
  }
  async function expand(instance: Data) {
    const id = ++endpointRef.current;
    if (expanded === instance.id) {
      setExpanded(null);
      return;
    }
    setExpanded(instance.id);
    setEndpoints([]);
    setLoadingEndpoints(true);
    try {
      const result = await api("/instances/" + instance.id + "/endpoints");
      if (id === endpointRef.current) setEndpoints(result.items);
    } catch (error) {
      notify(String(error), true);
    } finally {
      if (id === endpointRef.current) setLoadingEndpoints(false);
    }
  }
  const activeAlerts = alerts.filter((a) => !a.resolved_at),
    healthy = instances.filter((i) => i.health === "healthy").length;
  const visible = instances.filter(
    (i) =>
      (filter === "all" ||
        (filter === "healthy"
          ? i.health === "healthy"
          : filter === "unhealthy"
            ? i.health === "unhealthy"
            : i.state === "deploying" ||
              i.state === "restarting" ||
              i.state === "redeploying")) &&
      `${i.name} ${i.base_url} ${i.version}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );

  if (!authenticated)
    return (
      <div className="login">
        <div className="login-brand">
          <Brand />
          <span>让每一个 Mock 实例，都清晰可见。</span>
        </div>
        <form
          className="login-card"
          onSubmit={async (e) => {
            e.preventDefault();
            sessionStorage.setItem("panel-token", loginToken.trim());
            try {
              await api("/session");
              setAuthenticated(true);
            } catch (error) {
              notify(String(error), true);
            }
          }}
        >
          <div className="eyebrow">WORKSPACE ACCESS</div>
          <h1>进入服务控制台</h1>
          <p>使用管理员登录密码或访问令牌，管理你的部署与运行环境。</p>
          <label>
            访问令牌
            <input
              type="password"
              required
              value={loginToken}
              onChange={(e) => setLoginToken(e.target.value)}
              placeholder="输入 Panel 管理员令牌"
              autoComplete="current-password"
            />
          </label>
          <button className="primary">
            进入控制台 <ArrowRight size={16} />
          </button>
          <div className="hint">
            首次令牌位于 Panel 数据目录的 admin.token 文件；改密后请使用新密码。
          </div>
        </form>
        {toast && <Toast {...toast} />}
      </div>
    );

  return (
    <div className="shell">
      <aside className="sidebar">
        <Brand />
        <div className="workspace">
          <span className="workspace-icon">W</span>
          <div>
            默认工作空间<small>SELF-HOSTED / V1</small>
          </div>
          <ChevronDown size={14} />
        </div>
        <div className="nav-title">工作空间</div>
        <nav>
          {[
            ["overview", "总览", LayoutDashboard],
            ["instances", "服务实例", Layers3],
            ["jobs", "部署与任务", Terminal],
            ["alerts", "告警中心", Bell],
            ["environments", "远程环境", Server],
          ].map(([id, label, Icon]) => (
            <button
              key={String(id)}
              className={page === id ? "nav active" : "nav"}
              onClick={() => setPage(String(id))}
            >
              {React.createElement(Icon as typeof Server, { size: 18 })}
              <span>{String(label)}</span>
              {id === "alerts" && activeAlerts.length > 0 && (
                <b>{activeAlerts.length}</b>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <span className="status-dot" />
            内置监控已启用<small>数据保存在你的工作空间</small>
          </div>
          <button
            className={"nav " + (page === "settings" ? "active" : "")}
            onClick={() => setPage("settings")}
          >
            <Settings2 size={17} />
            设置与帮助
          </button>
          <button
            className="profile"
            onClick={() => {
              sessionStorage.removeItem("panel-token");
              setAuthenticated(false);
            }}
          >
            <span className="avatar">AD</span>
            <div>
              管理员<small>退出当前会话</small>
            </div>
            <LogOut size={16} />
          </button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div>
            工作空间 <ChevronRight size={13} />{" "}
            <strong>
              {
                {
                  overview: "总览",
                  instances: "服务实例",
                  jobs: "部署与任务",
                  alerts: "告警中心",
                  environments: "远程环境",
                  settings: "设置与帮助",
                }[page]
              }
            </strong>
          </div>
          <div className="top-tools">
            <span className="connection">
              <span className="status-dot" /> 本地控制平面
            </span>
            <button
              className="icon"
              title="刷新"
              onClick={() => action(refresh)}
            >
              <RefreshCw size={17} className={busy ? "spin" : ""} />
            </button>
            <button
              className="icon"
              title="查看告警"
              onClick={() => setPage("alerts")}
            >
              <Bell size={18} />
              {activeAlerts.length > 0 && <span className="bell-dot" />}
            </button>
            <button
              className="icon"
              title="需求与反馈"
              aria-label="需求与反馈"
              onClick={() => setFeedbackOpen(true)}
            >
              <Lightbulb size={18} />
            </button>
            <span className="avatar small">AD</span>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">MOCK SERVICE CONTROL</div>
              <h1>
                {
                  {
                    overview: "服务运行总览",
                    instances: "服务实例",
                    jobs: "部署与任务",
                    alerts: "告警中心",
                    environments: "远程环境",
                    settings: "工作空间设置",
                  }[page]
                }
              </h1>
              <p>
                {
                  {
                    overview: "从部署到诊断，掌握每个实例的运行状态。",
                    instances: "在一个地方管理你的 Mock 服务与接口。",
                    jobs: "查看部署进度、操作记录与回流文件。",
                    alerts: "持续关注异常，保留每一次恢复记录。",
                    environments: "管理可用于部署的 Linux 主机与 SSH 连接。",
                    settings: "独立运行，保留面向未来的扩展能力。",
                  }[page]
                }
              </p>
            </div>
            {["overview", "instances"].includes(page) && (
              <button className="primary" onClick={() => setDeployOpen(true)}>
                <Plus size={17} />
                新增实例
              </button>
            )}
            {page === "environments" && (
              <button className="primary" onClick={() => setEnvOpen(true)}>
                <Plus size={17} />
                新增环境
              </button>
            )}
          </div>
          {page === "overview" && (
            <>
              <div className="stats">
                <button
                  className="stat"
                  onClick={() => {
                    setFilter("all");
                    setPage("instances");
                  }}
                >
                  <div className="stat-label">
                    已部署实例
                    <Server size={17} />
                  </div>
                  <strong>
                    {instances.filter((i) => i.state === "deployed").length}
                    <small>个</small>
                  </strong>
                  <div className="stat-foot">
                    <span className="status-dot" />
                    {healthy} 个健康{" "}
                    <span className="muted">
                      / 共登记 {instances.length} 个
                    </span>
                    <ArrowRight size={15} />
                  </div>
                </button>
                <div className="stat">
                  <div className="stat-label">
                    业务请求量
                    <Activity size={17} />
                  </div>
                  <strong>
                    {metrics.available ? count(metrics.requests) : "—"}
                    <small>次</small>
                  </strong>
                  <div className="stat-foot">
                    最近{" "}
                    {minutes >= 60 ? minutes / 60 + " 小时" : minutes + " 分钟"}{" "}
                    <span className="muted">· 窗口估算</span>
                  </div>
                </div>
                <button className="stat" onClick={() => setPage("alerts")}>
                  <div className="stat-label">
                    待恢复告警
                    <Bell size={17} />
                  </div>
                  <strong className={activeAlerts.length ? "amber" : ""}>
                    {activeAlerts.length}
                    <small>条</small>
                  </strong>
                  <div className="stat-foot">
                    {activeAlerts.length
                      ? "查看需要关注的异常"
                      : "当前没有未恢复的告警"}
                    <ArrowRight size={15} />
                  </div>
                </button>
                <div className="stat accent">
                  <div className="stat-label">
                    平均响应耗时
                    <Zap size={17} />
                  </div>
                  <strong>
                    {metrics.available ? count(metrics.mean_duration_ms) : "—"}
                    <small>ms</small>
                  </strong>
                  <div className="stat-foot">
                    只统计业务请求 <ShieldCheck size={15} />
                  </div>
                </div>
              </div>
              <section className="chart-card">
                <div className="section-heading">
                  <div>
                    <h2>
                      流量总览 <span className="tag">内置监控</span>
                    </h2>
                    <p>业务请求趋势 · 探测与试调流量已排除</p>
                  </div>
                  <select
                    value={minutes}
                    onChange={(e) => setMinutes(Number(e.target.value))}
                  >
                    <option value={15}>最近 15 分钟</option>
                    <option value={60}>最近 1 小时</option>
                    <option value={360}>最近 6 小时</option>
                    <option value={1440}>最近 24 小时</option>
                  </select>
                </div>
                <TrafficChart metrics={metrics} />
                <div className="chart-footer">
                  <span>
                    <i className="legend green" />
                    请求量 <i className="legend orange" />非 200
                  </span>
                  <span>
                    平均 QPS <b>{count(metrics.qps)}</b>
                    <span className="divider" />非 200 比例{" "}
                    <b>{count((metrics.non_200_ratio || 0) * 100)}%</b>
                  </span>
                </div>
              </section>
            </>
          )}
          {["overview", "instances"].includes(page) && (
            <section className="instances-section">
              <div className="section-heading">
                <div>
                  <h2>
                    服务实例{" "}
                    <span className="number-tag">{instances.length}</span>
                  </h2>
                  <p>展开实例查看接口，直接检查、调用与诊断。</p>
                </div>
                <div className="filters">
                  <select
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  >
                    <option value="all">全部状态</option>
                    <option value="healthy">健康</option>
                    <option value="unhealthy">异常</option>
                    <option value="deploying">操作中</option>
                  </select>
                  <div className="search">
                    <Search size={15} />
                    <input
                      placeholder="搜索名称、地址或版本"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                    />
                  </div>
                </div>
              </div>
              {!visible.length ? (
                <Empty
                  icon={Server}
                  title={
                    instances.length
                      ? "没有匹配的实例"
                      : "部署你的第一个 Mock 实例"
                  }
                  text={
                    instances.length
                      ? "调整筛选条件，查看其他实例。"
                      : "连接 Linux 环境，上传源码或镜像，即可开始部署。"
                  }
                  action={
                    !instances.length ? (
                      <button
                        className="primary"
                        onClick={() => setDeployOpen(true)}
                      >
                        <Plus size={16} />
                        新增实例
                      </button>
                    ) : undefined
                  }
                />
              ) : (
                visible.map((instance) => (
                  <article
                    key={instance.id}
                    className={
                      "instance-card " +
                      (expanded === instance.id ? "expanded" : "")
                    }
                  >
                    <div className="instance-row">
                      <button
                        className="expand-button"
                        title="展开接口"
                        onClick={() => expand(instance)}
                      >
                        {expanded === instance.id ? (
                          <ChevronDown size={17} />
                        ) : (
                          <ChevronRight size={17} />
                        )}
                      </button>
                      <div className="service-symbol">
                        <Layers3 size={21} />
                      </div>
                      <div className="instance-identity">
                        <h3>
                          {instance.name}
                          <Health instance={instance} />
                        </h3>
                        <div className="mono">{instance.base_url}</div>
                      </div>
                      <div className="instance-meta">
                        <span>
                          {modeNames[instance.mode]}
                          <small>部署方式</small>
                        </span>
                        <span title={instance.version}>
                          {instance.version}
                          <small>镜像 / 源码文件</small>
                        </span>
                      </div>
                      <div className="row-actions">
                        <button
                          className="subtle"
                          disabled={busy}
                          onClick={() =>
                            action(async () => {
                              const result = await post(
                                "/instances/" + instance.id + "/health",
                              );
                              notify(
                                result.health === "healthy"
                                  ? "实例健康，管理协议可用"
                                  : result.health_error || "实例不健康",
                                result.health !== "healthy",
                              );
                            })
                          }
                        >
                          <Activity size={15} />
                          健康检查
                        </button>
                        <button
                          className="icon"
                          title={
                            instance.mode === "docker" &&
                            instance.origin !== "attached"
                              ? "重新部署实例"
                              : "重启实例"
                          }
                          disabled={
                            instance.origin === "attached" ||
                            instance.state === "undeployed"
                          }
                          onClick={() =>
                            instance.mode === "docker"
                              ? action(async () => {
                                  setReconfigure(
                                    await api(
                                      "/instances/" +
                                        instance.id +
                                        "/configuration",
                                    ),
                                  );
                                })
                              : setConfirm(instance)
                          }
                        >
                          <RefreshCw size={16} />
                        </button>
                        <details className="more">
                          <summary className="icon" title="更多操作">
                            <MoreHorizontal size={19} />
                          </summary>
                          <div className="menu">
                            {[
                              ["logs", "查看日志", FileText],
                              ["database", "查看数据库", Database],
                              ["requests", "失败请求", Activity],
                            ].map(([type, label, Icon]) => (
                              <button
                                key={String(type)}
                                onClick={(e) => {
                                  e.currentTarget
                                    .closest("details")
                                    ?.removeAttribute("open");
                                  setDrawer({ type, instance });
                                }}
                              >
                                {React.createElement(Icon as typeof Server, {
                                  size: 15,
                                })}
                                {String(label)}
                              </button>
                            ))}
                            <hr />
                            {["logs", "database", "traces"].map((kind) => (
                              <button
                                key={kind}
                                onClick={(e) => {
                                  e.currentTarget
                                    .closest("details")
                                    ?.removeAttribute("open");
                                  action(async () => {
                                    const job = await post(
                                      "/instances/" +
                                        instance.id +
                                        "/exports/" +
                                        kind,
                                    );
                                    setDrawer({ type: "job", jobId: job.id });
                                  });
                                }}
                              >
                                <ArrowDownToLine size={15} />
                                {
                                  {
                                    logs: "回流日志",
                                    database: "回流数据库",
                                    traces: "回流 Trace",
                                  }[kind]
                                }
                              </button>
                            ))}
                            <hr />
                            <button
                              onClick={(e) => {
                                e.currentTarget
                                  .closest("details")
                                  ?.removeAttribute("open");
                                setRemoveName("");
                                setRemove({ instance, kind: "forget" });
                              }}
                            >
                              移除监测
                            </button>
                            <button
                              disabled={
                                instance.origin === "attached" ||
                                instance.state === "undeployed"
                              }
                              onClick={(e) => {
                                e.currentTarget
                                  .closest("details")
                                  ?.removeAttribute("open");
                                setRemoveName("");
                                setRemove({ instance, kind: "undeploy" });
                              }}
                            >
                              卸载实例
                            </button>
                          </div>
                        </details>
                      </div>
                    </div>
                    <div className="instance-foot">
                      <span className="mono">{instance.id.slice(0, 8)}</span>
                      <span>{stateNames[instance.state]}</span>
                      {instance.origin === "attached" && (
                        <span className="tag">外部接入</span>
                      )}
                      {instance.remote_directory && (
                        <span title={instance.remote_directory}>
                          目录：{instance.remote_directory}
                        </span>
                      )}
                      <span>
                        <Clock3 size={12} />
                        {date(instance.checked_at)}
                      </span>
                      {instance.monitoring_error && (
                        <span
                          className="amber"
                          title={instance.monitoring_error}
                        >
                          监控采集异常
                        </span>
                      )}
                      {instance.health_error && (
                        <span
                          className="error-summary"
                          title={instance.health_error}
                        >
                          {instance.health_error}
                        </span>
                      )}
                    </div>
                    {expanded === instance.id && (
                      <div className="endpoint-list">
                        <div className="endpoint-heading">
                          接口目录 <span>{endpoints.length} 个接口</span>
                          <small>最近 {minutes} 分钟的业务统计</small>
                        </div>
                        {loadingEndpoints ? (
                          <div className="loading">
                            <Loader2 className="spin" size={18} />
                            正在发现接口…
                          </div>
                        ) : !endpoints.length ? (
                          <div className="empty-inline">
                            暂无接口清单。请检查实例连接及管理接口实现。
                          </div>
                        ) : (
                          endpoints.map((endpoint) => {
                            const metric = (
                              endpointMetrics.endpoints || []
                            ).find(
                              (m: Data) =>
                                m.endpoint === endpoint.path &&
                                m.method === endpoint.method,
                            );
                            return (
                              <div className="endpoint-row" key={endpoint.id}>
                                <span
                                  className={
                                    "method " + endpoint.method.toLowerCase()
                                  }
                                >
                                  {endpoint.method}
                                </span>
                                <div className="endpoint-name">
                                  <code>{endpoint.path}</code>
                                  <small>
                                    {endpoint.summary ||
                                      categoryNames[endpoint.category]}
                                  </small>
                                </div>
                                <button
                                  className="metric-button"
                                  onClick={() =>
                                    setDrawer({
                                      type: "metrics",
                                      instance,
                                      endpoint,
                                      minutes,
                                    })
                                  }
                                >
                                  <span className="status-dot" />
                                  200{" "}
                                  <b>{metric ? count(metric.ok_200) : "—"}</b>
                                </button>
                                <button
                                  className="metric-button"
                                  onClick={() =>
                                    setDrawer({
                                      type: "requests",
                                      instance,
                                      endpoint,
                                      minutes,
                                    })
                                  }
                                >
                                  <span className="status-dot orange-dot" />非
                                  200{" "}
                                  <b>{metric ? count(metric.non_200) : "—"}</b>
                                </button>
                                <button
                                  className="subtle"
                                  onClick={() =>
                                    setConsoleTarget({ instance, endpoint })
                                  }
                                >
                                  <Terminal size={15} />
                                  试调
                                  <ArrowRight size={13} />
                                </button>
                              </div>
                            );
                          })
                        )}
                      </div>
                    )}
                  </article>
                ))
              )}
            </section>
          )}
          {page === "environments" && (
            <div className="environment-grid">
              {!environments.length ? (
                <Empty
                  icon={Globe2}
                  title="连接你的运行环境"
                  text="添加 SSH 连接与可信主机指纹，凭据在服务端加密保存。"
                  action={
                    <button
                      className="primary"
                      onClick={() => setEnvOpen(true)}
                    >
                      新增环境
                    </button>
                  }
                />
              ) : (
                environments.map((env) => (
                  <section className="environment-card" key={env.id}>
                    <div className="section-heading">
                      <span className="service-symbol">
                        <Server size={22} />
                      </span>
                      <span className="tag">LINUX</span>
                    </div>
                    <h3>{env.name}</h3>
                    <code>
                      {env.username}@{env.host}:{env.port}
                    </code>
                    <p className="fingerprint">
                      {env.host_key_sha256 ||
                        env.learned_host_key_sha256 ||
                        "首次成功登录后自动记住指纹"}
                    </p>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        action(async () => {
                          const result = await post(
                            "/environments/" + env.id + "/check",
                          );
                          setDrawer({
                            type: "text",
                            title: "环境检查 · " + env.name,
                            data: result.output,
                          });
                        })
                      }
                    >
                      检查连接 <ArrowRight size={14} />
                    </button>
                    <button
                      className="subtle"
                      onClick={() => {
                        setDeleteEnvError("");
                        setDeleteEnv(env);
                      }}
                    >
                      删除环境
                    </button>
                  </section>
                ))
              )}
            </div>
          )}
          {page === "jobs" && (
            <section className="list-card">
              {!jobs.length ? (
                <Empty
                  icon={Terminal}
                  title="还没有操作记录"
                  text="部署、重启与文件回流任务会显示在这里。"
                />
              ) : (
                jobs.map((job) => (
                  <button
                    className="job-row"
                    key={job.id}
                    onClick={() => setDrawer({ type: "job", jobId: job.id })}
                  >
                    <span className={"job-symbol " + job.status}>
                      {job.status === "running" ? (
                        <Loader2 className="spin" size={19} />
                      ) : job.status === "succeeded" ? (
                        <CheckCircle2 size={19} />
                      ) : (
                        <Terminal size={19} />
                      )}
                    </span>
                    <div>
                      <strong>
                        {jobNames[job.operation]}
                        {job.kind ? " · " + job.kind : ""}
                      </strong>
                      <small>
                        {instances.find((i) => i.id === job.instance_id)
                          ?.name || job.instance_id}
                      </small>
                    </div>
                    <span className="muted">{date(job.created_at)}</span>
                    <span className={"badge " + job.status}>
                      {statusNames[job.status]}
                    </span>
                    <ChevronRight size={16} />
                  </button>
                ))
              )}
            </section>
          )}
          {page === "alerts" && (
            <section className="list-card">
              {!alerts.length ? (
                <Empty
                  icon={ShieldCheck}
                  title="当前没有告警"
                  text="连续三次健康检查失败、监控异常或任务失败时，会在此记录。"
                />
              ) : (
                alerts.map((alert) => (
                  <div className="alert-row" key={alert.id}>
                    <span
                      className={
                        "job-symbol " +
                        (alert.resolved_at ? "succeeded" : "failed")
                      }
                    >
                      <Bell size={18} />
                    </span>
                    <div>
                      <strong>
                        {instances.find((i) => i.id === alert.instance_id)
                          ?.name || alert.instance_id}{" "}
                        ·{" "}
                        {
                          {
                            health: "健康检查异常",
                            operation: "操作失败",
                            monitoring: "指标采集异常",
                          }[alert.kind as string]
                        }
                      </strong>
                      <p>{alert.message}</p>
                      <small>
                        {date(alert.created_at)}{" "}
                        {alert.resolved_at &&
                          " · 已恢复 " + date(alert.resolved_at)}
                      </small>
                    </div>
                    {alert.resolved_at ? (
                      <span className="badge succeeded">已恢复</span>
                    ) : (
                      <button
                        className="secondary"
                        disabled={!!alert.acknowledged}
                        onClick={() =>
                          action(() =>
                            post("/alerts/" + alert.id + "/acknowledge"),
                          )
                        }
                      >
                        {alert.acknowledged ? "已确认" : "确认告警"}
                      </button>
                    )}
                  </div>
                ))
              )}
            </section>
          )}
          {page === "settings" && (
            <section className="settings-card">
              <h2>管理员账号</h2>
              <p>
                修改控制台登录密码或访问令牌，不影响远程 SSH 凭据和 Mock
                服务令牌。
              </p>
              <div className="setting-row">
                <span>登录密码 / 访问令牌</span>
                <button
                  className="secondary"
                  onClick={() => setPasswordOpen(true)}
                >
                  修改登录密码
                </button>
              </div>
              <h2>内置监控</h2>
              <p>
                指标由 Panel 定时采集并保存在本地数据库，历史保留 7
                天。图表基于累计计数差值估算；实例重启或采集间断显示为缺失区间。
              </p>
              <div className="setting-row">
                <span>当前监控数据源</span>
                <span className="badge healthy">Builtin · 已启用</span>
              </div>
              <div className="setting-row">
                <span>Prometheus / Grafana</span>
                <button disabled className="secondary">
                  预留扩展 · 暂不支持
                </button>
              </div>
              <div className="setting-row">
                <span>文件回流保留期限</span>
                <span>24 小时</span>
              </div>
              <h2>部署约定</h2>
              <p>
                Linux 环境需要已有 Python ≥ 3.11、systemd
                或对应容器运行时。凭据需有非交互 sudo 权限（root
                除外）。主机指纹可选；留空时首次成功登录后自动记住，后续检测变更。
              </p>
              <p>
                K8s 清单需配置可访问的 Service 地址、管理
                token、持久化目录和节点调度约束；SSH
                连接必须指向镜像加载节点。Panel
                保留业务配置，为资源添加归属标签后应用。
              </p>
              <p>
                重启会清空实例内存中的多轮会话。查看与回流数据库使用 mock
                侧的只读接口和一致性快照。
              </p>
            </section>
          )}
          <footer>
            MOCK CONTROL <span>自托管工作空间 · V1</span>
            <span>页面每 10 秒刷新</span>
          </footer>
        </main>
      </div>
      {deployOpen && (
        <DeployDialog
          environments={environments}
          onClose={() => setDeployOpen(false)}
          onEnvironment={() => setEnvOpen(true)}
          onAttach={() => {
            setDeployOpen(false);
            setAttachOpen(true);
          }}
          onDone={(job) => {
            setDeployOpen(false);
            setDrawer({ type: "job", jobId: job.id });
            refresh();
          }}
        />
      )}
      {attachOpen && (
        <AttachDialog
          environments={environments}
          onClose={() => setAttachOpen(false)}
          onDone={() => {
            setAttachOpen(false);
            refresh();
            notify("实例已接入监测");
          }}
        />
      )}
      {remove && (
        <Modal
          title={remove.kind === "forget" ? "移除监测" : "卸载实例"}
          subtitle={remove.instance.name}
          onClose={() => setRemove(null)}
        >
          <p>
            {remove.kind === "forget"
              ? "将此实例从列表和监控中移除。远程服务、容器和文件保持原样，正在执行的任务仍会继续。之后可以通过“监测已有实例”重新接入。"
              : "将停止并删除该实例的运行资源，清理独立部署目录内的上传包、配置、数据库、日志和 Trace。额外挂载、共享镜像及系统 Python 包保留。此操作不可恢复，请先回流需要保留的数据。"}
          </p>
          {remove.kind === "undeploy" && (
            <label>
              输入实例名称确认
              <input
                value={removeName}
                onChange={(e) => setRemoveName(e.target.value)}
                placeholder={remove.instance.name}
              />
            </label>
          )}
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setRemove(null)}>
              取消
            </button>
            <button
              className="primary"
              disabled={
                busy ||
                (remove.kind === "undeploy" &&
                  removeName !== remove.instance.name)
              }
              onClick={() => {
                const item = remove;
                setRemove(null);
                action(async () => {
                  if (item.kind === "forget") {
                    await api("/instances/" + item.instance.id, {
                      method: "DELETE",
                    });
                    setExpanded(null);
                    notify("已移除监测，远程资源未改动");
                  } else {
                    const job = await post(
                      "/instances/" + item.instance.id + "/undeploy",
                      { confirm_name: removeName },
                    );
                    setDrawer({ type: "job", jobId: job.id });
                  }
                });
              }}
            >
              {remove.kind === "forget" ? "确认移除监测" : "确认卸载"}
            </button>
          </div>
        </Modal>
      )}
      {passwordOpen && (
        <PasswordDialog
          onClose={() => setPasswordOpen(false)}
          onChanged={() => {
            sessionStorage.removeItem("panel-token");
            setLoginToken("");
            setPasswordOpen(false);
            setDrawer(null);
            setConsoleTarget(null);
            setAuthenticated(false);
            notify("密码已修改，请使用新密码重新登录");
          }}
        />
      )}
      {deleteEnv && (
        <Modal
          title="删除远程环境"
          subtitle={deleteEnv.name}
          onClose={() => {
            if (!deletingEnv) setDeleteEnv(null);
          }}
        >
          <p>
            删除此环境的连接配置、SSH
            凭据和主机指纹。不会停止远程服务，也不会删除主机上的文件。
          </p>
          <p className="hint">
            仍被监测的未卸载实例或未完成任务会阻止删除；请先处理相关实例。只移除监测会保留远端资源。
          </p>
          {deleteEnvError && (
            <div className="form-error" role="alert">
              {deleteEnvError}
            </div>
          )}
          <div className="dialog-actions">
            <button
              className="secondary"
              disabled={deletingEnv}
              onClick={() => setDeleteEnv(null)}
            >
              取消
            </button>
            <button
              className="primary"
              disabled={deletingEnv}
              onClick={async () => {
                setDeletingEnv(true);
                setDeleteEnvError("");
                try {
                  await api("/environments/" + deleteEnv.id, {
                    method: "DELETE",
                  });
                  setDeleteEnv(null);
                  await refresh();
                  notify("环境已删除，远程主机未改动");
                } catch (err) {
                  setDeleteEnvError(String(err));
                } finally {
                  setDeletingEnv(false);
                }
              }}
            >
              {deletingEnv ? "正在删除…" : "确认删除环境"}
            </button>
          </div>
        </Modal>
      )}
      {envOpen && (
        <EnvironmentDialog
          onClose={() => setEnvOpen(false)}
          onDone={() => {
            setEnvOpen(false);
            refresh();
            notify("环境已保存");
          }}
        />
      )}
      {drawer && (
        <DataDrawer
          target={drawer}
          onClose={() => setDrawer(null)}
          onError={(message) => notify(message, true)}
        />
      )}
      {consoleTarget && (
        <ConsoleDrawer
          {...consoleTarget}
          onClose={() => setConsoleTarget(null)}
        />
      )}
      {reconfigure && (
        <DeployDialog
          initial={reconfigure}
          environments={environments}
          onClose={() => setReconfigure(null)}
          onEnvironment={() => {}}
          onAttach={() => {}}
          onDone={(job) => {
            setReconfigure(null);
            setDrawer({ type: "job", jobId: job.id });
            refresh();
          }}
        />
      )}
      {confirm && (
        <Modal
          title="重启实例"
          subtitle={confirm.name}
          onClose={() => setConfirm(null)}
        >
          <p>实例将短暂不可用，内存中的多轮会话会清空，持久化样本保留。</p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setConfirm(null)}>
              取消
            </button>
            <button
              className="primary"
              onClick={() => {
                const item = confirm;
                setConfirm(null);
                action(async () => {
                  const job = await post("/instances/" + item.id + "/restart");
                  setDrawer({ type: "job", jobId: job.id });
                });
              }}
            >
              确认重启
            </button>
          </div>
        </Modal>
      )}
      {feedbackOpen && (
        <FeedbackDrawer onClose={() => setFeedbackOpen(false)} />
      )}
      {toast && <Toast {...toast} />}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
