"""
JAL-ANOMALY  |  Storage layer.

In production this is TimescaleDB (meter hypertables) + PostGIS (parcels,
feeders, alerts) + MinIO (Cloud-Optimised GeoTIFFs) -- see deploy/sql/schema.sql
for the exact DDL.  For the prototype the same tables live in a single SQLite
file so the demo starts with `python run_pipeline.py` and no services.

Table names, columns and indexes are deliberately identical to the Postgres
schema, so swapping the connection string is the only migration.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DATA_DIR

# JAL_DB_PATH lets the prototype run when the project folder is on a network or
# cloud-synced drive, where SQLite's file locking is unreliable:
#     JAL_DB_PATH=/tmp/jal.db python run_pipeline.py
DB_PATH = Path(os.environ.get("JAL_DB_PATH", DATA_DIR / "jal.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeder (
    id TEXT PRIMARY KEY, name TEXT, block_id TEXT, supply_window TEXT,
    lon REAL, lat REAL);

CREATE TABLE IF NOT EXISTS distribution_transformer (
    id TEXT PRIMARY KEY, feeder_id TEXT, block_id TEXT,
    rating_kva INTEGER, lon REAL, lat REAL);

-- PostGIS: geom geometry(Point,4326) + GIST index
CREATE TABLE IF NOT EXISTS connection (
    connection_id TEXT PRIMARY KEY, dt_id TEXT, feeder_id TEXT, block_id TEXT,
    lon REAL, lat REAL, pump_hp REAL, sanctioned_load_kw REAL,
    connected_load_kw REAL, rated_input_kw REAL, crop TEXT, area_ha REAL,
    lulc_class TEXT, static_water_level_m REAL, total_head_m REAL,
    connection_age_years INTEGER, whitelist_category TEXT,
    distance_to_registered_ap_m INTEGER, block_category TEXT,
    stage_of_extraction REAL, attrs_json TEXT);

-- TimescaleDB: SELECT create_hypertable('meter_daily','ts', chunk_time_interval => INTERVAL '7 days')
CREATE TABLE IF NOT EXISTS meter_daily (
    connection_id TEXT, ts TEXT, day_index INTEGER,
    energy_kwh REAL, run_hours REAL, night_run_hours REAL, max_demand_kw REAL,
    rain_mm REAL, et0_mm REAL, tmax_c REAL,
    sm_fused REAL, sm_eos04 REAL, sm_eos04_age_days INTEGER, sm_smap REAL,
    s1_wetness REAL, ndvi REAL,
    est_volume_m3 REAL, expected_m3 REAL, excess_m3 REAL, excess_ratio REAL,
    PRIMARY KEY (connection_id, ts));
CREATE INDEX IF NOT EXISTS meter_daily_ts ON meter_daily(ts);

-- DT-level energy accounting: input energy vs the sum of the meters below it
CREATE TABLE IF NOT EXISTS dt_daily (
    dt_id TEXT, ts TEXT, day_index INTEGER,
    input_kwh REAL, metered_sum_kwh REAL,
    PRIMARY KEY (dt_id, ts));

CREATE TABLE IF NOT EXISTS load_profile (
    connection_id TEXT PRIMARY KEY, ts TEXT, slots_json TEXT);

CREATE TABLE IF NOT EXISTS risk_feature (
    connection_id TEXT PRIMARY KEY, features_json TEXT, families_json TEXT);

CREATE TABLE IF NOT EXISTS alert (
    alert_id TEXT PRIMARY KEY, connection_id TEXT, run_id TEXT, created_ts TEXT,
    risk_score REAL, band TEXT, triggered_families INTEGER,
    est_excess_m3 REAL, est_excess_kwh REAL,
    reasons_json TEXT, families_json TEXT, status TEXT DEFAULT 'open');
CREATE INDEX IF NOT EXISTS alert_band ON alert(band);

CREATE TABLE IF NOT EXISTS officer_feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT, connection_id TEXT, decision TEXT, note TEXT,
    officer TEXT, role TEXT, created_ts TEXT);

-- append-only, hash-chained: no UPDATE or DELETE grant is issued on this table
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, role TEXT,
    action TEXT, subject TEXT, payload_json TEXT, prev_hash TEXT, hash TEXT);

CREATE TABLE IF NOT EXISTS model_run (
    run_id TEXT PRIMARY KEY, ts TEXT, n_connections INTEGER, n_alerts INTEGER,
    metrics_json TEXT, config_json TEXT);
"""


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:                       # WAL is faster but is not supported on every mount
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    try:
        yield con
        con.commit()
    finally:
        con.close()


# Re-scoring clears the derived tables and re-ingests, but officer decisions and
# the audit log are records of what people did: they are never dropped.
DERIVED_TABLES = ["feeder", "distribution_transformer", "connection", "meter_daily",
                  "dt_daily", "load_profile", "risk_feature", "alert"]


def init(reset: bool = False) -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        if reset:
            for t in DERIVED_TABLES:
                con.execute(f"DELETE FROM {t}")


def insert_many(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as con:
        con.executemany(sql, [[r.get(c) for c in cols] for r in rows])


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def append_audit(actor: str, role: str, action: str, subject: str, payload: dict) -> str:
    """Hash-chained append-only entry: each row commits to the previous one."""
    import hashlib
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as con:
        prev = con.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = prev["hash"] if prev else "GENESIS"
        body = json.dumps({"ts": ts, "actor": actor, "role": role, "action": action,
                           "subject": subject, "payload": payload}, sort_keys=True)
        h = hashlib.sha256((prev_hash + body).encode()).hexdigest()
        con.execute("INSERT INTO audit_log (ts,actor,role,action,subject,payload_json,prev_hash,hash)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (ts, actor, role, action, subject, json.dumps(payload), prev_hash, h))
    return h


def verify_audit_chain() -> dict:
    import hashlib
    rows = query("SELECT * FROM audit_log ORDER BY seq")
    prev_hash = "GENESIS"
    for r in rows:
        body = json.dumps({"ts": r["ts"], "actor": r["actor"], "role": r["role"],
                           "action": r["action"], "subject": r["subject"],
                           "payload": json.loads(r["payload_json"])}, sort_keys=True)
        h = hashlib.sha256((prev_hash + body).encode()).hexdigest()
        if h != r["hash"] or r["prev_hash"] != prev_hash:
            return {"valid": False, "broken_at": r["seq"], "entries": len(rows)}
        prev_hash = h
    return {"valid": True, "entries": len(rows), "head": prev_hash[:16]}
