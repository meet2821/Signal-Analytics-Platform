"""Postgres access: bulk ingest (bronze), silver upserts, gold alerts, queries.

Use the Supabase *session pooler* connection string (IPv4, port 5432) in
DATABASE_URL, especially on hosts without IPv6 such as Render.
"""
from __future__ import annotations

import io
import os
from datetime import timedelta
from contextlib import contextmanager

import pandas as pd
import psycopg
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

_pool: ConnectionPool | None = None


_NET_KW = {
    "connect_timeout": 15,
    "keepalives": 1, "keepalives_idle": 15, "keepalives_interval": 5, "keepalives_count": 3,
    "tcp_user_timeout": 20000,  # ms: drop a connection whose packets go unacknowledged (network stall)
}


def _guard(conn) -> None:
    """Server-side safety nets so a dead client can never leave a session (and its locks) behind."""
    conn.execute("set statement_timeout = '20s'")
    conn.execute("set lock_timeout = '8s'")
    conn.execute("set idle_in_transaction_session_timeout = '120s'")


def _configure(conn) -> None:
    """Every pooled connection gets statement/lock/idle-in-transaction timeouts."""
    _guard(conn)
    conn.commit()


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "10")),
            kwargs={"autocommit": False, **_NET_KW},
            configure=_configure,
            check=ConnectionPool.check_connection,  # drop dead connections before handing them out
            max_lifetime=900, max_idle=120, timeout=10,  # recycle; never wait >10 s for a free connection
            open=True,
        )
    return _pool


@contextmanager
def connect():
    """Pooled connection. Callers commit explicitly; uncommitted work is rolled back."""
    with _get_pool().connection() as conn:
        yield conn


@contextmanager
def connect_once():
    """Single direct connection for CLI jobs: fails within 15 s instead of hanging."""
    with psycopg.connect(os.environ["DATABASE_URL"], **_NET_KW) as conn:
        _guard(conn)
        conn.commit()
        yield conn


# ---------------------------------------------------------------- bronze ----
_known_partitions: set = set()


def ensure_partitions(conn, raw: pd.DataFrame) -> None:
    for day in sorted(raw["ts"].dt.date.unique()):
        if day in _known_partitions:
            continue
        conn.execute("select mill.ensure_partition(%s)", (day,))
        _known_partitions.add(day)


def ingest_bronze(conn, raw: pd.DataFrame, chunk: int = 200_000) -> int:
    """COPY raw readings into the partitioned bronze table (fast bulk path)."""
    ensure_partitions(conn, raw)
    cols = ["mill_id", "signal", "ts", "value", "unit", "quality"]
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(raw), chunk):
            part = raw.iloc[i : i + chunk][cols]
            buf = io.StringIO()
            part.to_csv(buf, index=False, header=False)
            buf.seek(0)
            with cur.copy(
                "copy mill.bronze_readings (mill_id, signal, ts, value, unit, quality) "
                "from stdin with (format csv)"
            ) as cp:
                cp.write(buf.read())
            total += len(part)
    return total


def delete_bronze_range(conn, mill_id: str, start, end) -> None:
    """Makes `seed` re-runnable: clear the window before re-loading it."""
    conn.execute(
        "delete from mill.bronze_readings where mill_id=%s and ts between %s and %s",
        (mill_id, start, end),
    )


# ---------------------------------------------------------------- silver ----
_SILVER_COLS = ["mill_id", "signal", "bucket", "avg", "min", "max", "std", "n"]
_SILVER_UPSERT = """insert into mill.silver_signal_1s ({cols})
    select {cols} from _silver_stg
    on conflict (mill_id, signal, bucket) do update
    set avg=excluded.avg, min=excluded.min, max=excluded.max,
        std=excluded.std, n=excluded.n""".format(cols=", ".join(_SILVER_COLS))


