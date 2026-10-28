// All times are shown in Indian Standard Time (UTC+05:30, no daylight saving). Data stays UTC in the database.
export const IST_OFFSET_MS = 5.5 * 3600_000;
const shifted = (ms: number | string) => new Date(new Date(ms).getTime() + IST_OFFSET_MS).toISOString();
export const fmtTime = (ms: number) => shifted(ms).slice(11, 19);
export const fmtDateTime = (ms: number | string) => shifted(ms).slice(0, 19).replace("T", " ") + " IST";
export const fmtNum = (v: number | null | undefined) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  return a >= 1000 ? v.toFixed(0) : a >= 100 ? v.toFixed(1) : v.toFixed(2);
};
export const fmtPct = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}%`;
// <input type="datetime-local"> values are IST wall-clock times
export const toInputUtc = (ms: number) => shifted(ms).slice(0, 19);
export const fromInputUtc = (s: string) => new Date(s.length === 16 ? s + ":00Z" : s + "Z").getTime() - IST_OFFSET_MS;
