-- =========================================================================
-- JAL-ANOMALY  |  production schema  (TimescaleDB + PostGIS)
--
-- The prototype runs the same tables in SQLite (backend/db.py) so it starts
-- with one command.  This file is the target the pilot deploys against: the
-- table and column names are identical, so the migration is a connection
-- string plus the two extensions below.
-- =========================================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------- network
CREATE TABLE feeder (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    block_id          TEXT NOT NULL,
    supply_window     TEXT CHECK (supply_window IN ('day','night')),
    geom              geometry(Point, 4326)
);
CREATE INDEX feeder_geom_gix ON feeder USING GIST (geom);

CREATE TABLE distribution_transformer (
    id                TEXT PRIMARY KEY,
    feeder_id         TEXT REFERENCES feeder(id),
    block_id          TEXT NOT NULL,
    rating_kva        INTEGER,
    geom              geometry(Point, 4326)
);
CREATE INDEX dt_geom_gix ON distribution_transformer USING GIST (geom);

-- Geo-tagged pump index from the utility's consumer-indexing GIS.
-- No personal identifiers are carried into the analytics layer: the
-- connection id is the only key, and it is pseudonymised on ingest.
CREATE TABLE connection (
    connection_id                TEXT PRIMARY KEY,
    dt_id                        TEXT REFERENCES distribution_transformer(id),
    feeder_id                    TEXT REFERENCES feeder(id),
    block_id                     TEXT NOT NULL,
    geom                         geometry(Point, 4326) NOT NULL,
    parcel                       geometry(Polygon, 4326),
    pump_hp                      REAL,
    sanctioned_load_kw           REAL,
    connected_load_kw            REAL,
    rated_input_kw               REAL,
    crop                         TEXT,
    area_ha                      REAL,
    lulc_class                   TEXT,
    static_water_level_m         REAL,
    total_head_m                 REAL,
    connection_age_years         INTEGER,
    whitelist_category           TEXT,
    distance_to_registered_ap_m  INTEGER,
    block_category               TEXT,
    stage_of_extraction          REAL,
    attrs                        JSONB
);
CREATE INDEX connection_geom_gix ON connection USING GIST (geom);
CREATE INDEX connection_feeder_ix ON connection (feeder_id);

-- --------------------------------------------------------- meter time series
CREATE TABLE meter_daily (
    connection_id      TEXT NOT NULL REFERENCES connection(connection_id),
    ts                 TIMESTAMPTZ NOT NULL,
    day_index          INTEGER,
    energy_kwh         REAL,
    run_hours          REAL,
    night_run_hours    REAL,
    max_demand_kw      REAL,
    rain_mm            REAL,
    et0_mm             REAL,
    tmax_c             REAL,
    sm_fused           REAL,
    sm_eos04           REAL,
    sm_eos04_age_days  INTEGER,
    sm_smap            REAL,
    s1_wetness         REAL,
    ndvi               REAL,
    est_volume_m3      REAL,
    expected_m3        REAL,
    excess_m3          REAL,
    excess_ratio       REAL,
    PRIMARY KEY (connection_id, ts)
);
SELECT create_hypertable('meter_daily', 'ts', chunk_time_interval => INTERVAL '7 days');
SELECT add_compression_policy('meter_daily', INTERVAL '90 days');

-- Raw 15-minute DLMS load-profile blocks land here before daily rollup.
CREATE TABLE meter_block (
    connection_id  TEXT NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    active_kw      REAL,
    voltage_v      REAL,
    current_a      REAL,
    PRIMARY KEY (connection_id, ts)
);
SELECT create_hypertable('meter_block', 'ts', chunk_time_interval => INTERVAL '1 day');

CREATE TABLE dt_daily (
    dt_id            TEXT NOT NULL REFERENCES distribution_transformer(id),
    ts               TIMESTAMPTZ NOT NULL,
    day_index        INTEGER,
    input_kwh        REAL,
    metered_sum_kwh  REAL,
    PRIMARY KEY (dt_id, ts)
);
SELECT create_hypertable('dt_daily', 'ts', chunk_time_interval => INTERVAL '30 days');

-- ------------------------------------------------------------------ scoring
CREATE TABLE risk_feature (
    connection_id  TEXT PRIMARY KEY REFERENCES connection(connection_id),
    run_id         TEXT,
    features       JSONB,
    families       JSONB
);

CREATE TABLE alert (
    alert_id            TEXT PRIMARY KEY,
    connection_id       TEXT REFERENCES connection(connection_id),
    run_id              TEXT,
    created_ts          TIMESTAMPTZ DEFAULT now(),
    risk_score          REAL,
    band                TEXT CHECK (band IN ('NORMAL','MONITOR','SUSPICIOUS','HIGH_RISK')),
    triggered_families  INTEGER,
    est_excess_m3       REAL,
    est_excess_kwh      REAL,
    reasons             JSONB,
    families            JSONB,
    status              TEXT DEFAULT 'open'
);
CREATE INDEX alert_band_ix ON alert (band, risk_score DESC);

CREATE TABLE officer_feedback (
    feedback_id    BIGSERIAL PRIMARY KEY,
    alert_id       TEXT REFERENCES alert(alert_id),
    connection_id  TEXT REFERENCES connection(connection_id),
    decision       TEXT CHECK (decision IN ('confirmed','cleared','field_visit')),
    note           TEXT,
    officer        TEXT,
    role           TEXT,
    created_ts     TIMESTAMPTZ DEFAULT now()
);

-- Append-only, hash-chained.  The application role is granted INSERT and
-- SELECT only; no UPDATE or DELETE grant is ever issued on this table.
CREATE TABLE audit_log (
    seq        BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ DEFAULT now(),
    actor      TEXT,
    role       TEXT,
    action     TEXT,
    subject    TEXT,
    payload    JSONB,
    prev_hash  TEXT,
    hash       TEXT
);
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;

CREATE TABLE model_run (
    run_id         TEXT PRIMARY KEY,
    ts             TIMESTAMPTZ DEFAULT now(),
    n_connections  INTEGER,
    n_alerts       INTEGER,
    metrics        JSONB,
    config         JSONB
);

-- ---------------------------------------------------------------- rollups
CREATE MATERIALIZED VIEW block_excess AS
SELECT c.block_id,
       date_trunc('month', m.ts)          AS month,
       SUM(GREATEST(m.excess_m3, 0))      AS excess_m3,
       COUNT(DISTINCT c.connection_id)    AS connections
FROM meter_daily m JOIN connection c USING (connection_id)
GROUP BY 1, 2;
