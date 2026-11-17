import React, { useEffect } from "react";
import { Activity, Check, CircleHelp, Server, X } from "lucide-react";
import { Data, count } from "../api";
export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        m<span>·</span>
      </span>
      <div>
        mock<span>control</span>
      </div>
    </div>
  );
}
export function Toast({ message, error }: { message: string; error: boolean }) {
  return (
    <div role="status" className={"toast " + (error ? "error" : "")}>
      <span>{error ? <CircleHelp size={18} /> : <Check size={18} />}</span>
      {message}
    </div>
  );
}
export function Health({ instance }: { instance: Data }) {
  return (
    <span className={"badge " + instance.health}>
      <span className="status-dot" />
      {instance.health === "healthy"
        ? "健康"
        : instance.health === "unhealthy"
          ? "异常"
          : "待检查"}
    </span>
  );
}
export function Empty({
  icon: Icon,
  title,
  text,
  action,
}: {
  icon: typeof Server;
  title: string;
  text: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon size={28} />
      </span>
      <h3>{title}</h3>
      <p>{text}</p>
      {action}
    </div>
  );
}
export function Modal({
  title,
  subtitle,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);
  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={"modal " + (wide ? "wide" : "")}
      >
        <div className="dialog-heading">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon" aria-label="关闭" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
export function TrafficChart({ metrics }: { metrics: Data }) {
  const points: Data[] = metrics.series || [];
  const max = Math.max(1, ...points.map((p) => p.requests));
  const width = 960,
    height = 142;
  return (
    <div className="traffic-chart">
      <div className="chart-y">
        {[max, max / 2, 0].map((n, i) => (
          <span key={i}>{count(n)}</span>
        ))}
      </div>
      <div className="plot">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          role="img"
          aria-label="业务请求量与非200响应趋势"
        >
          <defs>
            <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#62a994" stopOpacity=".35" />
              <stop offset="100%" stopColor="#62a994" stopOpacity=".02" />
            </linearGradient>
          </defs>
          {[1, 71, 141].map((y) => (
            <line
              key={y}
              x1="0"
              y1={y}
              x2={width}
              y2={y}
              stroke="#e7eae6"
              strokeDasharray="4 5"
            />
          ))}
          {points.map((p, i) =>
            p.available ? (
              <g key={i}>
                <rect
                  x={(i * width) / points.length + 2}
                  y={height - (p.requests / max) * (height - 10)}
                  width={Math.max(2, width / points.length - 5)}
                  height={Math.max(1, (p.requests / max) * (height - 10))}
                  fill="#79b4a2"
                  rx="2"
                >
                  <title>
                    {new Date(p.timestamp * 1000).toLocaleTimeString()} ·{" "}
                    {count(p.requests)} 次请求
                  </title>
                </rect>
                <rect
                  x={(i * width) / points.length + 2}
                  y={height - (p.non_200 / max) * (height - 10)}
                  width={Math.max(2, width / points.length - 5)}
                  height={(p.non_200 / max) * (height - 10)}
                  fill="#d99967"
                  rx="2"
                />
              </g>
            ) : null,
          )}
        </svg>
        {!metrics.available && (
          <div className="chart-empty">
            <Activity size={22} />
            <span>等待采集业务指标</span>
            <small>至少两个连续采集点后显示趋势，缺失数据不会计为零。</small>
          </div>
        )}
        <div className="chart-x">
          {points
            .filter(
              (_, i) => i % Math.max(1, Math.floor(points.length / 5)) === 0,
            )
            .slice(0, 6)
            .map((p, i) => (
              <span key={i}>
                {new Date(p.timestamp * 1000).toLocaleTimeString("zh-CN", {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            ))}
        </div>
      </div>
    </div>
  );
}
