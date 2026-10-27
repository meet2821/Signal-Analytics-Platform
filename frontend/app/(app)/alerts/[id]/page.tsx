"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import LineChart, { Shade } from "@/components/LineChart";
import { api, Pt, toPts } from "@/lib/api";
import { fmtDateTime, fmtNum, fmtPct } from "@/lib/format";
import { SIGNALS, SIGNAL_META } from "@/lib/signals";

export default function IncidentPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  const [inc, setInc] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [rawSignal, setRawSignal] = useState("motor_current");
  const [raw, setRaw] = useState<Pt[] | null>(null);
  const [rawLoading, setRawLoading] = useState(false);

  useEffect(() => {
    if (!id) return;
    api(`/api/alerts/${id}/incident`, { pad_s: 90 })
      .then(setInc)
      .catch((e) => setError(e.message));
  }, [id]);

  async function loadRaw() {
    setRawLoading(true);
    try {
      const r = await api(`/api/alerts/${id}/raw/${rawSignal}`);
      setRaw(r.raw.map((p: any) => ({ t: new Date(p.t).getTime(), avg: p.v })));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRawLoading(false);
    }
  }

  if (error) return <div className="error">{error}</div>;
  if (!inc) return <div className="muted">Loading incident…</div>;

  const a = inc.alert;
  const from = new Date(a.started_at).getTime();
  const to = new Date(a.ended_at).getTime();
  const domain: [number, number] = [new Date(inc.window.start).getTime(), new Date(inc.window.end).getTime()];
  const shade: Shade[] = [{ from, to, label: "abnormal" }];
  const affected = new Set<string>(a.signals.map((s: any) => s.signal));

  return (
    <>
      <p>
        <Link href="/alerts">← Alert history</Link>
      </p>
      <div className="row between" style={{ marginTop: 0 }}>
        <div>
          <h1>
            Incident #{a.alert_id} <span className={`badge ${a.severity}`}>{a.severity.toUpperCase()}</span>
          </h1>
          <div className="muted">
            {a.mill_id} · {fmtDateTime(from)} → {fmtDateTime(to)} · {Math.round((to - from) / 1000)} s · rule{" "}
            <span className="mono">{a.rule}</span>
          </div>
        </div>
      </div>

      <section className="panel">
        <h3>Why this alert fired</h3>
        <div>{a.reason}</div>
      </section>

      <section className="panel">
        <h3>Normal vs abnormal, per signal</h3>
        <table>
          <thead>
            <tr>
              <th>Signal</th>
              <th className="num">Normal avg</th>
              <th className="num">Abnormal avg</th>
              <th className="num">Change</th>
              <th className="num">Peak z-score</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {SIGNALS.map((s) => {
              const n = inc.signals[s].normal.avg;
              const ab = inc.signals[s].abnormal.avg;
              const ev = a.signals.find((x: any) => x.signal === s);
              return (
                <tr key={s} className={affected.has(s) ? "hl" : ""}>
                  <td>{SIGNAL_META[s].label}</td>
                  <td className="num">{fmtNum(n)} {SIGNAL_META[s].unit}</td>
                  <td className="num">{fmtNum(ab)} {SIGNAL_META[s].unit}</td>
                  <td className="num">{n && ab ? fmtPct(ab / n - 1) : "–"}</td>
                  <td className="num">{ev ? ev.peak_z : "–"}</td>
                  <td>{affected.has(s) ? <span className="badge alert">triggered</span> : <span className="muted small">not flagged</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="muted small" style={{ marginTop: 6 }}>
          Normal = the {Math.round((from - domain[0]) / 1000) - 5} s before the incident; abnormal = the incident window.
        </div>
      </section>

      <div className="grid2">
        {SIGNALS.map((s) => (
          <section className="panel" key={s}>
            <h3>
              {SIGNAL_META[s].label} <span className="muted">({SIGNAL_META[s].unit})</span>{" "}
              {affected.has(s) ? <span className="badge alert">triggered</span> : null}
            </h3>
            <LineChart
              data={toPts(inc.signals[s].series)}
              unit={SIGNAL_META[s].unit}
              color={SIGNAL_META[s].color}
              height={180}
              band
              xDomain={domain}
              shades={shade}
              refLine={inc.signals[s].normal.avg != null ? { y: inc.signals[s].normal.avg, label: "normal avg" } : undefined}
            />
          </section>
        ))}
      </div>

      <section className="panel">
        <h3>Raw 20 ms samples</h3>
        <div className="row" style={{ marginTop: 0 }}>
          <label>
            Signal
            <select value={rawSignal} onChange={(e) => { setRawSignal(e.target.value); setRaw(null); }}>
              {SIGNALS.map((s) => (
                <option key={s} value={s}>{SIGNAL_META[s].label}</option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={loadRaw} disabled={rawLoading}>
            {rawLoading ? "Loading…" : "Load raw data"}
          </button>
        </div>
        {raw ? (
          raw.length ? (
            <LineChart data={raw} unit={SIGNAL_META[rawSignal].unit} color={SIGNAL_META[rawSignal].color} height={240} shades={shade} />
          ) : (
            <div className="muted">Raw samples for this incident are no longer retained (live mills keep raw data for a short time only).</div>
          )
        ) : (
          <div className="muted small">Fetches the bronze-layer samples around the incident (up to 6,000 points).</div>
        )}
      </section>
    </>
  );
}
