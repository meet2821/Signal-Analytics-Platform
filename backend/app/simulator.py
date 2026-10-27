"""Synthetic rolling-mill PLC signal generator.

Produces long-format readings (mill_id, signal, ts, value, unit) at a 20 ms
sampling interval (50 Hz) for six correlated signals.

Scenario timeline (reproducible via `seed`):
  * normal operation: noisy, slowly varying signals
  * LEGITIMATE changes (must NOT alert):
      - speed setpoint step (+10 %) ramped over 20 s, current/torque follow
      - load increase (heavier slab) ramped over 15 s, force/current/torque rise
  * ABNORMAL: a cobble. Within ~1 s the strip jams: rolling force, current and
    torque surge, speed collapses, vibration spikes, temperature kicks up.
    It lasts ~6 s, then the mill is stopped and signals decay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

SAMPLE_INTERVAL_S = 0.020  # 20 ms -> 50 samples / second / signal
HZ = 50

SIGNALS: dict[str, str] = {
    "motor_current": "A",
    "motor_speed": "rpm",
    "torque": "kNm",
    "vibration": "mm/s",
    "rolling_force": "kN",
    "temperature": "C",
}


@dataclass
class Scenario:
    """Event times are seconds from the start of the generated window."""

    duration_s: int = 600
    speed_step_at_s: float = 150.0   # legit: speed setpoint +10 %, ramp 20 s
    load_step_at_s: float = 300.0    # legit: heavier slab, ramp 15 s
    cobble_at_s: float = 450.0       # abnormal event
    cobble_duration_s: float = 6.0
    seed: int = 42
    # Optional ramp-backs (None = stay changed). Used by the looping live feed so
    # the cycle ends in the same operating state it starts in.
    speed_back_at_s: float | None = None
    load_back_at_s: float | None = None
    extra: dict = field(default_factory=dict)


def _ramp(t: np.ndarray, start: float, length: float) -> np.ndarray:
    """0 before start, linear to 1 over `length` seconds, 1 after."""
    return np.clip((t - start) / length, 0.0, 1.0)


def generate(
    start: datetime,
    scenario: Scenario | None = None,
    mill_id: str = "RM-01",
) -> pd.DataFrame:
    sc = scenario or Scenario()
    rng = np.random.default_rng(sc.seed)
    n = int(sc.duration_s * HZ)
    t = np.arange(1, n + 1) * SAMPLE_INTERVAL_S  # seconds since start

    # ---- operating condition (latent drivers) -------------------------------
    speed_up = _ramp(t, sc.speed_step_at_s, 20.0)
    load_up = _ramp(t, sc.load_step_at_s, 15.0)
    if sc.speed_back_at_s is not None:
        speed_up = speed_up - _ramp(t, sc.speed_back_at_s, 20.0)
    if sc.load_back_at_s is not None:
        load_up = load_up - _ramp(t, sc.load_back_at_s, 15.0)
    speed_cmd = 1.0 + 0.10 * speed_up
    load = 1.0 + 0.18 * load_up
    # slow natural wander (smooth random walk, per-second resolution)
    wander = np.cumsum(rng.normal(0, 1, n)) * 0.0006
    wander = wander - np.convolve(wander, np.ones(500) / 500, mode="same")
    wander = np.clip(wander, -0.02, 0.02)

    # ---- cobble envelope: fast attack, plateau, decay ------------------------
    c0, c1 = sc.cobble_at_s, sc.cobble_at_s + sc.cobble_duration_s
    attack = _ramp(t, c0, 0.8)
    decay = 1.0 - _ramp(t, c1, 2.0)
    cobble = np.clip(attack * decay, 0.0, 1.0)

    def noise(scale: float) -> np.ndarray:
        # mildly autocorrelated noise, looks like real sensor jitter
        w = rng.normal(0, scale, n)
        return 0.6 * w + 0.4 * np.roll(w, 1)

    # ---- signals ------------------------------------------------------------
    speed = 300.0 * speed_cmd * (1 + wander) * (1 - 0.40 * cobble) + noise(0.6)
    current = (
        120.0 * (0.55 * speed_cmd + 0.45 * load) * (1 + wander)
        * (1 + 0.65 * cobble)
        + noise(0.9)
    )
    torque = 0.82 * current * (1 + 0.03 * cobble) + noise(0.4)
    force = 1800.0 * load * (1 + 0.5 * (speed_cmd - 1)) * (1 + 0.85 * cobble) + noise(9.0)
    vib = (
        2.0 * speed_cmd * (1 + 0.5 * (load - 1)) * (1 + 0.1 * wander)
        + 6.0 * cobble
        + np.abs(noise(0.12))
        + 3.0 * cobble * np.abs(rng.normal(0, 1, n))
    )
    temp_base = 880.0 + 25.0 * (load - 1) + 40.0 * np.cumsum(np.full(n, 1.0)) / n * 0.0
    temp = temp_base + 30.0 * cobble + np.cumsum(rng.normal(0, 0.01, n)) + noise(0.15)

    values = {
        "motor_current": current,
        "motor_speed": speed,
        "torque": torque,
        "vibration": vib,
        "rolling_force": force,
        "temperature": temp,
    }

    ts = pd.to_datetime(
        [start + timedelta(seconds=float(x)) for x in t], utc=True
    )
    frames = [
        pd.DataFrame(
            {
                "mill_id": mill_id,
                "signal": name,
                "ts": ts,
                "value": np.round(vals, 4),
                "unit": SIGNALS[name],
                "quality": 0,
            }
        )
        for name, vals in values.items()
    ]
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["ts", "signal"], kind="stable").reset_index(drop=True)


def scenario_truth(start: datetime, scenario: Scenario | None = None) -> dict:
    """Ground-truth event times, used by tests and the README demo."""
    sc = scenario or Scenario()
    s = lambda x: start + timedelta(seconds=x)  # noqa: E731
    return {
        "speed_step": s(sc.speed_step_at_s),
        "load_step": s(sc.load_step_at_s),
        "cobble_start": s(sc.cobble_at_s),
        "cobble_end": s(sc.cobble_at_s + sc.cobble_duration_s),
    }


def default_start() -> datetime:
    """Fixed start so the demo dataset is fully reproducible."""
    return datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