def upsert_silver(conn, silver: pd.DataFrame) -> int:
    """Small batches (live, 6 rows/s): executemany. Large (seed): COPY to a temp table, one upsert."""
    n = len(silver)
    if n == 0:
        return 0
    with conn.cursor() as cur:
        if n >= 500:
            buf = io.StringIO()
            silver[_SILVER_COLS].to_csv(buf, index=False, header=False)
            cur.execute("create temp table _silver_stg (like mill.silver_signal_1s) on commit drop")
            with cur.copy(
                f"copy _silver_stg ({', '.join(_SILVER_COLS)}) from stdin with (format csv)"
            ) as cp:
                cp.write(buf.getvalue())
            cur.execute(_SILVER_UPSERT)
        else:
            rows = [
                (r.mill_id, r.signal, r.bucket.to_pydatetime(), float(r.avg), float(r.min),
                 float(r.max), float(r.std), int(r.n))
                for r in silver.itertuples()
            ]
            cur.executemany(
                """insert into mill.silver_signal_1s
                   (mill_id, signal, bucket, avg, min, max, std, n)
                   values (%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (mill_id, signal, bucket) do update
                   set avg=excluded.avg, min=excluded.min, max=excluded.max,
                       std=excluded.std, n=excluded.n""",
                rows,
            )
    return n


def load_silver(conn, mill_id: str, start=None, end=None) -> pd.DataFrame:
    q = "select mill_id, signal, bucket, avg, min, max, std, n from mill.silver_signal_1s where mill_id=%s"
    args: list = [mill_id]
    if start is not None:
        q += " and bucket >= %s"
        args.append(start)
    if end is not None:
        q += " and bucket <= %s"
        args.append(end)
    q += " order by bucket"
    with conn.cursor() as cur:
        cur.execute(q, args)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def latest_bucket(conn, mill_id: str):
    with conn.cursor() as cur:
        cur.execute("select max(bucket) from mill.silver_signal_1s where mill_id=%s", (mill_id,))
        r = cur.fetchone()[0]
    return pd.Timestamp(r) if r is not None else None


# ------------------------------------------------------------------ gold ----
def save_alerts(conn, alerts) -> int:
    """Upsert alerts keyed by (mill, rule, started_at); refresh evidence on re-detection."""
    n = 0
    with conn.cursor() as cur:
        for a in alerts:
            cur.execute(
                """insert into mill.gold_alerts
                   (mill_id, rule, severity, started_at, ended_at, reason)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (mill_id, rule, started_at) do update
                   set ended_at = excluded.ended_at,
                       severity = excluded.severity,
                       reason   = excluded.reason
                   where mill.gold_alerts.ended_at is distinct from excluded.ended_at
                      or mill.gold_alerts.reason   is distinct from excluded.reason
                   returning alert_id""",
                (a.mill_id, a.rule, a.severity, a.started_at.to_pydatetime(),
                 a.ended_at.to_pydatetime(), a.reason),
            )
            row = cur.fetchone()
            if row is None:  # unchanged re-detection
                continue
            n += 1
            cur.execute("delete from mill.gold_alert_signals where alert_id=%s", (row[0],))
            for e in a.signals:
                cur.execute(
                    """insert into mill.gold_alert_signals
                       (alert_id, signal, peak_z, peak_value, baseline_value)
                       values (%s,%s,%s,%s,%s)""",
                    (row[0], e["signal"], e["peak_z"], e["peak_value"], e["baseline_value"]),
                )
    return n


def list_alerts(conn, mill_id: str | None = None, alert_id: int | None = None) -> list[dict]:
    q = """select a.alert_id, a.mill_id, a.rule, a.severity, a.started_at,
                  a.ended_at, a.reason,
                  coalesce(json_agg(json_build_object(
                      'signal', s.signal, 'peak_z', s.peak_z,
                      'peak_value', s.peak_value,
                      'baseline_value', s.baseline_value))
                      filter (where s.signal is not null), '[]') as signals
           from mill.gold_alerts a
           left join mill.gold_alert_signals s using (alert_id)
           where true"""
    args: list = []
    if mill_id:
        q += " and a.mill_id = %s"
        args.append(mill_id)
    if alert_id is not None:
        q += " and a.alert_id = %s"
        args.append(alert_id)
    q += " group by a.alert_id order by a.started_at desc limit 500"
    with conn.cursor() as cur:
        cur.execute(q, args)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        for k in ("started_at", "ended_at"):
            r[k] = r[k].isoformat()
    return rows


