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
export async function upload(file: File, kind: string) {
  const body = new FormData();
  body.append("file", file);
  return api("/artifacts?kind=" + kind, { method: "POST", body });
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
