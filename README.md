# Mono Edge Signal Analytics

A full-stack platform that turns high-frequency rolling-mill PLC data (6 correlated signals at
20 ms / 50 Hz) into live monitoring, historical analytics and cobble alerts.

| | |
|---|---|
| **Live app** | `https://<your-app>.vercel.app` |
| **API** | `https://<your-api>.onrender.com` (`/docs` for the OpenAPI UI) |
| **Demo login** | `reviewer@example.com` / `<password>` (fill in after creating the user) |

## What it does

| Requirement | Where it lives |
|---|---|
| Authentication | Supabase Auth (email + password). Every `/api` route verifies the user's JWT; every page is behind a sign-in gate. |
| Signal generation and ingestion | `backend/app/simulator.py` (20 ms, 6 correlated signals). `POST /api/ingest` receives timestamped readings, bulk-loads them (`COPY`) into Postgres. A built-in live feed and a CLI streamer both use that same path. |
| Live dashboard | `/` Select a mill, see status (NORMAL/ALERT), 6 live signal cards, a 1 s-average chart with min/max band. Refreshes every 2 s. Replay mills play stored history through the same view. |
| Historical analytics | `/history` Pick mill, signal and UTC time range. Chart, summary stats and a normal-vs-abnormal comparison. |
| Anomaly detection and alerts | `backend/app/detection.py`. Recorded in `mill.gold_alerts` with timestamp, affected signals, per-signal evidence and a plain-language reason. |
| Incident exploration | `/alerts` history and `/alerts/<id>` per-incident page: all 6 signals around the event with the abnormal window shaded and the normal average as a reference line, a normal-vs-abnormal table, and raw 20 ms drill-down. |

## Architecture

```
                  every second: 50 samples x 6 signals
 simulator  ───────────────────────────────────────────►  POST /api/ingest  (X-Ingest-Key)
 (live feed / CLI / replay)                                        │
                                                                   ▼
        ┌───────────────────────── Supabase Postgres (schema "mill") ─────────────────────────┐
        │  BRONZE  bronze_readings     raw 20 ms samples, append-only, daily range partitions │
        │     │  1 s rollup (avg/min/max/std/n), idempotent upsert                            │
        │  SILVER  silver_signal_1s    the query layer for charts and history                 │
        │     │  detect_cobble() every few seconds over a trailing window                     │
        │  GOLD    gold_alerts + gold_alert_signals    incident, reason, evidence             │
        └──────────────────────────────────────┬───────────────────────────────────────────────┘
                                               │ psycopg pool
                      FastAPI (Render)  ── verifies Supabase JWT ──  Next.js dashboard (Vercel)
```

The design is a medallion pipeline on purpose: raw data is kept untouched, charts never scan 50 Hz
data, and alerts are a derived, re-computable layer. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the reasoning, data volumes and how this maps to Azure Databricks / PySpark / ADF.

## Anomaly detection (cobble)

A cobble is a **fast, coordinated** excursion; legitimate speed or load changes are **slow ramps**.
A signal is flagged at second *t* only if **both** hold:

1. **Statistical test**: its 3 s relative change is a robust outlier (|z| ≥ 6) against its own trailing
   120 s change distribution (median / MAD, lagged 5 s so the event cannot pollute its own baseline).
2. **Practical gate**, in the cobble direction: current +15 %, torque +15 %, force +20 %,
   speed −12 %, vibration +50 %, temperature +1.5 %.

An alert needs **≥ 3 signals flagged in the same second**; consecutive seconds merge into one incident.
Severity is `critical` when ≥ 5 signals are involved. Re-running detection is idempotent.

Simulated dataset (fixed seed, replayable): 10 minutes, 180,000 readings per mill.

| Time (UTC, 2026-10-19) | Event | Alert? |
|---|---|---|
| 10:02:30 | +10 % speed step ramped over 20 s (legitimate) | no |
| 10:05:00 | +18 % load step ramped over 15 s (legitimate) | no |
| 10:07:30 | Cobble: force +85 %, current +65 %, torque +70 %, speed −40 %, vibration ×4.5 | **yes**, onset 10:07:30, critical, 6 signals |

Mills in the demo: `RM-01`, `RM-02` (replayable history) and `RM-03` (continuous live feed that
produces a fresh cobble every 5 minutes).

## Repository layout

