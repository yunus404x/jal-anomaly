"""
STAGE 6 - ACT
Colour-coded risk map, officer review, and feedback that retrains the model.

This stage turns scores into the three things a utility actually uses: a map
layer, a work queue ordered by evidence, and a decision record.  Every officer
decision is written to the append-only audit log and becomes a training label
for the next run - the loop the Impact slide describes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import db
from config import BANDS, BLOCKS, DATA_DIR, DISTRICT, SIGNAL_FAMILIES

# PSPCL agricultural supply is unmetered-flat / subsidised; the state pays the
# DISCOM roughly this much per unit sold to agriculture (PSERC tariff order).
SUBSIDY_RS_PER_KWH = 5.66


def build_alerts(results: list[dict], run_id: str) -> list[dict]:
    alerts = []
    for r in sorted(results, key=lambda x: -x["risk_score"]):
        if r["band"] == "NORMAL":
            continue
        alerts.append({
            "alert_id": f"JA-{run_id[:6].upper()}-{len(alerts)+1:04d}",
            "connection_id": r["connection_id"], "run_id": run_id, "created_ts": r["ts"],
            "risk_score": r["risk_score"], "band": r["band"],
            "triggered_families": len(r["triggered_families"]),
            "est_excess_m3": r["est_excess_m3"], "est_excess_kwh": r["est_excess_kwh"],
            "reasons_json": json.dumps(r["reasons"]), "families_json": json.dumps(r["families"]),
            "status": "open",
        })
    db.insert_many("alert", alerts)
    return alerts


def record_feedback(alert_id: str, decision: str, note: str, officer: str, role: str) -> dict:
    """Officer confirms, clears, or queues a field visit.  Append-only."""
    rows = db.query("SELECT * FROM alert WHERE alert_id=?", (alert_id,))
    if not rows:
        raise KeyError(alert_id)
    alert = rows[0]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.insert_many("officer_feedback", [{"alert_id": alert_id, "connection_id": alert["connection_id"],
                                         "decision": decision, "note": note, "officer": officer,
                                         "role": role, "created_ts": ts}])
    status = {"confirmed": "confirmed", "cleared": "cleared",
              "field_visit": "field_visit_queued"}.get(decision, "open")
    with db.connect() as con:
        con.execute("UPDATE alert SET status=? WHERE alert_id=?", (status, alert_id))
    h = db.append_audit(officer, role, f"feedback:{decision}", alert_id,
                        {"connection_id": alert["connection_id"], "note": note,
                         "risk_score": alert["risk_score"], "band": alert["band"]})
    return {"alert_id": alert_id, "status": status, "audit_hash": h[:16], "ts": ts}


def training_labels() -> list[dict]:
    """Officer decisions become supervised labels for the next scoring run."""
    return db.query("""SELECT connection_id, decision, created_ts FROM officer_feedback
                       WHERE decision IN ('confirmed','cleared') ORDER BY created_ts""")


# ---------------------------------------------------------------------------
def build_payload(bundle: dict, verbose: bool = True) -> dict:
    results = {r["connection_id"]: r for r in bundle["results"]}
    conns = db.query("SELECT * FROM connection")
    for c in conns:
        c.update(json.loads(c.pop("attrs_json")))
    conn_by_id = {c["connection_id"]: c for c in conns}
    run_id = bundle["run_id"]
    alerts = build_alerts(bundle["results"], run_id)

    # ---- map: one point per connection, coloured by band -------------------
    features = []
    for c in conns:
        r = results[c["connection_id"]]
        features.append({
            "type": "Feature",
            "properties": {
                "connection_id": c["connection_id"], "risk_score": r["risk_score"],
                "band": r["band"], "colour": r["band_colour"],
                "feeder_id": c["feeder_id"], "dt_id": c["dt_id"], "block_id": c["block_id"],
                "block_category": c["block_category"], "crop": c["crop"],
                "crop_label": c["crop_label"], "area_ha": c["area_ha"],
                "pump_hp": c["pump_hp"], "excess_m3": r["est_excess_m3"],
                "whitelist": c["whitelist_category"],
                "families": r["families"], "n_triggered": len(r["triggered_families"]),
            },
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
        })
    connections_fc = {"type": "FeatureCollection", "features": features}

    feeders = db.query("SELECT * FROM feeder")
    dts = db.query("SELECT * FROM distribution_transformer")
    feeder_pt = {f["id"]: (f["lon"], f["lat"]) for f in feeders}
    dt_lines = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"feeder_id": d["feeder_id"]},
         "geometry": {"type": "LineString",
                      "coordinates": [list(feeder_pt[d["feeder_id"]]), [d["lon"], d["lat"]]]}}
        for d in dts if d["feeder_id"] in feeder_pt]}
    dt_points = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": d["id"], "feeder_id": d["feeder_id"],
                                           "rating_kva": d["rating_kva"]},
         "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]}} for d in dts]}

    # ---- KPIs --------------------------------------------------------------
    band_counts = {b["key"]: 0 for b in BANDS}
    for r in results.values():
        band_counts[r["band"]] += 1
    flagged = [r for r in results.values() if r["band"] != "NORMAL"]
    excess_total = sum(r["est_excess_m3"] for r in flagged)
    excess_kwh = sum(r["est_excess_kwh"] for r in flagged)
    all_excess = sorted((r["est_excess_m3"] for r in results.values()), reverse=True)
    top4 = sum(all_excess[:max(1, len(all_excess) // 25)])

    feeder_rows = []
    for f in feeders:
        ids = [c["connection_id"] for c in conns if c["feeder_id"] == f["id"]]
        rs = [results[i] for i in ids]
        feeder_rows.append({
            "feeder_id": f["id"], "name": f["name"], "block_id": f["block_id"],
            "supply_window": f["supply_window"], "connections": len(ids),
            "energy_mwh": round(sum(r["features"]["_season_energy_kwh"] for r in rs) / 1000, 1),
            "volume_mcm": round(sum(r["features"]["_season_volume_m3"] for r in rs) / 1e6, 3),
            "expected_mcm": round(sum(r["features"]["_season_expected_m3"] for r in rs) / 1e6, 3),
            "alerts": sum(1 for r in rs if r["band"] != "NORMAL"),
            "high_risk": sum(1 for r in rs if r["band"] == "HIGH_RISK"),
        })

    block_rows = []
    for bid in sorted({c["block_id"] for c in conns}):
        rs = [results[c["connection_id"]] for c in conns if c["block_id"] == bid]
        c0 = next(c for c in conns if c["block_id"] == bid)
        block_rows.append({
            "block_id": bid,
            "name": next((b["name"] for b in BLOCKS if b["id"] == bid), bid),
            "category": c0["block_category"], "stage_of_extraction": c0["stage_of_extraction"],
            "connections": len(rs), "alerts": sum(1 for r in rs if r["band"] != "NORMAL"),
            "excess_mcm": round(sum(r["est_excess_m3"] for r in rs) / 1e6, 3),
        })

    kpis = {
        "connections_monitored": len(conns),
        "meter_days": db.query("SELECT COUNT(*) n FROM meter_daily")[0]["n"],
        "season": DISTRICT["season"],
        "band_counts": band_counts,
        "alerts_open": len(alerts),
        "estimated_excess_mcm": round(excess_total / 1e6, 3),
        "estimated_excess_kwh": round(excess_kwh, 0),
        "estimated_subsidy_value_rs": round(excess_kwh * SUBSIDY_RS_PER_KWH, 0),
        "share_of_excess_in_top_4pct": round(top4 / max(sum(all_excess), 1), 3),
        "inspection_shortlist": band_counts["SUSPICIOUS"] + band_counts["HIGH_RISK"],
        "shortlist_pct": round(100 * (band_counts["SUSPICIOUS"] + band_counts["HIGH_RISK"]) / len(conns), 1),
        "feeders": feeder_rows, "blocks": block_rows,
    }

    # ---- per-connection detail --------------------------------------------
    series_rows = db.query("SELECT * FROM meter_daily ORDER BY connection_id, day_index")
    by_conn: dict[str, list] = {}
    for r in series_rows:
        by_conn.setdefault(r["connection_id"], []).append(r)
    profiles = {p["connection_id"]: json.loads(p["slots_json"])
                for p in db.query("SELECT * FROM load_profile")}

    details = {}
    for c in conns:
        cid = c["connection_id"]
        rs = by_conn[cid]
        r = results[cid]
        details[cid] = {
            "connection": {k: c[k] for k in
                           ("connection_id", "dt_id", "feeder_id", "block_id", "block_category",
                            "stage_of_extraction", "lon", "lat", "pump_hp", "sanctioned_load_kw",
                            "connected_load_kw", "rated_input_kw", "crop", "crop_label", "area_ha",
                            "lulc_class", "static_water_level_m", "total_head_m",
                            "connection_age_years", "whitelist_category",
                            "distance_to_registered_ap_m", "tariff")},
            "risk": {"score": r["risk_score"], "raw": r["risk_raw"], "band": r["band"],
                     "colour": r["band_colour"], "families": r["families"],
                     "triggered": r["triggered_families"], "capped_by": r["capped_by"],
                     "detectors": r["detectors"], "reasons": r["reasons"],
                     "excess_m3": r["est_excess_m3"], "excess_kwh": r["est_excess_kwh"],
                     "excess_lo": r["excess_m3_lo"], "excess_hi": r["excess_m3_hi"],
                     "conversion": r["conversion"],
                     "season_volume_m3": r["features"]["_season_volume_m3"],
                     "season_expected_m3": r["features"]["_season_expected_m3"],
                     "season_energy_kwh": r["features"]["_season_energy_kwh"]},
            "series": {
                "date": [x["ts"] for x in rs],
                "energy_kwh": [round(x["energy_kwh"], 1) for x in rs],
                "run_hours": [round(x["run_hours"], 1) for x in rs],
                "volume_m3": [round(x["est_volume_m3"] or 0) for x in rs],
                "expected_m3": [round(x["expected_m3"] or 0) for x in rs],
                "rain_mm": [round(x["rain_mm"], 1) for x in rs],
                "sm_fused": [round(x["sm_fused"], 3) if x["sm_fused"] is not None else None for x in rs],
                "ndvi": [round(x["ndvi"], 3) if x["ndvi"] is not None else None for x in rs],
            },
            "load_profile": profiles.get(cid, []),
        }

    payload = {
        "meta": {
            "app": "JAL-ANOMALY", "problem_statement": "SIH26015", "team": "SyntaxError",
            "district": DISTRICT, "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bands": BANDS, "families": SIGNAL_FAMILIES, "metrics": bundle["metrics"],
            "subsidy_rs_per_kwh": SUBSIDY_RS_PER_KWH,
            "data_sources": [
                "DISCOM HES/MDM load profiles (DLMS-COSEM, IS 15959)",
                "Consumer-indexing GIS (geo-tagged pump index)",
                "ISRO EOS-04 (RISAT-1A) soil moisture, 500 m / 17-day",
                "NASA SMAP L3 soil moisture, 9 km / daily",
                "Sentinel-1 GRD backscatter", "Sentinel-2 NDVI",
                "IMD gridded rainfall 0.25 deg",
                "Bhuvan LULC, SRISHTI watershed layers, DRISHTI field photos",
                "CGWB DWLR / India-WRIS water levels",
            ],
        },
        "kpis": kpis,
        "alerts": [{**a, "reasons": json.loads(a["reasons_json"]),
                    "families": json.loads(a["families_json"]),
                    "crop": conn_by_id[a["connection_id"]]["crop_label"],
                    "area_ha": conn_by_id[a["connection_id"]]["area_ha"],
                    "block_id": conn_by_id[a["connection_id"]]["block_id"],
                    "feeder_id": conn_by_id[a["connection_id"]]["feeder_id"],
                    "whitelist": conn_by_id[a["connection_id"]]["whitelist_category"],
                    "lon": conn_by_id[a["connection_id"]]["lon"],
                    "lat": conn_by_id[a["connection_id"]]["lat"]}
                   for a in alerts],
        "map": {"connections": connections_fc, "blocks": bundle["layers"]["blocks"],
                "soil_moisture": bundle["layers"]["soil_moisture"],
                "ndvi": bundle["layers"]["ndvi"], "rainfall_7d": bundle["layers"]["rainfall_7d"],
                "feeder_lines": dt_lines, "dts": dt_points},
        "details": details,
    }

    for a in payload["alerts"]:
        a.pop("reasons_json", None); a.pop("families_json", None)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "api_payload.json").write_text(json.dumps(payload, separators=(",", ":")))

    db.insert_many("model_run", [{"run_id": run_id, "ts": payload["meta"]["generated_at"],
                                  "n_connections": len(conns), "n_alerts": len(alerts),
                                  "metrics_json": json.dumps(bundle["metrics"]),
                                  "config_json": json.dumps({"bands": BANDS})}])
    db.append_audit("pipeline", "system", "act", f"run {run_id}",
                    {"alerts": len(alerts), "band_counts": band_counts,
                     "excess_mcm": kpis["estimated_excess_mcm"]})

    if verbose:
        size = (DATA_DIR / "api_payload.json").stat().st_size / 1e6
        print(f"[6/6] ACT      : {len(alerts)} alerts queued "
              f"({band_counts['HIGH_RISK']} high risk, {band_counts['SUSPICIOUS']} suspicious, "
              f"{band_counts['MONITOR']} monitor)")
        print(f"        excess   : {kpis['estimated_excess_mcm']} MCM, "
              f"{kpis['estimated_excess_kwh']/1000:.0f} MWh, "
              f"Rs {kpis['estimated_subsidy_value_rs']/1e5:.1f} lakh of subsidised energy")
        print(f"        payload  : data/api_payload.json ({size:.1f} MB)")
    return payload
