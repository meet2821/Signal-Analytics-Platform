export const fmtTime = (ms: number) => new Date(ms).toISOString().slice(11, 19);
export const fmtDateTime = (ms: number | string) =>
  new Date(ms).toISOString().slice(0, 19).replace("T", " ") + " UTC";
export const fmtNum = (v: number | null | undefined) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  return a >= 1000 ? v.toFixed(0) : a >= 100 ? v.toFixed(1) : v.toFixed(2);
};
export const fmtPct = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}%`;
// value for <input type="datetime-local"> in UTC
export const toInputUtc = (ms: number) => new Date(ms).toISOString().slice(0, 19);
export const fromInputUtc = (s: string) => new Date(s.length === 16 ? s + ":00Z" : s + "Z").getTime();
