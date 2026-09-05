"""
JAL-ANOMALY  |  FastAPI service.

    python -m uvicorn app:api --port 8000 --reload

Endpoints mirror what the production service exposes.  Role-based access is
carried in the X-Role header (viewer / officer / admin); in production this is
a Keycloak-issued JWT and the same check sits in a dependency.  Every write is
appended to the hash-chained audit log.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from config import BANDS, DATA_DIR, DISTRICT, ROOT, SIGNAL_FAMILIES
from pipeline import act

PAYLOAD_PATH = DATA_DIR / "api_payload.json"
FRONTEND = ROOT / "frontend"

api = FastAPI(title="JAL-ANOMALY", version="0.9.0-prototype",
              description="AI-powered groundwater pumping anomaly detection - SIH26015")
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache: dict = {}


def payload() -> dict:
    if not PAYLOAD_PATH.exists():
        raise HTTPException(503, "No scored run found. Run: python run_pipeline.py")
    stamp = PAYLOAD_PATH.stat().st_mtime
    if _cache.get("stamp") != stamp:
        _cache["data"] = json.loads(PAYLOAD_PATH.read_text())
        _cache["stamp"] = stamp
    return _cache["data"]


def require(role: str, allowed: set[str]) -> str:
    if role not in allowed:
        raise HTTPException(403, f"role '{role}' may not perform this action; "
                                 f"allowed: {sorted(allowed)}")
    return role


# ---------------------------------------------------------------------------
@api.get("/api/health")
def health():
    runs = db.query("SELECT * FROM model_run ORDER BY ts DESC LIMIT 1")
    return {"status": "ok", "district": DISTRICT["name"], "last_run": runs[0] if runs else None,
            "audit": db.verify_audit_chain()}


@api.get("/api/meta")
def meta():
    p = payload()
    return {**p["meta"], "band_counts": p["kpis"]["band_counts"]}


@api.get("/api/kpis")
def kpis():
    return payload()["kpis"]


@api.get("/api/map/connections")
def map_connections(band: str | None = None, feeder_id: str | None = None):
    fc = payload()["map"]["connections"]
    feats = fc["features"]
    if band:
        feats = [f for f in feats if f["properties"]["band"] == band]
    if feeder_id:
        feats = [f for f in feats if f["properties"]["feeder_id"] == feeder_id]
    return {"type": "FeatureCollection", "features": feats}


@api.get("/api/map/layers/{name}")
def map_layer(name: str):
    layers = payload()["map"]
    if name not in layers:
        raise HTTPException(404, f"unknown layer '{name}'. available: {sorted(layers)}")
    return layers[name]


@api.get("/api/alerts")
def alerts(band: str | None = None, feeder_id: str | None = None,
           limit: int = Query(200, le=1000)):
    rows = payload()["alerts"]
    if band:
        rows = [a for a in rows if a["band"] == band]
    if feeder_id:
        rows = [a for a in rows if a["feeder_id"] == feeder_id]
    live = {r["alert_id"]: r["status"] for r in db.query("SELECT alert_id, status FROM alert")}
    return [{**a, "status": live.get(a["alert_id"], a["status"])} for a in rows[:limit]]


@api.get("/api/connections/{connection_id}")
def connection_detail(connection_id: str):
    d = payload()["details"].get(connection_id)
    if not d:
        raise HTTPException(404, connection_id)
    fb = db.query("SELECT * FROM officer_feedback WHERE connection_id=? ORDER BY created_ts",
                  (connection_id,))
    alert = db.query("SELECT * FROM alert WHERE connection_id=? ORDER BY created_ts DESC LIMIT 1",
                     (connection_id,))
    return {**d, "feedback": fb, "alert": alert[0] if alert else None}


class Feedback(BaseModel):
    decision: str            # confirmed | cleared | field_visit
    note: str = ""
    officer: str = "field.officer@pspcl"


@api.post("/api/alerts/{alert_id}/feedback")
def feedback(alert_id: str, body: Feedback, x_role: str = Header(default="officer")):
    require(x_role, {"officer", "admin"})
    if body.decision not in ("confirmed", "cleared", "field_visit"):
        raise HTTPException(400, "decision must be confirmed, cleared or field_visit")
    try:
        return act.record_feedback(alert_id, body.decision, body.note, body.officer, x_role)
    except KeyError:
        raise HTTPException(404, alert_id)


@api.get("/api/feedback")
def feedback_list():
    return {"labels": act.training_labels(),
            "counts": db.query("SELECT decision, COUNT(*) n FROM officer_feedback GROUP BY decision")}


@api.post("/api/retrain")
def retrain(x_role: str = Header(default="viewer")):
    """Re-score the district with officer decisions folded into the label set."""
    require(x_role, {"admin"})
    labels = act.training_labels()
    db.append_audit("api", x_role, "retrain_requested", "model",
                    {"officer_labels_available": len(labels)})
    proc = subprocess.run([sys.executable, "run_pipeline.py", "--keep"],
                          cwd=Path(__file__).resolve().parent,
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr[-2000:])
    _cache.clear()
    return {"status": "retrained", "officer_labels_used": len(labels),
            "log": proc.stdout.splitlines()[-8:], "metrics": payload()["meta"]["metrics"]}


@api.get("/api/audit")
def audit(limit: int = 100):
    return {"chain": db.verify_audit_chain(),
            "entries": db.query("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,))}


@api.get("/api/energy-accounting")
def energy_accounting():
    """DT-level energy balance: input energy against the sum of consumer meters."""
    rows = db.query("""SELECT d.dt_id, dt.feeder_id, SUM(d.input_kwh) input_kwh,
                              SUM(d.metered_sum_kwh) metered_kwh
                       FROM dt_daily d JOIN distribution_transformer dt ON dt.id = d.dt_id
                       GROUP BY d.dt_id ORDER BY 1""")
    out = []
    for r in rows:
        gap = (r["input_kwh"] - r["metered_kwh"]) / max(r["input_kwh"], 1)
        out.append({**r, "loss_share": round(gap, 4),
                    "unexplained_share": round(max(0.0, gap - 0.07), 4),
                    "input_kwh": round(r["input_kwh"]), "metered_kwh": round(r["metered_kwh"])})
    return sorted(out, key=lambda x: -x["unexplained_share"])


@api.get("/api/bands")
def bands():
    return {"bands": BANDS, "families": SIGNAL_FAMILIES}


# --- static console ---------------------------------------------------------
if FRONTEND.exists():
    api.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @api.get("/")
    def index():
        return FileResponse(str(FRONTEND / "index.html"))

    @api.get("/api/payload")
    def full_payload():
        """Everything the console needs in one request (used by the offline build)."""
        return JSONResponse(payload())
