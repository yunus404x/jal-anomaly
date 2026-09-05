"""
STAGE 1 - INGEST
Meter load profiles and the geo-tagged pump index from the utility's HES/MDM.

A real deployment subscribes to the utility's head-end system over MQTT/REST and
parses DLMS/COSEM objects (IS 15959 Indian companion standard).  The OBIS codes
below are the ones an Indian smart meter actually publishes; the prototype reads
the same fields out of the generated export instead of off the wire.
"""
from __future__ import annotations

import json

import db
from config import DATA_DIR

# IS 15959 / DLMS-COSEM OBIS mapping actually used by Indian AMI meters
OBIS = {
    "1.0.1.8.0": "cumulative_active_energy_import_kwh",
    "1.0.1.6.0": "maximum_demand_kw",
    "1.0.99.1.0": "load_profile_block",          # 15/30-min capture period
    "1.0.0.9.2": "meter_clock_date",
    "1.0.96.7.21": "voltage_outage_event",       # supply-hour reconstruction
}
CAPTURE_PERIOD_MIN = 15


def run(bundle: dict | None = None, verbose: bool = True) -> dict:
    if bundle is None:
        bundle = json.loads((DATA_DIR / "raw_bundle.json").read_text())

    db.init(reset=True)

    db.insert_many("feeder", [{"id": f["id"], "name": f["name"], "block_id": f["block"],
                               "supply_window": f["supply_window"], "lon": f["lon"], "lat": f["lat"]}
                              for f in bundle["feeders"]])
    db.insert_many("distribution_transformer",
                   [{"id": d["id"], "feeder_id": d["feeder_id"], "block_id": d["block"],
                     "rating_kva": d["rating_kva"], "lon": d["lon"], "lat": d["lat"]}
                    for d in bundle["dts"]])

    conn_rows = []
    for c in bundle["connections"]:
        conn_rows.append({
            "connection_id": c["connection_id"], "dt_id": c["dt_id"], "feeder_id": c["feeder_id"],
            "block_id": c["block_id"], "lon": c["lon"], "lat": c["lat"], "pump_hp": c["pump_hp"],
            "sanctioned_load_kw": c["sanctioned_load_kw"], "connected_load_kw": c["connected_load_kw"],
            "rated_input_kw": c["rated_input_kw"], "crop": c["crop"], "area_ha": c["area_ha"],
            "lulc_class": c["lulc_class"], "static_water_level_m": c["static_water_level_m"],
            "total_head_m": c["total_head_m"], "connection_age_years": c["connection_age_years"],
            "whitelist_category": c["whitelist_category"],
            "distance_to_registered_ap_m": c["distance_to_registered_ap_m"],
            "block_category": c["block_category"], "stage_of_extraction": c["stage_of_extraction"],
            # truth_case is carried only so the prototype can be scored honestly;
            # the engine never reads it outside of pipeline/evaluate.py
            "attrs_json": json.dumps({k: c[k] for k in
                                      ("crop_label", "tariff", "prior_flags", "truth_case")}),
        })
    db.insert_many("connection", conn_rows)

    db.insert_many("load_profile",
                   [{"connection_id": cid, "ts": bundle["weather"]["dates"][-1],
                     "slots_json": json.dumps(slots)}
                    for cid, slots in bundle["load_profiles"].items()])

    meter_rows = [{"connection_id": r["connection_id"], "ts": r["date"], "day_index": r["day_index"],
                   "energy_kwh": r["energy_kwh"], "run_hours": r["run_hours"],
                   "night_run_hours": r["night_run_hours"], "max_demand_kw": r["max_demand_kw"],
                   "rain_mm": r["rain_mm"], "et0_mm": r["et0_mm"], "tmax_c": r["tmax_c"]}
                  for r in bundle["meter_daily"]]
    db.insert_many("meter_daily", meter_rows)
    db.insert_many("dt_daily", bundle.get("dt_daily", []))

    db.append_audit("pipeline", "system", "ingest",
                    f"{len(conn_rows)} connections",
                    {"source": "HES/MDM export (DLMS-COSEM, IS 15959)",
                     "capture_period_min": CAPTURE_PERIOD_MIN,
                     "obis_objects": list(OBIS), "meter_rows": len(meter_rows)})

    if verbose:
        print(f"[1/6] INGEST   : {len(conn_rows)} connections, {len(meter_rows)} meter-days, "
              f"{len(bundle['load_profiles'])} load profiles ({CAPTURE_PERIOD_MIN}-min blocks)")
    return bundle
