"""Looping live feed: replays a 300 s synthetic cycle in real time.

The content of every emitted second is a function of its TIMESTAMP (seconds since the
first call, modulo the cycle length), not of how many times we were called. So if the
database is slow and some seconds are skipped, the data has an honest gap instead of a
compressed timeline that could make a normal load change look like a cobble.

Each second holds exactly 50 samples per signal (x.00, x.02, ... x.98). The cycle
contains two legitimate operating changes and one cobble, so a deployed demo keeps
producing live data AND a fresh incident every 5 minutes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .simulator import SAMPLE_INTERVAL_S, Scenario, generate

CYCLE_S = 300
LIVE_SCENARIO = Scenario(
    duration_s=CYCLE_S,
    speed_step_at_s=40.0, speed_back_at_s=100.0,   # legit speed change and return
    load_step_at_s=120.0, load_back_at_s=165.0,    # legit load change and return
    cobble_at_s=200.0, cobble_duration_s=6.0,      # abnormal event
    seed=7,
)


class LiveFeed:
    def __init__(self, mill_id: str = "RM-03", scenario: Scenario = LIVE_SCENARIO):
        self.mill_id = mill_id
        self._epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cycle = generate(self._epoch, scenario, mill_id=mill_id)
        # generate() stamps samples 0.02 .. 300.00 s; move them to 0.00 .. 299.98 so that
        # every whole second contains exactly 50 samples.
        cycle["ts"] = cycle["ts"] - pd.Timedelta(seconds=SAMPLE_INTERVAL_S)
        sec = ((cycle["ts"] - pd.Timestamp(self._epoch)).dt.total_seconds() // 1).astype(int)
        self._chunks = {i: g.copy() for i, g in cycle.groupby(sec)}
        self._n = scenario.duration_s
        self._origin: datetime | None = None

    def next_second(self, now: datetime | None = None) -> pd.DataFrame:
        """The one-second chunk for wall-clock second `now`, timestamps shifted onto it."""
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        if self._origin is None:
            self._origin = now
        idx = int((now - self._origin).total_seconds()) % self._n
        chunk = self._chunks[idx].copy()
        shift = pd.Timestamp(now) - pd.Timestamp(self._epoch + timedelta(seconds=idx))
        chunk["ts"] = chunk["ts"] + shift
        return chunk.reset_index(drop=True)
