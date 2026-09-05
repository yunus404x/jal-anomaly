"""
JAL-ANOMALY  |  Example-data generator.

Builds a reproducible, physically consistent Kharif-2026 dataset for the
Sangrur pilot so the whole pipeline can be demonstrated without a DISCOM
data-sharing agreement in place.  Every stream mirrors, field for field, a
real source named on the Technical Approach slide:

    HES / MDM (DLMS-COSEM, IS 15959)  ->  15-minute + daily load profiles
    Consumer-indexing GIS             ->  geo-tagged pump index
    ISRO EOS-04 (RISAT-1A) 500 m      ->  soil moisture, 17-day repeat
    NASA SMAP L3 9 km                 ->  daily soil moisture, fills the gap
    IMD gridded rainfall 0.25 deg     ->  daily rainfall
    Sentinel-2 NDVI                   ->  5-day vegetation vigour
    Bhuvan LULC                       ->  land-use class per pump pixel
    CGWB DWLR / India-WRIS            ->  water level, block category

Ground truth (`truth_case`) is written alongside so the prototype can be
scored honestly; the risk engine never reads it except to build the small
set of historical officer labels a real utility would already hold.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta

import numpy as np

from config import (BLOCKS, CROPS, DISTRICT, FEEDERS, LULC_CLASSES, PUMP_SPECS,
                    DATA_DIR, DRAWDOWN_M, JOULES_PER_KWH, G, RHO, RANDOM_SEED)

# --------------------------------------------------------------------------
# Case mix.  Shares are deliberately close to what a district actually looks
# like: most connections are ordinary, the interesting ones are rare.
# --------------------------------------------------------------------------
CASE_MIX = [
    ("normal",             0.700),
    ("excess_extraction",  0.085),   # pumping far beyond crop demand
    ("no_crop_draw",       0.045),   # steady draw, no crop growing (NDVI flat, LULC not cropland)
    ("post_rain_pumping",  0.060),   # ignores rainfall and wet soil
    ("step_change",        0.040),   # sudden sustained jump mid-season
    ("legit_exception",    0.045),   # nursery / dairy / fish pond - high use, whitelisted
    ("under_reporting",    0.025),   # meter under-records against feeder energy balance
]

N_CONNECTIONS = 612
PROFILE_SLOTS = 96          # 15-minute slots, DLMS load profile


def _rng(seed_offset: int = 0) -> np.random.Generator:
    return np.random.default_rng(RANDOM_SEED + seed_offset)


def kc_for_day(crop: str, day: int) -> float:
    for lo, hi, kc in CROPS[crop]["kc"]:
        if lo <= day < hi:
            return kc
    return CROPS[crop]["kc"][-1][2]


def m3_per_kwh(head_m: float, eta: float) -> float:
    """Wire-to-water conversion: V = E * 3.6e6 * eta / (rho g H)."""
    return JOULES_PER_KWH * eta / (RHO * G * head_m)


# --------------------------------------------------------------------------
# 1. Weather:  IMD 0.25 deg rainfall + reference evapotranspiration per block
# --------------------------------------------------------------------------
def make_weather(days: int) -> dict:
    rng = _rng(1)
    start = date.fromisoformat(DISTRICT["start_date"])
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    monsoon_onset = 27                       # ~28 June, typical for south-west Punjab
    weather = {}
    for b in BLOCKS:
        rain, et0, tmax = [], [], []
        for d in range(days):
            wet = 0.08 if d < monsoon_onset else 0.42
            # monsoon spells rather than independent days
            if d > 0 and rain[-1] > 4 and rng.random() < 0.45:
                wet += 0.25
            if rng.random() < wet:
                mm = float(rng.gamma(1.6, 11.0))
                if rng.random() < 0.05:
                    mm *= 2.6                # heavy spell
            else:
                mm = 0.0
            rain.append(round(min(mm, 145.0), 1))
            t = 41.0 - 5.5 * (d / days) - (2.8 if mm > 8 else 0) + float(rng.normal(0, 1.1))
            tmax.append(round(t, 1))
            e = 7.4 - 2.2 * (d / days) - (2.0 if mm > 8 else 0) + float(rng.normal(0, 0.35))
            et0.append(round(max(1.8, e), 2))
        weather[b["id"]] = {"date": dates, "rain_mm": rain, "et0_mm": et0, "tmax_c": tmax}
    return {"dates": dates, "blocks": weather, "monsoon_onset_index": monsoon_onset}


# --------------------------------------------------------------------------
# 2. Network + geo-tagged pump index (consumer-indexing GIS)
# --------------------------------------------------------------------------
def block_rects() -> dict:
    """Illustrative block geometry: the district bbox split into four quadrants.
    Both the network layout and the map layer read this, so pumps always sit
    inside the block whose CGWB category is used to score them."""
    lon0, lat0, lon1, lat1 = DISTRICT["bbox"]
    quad = [(0, 0), (1, 0), (0, 1), (1, 1)]
    out = {}
    for b, (qx, qy) in zip(BLOCKS, quad):
        x0 = lon0 + (lon1 - lon0) * (0.02 + 0.49 * qx)
        y0 = lat0 + (lat1 - lat0) * (0.02 + 0.49 * qy)
        out[b["id"]] = (x0, y0, x0 + (lon1 - lon0) * 0.47, y0 + (lat1 - lat0) * 0.47)
    return out


def make_network() -> tuple[list, list]:
    """Feeder and distribution-transformer layout, one feeder per block, with the
    DTs spread on a jittered lattice across the block it serves."""
    rng = _rng(2)
    rects = block_rects()
    feeders, dts = [], []
    for f in FEEDERS:
        x0, y0, x1, y1 = rects[f["block"]]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        feeders.append({**f, "lon": round(cx, 5), "lat": round(cy, 5)})
        n = f["n_dt"]
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        for k in range(n):
            gx, gy = k % cols, k // cols
            px = x0 + (x1 - x0) * (0.16 + 0.68 * (gx + 0.5) / cols) + float(rng.normal(0, 0.010))
            py = y0 + (y1 - y0) * (0.16 + 0.68 * (gy + 0.5) / rows) + float(rng.normal(0, 0.009))
            dts.append({
                "id": f"{f['id']}-DT{k+1:02d}",
                "feeder_id": f["id"],
                "block": f["block"],
                "lon": round(px, 5), "lat": round(py, 5),
                "rating_kva": int(rng.choice([63, 100, 100, 160, 250])),
            })
    return feeders, dts


def make_connections(dts: list) -> list:
    rng = _rng(3)
    py_rng = random.Random(RANDOM_SEED + 3)
    block_by_id = {b["id"]: b for b in BLOCKS}
    cases, weights = zip(*CASE_MIX)

    conns = []
    for i in range(N_CONNECTIONS):
        dt = dts[i % len(dts)]
        blk = block_by_id[dt["block"]]
        case = py_rng.choices(cases, weights=weights, k=1)[0]

        if case == "no_crop_draw":
            crop = py_rng.choice(["fodder", "fodder", "cotton"])
            lulc = py_rng.choice(["Fallow", "Built-up / rural settlement", "Waterbody"])
        elif case == "legit_exception":
            crop = "vegetables"
            lulc = py_rng.choice(["Plantation", "Double/triple cropland"])
        else:
            crop = py_rng.choices(["paddy", "paddy", "paddy", "cotton", "maize", "fodder", "vegetables"],
                                  weights=[34, 24, 16, 9, 8, 6, 3], k=1)[0]
            lulc = py_rng.choices(LULC_CLASSES, weights=[46, 28, 9, 10, 5, 2], k=1)[0]

        hp = float(py_rng.choices([5.0, 7.5, 10.0, 15.0], weights=[8, 22, 40, 30], k=1)[0])
        spec = PUMP_SPECS[hp]
        area = round(float(np.clip(rng.normal(1.65, 0.8), 0.4, 6.0)), 2)
        if case in ("excess_extraction", "no_crop_draw"):
            area = round(area * 0.85, 2)

        static_level = round(blk["dwlr_depth_m"] + float(rng.normal(0, 3.2)), 1)
        head = round(static_level + DRAWDOWN_M + spec["delivery_head_m"] + spec["friction_head_m"], 1)
        eta_true = float(np.clip(spec["eta"] + rng.normal(0, 0.035), 0.28, 0.58))

        whitelist = None
        if case == "legit_exception":
            whitelist = py_rng.choice(["nursery", "dairy", "fish_pond"])
        elif py_rng.random() < 0.02:
            whitelist = py_rng.choice(["sandy_soil_block", "second_crop"])

        conns.append({
            "connection_id": f"AP-SGR-{10001 + i}",
            "dt_id": dt["id"],
            "feeder_id": dt["feeder_id"],
            "block_id": dt["block"],
            "block_name": blk["name"],
            "block_category": blk["category"],
            "stage_of_extraction": blk["stage_of_extraction"],
            "lon": round(dt["lon"] + float(rng.normal(0, 0.0062)), 5),
            "lat": round(dt["lat"] + float(rng.normal(0, 0.0052)), 5),
            "pump_hp": hp,
            "sanctioned_load_kw": round(hp * 0.746, 2),
            "connected_load_kw": round(hp * 0.746 * (py_rng.uniform(1.25, 1.65)
                                                     if case in ("excess_extraction", "no_crop_draw")
                                                     and py_rng.random() < 0.55 else 1.0), 2),
            "rated_input_kw": round(hp * 0.746 / 0.88, 2),
            "crop": crop,
            "crop_label": CROPS[crop]["label"],
            "area_ha": area,
            "lulc_class": lulc,
            "static_water_level_m": static_level,
            "total_head_m": head,
            "pump_efficiency": round(eta_true, 3),
            "m3_per_kwh_true": round(m3_per_kwh(head, eta_true), 3),
            "connection_age_years": int(py_rng.randint(1, 24)),
            "tariff": "AP flat-rate (subsidised)",
            "whitelist_category": whitelist,
            "distance_to_registered_ap_m": int(max(5, rng.normal(120, 90)) if case != "no_crop_draw"
                                               else max(300, rng.normal(950, 420))),
            "prior_flags": 0,
            "truth_case": case,
        })
    return conns


# --------------------------------------------------------------------------
# 3. Daily behaviour:  demand -> abstraction -> energy, plus the satellite and
#    weather values sampled for that pump's pixel.
# --------------------------------------------------------------------------
def simulate(conns: list, weather: dict) -> tuple[list, dict, dict]:
    rng = _rng(4)
    py_rng = random.Random(RANDOM_SEED + 4)
    days = DISTRICT["days"]
    dates = weather["dates"]
    rows = []
    profiles = {}
    sm_series_out = {}
    dt_true: dict = {}
    dt_met: dict = {}

    feeder_window = {f["id"]: f["supply_window"] for f in FEEDERS}

    for c in conns:
        crop = CROPS[c["crop"]]
        blkw = weather["blocks"][c["block_id"]]
        case = c["truth_case"]
        # each connection has its own habit: how promptly it responds to rain
        rain_response = {"post_rain_pumping": 0.12, "no_crop_draw": 0.05}.get(case, py_rng.uniform(0.55, 0.95))
        # Deliberate overlap between the classes: some ordinary farmers over-water
        # and some over-extractors are only mildly over.  Without this the example
        # data would be trivially separable and the accuracy figures meaningless.
        greed = {"excess_extraction": py_rng.uniform(1.35, 2.60),
                 "legit_exception": py_rng.uniform(1.30, 1.80),
                 "no_crop_draw": 1.0}.get(case, py_rng.uniform(0.78, 1.38))
        step_day = py_rng.randint(38, 62) if case == "step_change" else 10 ** 6
        under_report = py_rng.uniform(0.55, 0.72) if case == "under_reporting" else 1.0

        # root-zone soil-moisture bucket (volumetric, m3/m3)
        sm = 0.17 + float(rng.normal(0, 0.02))
        sm_true, ndvi_true = [], []
        vigour = 1.0 if case != "no_crop_draw" else py_rng.uniform(0.12, 0.28)

        for d in range(days):
            rain = blkw["rain_mm"][d]
            et0 = blkw["et0_mm"][d]
            kc = kc_for_day(c["crop"], d)
            etc = kc * et0
            perc = crop["percolation_mm"]

            # ---- what the crop actually needs today (net irrigation requirement)
            p_eff = min(rain * 0.75, 42.0)                     # USDA-SCS style effective rainfall
            need_mm = max(0.0, etc + perc - p_eff)
            if crop["puddling_mm"] and crop["puddling_window"][0] <= d < crop["puddling_window"][1]:
                need_mm += crop["puddling_mm"] / (crop["puddling_window"][1] - crop["puddling_window"][0])
            expected_m3 = need_mm * c["area_ha"] * 10.0 / crop["irrigation_efficiency"]

            # ---- what this connection actually pumps
            if case == "no_crop_draw":
                actual_m3 = c["area_ha"] * 10.0 * py_rng.uniform(9.0, 14.0)      # flat, weather-blind
            else:
                # a farmer who ignores rain keeps to yesterday's schedule
                rain_aware = 1.0 - rain_response * min(1.0, p_eff / max(etc + perc, 0.1))
                base = (etc + perc) * c["area_ha"] * 10.0 / crop["irrigation_efficiency"]
                actual_m3 = base * max(0.05, rain_aware) * greed
            if d >= step_day:
                actual_m3 *= 1.85
            if case == "normal" and py_rng.random() < 0.06:
                actual_m3 *= py_rng.uniform(0.2, 0.5)                            # skipped a turn
            actual_m3 = max(0.0, actual_m3 * float(rng.normal(1.0, 0.11)))

            # ---- root-zone soil-moisture bucket (300 mm zone, 0.5 porosity)
            #      infiltration - crop uptake, then free drainage above capacity
            applied_mm = actual_m3 * crop["irrigation_efficiency"] / (c["area_ha"] * 10.0)
            sm += (rain * 0.70 + applied_mm * 0.65 - etc * 0.9) / 150.0
            if c["crop"] == "paddy" and 12 <= d < 78 and case != "no_crop_draw":
                sm = max(sm, 0.34)                      # puddled field kept ponded
            sm -= max(0.0, sm - 0.42) * 0.5             # drainage above field capacity
            sm = float(np.clip(sm, 0.06, 0.48))
            sm_true.append(sm)

            # ---- NDVI from crop stage x vigour
            stage = math.sin(math.pi * min(1.0, max(0.0, (d - 8) / 74.0)))
            ndvi = crop["peak_ndvi"] * (0.32 + 0.68 * stage) * vigour + float(rng.normal(0, 0.012))
            ndvi_true.append(float(np.clip(ndvi, 0.04, 0.92)))

            # ---- energy at the pump, then what the meter actually registers
            energy_true = actual_m3 / c["m3_per_kwh_true"]
            run_h = energy_true / c["rated_input_kw"] if c["rated_input_kw"] else 0.0
            if run_h > 17.5:                                   # cap at physically plausible
                run_h = 17.5
                energy_true = run_h * c["rated_input_kw"]
                actual_m3 = energy_true * c["m3_per_kwh_true"]
            energy = energy_true * under_report                # under-registering meter
            # Share of run hours falling OUTSIDE the feeder's declared supply
            # window.  Ordinary connections stay inside it; connections drawing
            # more than the schedule allows have to run outside it.
            off_window = 0.16 if feeder_window[c["feeder_id"]] == "night" else 0.13
            if case in ("excess_extraction", "no_crop_draw", "step_change"):
                off_window = min(0.72, off_window + py_rng.uniform(0.20, 0.38))
            elif case == "post_rain_pumping":
                off_window = min(0.55, off_window + py_rng.uniform(0.04, 0.16))
            off_window = float(np.clip(off_window + rng.normal(0, 0.03), 0.02, 0.85))
            night_share = (1.0 - off_window) if feeder_window[c["feeder_id"]] == "night" else off_window
            rows.append({
                "connection_id": c["connection_id"],
                "date": dates[d],
                "day_index": d,
                "energy_kwh": round(energy, 2),
                "run_hours": round(run_h, 2),
                "night_run_hours": round(run_h * night_share, 2),
                "max_demand_kw": round(c["rated_input_kw"] * float(np.clip(rng.normal(0.94, 0.06), 0.6, 1.35)), 2),
                "rain_mm": rain,
                "et0_mm": et0,
                "tmax_c": blkw["tmax_c"][d],
                # ground truth kept for evaluation only
                "_true_volume_m3": round(actual_m3, 1),
                "_expected_m3": round(expected_m3, 1),
            })
            dt_true[(c["dt_id"], d)] = dt_true.get((c["dt_id"], d), 0.0) + energy_true
            dt_met[(c["dt_id"], d)] = dt_met.get((c["dt_id"], d), 0.0) + energy

        sm_series_out[c["connection_id"]] = (sm_true, ndvi_true)
        profiles[c["connection_id"]] = _load_profile(rows[-1], c, feeder_window[c["feeder_id"]], case, py_rng)

    # ---- DT-level energy accounting -------------------------------------
    # What the distribution transformer's own meter records is the sum of the
    # pumps below it plus technical (I2R + transformer) loss.  Where a consumer
    # meter under-registers, the gap opens up here first -- this is exactly the
    # feeder/DT energy accounting the Impact slide talks about.
    dt_rows = []
    tech_loss = {}
    for (dt_id, d), true_kwh in sorted(dt_true.items()):
        if dt_id not in tech_loss:
            tech_loss[dt_id] = float(np.clip(rng.normal(0.062, 0.011), 0.035, 0.095))
        met = dt_met[(dt_id, d)]
        dt_rows.append({"dt_id": dt_id, "ts": dates[d], "day_index": d,
                        "input_kwh": round(true_kwh * (1 + tech_loss[dt_id]) *
                                           float(rng.normal(1.0, 0.012)), 2),
                        "metered_sum_kwh": round(met, 2)})

    return rows, profiles, sm_series_out, dt_rows


def _load_profile(last_row: dict, conn: dict, window: str, case: str, py_rng: random.Random) -> list:
    """96-slot 15-minute DLMS load profile for the reference day (last day of season)."""
    run_h = last_row["run_hours"]
    slots = [0.0] * PROFILE_SLOTS
    kw = conn["rated_input_kw"]
    n_slots = int(round(run_h * 4))
    if n_slots <= 0:
        return slots
    if window == "night":
        starts = [88, 0, 20]          # 22:00, 00:00, 05:00
    else:
        starts = [36, 52, 8]          # 09:00, 13:00, 02:00 (the 02:00 block is the tell)
    if case in ("excess_extraction", "no_crop_draw", "post_rain_pumping"):
        starts = [88, 0, 12, 24]
    remaining = n_slots
    for s in starts:
        take = min(remaining, max(4, n_slots // len(starts) + py_rng.randint(-2, 2)))
        for k in range(take):
            slots[(s + k) % PROFILE_SLOTS] = round(kw * py_rng.uniform(0.86, 1.04), 2)
        remaining -= take
        if remaining <= 0:
            break
    return slots


# --------------------------------------------------------------------------
# 4. Satellite retrievals: EOS-04 17-day composite, SMAP daily, Sentinel-2 NDVI
# --------------------------------------------------------------------------
def sample_satellite(conns: list, sm_series: dict, weather: dict) -> list:
    """Return per-connection per-day satellite records, with realistic revisit gaps."""
    rng = _rng(5)
    days = DISTRICT["days"]
    out = []
    for c in conns:
        sm_true, ndvi_true = sm_series[c["connection_id"]]
        eos_offset = int(rng.integers(0, 17))
        s2_offset = int(rng.integers(0, 5))
        last_eos, last_eos_age = None, 99
        last_ndvi = None
        for d in range(days):
            # EOS-04 (RISAT-1A) C-band SAR, 500 m, 17-day repeat, ~92% retrieval
            if (d - eos_offset) % 17 == 0 and rng.random() < 0.92:
                last_eos = float(np.clip(sm_true[d] + rng.normal(0, 0.021), 0.03, 0.48))
                last_eos_age = 0
            else:
                last_eos_age += 1
            # SMAP L3, 9 km, daily -> block-scale, coarser and noisier
            smap = float(np.clip(sm_true[d] + rng.normal(0, 0.034), 0.03, 0.48))
            # Sentinel-1 GRD backscatter proxy for wetness corroboration
            s1_wet = float(np.clip(sm_true[d] + rng.normal(0, 0.028), 0.03, 0.48))
            # Sentinel-2 NDVI, 5-day, cloud losses in the monsoon
            if (d - s2_offset) % 5 == 0 and rng.random() < (0.55 if d > 27 else 0.85):
                last_ndvi = float(np.clip(ndvi_true[d] + rng.normal(0, 0.018), 0.02, 0.95))
            # fused soil moisture: EOS-04 when fresh, SMAP-anchored otherwise
            if last_eos is None:
                fused = smap
            else:
                w = max(0.0, 1.0 - last_eos_age / 17.0)
                fused = w * last_eos + (1 - w) * smap
            out.append({
                "connection_id": c["connection_id"],
                "day_index": d,
                "sm_eos04": round(last_eos, 4) if last_eos is not None else None,
                "sm_eos04_age_days": last_eos_age if last_eos is not None else None,
                "sm_smap": round(smap, 4),
                "s1_wetness": round(s1_wet, 4),
                "sm_fused": round(fused, 4),
                "ndvi": round(last_ndvi, 4) if last_ndvi is not None else None,
            })
    return out


# --------------------------------------------------------------------------
# 5. Map layers: block polygons, IMD rainfall grid, soil-moisture / NDVI grids
# --------------------------------------------------------------------------
def make_layers(conns: list, sm_series: dict, weather: dict) -> dict:
    rng = _rng(6)
    lon0, lat0, lon1, lat1 = DISTRICT["bbox"]
    last = DISTRICT["days"] - 1

    # --- block polygons (illustrative geometry, real CGWB categories) -------
    blocks_fc = {"type": "FeatureCollection", "features": []}
    rects = block_rects()
    for b in BLOCKS:
        x0, y0, x1, y1 = rects[b["id"]]
        blocks_fc["features"].append({
            "type": "Feature",
            "properties": {"block_id": b["id"], "name": b["name"], "category": b["category"],
                           "stage_of_extraction": b["stage_of_extraction"],
                           "dwlr_depth_m": b["dwlr_depth_m"]},
            "geometry": {"type": "Polygon", "coordinates": [[
                [round(x0, 5), round(y0, 5)], [round(x1, 5), round(y0, 5)],
                [round(x1, 5), round(y1, 5)], [round(x0, 5), round(y1, 5)],
                [round(x0, 5), round(y0, 5)]]]},
        })

    # --- gridded layers -----------------------------------------------------
    def grid(step: float, value_fn) -> dict:
        fc = {"type": "FeatureCollection", "features": []}
        y = lat0
        while y < lat1:
            x = lon0
            while x < lon1:
                v = value_fn(x + step / 2, y + step / 2)
                fc["features"].append({
                    "type": "Feature",
                    "properties": {"value": round(float(v), 4)},
                    "geometry": {"type": "Polygon", "coordinates": [[
                        [round(x, 5), round(y, 5)], [round(x + step, 5), round(y, 5)],
                        [round(x + step, 5), round(y + step, 5)], [round(x, 5), round(y + step, 5)],
                        [round(x, 5), round(y, 5)]]]},
                })
                x += step
            y += step
        return fc

    # inverse-distance interpolation from the pumps we simulated
    pts = np.array([[c["lon"], c["lat"]] for c in conns])
    sm_vals = np.array([sm_series[c["connection_id"]][0][last] for c in conns])
    ndvi_vals = np.array([sm_series[c["connection_id"]][1][last] for c in conns])

    def idw(vals):
        def f(x, y):
            d2 = (pts[:, 0] - x) ** 2 + (pts[:, 1] - y) ** 2 + 1e-6
            w = 1.0 / d2 ** 1.2
            k = np.argsort(d2)[:12]
            return float(np.sum(vals[k] * w[k]) / np.sum(w[k]))
        return f

    sm_grid = grid(0.012, idw(sm_vals))      # ~1.2 km display cells
    ndvi_grid = grid(0.012, idw(ndvi_vals))

    # IMD 0.25 deg rainfall, 7-day accumulation to the reference date
    rain7 = {b["id"]: sum(weather["blocks"][b["id"]]["rain_mm"][last - 6:last + 1]) for b in BLOCKS}
    def rain_at(x, y):
        # nearest block centre
        best, bestd = None, 1e9
        for b, feat in zip(BLOCKS, blocks_fc["features"]):
            ring = feat["geometry"]["coordinates"][0]
            cx = sum(p[0] for p in ring[:4]) / 4
            cy = sum(p[1] for p in ring[:4]) / 4
            d = (cx - x) ** 2 + (cy - y) ** 2
            if d < bestd:
                best, bestd = b["id"], d
        return rain7[best] * (0.85 + 0.3 * rng.random())
    rain_grid = grid(0.05, rain_at)

    return {"blocks": blocks_fc, "soil_moisture": sm_grid, "ndvi": ndvi_grid,
            "rainfall_7d": rain_grid, "rain7_by_block": rain7}


# --------------------------------------------------------------------------
def build(verbose: bool = True) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    weather = make_weather(DISTRICT["days"])
    feeders, dts = make_network()
    conns = make_connections(dts)
    rows, profiles, sm_series, dt_rows = simulate(conns, weather)
    sat = sample_satellite(conns, sm_series, weather)
    layers = make_layers(conns, sm_series, weather)

    bundle = {
        "meta": {"district": DISTRICT, "generated_for": "SIH26015 / JAL-ANOMALY prototype",
                 "n_connections": len(conns), "n_days": DISTRICT["days"],
                 "n_meter_rows": len(rows), "n_satellite_rows": len(sat)},
        "feeders": feeders, "dts": dts, "connections": conns,
        "weather": weather, "meter_daily": rows, "satellite": sat, "dt_daily": dt_rows,
        "load_profiles": profiles, "layers": layers,
    }
    (DATA_DIR / "raw_bundle.json").write_text(json.dumps(bundle))
    if verbose:
        print(f"connections      : {len(conns)}")
        print(f"meter daily rows : {len(rows)}")
        print(f"satellite rows   : {len(sat)}")
        print(f"load profiles    : {len(profiles)} x {PROFILE_SLOTS} slots")
        mix = {}
        for c in conns:
            mix[c["truth_case"]] = mix.get(c["truth_case"], 0) + 1
        print("case mix         :", mix)
    return bundle


if __name__ == "__main__":
    build()
