"""Pipeline CLI (run from the backend/ folder).

  python -m app.pipeline local                     # no DB: simulate, detect, print alert
  python -m app.pipeline seed                      # replayable history -> bronze/silver/gold
  python -m app.pipeline stream --api URL --key K  # real-time simulator POSTing to /api/ingest
  python -m app.pipeline detect --mill RM-01       # re-run detection over all silver
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:  # the `local` smoke test needs no database driver
    from . import db, ingest
except ModuleNotFoundError as _e:
    db = ingest = None
    _err = str(_e)

from .detection import detect_cobble, rollup_1s
from .live import LiveFeed
from .simulator import Scenario, default_start, generate, scenario_truth

SEED_MILLS = ["RM-01", "RM-02", "RM-03"]


def _seed_for(mill: str, fallback: int) -> int:
    """RM-01 -> 42, RM-02 -> 43, RM-03 -> 44 ... independent of the order mills are listed."""
    digits = "".join(ch for ch in mill if ch.isdigit())
    return 41 + int(digits) if digits else 42 + fallback


def seed(mills: list[str]) -> None:
    """Load fixed-seed history (10 min, 180k readings per mill). Safe to re-run."""
    t_all = time.monotonic()
    start = default_start()
    print("connecting to database...", flush=True)
    with db.connect_once() as conn:
        print(f"connected in {time.monotonic() - t_all:.1f}s", flush=True)
        for i, mill in enumerate(mills):
            t = time.monotonic()
            raw = generate(start, Scenario(seed=_seed_for(mill, i)), mill_id=mill)
            print(f"{mill}: generated {len(raw):,} readings", flush=True)
            db.delete_bronze_range(conn, mill, raw["ts"].min(), raw["ts"].max())
            n_raw = db.ingest_bronze(conn, raw)
            print(f"{mill}: bronze loaded ({time.monotonic() - t:.1f}s)", flush=True)
            n_silver = db.upsert_silver(conn, rollup_1s(raw))
            n_alerts = ingest.run_detection(conn, mill, lookback_s=None)
            conn.commit()
            print(f"{mill}: bronze={n_raw:,} silver={n_silver:,} alerts_written={n_alerts} "
                  f"({time.monotonic() - t:.1f}s)", flush=True)
    print(f"done in {time.monotonic() - t_all:.1f}s")
    print("ground truth:", {k: v.isoformat() for k, v in scenario_truth(start).items()})


def reset(mill: str) -> None:
    """Wipe all stored data for one mill, then reload its replay history. Use for a clean live restart."""
    print(f"wiping {mill}...", flush=True)
    with db.connect_once() as conn:
        db.reset_mill(conn, mill)
        conn.commit()
    print("wiped. reloading history...", flush=True)
    seed([mill])


def livecheck(mill: str, seconds: int) -> None:
    """Run the live write path in the foreground and print every step and error.

    Stop the API first (it would write the same mill). Writes live data for `mill`.
    """
    import platform
    import traceback

    import numpy
    import pandas

    print(f"python {platform.python_version()} | pandas {pandas.__version__} | numpy {numpy.__version__}", flush=True)
    feed = LiveFeed(mill)
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    ok = bad = 0
    with db.connect_once() as conn:
        print("connected. writing one second of 20 ms data per line...", flush=True)
        for i in range(seconds):
            t0 = time.monotonic()
            stamp += timedelta(seconds=1)
            try:
                res = ingest.process(conn, feed.next_second(stamp), detect_every_s=4.0)
                ok += 1
                print(f"[{i + 1:>3}] ok     {time.monotonic() - t0:5.2f}s  {res}", flush=True)
            except Exception:
                bad += 1
                conn.rollback()
                print(f"[{i + 1:>3}] FAILED {time.monotonic() - t0:5.2f}s", flush=True)
                traceback.print_exc()
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    print(f"finished: {ok} ok, {bad} failed", flush=True)



def stream(api: str, key: str, mill: str, seconds: int) -> None:
    """Emit live readings through one reusable HTTP connection.

    seconds <= 0 means run until Ctrl+C. A failed request is retried (same second, so the
    timeline stays contiguous) and never ends the run.
    """
    from urllib.parse import urlsplit

    feed = LiveFeed(mill)
    parsed = urlsplit(api.rstrip("/") + "/api/ingest")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("API must be a valid HTTP or HTTPS URL")
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    path = parsed.path or "/api/ingest"
    if parsed.query:
        path += "?" + parsed.query

    def new_conn():
        return connection_class(parsed.hostname, port=parsed.port, timeout=30)

    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    connection = new_conn()
    i = 0
    fails = 0
    try:
        while seconds <= 0 or i < seconds:
            t0 = time.monotonic()
            chunk = feed.next_second(stamp + timedelta(seconds=1))
            payload = {"readings": [
                {"mill_id": r.mill_id, "signal": r.signal, "timestamp": r.ts.isoformat(),
                 "value": r.value, "unit": r.unit, "quality": int(r.quality)}
                for r in chunk.itertuples()
            ]}
            body = json.dumps(payload).encode("utf-8")
            try:
                connection.request("POST", path, body=body, headers={
                    "Content-Type": "application/json", "Content-Length": str(len(body)),
                    "X-Ingest-Key": key, "Connection": "keep-alive"})
                response = connection.getresponse()
                response_body = response.read()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {response_body.decode('utf-8', errors='replace')[:200]}")
                result = json.loads(response_body)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # network blip / API restart / slow database: retry this second
                fails += 1
                print(f"[retry {fails}] {type(exc).__name__}: {exc}", flush=True)
                connection.close()
                connection = new_conn()
                time.sleep(min(10, 1 + fails))
                continue
            fails = 0
            stamp += timedelta(seconds=1)
            i += 1
            print(f"[{i}/{seconds or 'forever'}] {result}", flush=True)
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        print("stopped by user", flush=True)
    finally:
        connection.close()


def local() -> None:
    start = default_start()
    raw = generate(start)
    alerts = detect_cobble(rollup_1s(raw), "RM-01")
    print(f"{len(raw):,} raw readings -> {len(alerts)} alert(s)")
    for a in alerts:
        print(a.started_at, "->", a.ended_at, a.severity)
        print(a.reason)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed"); s.add_argument("--mills", nargs="+", default=SEED_MILLS)
    st = sub.add_parser("stream")
    st.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    st.add_argument("--key", default=os.environ.get("INGEST_KEY", ""))
    st.add_argument("--mill", default="RM-04"); st.add_argument("--seconds", type=int, default=0, help="0 = run until Ctrl+C")
    lc = sub.add_parser("livecheck"); lc.add_argument("--mill", default="RM-03"); lc.add_argument("--seconds", type=int, default=90)
    r = sub.add_parser("reset"); r.add_argument("--mill", default="RM-03")
    d = sub.add_parser("detect"); d.add_argument("--mill", default="RM-01")
    sub.add_parser("local")
    a = p.parse_args()
    if a.cmd in ("seed", "detect", "reset", "livecheck") and db is None:
        raise SystemExit(f"Missing dependency ({_err}). Run: pip install -r requirements.txt")
    if a.cmd == "seed":
        seed(a.mills)
    elif a.cmd == "reset":
        reset(a.mill)
    elif a.cmd == "livecheck":
        livecheck(a.mill, a.seconds)
    elif a.cmd == "stream":
        stream(a.api, a.key, a.mill, a.seconds)
    elif a.cmd == "detect":
        with db.connect_once() as conn:
            print("alerts written:", ingest.run_detection(conn, a.mill, lookback_s=None))
            conn.commit()
    else:
        local()


if __name__ == "__main__":
    main()