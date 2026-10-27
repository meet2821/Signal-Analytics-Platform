"""Shared ingestion path used by the HTTP endpoint, the live loop and the CLI.

raw readings -> bronze (COPY) -> silver (1 s rollup upsert) -> gold (detection)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import db
from .detection import detect_cobble, rollup_1s
from .live import LiveFeed

log = logging.getLogger("ingest")

DETECT_LOOKBACK_S = 15 * 60
DETECT_EVERY_S = float(os.environ.get("DETECT_EVERY_S", "10"))   # lower = more database work
BRONZE_KEEP_S = int(os.environ.get("BRONZE_KEEP_S", "600"))      # live raw-data retention
LIVE_MAX_MINUTES = float(os.environ.get("LIVE_MAX_MINUTES", "0"))  # 0 = run until the API stops
_last_detect: dict[str, float] = {}


def run_detection(conn, mill_id: str, lookback_s: int | None = DETECT_LOOKBACK_S) -> int:
    """Run the detector over silver (idempotent). lookback_s=None scans everything."""
    start = None
    if lookback_s is not None:
        latest = db.latest_bucket(conn, mill_id)
        if latest is None:
            return 0
        start = latest - pd.Timedelta(seconds=lookback_s)
    silver = db.load_silver(conn, mill_id, start=start)
    if silver.empty:
        return 0
    silver["bucket"] = pd.to_datetime(silver["bucket"], utc=True)
    for col in ("avg", "min", "max", "std"):
        silver[col] = silver[col].astype(float)
    return db.save_alerts(conn, detect_cobble(silver, mill_id))


def process(conn, raw: pd.DataFrame, detect_every_s: float = 5.0) -> dict:
    """Persist a batch of readings and (throttled) refresh alerts."""
    n_raw = db.ingest_bronze(conn, raw)
    silver = rollup_1s(raw)
    n_silver = db.upsert_silver(conn, silver)
    alerts = 0
    now = time.monotonic()
    for mill_id in raw["mill_id"].unique():
        if now - _last_detect.get(mill_id, 0.0) >= detect_every_s:
            _last_detect[mill_id] = now
            alerts += run_detection(conn, mill_id)
    conn.commit()
    return {"bronze_rows": n_raw, "silver_rows": n_silver, "new_or_updated_alerts": alerts}


# ------------------------------------------------------------- live loop ----
MAX_BATCH_S = 10       # seconds of data written per database round when catching up
MAX_LAG_S = 120        # beyond this the feed jumps to "now" (an honest gap, e.g. after an outage)
TICK_TIMEOUT_S = 15    # a database call slower than this is abandoned so the feed cannot hang
HEARTBEAT_S = 15

# Shared, read-only-for-others state: exposed through /health and /api/live/status.
STATE: dict = {
    "enabled": False, "mill": None, "running": False, "ticks": 0, "ok": 0, "errors": 0,
    "last_ok_at": None, "last_error": None, "last_error_at": None, "behind_s": 0, "last_tick_s": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_error(msg: str) -> None:
    STATE["errors"] += 1
    STATE["last_error"] = msg[:300]
    STATE["last_error_at"] = _now_iso()


def snapshot() -> dict:
    out = dict(STATE)
    if out["last_ok_at"]:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(out["last_ok_at"])).total_seconds()
        out["last_ok_age_s"] = round(age)
    else:
        out["last_ok_age_s"] = None
    return out


def _tick(chunk: pd.DataFrame, tick_no: int, mill_id: str) -> None:
    with db.connect() as conn:
        process(conn, chunk, detect_every_s=DETECT_EVERY_S)
        if tick_no % 60 == 0:
            db.prune_live(conn, mill_id, bronze_keep_s=BRONZE_KEEP_S)
            conn.commit()


async def live_loop(mill_id: str) -> None:
    """Keeps the live mill's timeline contiguous. Any failure is recorded, never fatal.

    Normally writes one whole second per tick. If the database is slow and we fall behind
    the wall clock, the missing seconds are written together in one batch (up to
    MAX_BATCH_S), so timestamps stay contiguous and the feed catches up instead of
    skipping or squeezing time. A hung database call is abandoned after TICK_TIMEOUT_S.
    """
    feed = LiveFeed(mill_id)
    log.info("live simulator started for %s", mill_id)
    STATE.update(enabled=True, mill=mill_id, running=True)
    next_stamp = datetime.now(timezone.utc).replace(microsecond=0)
    tick_no = 0
    last_beat = time.monotonic()
    started = time.monotonic()
    while True:
        if LIVE_MAX_MINUTES and time.monotonic() - started >= LIVE_MAX_MINUTES * 60:
            log.info("live feed stopped after LIVE_MAX_MINUTES=%s", LIVE_MAX_MINUTES)
            STATE["running"] = False
            return
        now = datetime.now(timezone.utc)
        wall = now.replace(microsecond=0)
        if next_stamp > wall:  # ahead of the clock: wait for the next second
            await asyncio.sleep(max(0.05, (next_stamp - now).total_seconds()))
            continue
        behind = int((wall - next_stamp).total_seconds()) + 1
        if behind > MAX_LAG_S:
            log.warning("live feed %s s behind, jumping to now", behind)
            next_stamp, behind = wall, 1
        STATE["behind_s"] = behind - 1
        t0 = time.monotonic()
        failed = False
        try:
            stamps = [next_stamp + timedelta(seconds=i) for i in range(min(behind, MAX_BATCH_S))]
            next_stamp = stamps[-1] + timedelta(seconds=1)
            chunk = pd.concat([feed.next_second(s) for s in stamps], ignore_index=True)
            await asyncio.wait_for(asyncio.to_thread(_tick, chunk, tick_no, mill_id), TICK_TIMEOUT_S)
            STATE["ok"] += 1
            STATE["last_ok_at"] = _now_iso()
            STATE["last_tick_s"] = round(time.monotonic() - t0, 2)
        except asyncio.CancelledError:
            STATE["running"] = False
            raise
        except asyncio.TimeoutError:
            failed = True
            msg = f"database call did not finish within {TICK_TIMEOUT_S}s (network stall?), batch abandoned"
            log.error("live feed: %s", msg)
            _record_error(msg)
        except Exception as exc:  # never let the feed die
            failed = True
            log.exception("live tick failed")
            _record_error(f"{type(exc).__name__}: {exc}")
        STATE["ticks"] = tick_no = tick_no + 1
        if time.monotonic() - last_beat >= HEARTBEAT_S:
            last_beat = time.monotonic()
            log.info("live %s: ok=%d errors=%d last_batch=%ss behind=%ss", mill_id, STATE["ok"],
                     STATE["errors"], STATE["last_tick_s"], STATE["behind_s"])
        if failed:
            await asyncio.sleep(1.0)  # do not hammer a failing database


async def live_supervisor(mill_id: str) -> None:
    """Restarts live_loop if it ever crashes outright."""
    while True:
        try:
            await live_loop(mill_id)
            return  # finished on purpose (LIVE_MAX_MINUTES)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("live loop crashed, restarting in 2 s")
            _record_error(f"loop crashed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(2.0)


def enabled() -> bool:
    return os.environ.get("LIVE_SIM", "0") == "1"