from datetime import datetime, timedelta, timezone

import pandas as pd

from app.detection import detect_cobble, rollup_1s
from app.live import CYCLE_S, LiveFeed


def _feed_seconds(n):
    feed = LiveFeed("RM-03")
    t0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
    return pd.concat(
        [feed.next_second(t0 + timedelta(seconds=i)) for i in range(n)], ignore_index=True
    )


def test_live_chunks_are_one_second_of_50hz_data_stamped_to_wall_clock():
    raw = _feed_seconds(5)
    assert raw["ts"].min() >= pd.Timestamp("2026-10-26 10:00:00", tz="UTC")
    assert raw["ts"].max() < pd.Timestamp("2026-10-26 10:00:05", tz="UTC")
    per_signal = raw.groupby("signal").size()
    assert (per_signal >= 5 * 50 - 1).all()


def test_each_live_cycle_raises_exactly_one_alert_and_the_loop_seam_is_quiet():
    raw = _feed_seconds(CYCLE_S * 3)
    alerts = detect_cobble(rollup_1s(raw), "RM-03")
    assert len(alerts) == 3  # one cobble per cycle, nothing at the wrap or on legit steps


def test_detection_survives_missing_seconds():
    """Regression: dropped 1 s buckets (scheduling jitter, lost batches) must not blind the detector."""
    feed = LiveFeed("RM-03")
    t0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
    chunks = []
    for i in range(CYCLE_S * 2):
        c = feed.next_second(t0 + timedelta(seconds=i))
        # drop ~3 % of seconds, but never the cobble attack itself
        if i % 31 == 7 and not (195 <= i % CYCLE_S <= 215):
            continue
        chunks.append(c)
    alerts = detect_cobble(rollup_1s(pd.concat(chunks, ignore_index=True)), "RM-03")
    assert len(alerts) == 2


def test_detection_survives_a_gap_right_before_the_cobble():
    feed = LiveFeed("RM-03")
    t0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
    chunks = [feed.next_second(t0 + timedelta(seconds=i)) for i in range(CYCLE_S)]
    del chunks[198]  # lose a second just before the attack
    alerts = detect_cobble(rollup_1s(pd.concat(chunks, ignore_index=True)), "RM-03")
    assert len(alerts) == 1


def test_every_second_has_exactly_50_samples_per_signal():
    raw = _feed_seconds(12)
    counts = raw.groupby([raw["ts"].dt.floor("1s"), "signal"]).size()
    assert (counts == 50).all()


def test_feed_content_follows_the_timestamp_not_the_call_count():
    """A skipped second must be a real gap, never a squeezed timeline."""
    t0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
    walker, jumper = LiveFeed("RM-03"), LiveFeed("RM-03")
    walker.next_second(t0)
    jumper.next_second(t0)
    for i in range(1, 5):
        walker.next_second(t0 + timedelta(seconds=i))
    pd.testing.assert_frame_equal(
        walker.next_second(t0 + timedelta(seconds=5)),
        jumper.next_second(t0 + timedelta(seconds=5)),
    )