def get_alert(conn, alert_id: int) -> dict | None:
    rows = list_alerts(conn, alert_id=alert_id)
    return rows[0] if rows else None


# --------------------------------------------------------------- queries ----
def signal_series(conn, mill_id, signal, start, end, max_points=1200) -> list[dict]:
    """Downsampled series from silver, bucketed to cap payload size."""
    span = max((end - start).total_seconds(), 1)
    step = max(int(-(-span // max_points)), 1)  # ceil
    with conn.cursor() as cur:
        cur.execute(
            """select to_timestamp(floor(extract(epoch from bucket) / %s) * %s) as t,
                      avg(avg) as avg, min(min) as min, max(max) as max
               from mill.silver_signal_1s
               where mill_id=%s and signal=%s and bucket between %s and %s
               group by 1 order by 1""",
            (step, step, mill_id, signal, start, end),
        )
        return [
            {"t": r[0].isoformat(), "avg": r[1], "min": r[2], "max": r[3]}
            for r in cur.fetchall()
        ]


def signal_summary(conn, mill_id, signal, start, end) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """select avg(avg), min(min), max(max), coalesce(avg(std),0), coalesce(sum(n),0)
               from mill.silver_signal_1s
               where mill_id=%s and signal=%s and bucket between %s and %s""",
            (mill_id, signal, start, end),
        )
        r = cur.fetchone()
    return {"avg": r[0], "min": r[1], "max": r[2], "avg_std": r[3], "samples": int(r[4])}


def raw_window(conn, mill_id, signal, start, end, limit=6000) -> list[dict]:
    """Raw 20 ms samples for a short incident window (drill-down)."""
    with conn.cursor() as cur:
        cur.execute(
            """select ts, value from mill.bronze_readings
               where mill_id=%s and signal=%s and ts between %s and %s
               order by ts limit %s""",
            (mill_id, signal, start, end, limit),
        )
        return [{"t": r[0].isoformat(), "v": r[1]} for r in cur.fetchall()]


def list_mills(conn) -> list[dict]:
    """Each mill with its replay-history window and live window.

    hist_* = data inside the fixed seeded replay window, live_* = data after it.
    `live` = new data arrived in the last 30 s.
    """
    cut = seed_history_end()
    with conn.cursor() as cur:
        cur.execute(
            """select mill_id, min(bucket), max(bucket),
                      max(bucket) > now() - interval '30 seconds',
                      min(bucket) filter (where bucket <= %(c)s), max(bucket) filter (where bucket <= %(c)s),
                      min(bucket) filter (where bucket >  %(c)s), max(bucket) filter (where bucket >  %(c)s)
               from mill.silver_signal_1s
               where signal = 'motor_current'
               group by mill_id order by mill_id""",
            {"c": cut},
        )
        iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
        return [
            {"mill_id": r[0], "start": iso(r[1]), "end": iso(r[2]), "live": bool(r[3]),
             "hist_start": iso(r[4]), "hist_end": iso(r[5]),
             "live_start": iso(r[6]), "live_end": iso(r[7])}
            for r in cur.fetchall()
        ]


def last_run(conn, mill_id: str, max_len_s: int = 1800, gap_s: int = 60):
    """The most recent contiguous stretch of live data (a gap > gap_s starts a new run).

    Capped to the last max_len_s seconds so the replay slider stays usable. None if the
    mill has no live data after the seeded replay window.
    """
    cut = seed_history_end()
    with conn.cursor() as cur:
        cur.execute(
            "select max(bucket) from mill.silver_signal_1s where mill_id=%s and signal='motor_current' and bucket > %s",
            (mill_id, cut),
        )
        end = cur.fetchone()[0]
        if end is None:
            return None
        lo = end - timedelta(seconds=max_len_s)
        cur.execute(
            """select min(bucket), max(bucket) filter (where gap > make_interval(secs => %s))
               from (select bucket, bucket - lag(bucket) over (order by bucket) as gap
                     from mill.silver_signal_1s
                     where mill_id=%s and signal='motor_current' and bucket > %s and bucket >= %s) t""",
            (gap_s, mill_id, cut, lo),
        )
        first, run_start = cur.fetchone()
    return {"start": (run_start or first).isoformat(), "end": end.isoformat()}


def multi_series(conn, mill_id, start, end, max_points=600) -> dict:
    """All signals in ONE query: {signal: [{t, avg, min, max}, ...]} bucketed to <= max_points."""
    span = max((end - start).total_seconds(), 1)
    step = max(int(-(-span // max_points)), 1)  # ceil
    out: dict[str, list] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select signal, to_timestamp(floor(extract(epoch from bucket) / %s) * %s) as t,
                      avg(avg), min(min), max(max)
               from mill.silver_signal_1s
               where mill_id=%s and bucket between %s and %s
               group by 1, 2 order by 1, 2""",
            (step, step, mill_id, start, end),
        )
        for sig, t, av, mn, mx in cur.fetchall():
            out.setdefault(sig, []).append({"t": t.isoformat(), "avg": av, "min": mn, "max": mx})
    return out


def latest_status(conn, mill_id: str) -> dict:
    """Cheap status: newest bucket (index-backed), its values, and any alert active at that time."""
    with conn.cursor() as cur:
        cur.execute("select max(bucket) from mill.silver_signal_1s where mill_id=%s", (mill_id,))
        latest_t = cur.fetchone()[0]
        latest: dict = {}
        active = None
        if latest_t is not None:
            cur.execute(
                "select signal, avg from mill.silver_signal_1s where mill_id=%s and bucket=%s",
                (mill_id, latest_t),
            )
            latest = {r[0]: {"t": latest_t.isoformat(), "value": r[1]} for r in cur.fetchall()}
            cur.execute(
                """select alert_id from mill.gold_alerts
                   where mill_id=%s and ended_at >= %s - interval '60 seconds'
                     and started_at <= %s
                   order by started_at desc limit 1""",
                (mill_id, latest_t, latest_t),
            )
            r = cur.fetchone()
            active = r[0] if r else None
    return {
        "mill_id": mill_id,
        "signals": latest,
        "latest": latest_t.isoformat() if latest_t else None,
        "active_alert_id": active,
        "status": "ALERT" if active else "NORMAL",
    }


# ------------------------------------------------------------- retention ----
def seed_history_end():
    """End of the fixed replay history. Retention never touches data at or before this."""
    from .simulator import Scenario, default_start

    return default_start() + timedelta(seconds=Scenario().duration_s + 60)


def reset_mill(conn, mill_id: str) -> None:
    """Delete everything stored for one mill (bronze, silver, alerts and their evidence)."""
    conn.execute("delete from mill.gold_alerts where mill_id=%s", (mill_id,))  # cascades to evidence
    conn.execute("delete from mill.silver_signal_1s where mill_id=%s", (mill_id,))
    conn.execute("delete from mill.bronze_readings where mill_id=%s", (mill_id,))


def prune_live(conn, mill_id: str, bronze_keep_s: int = 1200, silver_keep_s: int = 86400) -> None:
    """Keep the free-tier database small for the continuously running live mill.

    Raw 20 ms rows older than `bronze_keep_s` are dropped unless they sit within
    30 s of an alert (so incident drill-down keeps working). Silver and alerts
    are kept `silver_keep_s`. The seeded replay history of the mill is never pruned.
    """
    keep_from = seed_history_end()
    conn.execute(
        """delete from mill.bronze_readings b
           where b.mill_id = %s
             and b.ts > %s
             and b.ts < now() - make_interval(secs => %s)
             and not exists (
                 select 1 from mill.gold_alerts a
                 where a.mill_id = b.mill_id
                   and b.ts between a.started_at - interval '30 seconds'
                                and a.ended_at   + interval '30 seconds')""",
        (mill_id, keep_from, bronze_keep_s),
    )
    conn.execute(
        """delete from mill.silver_signal_1s
           where mill_id=%s and bucket > %s and bucket < now() - make_interval(secs => %s)""",
        (mill_id, keep_from, silver_keep_s),
    )
    conn.execute(
        """delete from mill.gold_alerts
           where mill_id=%s and started_at > %s and started_at < now() - make_interval(secs => %s)""",
        (mill_id, keep_from, silver_keep_s),
    )