```
backend/            FastAPI service, simulator, pipeline, detection, tests
  app/simulator.py    synthetic PLC signals + scenarios
  app/live.py         looping 300 s real-time feed
  app/ingest.py       bronze -> silver -> gold path, live loop, retention
  app/detection.py    1 s rollups + cobble detector
  app/db.py           Postgres access (pooled), retention
  app/main.py         API + auth
  app/pipeline.py     CLI: local | seed | stream | detect
  tests/              detector and live-feed tests
db/schema.sql       tables, partitions, indexes, row-level security
frontend/           Next.js (App Router, TypeScript) dashboard, no chart library
render.yaml         Render blueprint for the API
.github/workflows/  CI (tests + build) and an optional keep-alive ping
docs/               ARCHITECTURE.md, DEPLOYMENT.md
```

## Run locally

Prerequisites: Python 3.12+, Node 20+, a Supabase project (free) **or** local Postgres 16.

```bash
# 0. See the detector work with no database at all
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m app.pipeline local        # 180,000 readings -> 1 alert with reason
python -m pytest                    # detector + live feed tests

# 1. Database: run db/schema.sql in the Supabase SQL editor (or: psql "$DATABASE_URL" -f ../db/schema.sql)
cp .env.example .env                # fill in DATABASE_URL, SUPABASE_URL, INGEST_KEY

# 2. Load the replayable history (bronze -> silver -> gold). Safe to re-run.
python -m app.pipeline seed

# 3. Start the API (set LIVE_SIM=1 in .env to also run the built-in live mill RM-03)
uvicorn app.main:app --reload       # http://localhost:8000/docs

# 4. Start the frontend
cd ../frontend
cp .env.local.example .env.local    # API URL + Supabase URL / anon key
npm install && npm run dev          # http://localhost:3000  -> sign up, then explore
```

### Starting / replaying the signal simulator

| Goal | Command |
|---|---|
| Replay the fixed-seed history containing the cobble | `python -m app.pipeline seed` then open **Live → RM-01** and press *Jump to before cobble*, or **Alerts → Inspect** |
| Continuous live feed inside the API | set `LIVE_SIM=1` (mill `RM-03`) |
| Live feed as a separate process through the HTTP ingestion path | `python -m app.pipeline stream --api http://localhost:8000 --key $INGEST_KEY --mill RM-04 --seconds 330` |
| Re-run detection over stored data | `python -m app.pipeline detect --mill RM-01` |

Local development without Supabase: run Postgres locally, apply `db/schema.sql`, set `AUTH_DISABLED=1`
in `backend/.env` and `NEXT_PUBLIC_AUTH_DISABLED=1` in `frontend/.env.local`. Never set either in production.

## API

All routes need `Authorization: Bearer <Supabase access token>` except `/health` and `/api/ingest`
(which needs `X-Ingest-Key`).

| Route | Purpose |
|---|---|
| `GET /api/mills` | mills with data range and `live` flag, signal units |
| `GET /api/mills/{mill}/status` | latest value per signal, NORMAL/ALERT |
| `GET /api/mills/{mill}/signals/{signal}/series?start&end&max_points` | downsampled series + summary |
| `GET /api/alerts?mill_id=` | alert history |
| `GET /api/alerts/{id}/incident?pad_s=` | all signals around an alert, normal vs abnormal summaries |
| `GET /api/alerts/{id}/raw/{signal}` | raw 20 ms samples for the incident |
| `POST /api/ingest` | machine path, body `{"readings":[{"mill_id","signal","timestamp","value","unit"}]}` |

## Deploy

Full step-by-step guide (GitHub → Supabase → Render → Vercel, with a verification checklist and
troubleshooting): **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## Design notes and trade-offs

- **Free-tier friendly.** 50 Hz × 6 signals is 300 rows/s. Raw data for the live mill is kept 20 minutes
  (plus ±30 s around alerts); the 1 s silver layer and alerts are kept 24 h. Replay mills are static.
- **Charts read silver, not bronze.** A 10-minute window is 3,600 points per signal instead of 180,000.
  Raw 20 ms data is only fetched for the short incident drill-down.
- **Whole-second batches.** Silver is recomputed per 1 s bucket, so ingest batches should align to whole seconds.
- **Next steps.** Per-mill adaptive thresholds, a second detector for slow drift (EWMA/CUSUM on the vibration
  residual vs speed), alert acknowledgement and notifications, Delta Lake / Structured Streaming for real scale.
