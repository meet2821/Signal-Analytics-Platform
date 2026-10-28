"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Pt } from "@/lib/api";
import { fmtNum, fmtTime } from "@/lib/format";

export type Shade = { from: number; to: number; color?: string; label?: string };

type Props = {
  data: Pt[];
  unit?: string;
  color?: string;
  height?: number;
  band?: boolean; // min/max envelope
  shades?: Shade[]; // highlighted time ranges (alerts)
  xDomain?: [number, number];
  refLine?: { y: number; label?: string }; // e.g. normal average
  playhead?: number;
  compact?: boolean; // sparkline: no axes
  onPan?: (deltaMs: number) => void; // drag or scroll over the chart to move through time
};

function niceTicks(lo: number, hi: number, n = 4): number[] {
  const span = hi - lo || 1;
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

export default function LineChart({
  data,
  unit,
  color = "#2dd4bf",
  height = 220,
  band = false,
  shades = [],
  xDomain,
  refLine,
  playhead,
  compact = false,
  onPan,
}: Props) {
  const H = height;
  const m = compact ? { l: 4, r: 4, t: 4, b: 4 } : { l: 56, r: 14, t: 10, b: 24 };
  const [hover, setHover] = useState<number | null>(null);
  const [W, setW] = useState(800); // real pixel width so text is never stretched
  const boxRef = useRef<HTMLDivElement>(null);
  const dragX = useRef<number | null>(null);
  const panRef = useRef(onPan);
  panRef.current = onPan;
  const spanRef = useRef(1);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const measure = () => setW(Math.max(120, Math.round(el.clientWidth)));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const g = useMemo(() => {
    const pts = data.filter((p) => p && Number.isFinite(p.avg));
    const x0 = xDomain ? xDomain[0] : pts.length ? pts[0].t : 0;
    const x1 = xDomain ? xDomain[1] : pts.length ? pts[pts.length - 1].t : 1;
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of pts) {
      lo = Math.min(lo, band && p.min !== undefined ? p.min : p.avg);
      hi = Math.max(hi, band && p.max !== undefined ? p.max : p.avg);
    }
    if (refLine) {
      lo = Math.min(lo, refLine.y);
      hi = Math.max(hi, refLine.y);
    }
    if (!Number.isFinite(lo)) {
      lo = 0;
      hi = 1;
    }
    if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    }
    const pad = (hi - lo) * 0.08;
    lo -= pad;
    hi += pad;
    const sx = (t: number) => m.l + ((t - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
    const sy = (v: number) => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);

    // break the line across gaps (> 3x the median step)
    const steps = pts.slice(1).map((p, i) => p.t - pts[i].t).sort((a, b) => a - b);
    const gap = (steps.length ? steps[Math.floor(steps.length / 2)] : 1000) * 3;
    let d = "";
    pts.forEach((p, i) => {
      const cmd = i === 0 || p.t - pts[i - 1].t > gap ? "M" : "L";
      d += `${cmd}${sx(p.t).toFixed(1)},${sy(p.avg).toFixed(1)}`;
    });
    let area = "";
    if (band && pts.length > 1) {
      const up = pts.map((p) => `${sx(p.t).toFixed(1)},${sy(p.max ?? p.avg).toFixed(1)}`);
      const dn = pts
        .slice()
        .reverse()
        .map((p) => `${sx(p.t).toFixed(1)},${sy(p.min ?? p.avg).toFixed(1)}`);
      area = `M${up.join("L")}L${dn.join("L")}Z`;
    }
    return { pts, x0, x1, lo, hi, sx, sy, d, area };
  }, [data, band, xDomain, refLine, H, compact, W]);

  spanRef.current = g.x1 - g.x0;
  const PLOT_PAD = 70; // left + right margins of the plot area

  // wheel / trackpad scroll over the chart moves time (non-passive so the page does not scroll too)
  useEffect(() => {
    const el = boxRef.current;
    if (!el || compact || !onPan) return;
    const h = (e: WheelEvent) => {
      const cb = panRef.current;
      if (!cb) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * unit;
      cb((d * spanRef.current) / Math.max(1, el.clientWidth - PLOT_PAD));
    };
    el.addEventListener("wheel", h, { passive: false });
    return () => el.removeEventListener("wheel", h);
  }, [compact, !!onPan]);

  const onDown = (e: React.PointerEvent) => {
    if (compact || !onPan) return;
    dragX.current = e.clientX;
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setHover(null);
  };
  const endDrag = () => {
    dragX.current = null;
  };

  const onMove = (e: React.PointerEvent) => {
    if (dragX.current !== null && panRef.current && boxRef.current) {
      const dx = e.clientX - dragX.current;
      dragX.current = e.clientX;
      panRef.current((-dx * spanRef.current) / Math.max(1, boxRef.current.clientWidth - PLOT_PAD));
      return;
    }
    if (compact || !g.pts.length || !boxRef.current) return;
    const r = boxRef.current.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = g.x0 + ((px - m.l) / (W - m.l - m.r)) * (g.x1 - g.x0);
    let best = 0;
    let bd = Infinity;
    g.pts.forEach((p, i) => {
      const dd = Math.abs(p.t - t);
      if (dd < bd) {
        bd = dd;
        best = i;
      }
    });
    setHover(best);
  };

  const hp = hover !== null ? g.pts[hover] : null;
  const yTicks = compact ? [] : niceTicks(g.lo, g.hi);
  const xTicks = compact ? [] : [0, 0.25, 0.5, 0.75, 1].map((f) => g.x0 + f * (g.x1 - g.x0));

  return (
    <div
      ref={boxRef}
      className={`chart${onPan && !compact ? " pannable" : ""}`}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onMouseLeave={() => setHover(null)}
    >
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img">
        {shades.map((s, i) => {
          const a = Math.max(g.sx(s.from), m.l);
          const b = Math.min(g.sx(s.to), W - m.r);
          if (b <= a) return null;
          return (
            <g key={i}>
              <rect x={a} y={m.t} width={b - a} height={H - m.t - m.b} fill={s.color || "var(--alert)"} opacity={0.16} />
              {!compact && s.label ? (
                <text x={a + 4} y={m.t + 12} fontSize={11} fill={s.color || "var(--alert)"}>
                  {s.label}
                </text>
              ) : null}
            </g>
          );
        })}
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={m.l} x2={W - m.r} y1={g.sy(v)} y2={g.sy(v)} stroke="var(--grid)" />
            <text x={m.l - 6} y={g.sy(v) + 4} fontSize={11} textAnchor="end" fill="var(--muted)">
              {fmtNum(v)}
            </text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text
            key={i}
            x={g.sx(t)}
            y={H - 6}
            fontSize={11}
            textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"}
            fill="var(--muted)"
          >
            {fmtTime(t)}
          </text>
        ))}
        {refLine ? (
          <g>
            <line
              x1={m.l}
              x2={W - m.r}
              y1={g.sy(refLine.y)}
              y2={g.sy(refLine.y)}
              stroke="var(--muted)"
              strokeDasharray="4 4"
            />
            {!compact && refLine.label ? (
              <text x={W - m.r - 4} y={g.sy(refLine.y) - 4} fontSize={11} textAnchor="end" fill="var(--muted)">
                {refLine.label}
              </text>
            ) : null}
          </g>
        ) : null}
        {g.area ? <path d={g.area} fill={color} opacity={0.14} /> : null}
        <path d={g.d} fill="none" stroke={color} strokeWidth={compact ? 1.5 : 2} vectorEffect="non-scaling-stroke" />
        {playhead !== undefined ? (
          <line x1={g.sx(playhead)} x2={g.sx(playhead)} y1={m.t} y2={H - m.b} stroke="var(--text)" opacity={0.6} />
        ) : null}
        {hp ? (
          <g>
            <line x1={g.sx(hp.t)} x2={g.sx(hp.t)} y1={m.t} y2={H - m.b} stroke="var(--muted)" />
            <circle cx={g.sx(hp.t)} cy={g.sy(hp.avg)} r={4} fill={color} />
          </g>
        ) : null}
      </svg>
      {hp ? (
        <div className="tip" style={{ left: `${(g.sx(hp.t) / W) * 100}%` }}>
          <div>{fmtTime(hp.t)} IST</div>
          <b>
            {fmtNum(hp.avg)} {unit}
          </b>
          {band && hp.min !== undefined ? (
            <div className="muted">
              {fmtNum(hp.min)} – {fmtNum(hp.max)}
            </div>
          ) : null}
        </div>
      ) : null}
      {!g.pts.length ? <div className="chart-empty">No data in this range</div> : null}
    </div>
  );
}
