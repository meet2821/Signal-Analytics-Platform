"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { SIGNAL_META } from "@/lib/signals";

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<any[]>([]);
  const [mills, setMills] = useState<string[]>([]);
  const [mill, setMill] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api("/api/mills")
      .then((r) => setMills(r.mills.map((m: any) => m.mill_id)))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api("/api/alerts", { mill_id: mill || undefined })
        .then((r) => {
          if (!alive) return;
          setAlerts(r.alerts);
          setError(null);
          setLoaded(true);
        })
        .catch((e) => alive && setError(e.message));
    load();
    const id = setInterval(load, 10_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [mill]);

  return (
    <>
      <div className="row between">
        <div>
          <h1>Alert history</h1>
          <div className="muted">Every detected incident, newest first. Open one to compare it with normal operation.</div>
        </div>
        <label>
          Mill
          <select value={mill} onChange={(e) => setMill(e.target.value)}>
            <option value="">All mills</option>
            {mills.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? <div className="error">{error}</div> : null}
      <section className="panel">
        <table>
          <thead>
            <tr>
              <th>Started (IST)</th>
              <th>Mill</th>
              <th>Severity</th>
              <th>Affected signals</th>
              <th>Reason</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((a) => (
              <tr key={a.alert_id}>
                <td className="mono">{fmtDateTime(a.started_at)}</td>
                <td>{a.mill_id}</td>
                <td>
                  <span className={`badge ${a.severity}`}>{a.severity.toUpperCase()}</span>
                </td>
                <td>
                  <div className="chips">
                    {a.signals.map((s: any) => (
                      <span className="chip" key={s.signal}>
                        {SIGNAL_META[s.signal]?.label ?? s.signal}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="small" style={{ maxWidth: 420 }}>
                  {a.reason.length > 150 ? a.reason.slice(0, 150) + "…" : a.reason}
                </td>
                <td>
                  <Link href={`/alerts/${a.alert_id}`}>Inspect →</Link>
                </td>
              </tr>
            ))}
            {loaded && alerts.length === 0 ? (
              <tr>
                <td colSpan={6} className="muted">
                  No alerts yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>
    </>
  );
}
