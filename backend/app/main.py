"""FastAPI service.

All /api routes need a Supabase-issued user JWT (Authorization: Bearer ...),
except POST /api/ingest, which is a machine-to-machine path protected by the
X-Ingest-Key header (this is where a PLC gateway / simulator would post).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from datetime import datetime, timedelta

import jwt
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from pydantic import BaseModel, Field

from . import db, ingest
from .simulator import SIGNALS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if ingest.enabled():
        task = asyncio.create_task(ingest.live_supervisor(os.environ.get("LIVE_MILL", "RM-03")))
    yield
    if task:
        task.cancel()


app = FastAPI(title="Mono Edge Signal Analytics", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
bearer = HTTPBearer(auto_error=False)
_jwks_clients: dict[str, PyJWKClient] = {}


# ------------------------------------------------------------------ auth ----
def _decode_supabase_jwt(token: str) -> dict:
    """Supports legacy HS256 projects and new asymmetric (ES256/RS256) JWKS projects."""
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == "HS256":
        secret = os.environ.get("SUPABASE_JWT_SECRET")
        if not secret:
            raise HTTPException(500, "Server misconfigured: token is HS256 but SUPABASE_JWT_SECRET is not set")
        return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
    base = os.environ.get("SUPABASE_URL")
    if not base:
        raise HTTPException(500, f"Server misconfigured: token is {alg} but SUPABASE_URL is not set")
    parts = urlsplit(base.strip())  # tolerate extra paths such as /rest/v1/ pasted from the dashboard
    url = f"{parts.scheme}://{parts.netloc}/auth/v1/.well-known/jwks.json"
    # Supabase's JWKS endpoint expects the project's public (anon/publishable) key as `apikey`.
    anon = os.environ.get("SUPABASE_ANON_KEY")
    client = _jwks_clients.setdefault(
        url, PyJWKClient(url, cache_keys=True, headers={"apikey": anon} if anon else None)
    )
    try:
        key = client.get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientError as exc:
        hint = "" if anon else " (set SUPABASE_ANON_KEY on the API)"
        raise HTTPException(401, f"Cannot fetch Supabase signing keys: {exc}{hint}") from exc
    return jwt.decode(token, key, algorithms=["ES256", "RS256"], audience="authenticated")


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if os.environ.get("AUTH_DISABLED") == "1":  # local development only
        return {"sub": "local-dev"}
    if cred is None:
        raise HTTPException(401, "Missing bearer token")
    try:
        return _decode_supabase_jwt(cred.credentials)
    except (jwt.PyJWTError, KeyError) as exc:
        raise HTTPException(401, f"Invalid token: {exc}") from exc


def _ts(s: str) -> datetime:
    return pd.Timestamp(s).to_pydatetime()


# ------------------------------------------------------------------ meta ----
@app.get("/health")
def health() -> dict:
    live = ingest.snapshot()
    return {"ok": True, "live": {k: live[k] for k in ("enabled", "mill", "running", "ok", "errors", "last_ok_age_s")}}


@app.get("/api/live/status")
def live_status(_: dict = Depends(current_user)):
    """State of the built-in live feed, including the last error (shown in the dashboard)."""
    return ingest.snapshot()


@app.get("/api/mills")
def mills(_: dict = Depends(current_user)):
    with db.connect() as conn:
        return {"mills": db.list_mills(conn),
                "signals": [{"signal": k, "unit": v} for k, v in SIGNALS.items()]}


@app.get("/api/mills/{mill_id}/status")
def status(mill_id: str, _: dict = Depends(current_user)):
    with db.connect() as conn:
        return db.latest_status(conn, mill_id)


@app.get("/api/mills/{mill_id}/last-run")
def mill_last_run(mill_id: str, _: dict = Depends(current_user)):
    """Start/end of the most recent live run (what "Replay history" plays for a live mill)."""
    with db.connect() as conn:
        return db.last_run(conn, mill_id) or {"start": None, "end": None}


@app.get("/api/mills/{mill_id}/series")
def mill_series(
    mill_id: str,
    start: datetime,
    end: datetime,
    max_points: int = Query(600, ge=50, le=5000),
    _: dict = Depends(current_user),
):
    """All six signals for a time range in one request (dashboard / replay)."""
    if end <= start:
        raise HTTPException(422, "end must be after start")
    with db.connect() as conn:
        return {"series": db.multi_series(conn, mill_id, start, end, max_points)}


# --------------------------------------------------------------- history ----
@app.get("/api/mills/{mill_id}/signals/{signal}/series")
def series(
    mill_id: str,
    signal: str,
    start: datetime,
    end: datetime,
    max_points: int = Query(1200, ge=50, le=5000),
    _: dict = Depends(current_user),
):
    if signal not in SIGNALS:
        raise HTTPException(404, "Unknown signal")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    with db.connect() as conn:
        return {
            "series": db.signal_series(conn, mill_id, signal, start, end, max_points),
            "summary": db.signal_summary(conn, mill_id, signal, start, end),
            "unit": SIGNALS[signal],
        }


# ---------------------------------------------------------------- alerts ----
@app.get("/api/alerts")
def alerts(mill_id: str | None = None, _: dict = Depends(current_user)):
    with db.connect() as conn:
        return {"alerts": db.list_alerts(conn, mill_id)}


@app.get("/api/alerts/{alert_id}/incident")
def incident(
    alert_id: int,
    pad_s: int = Query(60, ge=10, le=600),
    _: dict = Depends(current_user),
):
    """Per-signal behaviour around an alert: normal baseline window vs incident window."""
    with db.connect() as conn:
        a = db.get_alert(conn, alert_id)
        if not a:
            raise HTTPException(404, "Alert not found")
        started, ended = _ts(a["started_at"]), _ts(a["ended_at"])
        lo, hi = started - timedelta(seconds=pad_s), ended + timedelta(seconds=pad_s)
        normal_hi = started - timedelta(seconds=5)
        out = {}
        for sig, unit in SIGNALS.items():
            out[sig] = {
                "series": db.signal_series(conn, a["mill_id"], sig, lo, hi),
                "normal": db.signal_summary(conn, a["mill_id"], sig, lo, normal_hi),
                "abnormal": db.signal_summary(conn, a["mill_id"], sig, started, ended),
                "unit": unit,
            }
        return {"alert": a, "window": {"start": lo.isoformat(), "end": hi.isoformat()},
                "signals": out}


@app.get("/api/alerts/{alert_id}/raw/{signal}")
def incident_raw(alert_id: int, signal: str, _: dict = Depends(current_user)):
    """Raw 20 ms samples (bronze) around the incident, for drill-down."""
    if signal not in SIGNALS:
        raise HTTPException(404, "Unknown signal")
    with db.connect() as conn:
        a = db.get_alert(conn, alert_id)
        if not a:
            raise HTTPException(404, "Alert not found")
        lo = _ts(a["started_at"]) - timedelta(seconds=5)
        hi = _ts(a["ended_at"]) + timedelta(seconds=5)
        return {"raw": db.raw_window(conn, a["mill_id"], signal, lo, hi)}


# ------------------------------------------------------------- ingestion ----
class Reading(BaseModel):
    mill_id: str
    signal: str
    timestamp: datetime
    value: float
    unit: str | None = None
    quality: int = 0


class IngestBatch(BaseModel):
    readings: list[Reading] = Field(max_length=20000)


@app.post("/api/ingest")
def ingest_batch(batch: IngestBatch, x_ingest_key: str | None = Header(default=None)):
    """Receive PLC readings (same shape as the brief's example payload) and persist them.

    Send whole-second batches: silver rollups are recomputed per second bucket.
    """
    key = os.environ.get("INGEST_KEY")
    if not key:
        raise HTTPException(503, "Ingestion disabled: INGEST_KEY not configured")
    if x_ingest_key is None or not hmac.compare_digest(x_ingest_key, key):
        raise HTTPException(401, "Bad ingest key")
    if not batch.readings:
        return {"bronze_rows": 0, "silver_rows": 0, "new_or_updated_alerts": 0}
    bad = {r.signal for r in batch.readings} - set(SIGNALS)
    if bad:
        raise HTTPException(422, f"Unknown signals: {sorted(bad)}")
    raw = pd.DataFrame([r.model_dump() for r in batch.readings]).rename(columns={"timestamp": "ts"})
    raw["ts"] = pd.to_datetime(raw["ts"], utc=True)
    raw["unit"] = raw["unit"].fillna(raw["signal"].map(SIGNALS))
    with db.connect() as conn:
        return ingest.process(conn, raw)