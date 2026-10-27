# Architecture and design decisions

This project is deliberately **data-engineering-first**: the interesting work is the pipeline
(ingest → model → derive → serve), and the UI is a thin layer over well-shaped tables.

## 1. Data volume and why it shapes everything

20 ms sampling = 50 samples/s/signal × 6 signals = **300 rows/s ≈ 26 M rows/day per mill**.
Querying that directly for a chart is wasteful, and storing it forever on a free-tier database is impossible.
So the design separates *what we keep*, *what we query* and *what we derive*:

| Layer | Table | Grain | Retention | Used by |
|---|---|---|---|---|
| Bronze | `mill.bronze_readings` | 1 raw sample | replay mills: kept; live mill: 20 min (+/-30 s around alerts) | incident raw drill-down |
| Silver | `mill.silver_signal_1s` | mill × signal × 1 s (avg, min, max, std, n) | replay: kept; live: 24 h | all charts, history, detection |
| Gold | `mill.gold_alerts`, `gold_alert_signals` | one incident / one signal per incident | replay: kept; live: 24 h | alert list, incident page |

Silver is ~50× smaller than bronze, and the min/max columns preserve spikes that plain averaging would hide.

## 2. Ingestion

```
simulator → POST /api/ingest (X-Ingest-Key) → pandas DataFrame → COPY into bronze
                                                             └→ 1 s rollup → upsert silver
                                                             └→ (throttled) detect → upsert gold
```

- **Bulk path.** Bronze is loaded with Postgres `COPY` (psycopg `cursor.copy`), the fastest way to append rows.
- **Partitioning.** `bronze_readings` is range-partitioned by day; `mill.ensure_partition(day)` creates partitions
  on demand and adds a `(mill_id, signal, ts)` index per partition. Dropping or pruning old data is cheap.
- **Idempotency.** Silver uses `INSERT ... ON CONFLICT DO UPDATE`; gold uses a natural key
  `(mill_id, rule, started_at)`. Re-running seed or detection never duplicates anything.
- **Micro-batching.** One whole second (300 rows) per request keeps silver rollups exact, and a request is ~20 KB.
- **Two feeds, one path.** The built-in live loop and `python -m app.pipeline stream` both go through the same
  `ingest.process()`, so the HTTP endpoint is the single contract for a real PLC gateway.
- **Security of the machine path.** Ingest is protected by a shared key (constant-time compare), separate from user JWTs.

## 3. Simulation (`simulator.py`)

Latent drivers (speed command, load, slow wander) generate six correlated signals:

- torque ≈ 0.82 × current; current follows speed and load; force follows load; vibration follows speed and load.
- **Legitimate changes** (must not alert): +10 % speed ramp over 20 s, +18 % load ramp over 15 s.
- **Cobble**: fast attack (0.8 s) then 6 s plateau then decay. Force +85 %, current +65 %, torque +70 %, speed −40 %,
  vibration up and noisy, temperature +30 °C.
- Everything is seeded, so the dataset (and therefore the alert) is fully reproducible.

## 4. Detection (`detection.py`)

Why not a plain threshold or a plain z-score? Both fire on legitimate load changes, because levels legitimately move.
The detector exploits the *signature* of a cobble instead: speed and shape of change, across several signals at once.

1. Aggregate to 1 s (silver).
2. For each signal, compute the 3 s relative change.
3. Score it against that signal's own trailing 120 s history with robust statistics (median and MAD),
   lagged 5 s so the event cannot contaminate its baseline.
4. Flag only if the z-score is large **and** the change exceeds a physical-size gate in the cobble direction.
5. Raise an alert when ≥ 3 signals are flagged in the same second; merge consecutive seconds.
6. Store evidence: per-signal baseline, peak and z, plus a readable reason.

Verified by tests: exactly one alert at the cobble, none on the two legitimate changes, none at the loop seam of
the live feed over three consecutive cycles, and fully reproducible results.

Limits: thresholds are global and hand-set; slow drifts (e.g. gradual bearing wear) would need a separate
detector (EWMA/CUSUM on vibration residual vs speed). The approach is intentionally explainable, since an operator
needs to know *why* an alert fired.

## 5. Serving and auth

- FastAPI, psycopg connection pool, parameterised SQL everywhere.
- Series endpoints bucket silver into at most `max_points` points in SQL (`floor(epoch / step)`), so payloads stay small at any range.
- Supabase Auth issues JWTs; the API verifies them locally (HS256 secret for legacy projects, JWKS for asymmetric keys).
  Row-level-security policies also restrict direct Supabase access to authenticated users.
- Frontend: Next.js App Router. Custom SVG chart component (no chart dependency): line, min/max band,
  shaded alert windows, reference line, tooltip, sparkline mode.

## 6. How this maps to your Azure / Databricks stack

The same shape scales up directly; this is the answer to "how would you do this in production?":

| Here | Production equivalent |
|---|---|
| Simulator → `/api/ingest` | PLC → IoT Hub / Event Hubs (Kafka endpoint) |
| `bronze_readings` (partitioned by day) | Delta table `bronze.readings`, Auto Loader or Structured Streaming from Event Hubs, partitioned by date, `OPTIMIZE ... ZORDER BY (mill_id, signal)` |
| `rollup_1s` + upsert to silver | Structured Streaming `groupBy(window("ts","1 second"), "mill_id","signal")` with watermark, `MERGE INTO silver` via `foreachBatch` |
| `detect_cobble` (pandas) | Same logic as a PySpark pandas UDF / `applyInPandas` per mill, or Spark window functions (`lag`, rolling median via `percentile_approx`) |
| Alert upsert `ON CONFLICT` | `MERGE INTO gold.alerts` keyed by (mill, rule, started_at) |
| `prune_live` retention | Delta retention + `VACUUM`, or lifecycle policies on ADLS |
| Pipeline CLI (`seed`, `detect`) | ADF / Databricks Workflows jobs, parametrised by mill and date |
| Data quality | Add `quality` flag checks, expectations (DLT) for nulls, out-of-range values, late / duplicate samples |

## 6b. What I would add with more time

- Late and out-of-order data handling (watermarks) and per-signal quality flags in detection.
- Detection backtesting harness: replay many simulated scenarios and report precision, recall and detection delay.
- Alert acknowledgement, notifications (email / Teams webhook), and per-mill threshold configuration.
- Continuous aggregates / TimescaleDB for automatic rollups.
- End-to-end tests against a Postgres service container in CI.
