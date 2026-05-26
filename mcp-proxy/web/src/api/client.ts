/** API client: Bearer access in memory, refresh httpOnly cookie, CSRF double-submit. */

const CSRF_COOKIE = "mcp_csrf";

let accessToken: string | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

/** After full page load, restore access JWT using httpOnly refresh + CSRF cookies. */
export async function trySessionRestore(): Promise<boolean> {
  if (accessToken) return true;
  return refreshAccessToken();
}

function readCsrfCookie(): string {
  const m = document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : "";
}

export function apiFetch(
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const csrf = readCsrfCookie();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return fetch(input, { ...init, headers, credentials: "include" });
}

export async function refreshAccessToken(): Promise<boolean> {
  const csrf = readCsrfCookie();
  if (!csrf) return false;
  const r = await apiFetch("/auth/refresh", { method: "POST" });
  if (!r.ok) return false;
  const data = (await r.json()) as { access?: string };
  if (data.access) {
    accessToken = data.access;
    return true;
  }
  return false;
}

export async function apiFetchWithRefresh(
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  let res = await apiFetch(input, init);
  if (res.status === 401 && accessToken) {
    const ok = await refreshAccessToken();
    if (ok) res = await apiFetch(input, init);
  }
  return res;
}
