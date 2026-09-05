"""
Feature construction for the five evidence families.

Every feature is a plain, inspectable quantity computed from the enriched
meter-day table.  Nothing here is a black box: an officer can be shown the
number, the threshold it crossed and the days it came from.
"""
from __future__ import annotations

import numpy as np

import db
from config import CROPS, SIGNAL_FAMILIES

WET_SOIL = 0.32              # volumetric field capacity
HEAVY_RAIN_MM = 20.0
TECHNICAL_LOSS_ALLOWANCE = 0.07   # I2R + transformer loss a DT is allowed

# season m3/ha that a well-irrigated crop should not need to exceed
CROP_NORM_M3_PER_HA = {"paddy": 14500, "cotton": 5200, "maize": 5600,
                       "fodder": 6400, "vegetables": 6800}


def ramp(v, lo, hi):
    """Linear 0..1 concern between two documented thresholds."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0.0
    if hi == lo:
        return 0.0
    return float(np.clip((v - lo) / (hi - lo), 0.0, 1.0))


def _series(rows: list[dict]) -> dict:
    a = {k: np.array([r[k] if r[k] is not None else np.nan for r in rows], dtype=float)
         for k in ("energy_kwh", "run_hours", "night_run_hours", "max_demand_kw", "rain_mm",
                   "et0_mm", "sm_fused", "ndvi", "est_volume_m3", "expected_m3", "excess_ratio")}
    a["rain3"] = np.convolve(np.nan_to_num(a["rain_mm"]), np.ones(3), "same")
    return a


def build(verbose: bool = True) -> tuple[list[dict], list[str]]:
    conns = db.query("SELECT * FROM connection")
    supply_window = {f["id"]: f["supply_window"] for f in db.query("SELECT * FROM feeder")}
    by_conn: dict[str, list[dict]] = {}
    for r in db.query("SELECT * FROM meter_daily ORDER BY connection_id, day_index"):
        by_conn.setdefault(r["connection_id"], []).append(r)

    # peer groups: same crop, same block -- the comparison an officer would make
    peer_kwh_ha: dict[tuple, list] = {}
    crop_sm: dict[str, list] = {}
    for c in conns:
        s = _series(by_conn[c["connection_id"]])
        peer_kwh_ha.setdefault((c["crop"], c["block_id"]), []).append(
            float(np.nansum(s["energy_kwh"])) / max(c["area_ha"], 0.1))
        crop_sm.setdefault(c["crop"], []).extend(np.nan_to_num(s["sm_fused"]).tolist())
    peer_median = {k: float(np.median(v)) for k, v in peer_kwh_ha.items()}
    # "wet" is crop-relative: a puddled paddy field is supposed to be saturated,
    # so the bar is the wettest decile of soil moisture observed for that crop -
    # wetter than the crop itself ever needs.
    crop_wet = {k: float(np.percentile(np.array(v), 90)) for k, v in crop_sm.items()}
    crop_sm_med = {k: float(np.median(np.array(v))) for k, v in crop_sm.items()}

    # DT energy accounting: unexplained loss beyond the technical allowance
    dt_gap = {}
    for r in db.query("""SELECT dt_id, SUM(input_kwh) i, SUM(metered_sum_kwh) m
                         FROM dt_daily GROUP BY dt_id"""):
        if r["i"]:
            dt_gap[r["dt_id"]] = round(max(0.0, (r["i"] - r["m"]) / r["i"] - TECHNICAL_LOSS_ALLOWANCE), 4)

    out = []
    for c in conns:
        rows = by_conn[c["connection_id"]]
        s = _series(rows)
        n = len(rows)
        area = max(c["area_ha"], 0.1)
        vol = np.nan_to_num(s["est_volume_m3"])
        exp = np.nan_to_num(s["expected_m3"])
        total_vol = float(vol.sum())
        f: dict[str, float] = {}

        # ---------------- ELECTRICITY -------------------------------------
        kwh_ha = float(np.nansum(s["energy_kwh"])) / area
        f["_elec_kwh_per_ha"] = round(kwh_ha, 1)
        f["elec_peer_ratio"] = round(kwh_ha / max(peer_median[(c["crop"], c["block_id"])], 1.0), 3)
        f["elec_run_hours_p90"] = round(float(np.nanpercentile(s["run_hours"], 90)), 2)
        run_sum = float(np.nansum(s["run_hours"]))
        night_sum = float(np.nansum(s["night_run_hours"]))
        f["elec_night_share"] = round(night_sum / max(run_sum, 0.1), 3)
        # hours run outside the feeder's declared agricultural supply window
        off_sum = (run_sum - night_sum) if supply_window.get(c["feeder_id"]) == "night" else night_sum
        f["elec_offwindow_share"] = round(off_sum / max(run_sum, 0.1), 3)
        med14 = np.array([np.nanmedian(s["energy_kwh"][max(0, i - 14):i + 1]) for i in range(n)])
        f["elec_spike_days"] = int(np.sum(s["energy_kwh"] > 1.8 * np.maximum(med14, 1.0)))
        f["elec_load_extension"] = round(c["connected_load_kw"] / max(c["sanctioned_load_kw"], 0.1), 3)
        f["elec_supply_hours_exceeded"] = int(np.sum(s["run_hours"] > 12.0))
        f["elec_dt_loss_share"] = dt_gap.get(c["dt_id"], 0.0)
        # metered energy below what a visibly growing crop needed -> under-registration
        f["elec_demand_deficit"] = round(max(0.0, 1.0 - total_vol / max(float(exp.sum()), 1.0)), 3)

        # ---------------- WEATHER -----------------------------------------
        heavy = np.zeros(n, dtype=bool)
        for i in range(n):
            if s["rain_mm"][i] >= HEAVY_RAIN_MM:
                heavy[i:min(n, i + 3)] = True
        # the comparison that matters: pumping after rain against pumping in a
        # dry spell.  A rain-responsive farmer falls to a third; a rain-blind
        # one carries on at the same rate.
        dry = np.nan_to_num(s["rain3"]) < 5.0
        dry_mean = float(np.mean(vol[dry])) if dry.any() else float(np.mean(vol))
        f["wx_post_rain_ratio"] = (round(float(np.mean(vol[heavy])) / max(dry_mean, 1e-6), 3)
                                   if heavy.any() else 0.5)
        with np.errstate(invalid="ignore"):
            corr = np.corrcoef(np.nan_to_num(s["rain3"]), vol)[0, 1] if n > 5 else 0.0
        f["wx_rain_correlation"] = round(float(0.0 if np.isnan(corr) else corr), 3)
        wet_days = np.nan_to_num(s["rain3"]) >= 25.0
        f["wx_wet_day_volume_share"] = round(float(vol[wet_days].sum()) / max(total_vol, 1e-6), 3)
        f["_wx_heavy_rain_days"] = int(np.sum(s["rain_mm"] >= HEAVY_RAIN_MM))

        # ---------------- SATELLITE ---------------------------------------
        wet_soil = np.nan_to_num(s["sm_fused"]) > crop_wet.get(c["crop"], WET_SOIL)
        f["sat_wet_soil_volume_share"] = round(float(vol[wet_soil].sum()) / max(total_vol, 1e-6), 3)
        f["sat_volume_weighted_sm"] = round(float(np.nansum(np.nan_to_num(s["sm_fused"]) * vol) /
                                                  max(total_vol, 1e-6)), 4)
        f["_sat_wet_threshold"] = round(crop_wet.get(c["crop"], WET_SOIL), 3)
        f["sat_sm_above_crop_norm"] = round(f["sat_volume_weighted_sm"] -
                                            crop_sm_med.get(c["crop"], WET_SOIL), 4)
        # Does this connection pump as hard on its own wettest days as on its
        # driest?  A farmer reading the field backs off; a rain-blind schedule
        # does not.  Independent of the rain gauge - this is the SAR/SMAP view.
        sm_arr = np.nan_to_num(s["sm_fused"])
        lo_t, hi_t = np.percentile(sm_arr, [33, 67])
        dry_v = vol[sm_arr <= lo_t]
        wet_v = vol[sm_arr >= hi_t]
        f["sat_wet_dry_pump_ratio"] = round(float(np.mean(wet_v)) /
                                            max(float(np.mean(dry_v)), 1e-6), 3) \
            if len(dry_v) and len(wet_v) else 1.0
        peak_ndvi = float(np.nanmax(s["ndvi"])) if np.isfinite(s["ndvi"]).any() else 0.0
        f["_sat_ndvi_peak"] = round(peak_ndvi, 3)
        f["sat_ndvi_deficit"] = round(max(0.0, 1.0 - peak_ndvi / CROPS[c["crop"]]["peak_ndvi"]), 3)
        f["sat_lulc_non_crop"] = 1 if c["lulc_class"] in ("Fallow", "Built-up / rural settlement",
                                                          "Waterbody") else 0
        f["_sat_eos04_coverage"] = round(float(np.mean([r["sm_eos04"] is not None for r in rows])), 3)

        # ---------------- GEOGRAPHY ---------------------------------------
        f["geo_stage_of_extraction"] = c["stage_of_extraction"]
        f["_geo_over_exploited"] = 1 if c["block_category"] == "Over-exploited" else 0
        f["_geo_m3_per_ha_season"] = round(total_vol / area, 1)
        norm = CROP_NORM_M3_PER_HA.get(c["crop"], 7000)
        f["geo_m3_per_ha_vs_norm"] = round(total_vol / area / norm, 3)
        f["geo_distance_to_registered_ap_m"] = c["distance_to_registered_ap_m"]
        f["geo_water_level_depth_m"] = c["static_water_level_m"]

        # ---------------- HISTORY -----------------------------------------
        ratio = np.nan_to_num(s["excess_ratio"], nan=1.0)
        f["hist_excess_days"] = int(np.sum(ratio > 1.5))
        run_len = best = 0
        for v in ratio:
            run_len = run_len + 1 if v > 1.3 else 0
            best = max(best, run_len)
        f["hist_persistence_days"] = int(best)
        half = n // 2
        prev, nxt = vol[max(0, half - 14):half], vol[half:half + 14]
        step = 0.0
        for cut in range(20, n - 20, 5):
            a, b = vol[cut - 14:cut].mean(), vol[cut:cut + 14].mean()
            step = max(step, (b - a) / max(a, 1.0))
        f["hist_step_change"] = round(float(step), 3)
        f["hist_prior_flags"] = 0
        f["hist_mean_excess_ratio"] = round(float(np.median(ratio)), 3)

        # ---------------- season totals used by the console ---------------
        f["_season_volume_m3"] = round(total_vol, 0)
        f["_season_expected_m3"] = round(float(exp.sum()), 0)
        f["_season_excess_m3"] = round(max(0.0, total_vol - float(exp.sum())), 0)
        f["_season_energy_kwh"] = round(float(np.nansum(s["energy_kwh"])), 0)

        out.append({"connection_id": c["connection_id"], "features": f})

    names = [k for k in out[0]["features"] if not k.startswith("_")]
    if verbose:
        print(f"        features : {len(names)} per connection across "
              f"{len(SIGNAL_FAMILIES)} evidence families")
    return out, names


# ---------------------------------------------------------------------------
# Family sub-scores.  Each is a documented, weighted combination of its own
# features, on a 0..1 concern scale.  FAMILY_TRIGGER (0.55) is the line a
# family has to cross to count towards the gate in score.py.
# ---------------------------------------------------------------------------
def family_scores(f: dict) -> dict:
    """Thresholds come from scripts/calibrate.py: the low end is the 85th
    percentile of ordinary connections, the high end the 90th percentile of
    connections a field officer confirmed in an earlier season."""
    elec = (0.24 * ramp(f["elec_peer_ratio"], 1.12, 1.95)
            + 0.22 * ramp(f["elec_offwindow_share"], 0.18, 0.46)
            + 0.12 * ramp(f["elec_run_hours_p90"], 9.5, 14.5)
            + 0.10 * ramp(f["elec_spike_days"], 1, 7)
            + 0.08 * ramp(f["elec_load_extension"], 1.02, 1.35)
            + 0.16 * ramp(f["elec_demand_deficit"], 0.12, 0.35)
            + 0.08 * ramp(f["elec_dt_loss_share"], 0.004, 0.030))

    wx = (0.50 * ramp(f["wx_post_rain_ratio"], 0.45, 0.88)
          + 0.30 * ramp(f["wx_rain_correlation"], -0.36, -0.04)
          + 0.20 * ramp(f["wx_wet_day_volume_share"], 0.28, 0.42))

    sat = (0.28 * ramp(f["sat_wet_soil_volume_share"], 0.05, 0.22)
           + 0.28 * ramp(f["sat_ndvi_deficit"], 0.06, 0.55)
           + 0.30 * ramp(f["sat_wet_dry_pump_ratio"], 0.78, 1.05)
           + 0.08 * ramp(f["sat_sm_above_crop_norm"], -0.045, 0.005)
           + 0.06 * f["sat_lulc_non_crop"])

    geo = (0.30 * ramp(f["geo_stage_of_extraction"], 90, 175)
           + 0.34 * ramp(f["geo_m3_per_ha_vs_norm"], 1.20, 1.90)
           + 0.24 * ramp(f["geo_distance_to_registered_ap_m"], 220, 800)
           + 0.12 * ramp(f["geo_water_level_depth_m"], 22, 36))

    hist = (0.28 * ramp(f["hist_excess_days"], 30, 75)
            + 0.26 * ramp(f["hist_persistence_days"], 12, 40)
            + 0.20 * ramp(f["hist_mean_excess_ratio"], 1.45, 2.60)
            + 0.16 * ramp(f["hist_step_change"], 0.38, 1.05)
            + 0.10 * ramp(f["hist_prior_flags"], 0, 3))

    return {"electricity": round(min(1.0, elec), 3), "weather": round(min(1.0, wx), 3),
            "satellite": round(min(1.0, sat), 3), "geography": round(min(1.0, geo), 3),
            "history": round(min(1.0, hist), 3)}
