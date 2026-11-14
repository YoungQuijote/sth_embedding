import type { Data } from "./api";
export const modeNames: Data = {
  bare: "裸机",
  docker: "Docker",
  k8s: "Kubernetes",
};
export const stateNames: Data = {
  pending: "待部署",
  deploying: "部署中",
  restarting: "重启中",
  deployed: "已部署",
  failed: "操作失败",
  unknown: "状态待确认",
};
export const categoryNames: Data = {
  business: "业务查询",
  registry: "数据注册",
  debug: "调试",
  management: "运维",
};
export const jobNames: Data = {
  deploy: "部署实例",
  restart: "重启实例",
  export: "文件回流",
};
export const statusNames: Data = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  interrupted: "已中断",
};
export const enc = encodeURIComponent;
