import { supabase } from "./supabase";

const API = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

export type Pt = { t: number; avg: number; min?: number; max?: number };

export async function api(path: string, params?: Record<string, string | number | undefined>) {
  const qs = params
    ? "?" +
      Object.entries(params)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
        .join("&")
    : "";
  const doFetch = (token?: string) =>
    fetch(`${API}${path}${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
    });

  let token = (await supabase.auth.getSession()).data.session?.access_token;
  let res = await doFetch(token);
  if (res.status === 401 && token) {
    // the access token may just have expired: refresh once and retry
    const refreshed = await supabase.auth.refreshSession();
    token = refreshed.data.session?.access_token;
    if (token) res = await doFetch(token);
  }
  // Show the server's reason instead of silently bouncing to /login (AuthGate handles "no session").
  if (!res.ok) throw new Error(`${res.status}: ${(await res.text()).slice(0, 300)}`);
  return res.json();
}

export const toPts = (rows: any[]): Pt[] =>
  (rows || []).map((r) => ({ t: new Date(r.t).getTime(), avg: r.avg, min: r.min, max: r.max }));

export const iso = (ms: number) => new Date(ms).toISOString();
