"""The live loop must survive database errors, hangs and crashes, and say what went wrong."""
import asyncio
import time

import pytest

pytest.importorskip("psycopg")  # app.ingest imports the database layer

from app import ingest  # noqa: E402


def _run(tick, seconds, supervised=False):
    seen = []
    ingest.STATE.update(ok=0, errors=0, ticks=0, last_error=None, last_ok_at=None)

    def wrapped(chunk, n, mill):
        seen.append(n)
        return tick(chunk, n, mill)

    original = ingest._tick
    ingest._tick = wrapped
    try:
        coro = (ingest.live_supervisor if supervised else ingest.live_loop)("RM-03")
        try:
            asyncio.run(asyncio.wait_for(coro, seconds))
        except asyncio.TimeoutError:
            pass
    finally:
        ingest._tick = original
    return seen


def test_database_errors_are_recorded_and_the_feed_keeps_going():
    def tick(chunk, n, mill):
        if n < 3:
            raise RuntimeError("connection closed")

    seen = _run(tick, 7)
    assert max(seen) >= 4                                  # kept ticking after the failures
    assert ingest.STATE["errors"] >= 3
    assert "connection closed" in ingest.STATE["last_error"]
    assert ingest.STATE["ok"] >= 1                         # and recovered


def test_a_hung_database_call_is_abandoned():
    old = ingest.TICK_TIMEOUT_S
    ingest.TICK_TIMEOUT_S = 0.5
    try:
        def tick(chunk, n, mill):
            if n == 0:
                time.sleep(2.5)  # a stalled network call

        seen = _run(tick, 6)
    finally:
        ingest.TICK_TIMEOUT_S = old
    assert max(seen) >= 2
    assert ingest.STATE["errors"] >= 1 and "did not finish" in ingest.STATE["last_error"]
    assert ingest.STATE["ok"] >= 1


def test_the_supervisor_restarts_a_crashed_loop(monkeypatch=None):
    calls = {"n": 0}
    real = ingest.live_loop

    async def crashing(mill_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        await real(mill_id)

    ingest.live_loop = crashing
    try:
        _run(lambda c, n, m: None, 6, supervised=True)
    finally:
        ingest.live_loop = real
    assert calls["n"] >= 2
    assert "boom" in (ingest.STATE["last_error"] or "")
