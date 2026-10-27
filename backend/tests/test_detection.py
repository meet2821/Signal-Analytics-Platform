import pandas as pd

from app.detection import detect_cobble, rollup_1s
from app.simulator import HZ, SIGNALS, Scenario, default_start, generate, scenario_truth


def _run(sc=None):
    start = default_start()
    raw = generate(start, sc)
    return start, raw, detect_cobble(rollup_1s(raw), "RM-01")


def test_sampling_is_20ms_and_all_signals_present():
    start, raw, _ = _run(Scenario(duration_s=60, cobble_at_s=40, speed_step_at_s=10, load_step_at_s=25))
    one = raw[raw.signal == "motor_current"]
    assert len(one) == 60 * HZ
    assert (one.ts.diff().dropna() == pd.Timedelta(milliseconds=20)).all()
    assert set(raw.signal) == set(SIGNALS)


def test_cobble_raises_exactly_one_alert_at_the_right_time():
    start, _, alerts = _run()
    truth = scenario_truth(start)
    assert len(alerts) == 1
    a = alerts[0]
    assert abs((a.started_at - truth["cobble_start"]).total_seconds()) <= 3
    assert len(a.signals) >= 5


def test_legitimate_load_and_speed_changes_do_not_alert():
    # same run, cobble pushed beyond the window -> must be silent
    _, _, alerts = _run(Scenario(cobble_at_s=10_000))
    assert alerts == []


def test_detection_is_reproducible():
    _, _, a1 = _run()
    _, _, a2 = _run()
    assert [x.started_at for x in a1] == [x.started_at for x in a2]
