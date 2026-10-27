"""Silver rollups and gold anomaly detection.

Detection approach: multi-signal, change-based, directional.

A cobble is a *fast, coordinated* excursion: current/torque/force surge,
speed collapses and vibration/temperature rise, all within a few seconds.
Legitimate load or speed changes are *slow ramps*, so they move signals far
less over the same 3 s horizon. We therefore flag a signal at second t only if
BOTH hold:

  1. statistical test: the 3 s relative change is a robust outlier (|z| >= Z_MIN)
     versus that signal's own trailing 120 s change distribution
     (median / MAD, lagged 5 s so the event never pollutes its own baseline);
  2. practical gate: the change is large enough, in the cobble direction
     (e.g. current +15 %, speed -12 %).

An alert needs >= MIN_SIGNALS signals flagged in the same second. Consecutive
flagged seconds merge into one incident.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

HORIZON_S = 3
BASELINE_S = 120
BASELINE_LAG_S = 5
Z_MIN = 6.0
MIN_SIGNALS = 3
MERGE_GAP_S = 4

# signal -> (direction, minimum relative change)
RULES: dict[str, tuple[int, float]] = {
    "motor_current": (+1, 0.15),
    "torque": (+1, 0.15),
    "rolling_force": (+1, 0.20),
    "motor_speed": (-1, 0.12),
    "vibration": (+1, 0.50),
    "temperature": (+1, 0.015),
}


def rollup_1s(raw: pd.DataFrame) -> pd.DataFrame:
    """Silver layer: 1 s aggregates per (mill_id, signal)."""
    df = raw.copy()
    df["bucket"] = df["ts"].dt.floor("1s")
    out = (
        df.groupby(["mill_id", "signal", "bucket"])["value"]
        .agg(avg="mean", min="min", max="max", std="std", n="count")
        .reset_index()
    )
    out["std"] = out["std"].fillna(0.0)
    return out


def _robust_scale(s: pd.Series) -> pd.Series:
    """Trailing, lagged median/MAD-based sigma of a series."""
    def mad_sigma(x: np.ndarray) -> float:
        x = x[~np.isnan(x)]  # missing seconds must not poison the baseline
        if x.size < 30:
            return np.nan
        med = np.median(x)
        return 1.4826 * np.median(np.abs(x - med))

    sigma = (
        s.rolling(BASELINE_S, min_periods=30).apply(mad_sigma, raw=True)
    ).shift(BASELINE_LAG_S)
    med = s.rolling(BASELINE_S, min_periods=30).median().shift(BASELINE_LAG_S)
    return med, sigma


@dataclass
class Alert:
    mill_id: str
    rule: str
    severity: str
    started_at: pd.Timestamp
    ended_at: pd.Timestamp
    reason: str
    signals: list[dict]


def detect_cobble(silver: pd.DataFrame, mill_id: str) -> list[Alert]:
    s = silver[silver["mill_id"] == mill_id]
    wide = s.pivot(index="bucket", columns="signal", values="avg").sort_index()
    wide = wide.asfreq("1s")  # make gaps explicit
    wide = wide.interpolate(limit=3, limit_area="inside")  # bridge short gaps (late/dropped seconds)
    present = [c for c in RULES if c in wide.columns]
    if len(present) < MIN_SIGNALS:
        return []

    flags = pd.DataFrame(False, index=wide.index, columns=present)
    zs = pd.DataFrame(0.0, index=wide.index, columns=present)
    rel = pd.DataFrame(0.0, index=wide.index, columns=present)

    for sig in present:
        direction, gate = RULES[sig]
        x = wide[sig]
        change = (x - x.shift(HORIZON_S)) / x.shift(HORIZON_S).abs()
        med, sigma = _robust_scale(change)
        sigma = sigma.clip(lower=1e-4)
        z = (change - med) / sigma
        rel[sig] = change
        zs[sig] = z
        flags[sig] = (
            (direction * z >= Z_MIN) & (direction * change >= gate)
        ).fillna(False)

    hit = flags.sum(axis=1) >= MIN_SIGNALS
    hit_times = list(hit.index[hit])
    if not hit_times:
        return []

    # merge consecutive flagged seconds into incidents
    groups: list[list[pd.Timestamp]] = [[hit_times[0]]]
    for t in hit_times[1:]:
        if (t - groups[-1][-1]).total_seconds() <= MERGE_GAP_S:
            groups[-1].append(t)
        else:
            groups.append([t])

    alerts: list[Alert] = []
    for g in groups:
        first, last = g[0], g[-1]
        # first second whose value already differs from the baseline 3 s earlier
        onset = first
        window = flags.loc[first:last]
        affected = [c for c in present if window[c].any()]
        base_win = wide.loc[onset - pd.Timedelta(seconds=30): onset - pd.Timedelta(seconds=1)]
        evidence = []
        for sig in affected:
            direction, _ = RULES[sig]
            seg = wide.loc[onset:last + pd.Timedelta(seconds=HORIZON_S), sig]
            peak = seg.max() if direction > 0 else seg.min()
            base = float(base_win[sig].median()) if len(base_win) else float("nan")
            peak_z = float(zs.loc[first:last, sig].abs().max())
            evidence.append(
                {
                    "signal": sig,
                    "peak_z": round(peak_z, 1),
                    "peak_value": round(float(peak), 2),
                    "baseline_value": round(base, 2),
                }
            )
        parts = [
            f"{e['signal']} {e['baseline_value']:g} -> {e['peak_value']:g} "
            f"({(e['peak_value'] / e['baseline_value'] - 1) * 100:+.0f}%)"
            for e in evidence
            if e["baseline_value"]
        ]
        reason = (
            f"Cobble signature: {len(affected)} signals moved sharply in the "
            f"same {HORIZON_S}s window (current/torque/force up, speed down, "
            f"vibration up). " + "; ".join(parts)
        )
        severity = "critical" if len(affected) >= 5 else "warning"
        alerts.append(
            Alert(
                mill_id=mill_id,
                rule="cobble_signature",
                severity=severity,
                started_at=onset,
                ended_at=last + pd.Timedelta(seconds=1),
                reason=reason,
                signals=evidence,
            )
        )
    return alerts
