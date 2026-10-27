"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import SignalBoard from "@/components/SignalBoard";
import type { Shade } from "@/components/LineChart";
import { api, iso, Pt, toPts } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { SIGNALS } from "@/lib/signals";

type MillInfo = {
  mill_id: string;
  start: string;
  end: string;
  live: boolean; // new data arrived in the last 30 s
  hist_start: string | null; // fixed replay-history window (seeded)
  hist_end: string | null;
  live_start: string | null; // data written by a live feed
  live_end: string | null;
};
type Mode = "live" | "replay";
const WINDOW_MS = 120_000;
const ms = (s: string) => new Date(s).getTime();

const label = (m: MillInfo) =>
  m.live ? "● live" : m.hist_start || m.live_end ? "(replay)" : "(live feed stopped)";

export default function Dashboard() {
  const [mills, setMills] = useState<MillInfo[]>([]);
  const [mill, setMill] = useState("");
  const [choice, setChoice] = useState<Record<string, Mode>>({}); // user's explicit mode per mill
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = (first: boolean) =>
      api("/api/mills")
        .then((r) => {
          if (!alive) return;
          setMills(r.mills);
          setError(null);
          if (first) {
            // prefer a live mill, otherwise the first one
            const live = r.mills.find((m: MillInfo) => m.live);
            setMill((live || r.mills[0])?.mill_id || "");
          }
        })
        .catch((e) => alive && setError(e.message));
    load(true);
    const id = setInterval(() => load(false), 15_000); // keeps the live / replay labels honest
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const info = mills.find((m) => m.mill_id === mill);
  const hasLive = !!info?.live_end;
  const hasHist = !!info?.hist_start;
  const mode: Mode = !info
    ? "live"
    : choice[info.mill_id] ?? (info.live || !(hasHist || hasLive) ? "live" : "replay");

  // A mill that has been fed live replays its most recent live run; otherwise its seeded history.
  const [lastRun, setLastRun] = useState<Record<string, [number, number] | null>>({});
  useEffect(() => {
    if (!info || mode !== "replay" || !hasLive) return;
    let alive = true;
    api(`/api/mills/${info.mill_id}/last-run`)
      .then((r) => alive && setLastRun((c) => ({ ...c, [info.mill_id]: r.start ? [ms(r.start), ms(r.end)] : null })))
      .catch(() => alive && setLastRun((c) => ({ ...c, [info.mill_id]: null })));
    return () => {
      alive = false;
    };
  }, [info?.mill_id, mode, hasLive]);

  const fallbackRange: [number, number] | null = !info
    ? null
    : hasHist
    ? [ms(info.hist_start!), ms(info.hist_end!)]
    : [Math.max(ms(info.start), ms(info.end) - 15 * 60_000), ms(info.end)];
  const replayRange: [number, number] | null = !info
    ? null
    : hasLive
    ? info.mill_id in lastRun
      ? lastRun[info.mill_id] ?? fallbackRange
      : null
    : fallbackRange;

  return (
    <>
      <div className="row between">
        <div>
          <h1>Live monitoring</h1>
          <div className="muted">Select a mill and click a signal to inspect it. All times UTC.</div>
        </div>
        <label>
          Mill
          <select value={mill} onChange={(e) => setMill(e.target.value)}>
            {mills.map((m) => (
              <option key={m.mill_id} value={m.mill_id}>
                {m.mill_id} {label(m)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? <div className="error">{error}</div> : null}
      {!info && !error ? <div className="note">No mills yet. Run the seed command (see README) to load data.</div> : null}

      {info && hasLive ? (
        <div className="row" style={{ marginTop: 0 }}>
          <button
            className={mode === "live" ? "btn" : "btn ghost"}
            onClick={() => setChoice((c) => ({ ...c, [info.mill_id]: "live" }))}
          >
            ● Live feed
          </button>
          <button
            className={mode === "replay" ? "btn" : "btn ghost"}
            onClick={() => setChoice((c) => ({ ...c, [info.mill_id]: "replay" }))}
          >
            ▶ Replay history
          </button>
        </div>
      ) : null}

      {info && mode === "live" ? (
        <LiveView
          key={`${info.mill_id}-live`}
          mill={info.mill_id}
          startMs={ms(info.live_start ?? info.start)}
          feedRunning={info.live}
        />
      ) : null}
      {info && mode === "replay" && replayRange ? (
        <ReplayView key={`${info.mill_id}-replay-${replayRange[0]}-${replayRange[1]}`} mill={info.mill_id} range={replayRange} />
      ) : null}
    </>
  );
}

function StatusBar({ status, alertId, extra }: { status: string; alertId?: number | null; extra?: React.ReactNode }) {
  return (
    <>
      <div className="row between" style={{ margin: "0 0 12px" }}>
        <div>
          <span className={`badge ${status === "ALERT" ? "alert" : "ok"}`}>{status === "ALERT" ? "ALERT" : "NORMAL"}</span>{" "}
          {extra}
        </div>
      </div>
      {status === "ALERT" && alertId ? (
        <div className="banner">
          <span>Abnormal behaviour detected. Cobble signature across multiple signals.</span>
          <Link href={`/alerts/${alertId}`}>Inspect incident →</Link>
        </div>
      ) : null}
    </>
  );
}

/* One request for all six signals. */
async function loadSeries(mill: string, start: number, end: number, maxPoints: number) {
  const r = await api(`/api/mills/${mill}/series`, { start: iso(start), end: iso(end), max_points: maxPoints });
  const out: Record<string, Pt[]> = {};
  SIGNALS.forEach((s) => (out[s] = toPts(r.series?.[s] ?? [])));
  return out;
}

/* ------------------------------------------------------------------ live -- */
const WINDOWS = [
  { label: "2 min", ms: 120_000 },
  { label: "5 min", ms: 300_000 },
  { label: "15 min", ms: 900_000 },
];

function LiveView({ mill, startMs, feedRunning }: { mill: string; startMs: number; feedRunning: boolean }) {
  const [data, setData] = useState<Record<string, Pt[]>>({});
  const [status, setStatus] = useState<any>(null);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [sel, setSel] = useState("motor_current");
  const [windowMs, setWindowMs] = useState(120_000);
  const [endMs, setEndMs] = useState<number | null>(null); // null = follow live data
  const [domain, setDomain] = useState<[number, number]>([Date.now() - WINDOW_MS, Date.now()]);
  const [lagS, setLagS] = useState(0);
  const [diag, setDiag] = useState<any>(null); // built-in feed state, fetched only when data goes stale
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const inflight = useRef(false);
  const alertsAt = useRef(0);

  const load = useCallback(async () => {
    const following = endMs === null;
    if (following) {
      if (inflight.current) return;
      inflight.current = true;
    }
    const my = ++seq.current;
    try {
      const st = await api(`/api/mills/${mill}/status`);
      if (!st.latest) throw new Error("No data for this mill yet");
      const latest = ms(st.latest);
      const end = following ? latest : Math.min(endMs as number, latest);
      const start = end - windowMs;
      // alerts change rarely: refresh them every 8 s instead of every poll
      const needAlerts = Date.now() - alertsAt.current > 8_000 || !following;
      const [series, al] = await Promise.all([
        loadSeries(mill, start, end, 600),
        needAlerts ? api("/api/alerts", { mill_id: mill }) : Promise.resolve(null),
      ]);
      if (my !== seq.current) return; // a newer request superseded this one
      if (al) {
        setAlerts(al.alerts);
        alertsAt.current = Date.now();
      }
      setData(series);
      setStatus(st);
      setDomain([start, end]);
      setLagS(Math.max(0, Math.round((Date.now() - latest) / 1000)));
      if (Date.now() - latest > 20_000) api("/api/live/status").then(setDiag).catch(() => {});
      else setDiag(null);
      setError(null);
    } catch (e: any) {
      if (my === seq.current) setError(e.message);
    } finally {
      if (following) inflight.current = false;
    }
  }, [mill, endMs, windowMs]);

  useEffect(() => {
    const t = setTimeout(load, endMs === null ? 0 : 200); // small debounce while dragging the slider
    if (endMs !== null) return () => clearTimeout(t);
    const id = setInterval(load, 2000);
    return () => {
      clearTimeout(t);
      clearInterval(id);
    };
  }, [load, endMs]);

  const latestMs = status ? ms(status.latest) : 0;
  const alertWins = alerts.map((a) => ({ id: a.alert_id, from: ms(a.started_at), to: ms(a.ended_at) }));
  const lastAlert = alerts[0]; // API returns newest first
  const viewing = endMs !== null;
  const activeInView = alertWins.find((a) => domain[1] >= a.from && domain[1] <= a.to + 60_000);
  const shownStatus = viewing ? (activeInView ? "ALERT" : "NORMAL") : status?.status ?? "NORMAL";
  const shownAlertId = viewing ? activeInView?.id : status?.active_alert_id;
  const shades: Shade[] = alertWins
    .filter((w) => w.to >= domain[0] && w.from <= domain[1])
    .map((w) => ({ from: w.from, to: w.to, label: "alert" }));
  const sliderValue = endMs ?? latestMs;
  const sliderMin = Math.min(Math.max(startMs, latestMs - 15 * 60_000), sliderValue);
  const stale = !viewing && (lagS > 20 || !feedRunning) && latestMs > 0;

  return (
    <>
      {error ? <div className="error">{error}</div> : null}
      {stale ? (
        <div className="error">
          <b>
            No new data for {lagS < 120 ? `${lagS} s` : `${Math.round(lagS / 60)} min`}: the live feed for {mill} has
            stopped.
          </b>
          <div className="small" style={{ marginTop: 4 }}>
            {diag && diag.mill === mill ? (
              !diag.enabled ? (
                <>The API was started without <code>LIVE_SIM=1</code>, so no live feed is running.</>
              ) : diag.last_error ? (
                <>
                  Reason reported by the API ({diag.errors} failed batches, {diag.ok} succeeded):{" "}
                  <code>{diag.last_error}</code>
                </>
              ) : (
                <>The API's feed reports no errors (last successful write {diag.last_ok_age_s ?? "never"} s ago).</>
              )
            ) : (
              <>
                Start the API with <code>LIVE_SIM=1</code>, or switch to “Replay history”.
              </>
            )}
          </div>
        </div>
      ) : null}
      {latestMs ? (
        <div className="row">
          <button className={viewing ? "btn ghost" : "btn"} onClick={() => setEndMs(null)}>
            Follow latest
          </button>
          <label>
            Window
            <select value={windowMs} onChange={(e) => setWindowMs(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.ms} value={w.ms}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Go back in time · viewing up to {fmtDateTime(domain[1])}
            <input
              type="range"
              min={sliderMin}
              max={latestMs}
              step={1000}
              value={sliderValue}
              onChange={(e) => {
                const v = Number(e.target.value);
                setEndMs(v >= latestMs - 1500 ? null : v);
              }}
            />
          </label>
          {lastAlert ? (
            <button
              className="btn ghost"
              onClick={() => setEndMs(Math.min(ms(lastAlert.ended_at) + 30_000, latestMs))}
            >
              Jump to last alert
            </button>
          ) : null}
        </div>
      ) : null}
      {status ? (
        <StatusBar
          status={shownStatus}
          alertId={shownAlertId}
          extra={
            <span className="muted">
              {viewing
                ? "viewing past data, not updating. Click “Follow latest” to resume."
                : `latest sample ${fmtDateTime(status.latest)} · refreshing every 2 s`}
              {lastAlert ? (
                <>
                  {" "}
                  · last alert {fmtDateTime(lastAlert.started_at)}{" "}
                  <Link href={`/alerts/${lastAlert.alert_id}`}>inspect</Link>
                </>
              ) : null}
            </span>
          }
        />
      ) : null}
      <SignalBoard data={data} selected={sel} onSelect={setSel} shades={shades} xDomain={domain} />
    </>
  );
}

/* ---------------------------------------------------------------- replay -- */
function ReplayView({ mill, range }: { mill: string; range: [number, number] }) {
  const [start, end] = range;
  const [full, setFull] = useState<Record<string, Pt[]>>({});
  const [alerts, setAlerts] = useState<any[]>([]);
  const [cursor, setCursor] = useState(Math.min(start + WINDOW_MS, end));
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(10);
  const [sel, setSel] = useState("motor_current");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([api("/api/alerts", { mill_id: mill }), loadSeries(mill, start, end, 1000)])
      .then(([al, series]) => {
        if (!alive) return;
        setFull(series);
        setAlerts(al.alerts);
        setError(null);
      })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [mill, start, end]);

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      setCursor((c) => {
        const n = c + speed * 200;
        if (n >= end) {
          setPlaying(false);
          return end;
        }
        return n;
      });
    }, 200);
    return () => clearInterval(id);
  }, [playing, speed, end]);

  const lo = cursor - WINDOW_MS;
  const windowed = useMemo(() => {
    const out: Record<string, Pt[]> = {};
    SIGNALS.forEach((s) => (out[s] = (full[s] || []).filter((p) => p.t >= lo && p.t <= cursor)));
    return out;
  }, [full, lo, cursor]);

  // only the alerts that belong to this replay window
  const alertTimes = alerts
    .map((a) => ({ id: a.alert_id, from: ms(a.started_at), to: ms(a.ended_at) }))
    .filter((a) => a.to >= start && a.from <= end);
  const active = alertTimes.find((a) => cursor >= a.from && cursor <= a.to + 60_000);
  const shades: Shade[] = alertTimes
    .filter((a) => a.from <= cursor && a.to >= lo)
    .map((a) => ({ from: a.from, to: Math.min(a.to, cursor), label: "alert" }));
  const firstAlert = alertTimes.slice().sort((a, b) => a.from - b.from)[0];

  return (
    <>
      <div className="note">
        <b>{mill}</b> replay: stored data from {fmtDateTime(start)} to {fmtDateTime(end)} played back through the same
        dashboard. Press play, or jump to just before the cobble.
      </div>
      {error ? <div className="error">{error}</div> : null}
      {loading ? <div className="muted">Loading history…</div> : null}
      <div className="row">
        <button className="btn" onClick={() => setPlaying((p) => !p)}>
          {playing ? "Pause" : cursor >= end ? "Restart" : "Play"}
        </button>
        <label>
          Speed
          <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
            {[1, 5, 10, 20, 50].map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
        <label>
          Position · {fmtDateTime(cursor)}
          <input
            type="range"
            min={Math.min(start + 10_000, end)}
            max={end}
            step={1000}
            value={cursor}
            onChange={(e) => setCursor(Number(e.target.value))}
          />
        </label>
        {firstAlert ? (
          <button
            className="btn ghost"
            onClick={() => {
              setCursor(Math.max(firstAlert.from - 40_000, start + 10_000));
              setPlaying(true);
            }}
          >
            Jump to before cobble
          </button>
        ) : null}
      </div>
      <StatusBar status={active ? "ALERT" : "NORMAL"} alertId={active?.id} />
      <SignalBoard data={windowed} selected={sel} onSelect={setSel} shades={shades} xDomain={[lo, cursor]} />
    </>
  );
}
