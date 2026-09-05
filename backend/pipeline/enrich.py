"""
STAGE 2 - ENRICH
Soil moisture, rainfall, NDVI and land use sampled for each pump pixel.

Source           Resolution   Revisit    Role
---------------  -----------  ---------  ------------------------------------
ISRO EOS-04      500 m        17 days    primary soil moisture (92% retrieval)
NASA SMAP L3     9 km         daily      fills the EOS-04 revisit gap
Sentinel-1 GRD   ~20 m        6-12 days  wetness corroboration
Sentinel-2       10 m         5 days     NDVI, is a crop actually growing
IMD gridded      0.25 deg     daily      rainfall
Bhuvan LULC      -            seasonal   land-use class per pixel

The fusion rule is the answer to challenge 2 on the Feasibility slide: EOS-04 is
too coarse in time to stand alone, so it is weighted by its own age and SMAP
carries the days in between.  Soil moisture corroborates, it never triggers.
"""
from __future__ import annotations

import db


def run(bundle: dict, verbose: bool = True) -> dict:
    sat_by_key = {(s["connection_id"], s["day_index"]): s for s in bundle["satellite"]}

    updates = []
    for r in bundle["meter_daily"]:
        s = sat_by_key.get((r["connection_id"], r["day_index"]))
        if not s:
            continue
        updates.append((s["sm_fused"], s["sm_eos04"], s["sm_eos04_age_days"], s["sm_smap"],
                        s["s1_wetness"], s["ndvi"], r["connection_id"], r["date"]))

    with db.connect() as con:
        con.executemany(
            "UPDATE meter_daily SET sm_fused=?, sm_eos04=?, sm_eos04_age_days=?, sm_smap=?,"
            " s1_wetness=?, ndvi=? WHERE connection_id=? AND ts=?", updates)
        # forward-fill NDVI across cloud gaps (Sentinel-2 misses ~45% of monsoon passes)
        con.execute("""
            UPDATE meter_daily SET ndvi = (
                SELECT m2.ndvi FROM meter_daily m2
                WHERE m2.connection_id = meter_daily.connection_id
                  AND m2.day_index <= meter_daily.day_index AND m2.ndvi IS NOT NULL
                ORDER BY m2.day_index DESC LIMIT 1)
            WHERE ndvi IS NULL""")
        # back-fill the head of the season (first pass may land after day 0)
        con.execute("""
            UPDATE meter_daily SET ndvi = (
                SELECT m2.ndvi FROM meter_daily m2
                WHERE m2.connection_id = meter_daily.connection_id AND m2.ndvi IS NOT NULL
                ORDER BY m2.day_index ASC LIMIT 1)
            WHERE ndvi IS NULL""")

    gaps = db.query("SELECT COUNT(*) n FROM meter_daily WHERE ndvi IS NULL")[0]["n"]
    eos_fresh = db.query("SELECT AVG(sm_eos04_age_days) a FROM meter_daily")[0]["a"] or 0

    db.append_audit("pipeline", "system", "enrich", f"{len(updates)} pixel-days",
                    {"sources": ["ISRO EOS-04 500m/17d", "NASA SMAP L3 9km daily",
                                 "Sentinel-1 GRD", "Sentinel-2 NDVI 5d",
                                 "IMD 0.25deg rainfall", "Bhuvan LULC"],
                     "mean_eos04_age_days": round(eos_fresh, 2)})
    if verbose:
        print(f"[2/6] ENRICH   : {len(updates)} pixel-days joined, mean EOS-04 age "
              f"{eos_fresh:.1f} d, NDVI gaps left after fill: {gaps}")
    return bundle
