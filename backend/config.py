"""
JAL-ANOMALY  |  Central configuration.

Every constant the risk engine uses lives here so that porting the pilot to
another district is a configuration change, not a code change (Feasibility
slide, point 5).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DIST_DIR = ROOT / "dist"

# ---------------------------------------------------------------------------
# Pilot area  --  Sangrur district, Punjab.  Blocks assessed by CGWB as
# OVER-EXPLOITED; paddy belt on flat-rate agricultural supply.
# ---------------------------------------------------------------------------
DISTRICT = {
    "name": "Sangrur",
    "state": "Punjab",
    "discom": "PSPCL",
    "season": "Kharif 2026",
    "start_date": "2026-06-01",
    "days": 90,
    "bbox": [75.72, 30.06, 76.14, 30.44],       # lon_min, lat_min, lon_max, lat_max
    "centre": [75.93, 30.25],
}

# Assessment blocks (CGWB categorisation drives the GEOGRAPHY signal family).
BLOCKS = [
    {"id": "SGR-SUNAM",   "name": "Sunam",      "category": "Over-exploited", "stage_of_extraction": 178.4, "dwlr_depth_m": 30.0},
    {"id": "SGR-LEHRA",   "name": "Lehragaga",  "category": "Over-exploited", "stage_of_extraction": 164.9, "dwlr_depth_m": 27.0},
    {"id": "SGR-DHURI",   "name": "Dhuri",      "category": "Critical",       "stage_of_extraction": 96.2,  "dwlr_depth_m": 21.0},
    {"id": "SGR-MALER",   "name": "Malerkotla", "category": "Semi-critical",  "stage_of_extraction": 78.1,  "dwlr_depth_m": 16.0},
]

# 11 kV agricultural feeders in the pilot (HES/MDM is indexed by feeder -> DT -> connection).
FEEDERS = [
    {"id": "AP-F-011", "name": "Sunam Rural AP-1",   "block": "SGR-SUNAM", "n_dt": 9,  "supply_window": "night"},
    {"id": "AP-F-024", "name": "Lehragaga AP-2",     "block": "SGR-LEHRA", "n_dt": 8,  "supply_window": "day"},
    {"id": "AP-F-037", "name": "Dhuri Rural AP-1",   "block": "SGR-DHURI", "n_dt": 7,  "supply_window": "night"},
    {"id": "AP-F-052", "name": "Malerkotla AP-3",    "block": "SGR-MALER", "n_dt": 6,  "supply_window": "day"},
]

# ---------------------------------------------------------------------------
# STAGE 3  --  kWh to m3 conversion (pump curve, head, efficiency)
#   V(m3) = E(kWh) * 3.6e6 * eta_overall / (rho * g * H_total)
#         = E * 367.1 * eta / H
# ---------------------------------------------------------------------------
RHO = 1000.0            # kg/m3
G = 9.81                # m/s2
JOULES_PER_KWH = 3.6e6

PUMP_SPECS = {
    # hp: (overall wire-to-water efficiency, delivery head m, friction head m)
    5.0:  {"eta": 0.38, "delivery_head_m": 6.0, "friction_head_m": 3.0},
    7.5:  {"eta": 0.42, "delivery_head_m": 6.5, "friction_head_m": 3.5},
    10.0: {"eta": 0.46, "delivery_head_m": 7.0, "friction_head_m": 4.0},
    15.0: {"eta": 0.50, "delivery_head_m": 8.0, "friction_head_m": 4.5},
}
DRAWDOWN_M = 6.0        # typical pumping drawdown added to static water level

# ---------------------------------------------------------------------------
# STAGE 4  --  Expected crop water demand (FAO-56 single crop coefficient)
#   ETc = Kc * ET0                     [mm/day]
#   NIR = max(0, ETc + percolation - effective rainfall)
#   Gross volume = NIR * area_ha * 10 / irrigation_efficiency      [m3/day]
# ---------------------------------------------------------------------------
CROPS = {
    "paddy": {
        "label": "Paddy (transplanted)",
        "kc": [(0, 15, 1.10), (15, 45, 1.20), (45, 75, 1.15), (75, 90, 0.95)],
        "percolation_mm": 3.2,
        "puddling_mm": 180.0, "puddling_window": (12, 22),   # days from season start
        "irrigation_efficiency": 0.62,
        "peak_ndvi": 0.82,
    },
    "cotton": {
        "label": "Cotton (Bt)",
        "kc": [(0, 20, 0.45), (20, 50, 0.90), (50, 80, 1.15), (80, 90, 0.75)],
        "percolation_mm": 1.0, "puddling_mm": 0.0, "puddling_window": (0, 0),
        "irrigation_efficiency": 0.62, "peak_ndvi": 0.66,
    },
    "maize": {
        "label": "Kharif maize",
        "kc": [(0, 18, 0.40), (18, 45, 1.00), (45, 72, 1.15), (72, 90, 0.70)],
        "percolation_mm": 1.2, "puddling_mm": 0.0, "puddling_window": (0, 0),
        "irrigation_efficiency": 0.60, "peak_ndvi": 0.71,
    },
    "fodder": {
        "label": "Green fodder",
        "kc": [(0, 90, 0.85)],
        "percolation_mm": 1.0, "puddling_mm": 0.0, "puddling_window": (0, 0),
        "irrigation_efficiency": 0.58, "peak_ndvi": 0.58,
    },
    "vegetables": {
        "label": "Vegetables / nursery",
        "kc": [(0, 25, 0.60), (25, 60, 1.05), (60, 90, 0.90)],
        "percolation_mm": 1.5, "puddling_mm": 0.0, "puddling_window": (0, 0),
        "irrigation_efficiency": 0.80, "peak_ndvi": 0.62,
    },
}

# Bhuvan LULC classes retained per pump pixel.
LULC_CLASSES = ["Kharif cropland", "Double/triple cropland", "Plantation",
                "Fallow", "Built-up / rural settlement", "Waterbody"]

# ---------------------------------------------------------------------------
# STAGE 5  --  Risk bands.  Colours are the ones used on the map legend.
# ---------------------------------------------------------------------------
BANDS = [
    {"key": "NORMAL",     "min": 0,  "max": 30, "colour": "#1f9d8f", "order": 0,
     "meaning": "Pumping is consistent with crop stage, soil condition and recent weather. No action."},
    {"key": "MONITOR",    "min": 30, "max": 55, "colour": "#e0a217", "order": 1,
     "meaning": "Mild deviation from the connection's own baseline. Keep under observation, no field visit."},
    {"key": "SUSPICIOUS", "min": 55, "max": 75, "colour": "#e2711d", "order": 2,
     "meaning": "Sustained off-peak draw against wet soil and recent rainfall, with no crop-stage justification. Queue for a field check."},
    {"key": "HIGH_RISK",  "min": 75, "max": 101, "colour": "#c62828", "order": 3,
     "meaning": "Several factors align inside a groundwater-stressed block, and the pattern repeats. Priority inspection."},
]

# The five evidence families.  A family is "triggered" when its own sub-score
# crosses FAMILY_TRIGGER.  No single family can raise an alert on its own:
# SUSPICIOUS needs >=2 families, HIGH RISK needs >=3.
SIGNAL_FAMILIES = {
    "electricity": {"label": "Electricity", "weight": 0.30,
                    "desc": "Consumption magnitude, run duration, time of operation, historical baseline, sudden spikes"},
    "weather":     {"label": "Weather", "weight": 0.18,
                    "desc": "Recent rainfall, temperature, short-range forecast"},
    "satellite":   {"label": "Satellite", "weight": 0.22,
                    "desc": "Soil moisture, NDVI vegetation vigour, land-use classification"},
    "geography":   {"label": "Geography", "weight": 0.15,
                    "desc": "Groundwater-stressed blocks, agricultural parcels, distance from a registered agricultural connection"},
    "history":     {"label": "History", "weight": 0.15,
                    "desc": "Previously flagged events, seasonal consumption pattern, past officer feedback"},
}
FAMILY_TRIGGER = 0.55
GATE = {"SUSPICIOUS": 2, "HIGH_RISK": 3}

# Model blend (stage 5).  Unsupervised + temporal + supervised, then gated.
MODEL_WEIGHTS = {"isolation_forest": 0.30, "temporal_residual": 0.25, "supervised": 0.45}

# Whitelisted exception categories are never escalated past MONITOR
# (Feasibility slide, strategy 4).
WHITELIST_CATEGORIES = ["nursery", "dairy", "fish_pond", "sandy_soil_block", "second_crop"]
WHITELIST_CAP_BAND = "MONITOR"

RANDOM_SEED = 26015          # SIH26015
