// One fetch wrapper for /api/v1: same-origin cookie session, the per-session CSRF header on writes, errors as values.
let csrf = "";

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `HTTP ${status}`);
    this.status = status;
    this.code = (body && body.error) || "error";
    this.body = body || {};
  }
}

export function setCsrf(v) { csrf = v || ""; }

export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { message: text.slice(0, 200) }; } }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const get = (p) => api("GET", p);
export const post = (p, b) => api("POST", p, b === undefined ? {} : b);
export const patch = (p, b) => api("PATCH", p, b);
export const del = (p) => api("DELETE", p);
export const q = (params) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v !== null && v !== undefined) u.set(k, v);
  const s = u.toString();
  return s ? `?${s}` : "";
};
