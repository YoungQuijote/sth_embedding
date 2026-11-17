export type Data = Record<string, any>;
export const authHeaders = () => ({
  Authorization: "Bearer " + (sessionStorage.getItem("panel-token") || ""),
});
export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      const value = JSON.parse(text);
      message =
        typeof value.detail === "string"
          ? value.detail
          : JSON.stringify(value.detail || value);
    } catch {
      /* Non-JSON upstream error */
    }
    if (response.status === 401)
      window.dispatchEvent(new Event("panel-unauthorized"));
    throw new Error(message || `HTTP ${response.status}`);
  }
  return response.json();
}
export const post = (path: string, body?: unknown) =>
  api(path, {
    method: "POST",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
export type UploadProgress = {
  sent: number;
  total: number;
  seconds: number;
  bytesPerSecond: number;
};
export async function upload(
  file: File,
  kind: string,
  onProgress?: (value: UploadProgress) => void,
  signal?: AbortSignal,
) {
  const policy = await api("/upload-policy");
  const limit = policy.limits[kind];
  if (!file.size || file.size > limit)
    throw new Error(
      `${kind} 文件大小不符合要求：最大 ${(limit / 1024 ** 2).toFixed(0)} MiB，当前 ${(file.size / 1024 ** 2).toFixed(1)} MiB`,
    );
  return new Promise<any>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const started = performance.now();
    xhr.open(
      "POST",
      "/api/artifacts?kind=" +
        encodeURIComponent(kind) +
        "&filename=" +
        encodeURIComponent(file.name),
    );
    xhr.setRequestHeader("Authorization", authHeaders().Authorization);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Upload-Size", String(file.size));
    const abort = () => xhr.abort();
    signal?.addEventListener("abort", abort, { once: true });
    xhr.onloadend = () => signal?.removeEventListener("abort", abort);
    xhr.upload.onprogress = (e) => {
      const seconds = Math.max(0.001, (performance.now() - started) / 1000);
      onProgress?.({
        sent: e.loaded,
        total: file.size,
        seconds,
        bytesPerSecond: e.loaded / seconds,
      });
    };
    xhr.onerror = () => reject(new Error("上传连接失败，请检查网络或代理限制"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.onload = () => {
      let value;
      try {
        value = JSON.parse(xhr.responseText);
      } catch {
        value = { detail: xhr.responseText };
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(value);
      else
        reject(
          new Error(
            typeof value.detail === "string"
              ? value.detail
              : JSON.stringify(value.detail || value),
          ),
        );
    };
    if (signal?.aborted) {
      reject(new Error("上传已取消"));
      return;
    }
    xhr.send(file);
  });
}
export async function download(id: string) {
  const ticket = await post("/downloads/" + id + "/ticket");
  const anchor = document.createElement("a");
  anchor.href = ticket.url;
  anchor.rel = "noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
export const date = (seconds?: number) =>
  seconds
    ? new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false })
    : "尚未检查";
export const count = (n: number) =>
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(n || 0);

export function resolveSchema(schema: Data, document: Data): Data {
  if (schema?.$ref?.startsWith("#/"))
    return resolveSchema(
      schema.$ref
        .slice(2)
        .split("/")
        .reduce(
          (o: Data, k: string) =>
            o?.[k.replace(/~1/g, "/").replace(/~0/g, "~")],
          document,
        ) || {},
      document,
    );
  return schema || {};
}
export function schemaExample(raw: Data, document: Data, depth = 0): any {
  if (depth > 8) return null;
  const schema = resolveSchema(raw, document);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.enum?.length) return schema.enum[0];
  if (schema.anyOf || schema.oneOf)
    return schemaExample(
      (schema.anyOf || schema.oneOf).find((s: Data) => s.type !== "null") || {},
      document,
      depth + 1,
    );
  if (schema.type === "object" || schema.properties)
    return Object.fromEntries(
      Object.entries(schema.properties || {}).map(([key, value]) => [
        key,
        schemaExample(value as Data, document, depth + 1),
      ]),
    );
  if (schema.type === "array") return [];
  if (schema.type === "integer" || schema.type === "number")
    return schema.minimum ?? 0;
  if (schema.type === "boolean") return false;
  return "";
}
