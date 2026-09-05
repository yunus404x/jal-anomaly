"""
STAGE 3 - CONVERT
kWh translated to pumped volume using pump curve, head and efficiency.

    V (m3) = E (kWh) x 3.6e6 x eta_wire_to_water / (rho x g x H_total)
    H_total = static water level (CGWB DWLR) + drawdown + delivery + friction

The engine deliberately uses *nameplate* efficiency and the block DWLR level -
what a utility can actually know - not the per-pump truth used to synthesise the
data.  The resulting estimation error is real and is reported as a confidence
band, because an anomaly must survive it before anyone is sent to a field.
"""
from __future__ import annotations

import statistics

import db
from config import DRAWDOWN_M, G, JOULES_PER_KWH, PUMP_SPECS, RHO


def m3_per_kwh(head_m: float, eta: float) -> float:
    return JOULES_PER_KWH * eta / (RHO * G * head_m)


def estimate_conversion(conn: dict) -> dict:
    """What the engine assumes for this connection, from utility-held data only."""
    spec = PUMP_SPECS[float(conn["pump_hp"])]
    head = (conn["static_water_level_m"] + DRAWDOWN_M
            + spec["delivery_head_m"] + spec["friction_head_m"])
    eta = spec["eta"]                              # nameplate, not measured
    return {"assumed_head_m": round(head, 1), "assumed_eta": eta,
            "m3_per_kwh": round(m3_per_kwh(head, eta), 3),
            # +/- one standard pump-test spread, carried through to the alert
            "m3_per_kwh_lo": round(m3_per_kwh(head * 1.15, eta * 0.85), 3),
            "m3_per_kwh_hi": round(m3_per_kwh(head * 0.88, eta * 1.12), 3)}


def run(bundle: dict, verbose: bool = True) -> dict:
    conns = db.query("SELECT * FROM connection")
    factors = {}
    for c in conns:
        factors[c["connection_id"]] = estimate_conversion(c)

    rows = db.query("SELECT connection_id, ts, energy_kwh FROM meter_daily")
    updates = [(round(r["energy_kwh"] * factors[r["connection_id"]]["m3_per_kwh"], 1),
                r["connection_id"], r["ts"]) for r in rows]
    with db.connect() as con:
        con.executemany("UPDATE meter_daily SET est_volume_m3=? WHERE connection_id=? AND ts=?",
                        updates)

    # honest error report against the (hidden) true conversion factor
    truth = {c["connection_id"]: c for c in bundle["connections"]}
    errs = [abs(factors[cid]["m3_per_kwh"] - truth[cid]["m3_per_kwh_true"]) /
            truth[cid]["m3_per_kwh_true"] for cid in factors]
    mape = 100 * statistics.mean(errs)

    bundle["conversion_factors"] = factors
    db.append_audit("pipeline", "system", "convert", f"{len(updates)} meter-days",
                    {"method": "V = E*3.6e6*eta/(rho*g*H)", "drawdown_m": DRAWDOWN_M,
                     "mean_abs_pct_error_vs_truth": round(mape, 2)})
    if verbose:
        med = statistics.median(f["m3_per_kwh"] for f in factors.values())
        print(f"[3/6] CONVERT  : median {med:.2f} m3/kWh, "
              f"conversion error vs ground truth {mape:.1f}% (carried as a confidence band)")
    return bundle
