"use client";
import type { Pt } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { SIGNALS, SIGNAL_META } from "@/lib/signals";
import LineChart, { Shade } from "./LineChart";

/** Six signal cards (latest value + sparkline) and one large chart for the selected signal. */
export default function SignalBoard({
  data,
  selected,
  onSelect,
  shades,
  xDomain,
}: {
  data: Record<string, Pt[]>;
  selected: string;
  onSelect: (s: string) => void;
  shades: Shade[];
  xDomain: [number, number];
}) {
  const meta = SIGNAL_META[selected];
  return (
    <>
      <div className="cards">
        {SIGNALS.map((s) => {
          const pts = data[s] || [];
          const last = pts.length ? pts[pts.length - 1].avg : null;
          return (
            <button
              key={s}
              className={`card sig ${selected === s ? "sel" : ""}`}
              onClick={() => onSelect(s)}
              style={{ borderTopColor: SIGNAL_META[s].color }}
            >
              <div className="muted small">{SIGNAL_META[s].label}</div>
              <div className="val">
                {fmtNum(last)} <span className="unit">{SIGNAL_META[s].unit}</span>
              </div>
              <LineChart data={pts} color={SIGNAL_META[s].color} height={38} compact xDomain={xDomain} shades={shades} />
            </button>
          );
        })}
      </div>
      <section className="panel">
        <h3>
          {meta.label} <span className="muted">({meta.unit}) · 1 s average with min/max band</span>
        </h3>
        <LineChart
          data={data[selected] || []}
          unit={meta.unit}
          color={meta.color}
          height={300}
          band
          xDomain={xDomain}
          shades={shades}
        />
      </section>
    </>
  );
}
