"""
STAGE 4 - MODEL
Expected crop water demand compared against observed abstraction.

    ETc          = Kc(crop, days after sowing) x ET0          [mm/d]   FAO-56
    P_eff        = min(0.75 x rainfall, 42)                   [mm/d]   USDA-SCS
    NIR          = max(0, ETc + percolation - P_eff)          [mm/d]
    Gross demand = NIR x area x 10 / irrigation efficiency     [m3/d]

Two corrections keep the baseline honest:

1. Crop-presence factor.  The declared crop in the consumer-indexing GIS is
   checked against Sentinel-2 NDVI for that pixel.  A parcel whose NDVI never
   leaves bare-soil values is not carrying the crop it claims, so its water
   entitlement collapses towards zero and the observed draw has nothing to
   justify it.
2. Field-capacity correction.  Where fused soil moisture is already at or above
   field capacity, the crop's need is met from storage, so expected irrigation
   for that day is reduced - this is what makes post-rain pumping visible.
"""
from __future__ import annotations

import db
from config import CROPS


def kc_for_day(crop: str, day: int) -> float:
    for lo, hi, kc in CROPS[crop]["kc"]:
        if lo <= day < hi:
            return kc
    return CROPS[crop]["kc"][-1][2]


def expected_ndvi(crop: str, day: int) -> float:
    """The NDVI a healthy stand of this crop should be showing on this day."""
    import math
    stage = math.sin(math.pi * min(1.0, max(0.0, (day - 8) / 74.0)))
    return CROPS[crop]["peak_ndvi"] * (0.32 + 0.68 * stage)


FIELD_CAPACITY = 0.32          # volumetric m3/m3, medium-textured Punjab loam


def run(bundle: dict, verbose: bool = True) -> dict:
    conns = {c["connection_id"]: c for c in db.query("SELECT * FROM connection")}
    rows = db.query("SELECT connection_id, ts, day_index, est_volume_m3, rain_mm, et0_mm,"
                    " sm_fused, ndvi FROM meter_daily ORDER BY connection_id, day_index")

    updates = []
    for r in rows:
        c = conns[r["connection_id"]]
        crop = CROPS[c["crop"]]
        d = r["day_index"]

        etc = kc_for_day(c["crop"], d) * (r["et0_mm"] or 0)
        p_eff = min((r["rain_mm"] or 0) * 0.75, 42.0)
        need = max(0.0, etc + crop["percolation_mm"] - p_eff)

        lo, hi = crop["puddling_window"]
        if crop["puddling_mm"] and lo <= d < hi:
            need += crop["puddling_mm"] / (hi - lo)

        # 1. crop presence, from Sentinel-2
        exp_ndvi = expected_ndvi(c["crop"], d)
        presence = 1.0
        if r["ndvi"] is not None and exp_ndvi > 0.05:
            presence = min(1.15, max(0.12, (r["ndvi"] / exp_ndvi) ** 1.4))

        # 2. soil already at field capacity -> demand met from storage
        wet_relief = 1.0
        if r["sm_fused"] is not None and r["sm_fused"] > FIELD_CAPACITY:
            wet_relief = max(0.25, 1.0 - 2.2 * (r["sm_fused"] - FIELD_CAPACITY))

        expected = need * c["area_ha"] * 10.0 / crop["irrigation_efficiency"] * presence * wet_relief
        obs = r["est_volume_m3"] or 0.0
        excess = obs - expected
        ratio = obs / expected if expected > 1.0 else (3.0 if obs > 25 else 1.0)
        updates.append((round(expected, 1), round(excess, 1), round(min(ratio, 12.0), 3),
                        r["connection_id"], r["ts"]))

    with db.connect() as con:
        con.executemany("UPDATE meter_daily SET expected_m3=?, excess_m3=?, excess_ratio=?"
                        " WHERE connection_id=? AND ts=?", updates)

    agg = db.query("SELECT SUM(est_volume_m3) v, SUM(expected_m3) e FROM meter_daily")[0]
    db.append_audit("pipeline", "system", "model", f"{len(updates)} meter-days",
                    {"method": "FAO-56 single crop coefficient with NDVI crop-presence"
                               " and field-capacity correction",
                     "observed_mcm": round((agg["v"] or 0) / 1e6, 3),
                     "expected_mcm": round((agg["e"] or 0) / 1e6, 3)})
    if verbose:
        print(f"[4/6] MODEL    : observed {agg['v']/1e6:.2f} MCM vs demand baseline "
              f"{agg['e']/1e6:.2f} MCM across the pilot")
    return bundle
