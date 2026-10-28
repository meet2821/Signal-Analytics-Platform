"use client";
import { useEffect, useMemo, useState } from "react";
import LineChart, { Shade } from "@/components/LineChart";
import { api, iso, Pt, toPts } from "@/lib/api";
import { fmtDateTime, fmtNum, fmtPct, fromInputUtc, toInputUtc } from "@/lib/format";
import { SIGNALS, SIGNAL_META } from "@/lib/signals";

type MillInfo = { mill_id: string; start: string; end: string; live: boolean; hist_start: string | null; hist_end: string | null };

function stats(pts: Pt[]) {
  if (!pts.length) return null;
  let sum = 0, lo = Infinity, hi = -Infinity;
  for (const p of pts) {
    sum += p.avg;
    lo = Math.min(lo, p.min ?? p.avg);
    hi = Math.max(hi, p.max ?? p.avg);
  }
  return { avg: sum / pts.length, min: lo, max: hi, n: pts.length };
}

export default function HistoryPage() {
  const [mills, setMills] = useState<MillInfo[]>([]);
  const [mill, setMill] = useState("");
  const [signal, setSignal] = useState("motor_current");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [pts, setPts] = useState<Pt[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const info = mills.find((m) => m.mill_id === mill);

  function applyRange(a: number, b: number) {
    setFrom(toInputUtc(a));
    setTo(toInputUtc(b));
    setRange([a, b]);
  }

  useEffect(() => {
    api("/api/mills")
      .then((r) => {
        setMills(r.mills);
        if (r.mills[0]) setMill(r.mills[0].mill_id);
      })
      .catch((e) => setError(e.message));
  }, []);

  // default range whenever the mill changes
  useEffect(() => {
    if (!info) return;
    const end = new Date(info.end).getTime();
    if (info.live) applyRange(end - 15 * 60_000, end); // a running feed: the last 15 minutes
    else if (info.hist_start && info.hist_end)
      applyRange(new Date(info.hist_start).getTime(), new Date(info.hist_end).getTime()); // replay history
    else applyRange(new Date(info.start).getTime(), end);
  }, [mill, mills.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!range || !mill) return;
    let alive = true;
    setLoading(true);
    Promise.all([
      api(`/api/mills/${mill}/signals/${signal}/series`, { start: iso(range[0]), end: iso(range[1]), max_points: 1200 }),
      api("/api/alerts", { mill_id: mill }),
    ])
      .then(([s, a]) => {
        if (!alive) return;
        setPts(toPts(s.series));
        setSummary(s.summary);
        setAlerts(a.alerts);
        setError(null);
      })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [mill, signal, range]);

  const windows = alerts
    .map((a) => ({ id: a.alert_id, from: new Date(a.started_at).getTime(), to: new Date(a.ended_at).getTime() }))
    .filter((w) => range && w.to >= range[0] && w.from <= range[1]);
  const shades: Shade[] = windows.map((w) => ({ from: w.from, to: w.to, label: `alert #${w.id}` }));

  const { normal, abnormal } = useMemo(() => {
    const inAlert = (p: Pt) => windows.some((w) => p.t >= w.from && p.t <= w.to);
    return { normal: stats(pts.filter((p) => !inAlert(p))), abnormal: stats(pts.filter(inAlert)) };
  }, [pts, alerts, range]); // eslint-disable-line react-hooks/exhaustive-deps

  const meta = SIGNAL_META[signal];
  const firstAlert = alerts.slice().sort((a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime())[0];

  return (
    <>
      <h1>Historical analytics</h1>
      <div className="muted">Explore stored readings by signal and time range. Times are IST (UTC+05:30).</div>

      <div className="row">
        <label>
          Mill
          <select value={mill} onChange={(e) => setMill(e.target.value)}>
            {mills.map((m) => (
              <option key={m.mill_id} value={m.mill_id}>
                {m.mill_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          Signal
          <select value={signal} onChange={(e) => setSignal(e.target.value)}>
            {SIGNALS.map((s) => (
              <option key={s} value={s}>
                {SIGNAL_META[s].label}
              </option>
            ))}
          </select>
        </label>
        <label>
          From (IST)
          <input type="datetime-local" step={1} value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          To (IST)
          <input type="datetime-local" step={1} value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <button
          className="btn"
          onClick={() => {
            const a = fromInputUtc(from), b = fromInputUtc(to);
            if (Number.isFinite(a) && Number.isFinite(b) && b > a) setRange([a, b]);
            else setError("Choose a valid range where To is after From");
          }}
        >
          Apply
        </button>
        {info ? (
          <button className="btn ghost" onClick={() => applyRange(new Date(info.start).getTime(), new Date(info.end).getTime())}>
            Full range
          </button>
        ) : null}
        {info?.hist_start && info?.hist_end ? (
          <button
            className="btn ghost"
            onClick={() => applyRange(new Date(info.hist_start!).getTime(), new Date(info.hist_end!).getTime())}
          >
            Replay-history window
          </button>
        ) : null}
        {firstAlert ? (
          <button
            className="btn ghost"
            onClick={() => {
              const t = new Date(firstAlert.started_at).getTime();
              applyRange(t - 120_000, new Date(firstAlert.ended_at).getTime() + 120_000);
            }}
          >
            Around first alert
          </button>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      <section className="panel">
        <h3>
          {meta.label} <span className="muted">({meta.unit}) {loading ? "· loading…" : ""}</span>
        </h3>
        <LineChart data={pts} unit={meta.unit} color={meta.color} height={320} band shades={shades}
          xDomain={range ?? undefined} />
        <div className="muted small" style={{ marginTop: 6 }}>
          Line = average, band = min/max. Long ranges are downsampled to at most 1,200 points. Shaded areas are alerts.
        </div>
      </section>

      <div className="grid2">
        <section className="panel">
          <h3>Summary for selected range</h3>
          {summary ? (
            <table>
              <tbody>
                <tr><th>Average</th><td className="num">{fmtNum(summary.avg)} {meta.unit}</td></tr>
                <tr><th>Minimum</th><td className="num">{fmtNum(summary.min)} {meta.unit}</td></tr>
                <tr><th>Maximum</th><td className="num">{fmtNum(summary.max)} {meta.unit}</td></tr>
                <tr><th>Avg. within-second std</th><td className="num">{fmtNum(summary.avg_std)}</td></tr>
                <tr><th>Raw samples</th><td className="num">{Number(summary.samples).toLocaleString()}</td></tr>
              </tbody>
            </table>
          ) : (
            <div className="muted">No data</div>
          )}
        </section>

        <section className="panel">
          <h3>Normal vs abnormal periods</h3>
          {windows.length === 0 ? (
            <div className="muted">No alerts overlap this range, so everything shown is normal operation.</div>
          ) : (
            <table>
              <thead>
                <tr><th>Period</th><th className="num">Avg</th><th className="num">Min</th><th className="num">Max</th><th className="num">vs normal</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>Normal</td>
                  <td className="num">{fmtNum(normal?.avg)}</td>
                  <td className="num">{fmtNum(normal?.min)}</td>
                  <td className="num">{fmtNum(normal?.max)}</td>
                  <td className="num">–</td>
                </tr>
                <tr className="hl">
                  <td>Abnormal ({windows.length} alert{windows.length > 1 ? "s" : ""})</td>
                  <td className="num">{fmtNum(abnormal?.avg)}</td>
                  <td className="num">{fmtNum(abnormal?.min)}</td>
                  <td className="num">{fmtNum(abnormal?.max)}</td>
                  <td className="num">{normal && abnormal ? fmtPct(abnormal.avg / normal.avg - 1) : "–"}</td>
                </tr>
              </tbody>
            </table>
          )}
          <div className="muted small" style={{ marginTop: 6 }}>
            Computed from the displayed 1 s points.{" "}
            {windows[0] ? `First alert window starts ${fmtDateTime(windows[0].from)}.` : ""}
          </div>
        </section>
      </div>
    </>
  );
}
