"""
STAGE 5 - SCORE
Multi-factor risk score across the five signal families.

Three detectors look at the same feature matrix from different angles:

  isolation_forest   unsupervised outliers - catches patterns nobody labelled
  temporal_residual  deviation from the connection's own seasonal baseline
                     (PyTorch LSTM-autoencoder when torch is installed,
                      EWMA peer-baseline residual otherwise - same interface)
  supervised         XGBoost trained on connections a field officer has
                     already confirmed or cleared in earlier seasons

Their blend is combined with the weighted family scores, then two rules apply:

  GATE       no single family can raise an alert on its own.  SUSPICIOUS needs
             two families over 0.55, HIGH RISK needs three.
  WHITELIST  nurseries, dairies, fish ponds and other registered exceptions are
             never escalated past MONITOR.

SHAP values over the supervised model turn each score into reason codes, so the
officer sees why, in the units of the evidence, before deciding anything.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

import db
from config import (BANDS, FAMILY_TRIGGER, GATE, MODEL_WEIGHTS, SIGNAL_FAMILIES,
                    RANDOM_SEED, WHITELIST_CAP_BAND)
from pipeline import features as feat

# What a field officer confirms is a *water* anomaly.  Meter under-registration
# is real but it is an energy-accounting finding, so it is not in the label set;
# it surfaces through the DT energy-balance feature and the feeder table instead.
ANOMALOUS_TRUTH = {"excess_extraction", "no_crop_draw", "post_rain_pumping",
                   "step_change"}
REVIEWED_SHARE = 0.32          # connections a utility would already have labelled


# ---------------------------------------------------------------------------
def band_for(score: float) -> dict:
    for b in BANDS:
        if b["min"] <= score < b["max"]:
            return b
    return BANDS[-1]


def _rank01(x: np.ndarray) -> np.ndarray:
    """Rank-normalise to 0..1 so the three detectors are on a common scale."""
    order = np.argsort(np.argsort(x))
    return order / max(len(x) - 1, 1)


def _previously_reviewed(cid: str) -> bool:
    h = int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 < REVIEWED_SHARE


# ---------------------------------------------------------------------------
# Detector 2: temporal residual
# ---------------------------------------------------------------------------
def temporal_residual(conn_ids: list[str], verbose: bool = True) -> tuple[np.ndarray, str]:
    """Per-connection reconstruction / baseline residual over the daily series."""
    rows = db.query("SELECT connection_id, day_index, est_volume_m3, expected_m3, rain_mm,"
                    " sm_fused, ndvi FROM meter_daily ORDER BY connection_id, day_index")
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["connection_id"], []).append(r)

    channels = []
    for cid in conn_ids:
        rs = series[cid]
        v = np.array([r["est_volume_m3"] or 0.0 for r in rs])
        e = np.array([r["expected_m3"] or 0.0 for r in rs])
        rain = np.array([r["rain_mm"] or 0.0 for r in rs])
        sm = np.array([r["sm_fused"] or 0.0 for r in rs])
        nd = np.array([r["ndvi"] or 0.0 for r in rs])
        channels.append(np.stack([np.log1p(v), np.log1p(e), np.log1p(rain), sm, nd], axis=1))
    X = np.stack(channels)                       # (n_conn, n_days, 5)

    backend = "ewma_peer_baseline"
    try:
        import torch                              # noqa: F401
        score, backend = _lstm_autoencoder(X)
    except Exception as exc:                       # torch missing or CPU-only failure
        if verbose and "torch" not in str(exc).lower():
            print(f"        temporal : LSTM-AE unavailable ({exc.__class__.__name__}),"
                  f" using EWMA baseline")
        score = _ewma_residual(X)
    return _rank01(score), backend


def _ewma_residual(X: np.ndarray, span: int = 10) -> np.ndarray:
    """Residual of observed volume against an EWMA of its own demand baseline."""
    obs, exp = X[:, :, 0], X[:, :, 1]
    alpha = 2.0 / (span + 1)
    base = np.zeros_like(obs)
    base[:, 0] = exp[:, 0]
    for t in range(1, obs.shape[1]):
        base[:, t] = alpha * exp[:, t] + (1 - alpha) * base[:, t - 1]
    # anchor each connection to its own first-three-week offset
    offset = np.median(obs[:, :21] - base[:, :21], axis=1, keepdims=True)
    resid = obs - (base + offset)
    return np.mean(np.clip(resid, 0, None) ** 2, axis=1)


def _lstm_autoencoder(X: np.ndarray, window: int = 14, epochs: int = 12) -> tuple[np.ndarray, str]:
    """PyTorch LSTM autoencoder over 14-day windows; score = reconstruction MSE."""
    import torch
    import torch.nn as nn

    torch.manual_seed(RANDOM_SEED)
    mu, sd = X.mean(axis=(0, 1)), X.std(axis=(0, 1)) + 1e-6
    Z = (X - mu) / sd
    n, days, ch = Z.shape
    wins, owner = [], []
    for i in range(n):
        for t in range(0, days - window + 1, 2):
            wins.append(Z[i, t:t + window]); owner.append(i)
    W = torch.tensor(np.array(wins), dtype=torch.float32)
    owner = np.array(owner)

    class AE(nn.Module):
        def __init__(self, ch, hid=24):
            super().__init__()
            self.enc = nn.LSTM(ch, hid, batch_first=True)
            self.dec = nn.LSTM(hid, hid, batch_first=True)
            self.out = nn.Linear(hid, ch)

        def forward(self, x):
            _, (h, _) = self.enc(x)
            rep = h[-1].unsqueeze(1).repeat(1, x.size(1), 1)
            y, _ = self.dec(rep)
            return self.out(y)

    model = AE(ch)
    opt = torch.optim.Adam(model.parameters(), lr=3e-3)
    lossf = nn.MSELoss()
    for _ in range(epochs):
        perm = torch.randperm(len(W))
        for k in range(0, len(W), 256):
            b = W[perm[k:k + 256]]
            opt.zero_grad(); loss = lossf(model(b), b); loss.backward(); opt.step()
    with torch.no_grad():
        err = ((model(W) - W) ** 2).mean(dim=(1, 2)).numpy()
    return np.array([err[owner == i].mean() for i in range(n)]), "pytorch_lstm_autoencoder"


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------
REASONS = {
    "elec_peer_ratio": ("Electricity", lambda v, c: f"Consumes {v:.1f}x the energy per hectare of "
                        f"neighbouring {c['crop']} connections on the same feeder"),
    "elec_night_share": ("Electricity", lambda v, c: f"{v*100:.0f}% of run hours fall outside the "
                         f"declared supply window for this feeder"),
    "elec_run_hours_p90": ("Electricity", lambda v, c: f"Runs up to {v:.1f} hours a day, against a "
                           f"{'night' if v > 12 else 'daytime'} agricultural supply of about 8 hours"),
    "elec_spike_days": ("Electricity", lambda v, c: f"{int(v)} days with consumption more than "
                        f"1.8x its own 14-day median"),
    "elec_load_extension": ("Electricity", lambda v, c: f"Connected load is {v:.2f}x the sanctioned "
                            f"{c['sanctioned_load_kw']:.1f} kW"),
    "elec_supply_hours_exceeded": ("Electricity", lambda v, c: f"{int(v)} days of running beyond 12 h, "
                                   f"longer than the feeder's scheduled supply"),
    "elec_offwindow_share": ("Electricity", lambda v, c: f"{v*100:.0f}% of run hours fall outside "
                             f"the feeder's declared agricultural supply window"),
    "elec_demand_deficit": ("Electricity", lambda v, c: f"Metered energy accounts for only "
                            f"{(1-v)*100:.0f}% of the water the visible crop needed - the meter may "
                            f"be under-registering"),
    "elec_dt_loss_share": ("Electricity", lambda v, c: f"Its distribution transformer shows "
                           f"{v*100:.1f}% unexplained energy loss beyond the technical allowance"),
    "sat_wet_dry_pump_ratio": ("Satellite", lambda v, c: (
        f"Pumps {v:.2f}x as much on its wettest days as on its driest - a farmer reading the "
        f"field backs off" if v >= 0.90 else
        f"Draws {v:.2f}x as much on wet soil as on dry, against {0.65:.2f} for a typical "
        f"connection")),
    "sat_sm_above_crop_norm": ("Satellite", lambda v, c: f"Volume-weighted soil moisture sits "
                               f"{v*100:+.1f} points against the median for this crop"),
    "wx_post_rain_ratio": ("Weather", lambda v, c: (
        f"Pumping in the 72 h after heavy rainfall holds at {v:.2f}x its dry-spell rate - "
        f"the schedule does not read the rain gauge" if v >= 0.80 else
        f"Pumping in the 72 h after heavy rainfall runs at {v:.2f}x its dry-spell rate, "
        f"a smaller reduction than its neighbours make")),
    "wx_rain_correlation": ("Weather", lambda v, c: f"Abstraction correlates {v:+.2f} with 3-day "
                            f"rainfall - a rain-responsive farmer scores negative"),
    "wx_wet_day_volume_share": ("Weather", lambda v, c: f"{v*100:.0f}% of the season's water was "
                                f"pumped on days following 25 mm or more of rain"),
    "sat_wet_soil_volume_share": ("Satellite", lambda v, c: f"{v*100:.0f}% of abstraction happened "
                                  f"with fused soil moisture above field capacity"),
    "sat_ndvi_deficit": ("Satellite", lambda v, c: f"Peak NDVI is {v*100:.0f}% below what a healthy "
                         f"{c['crop']} stand reaches - the declared crop is not visible"),
    "sat_volume_weighted_sm": ("Satellite", lambda v, c: f"Volume-weighted soil moisture at the pump "
                               f"pixel is {v:.2f} m3/m3, at or above field capacity"),
    "sat_lulc_non_crop": ("Satellite", lambda v, c: f"Bhuvan land use for this pixel is "
                          f"'{c['lulc_class']}', not cropland"),
    "geo_stage_of_extraction": ("Geography", lambda v, c: f"{c['block_name']} block is "
                                f"{c['block_category'].lower()} at {v:.0f}% stage of extraction"),
    "geo_m3_per_ha_vs_norm": ("Geography", lambda v, c: f"Applies {v:.2f}x the seasonal water depth a "
                              f"{c['crop']} crop of this area should need"),
    "geo_distance_to_registered_ap_m": ("Geography", lambda v, c: f"Pump sits {int(v)} m from the "
                                        f"nearest registered agricultural connection"),
    "geo_m3_per_ha_season": ("Geography", lambda v, c: f"Seasonal application of {v:,.0f} m3/ha"),
    "geo_water_level_depth_m": ("Geography", lambda v, c: f"Water level in this block stands at "
                                f"{v:.1f} m below ground"),
    "hist_excess_days": ("History", lambda v, c: f"{int(v)} days this season above 1.5x its own "
                         f"crop-stage demand"),
    "hist_persistence_days": ("History", lambda v, c: f"Longest unbroken run of over-abstraction is "
                              f"{int(v)} days - a pattern, not a one-off"),
    "hist_step_change": ("History", lambda v, c: f"Sustained step change of {v*100:.0f}% in the "
                         f"14-day mean partway through the season"),
    "hist_prior_flags": ("History", lambda v, c: f"{int(v)} previously confirmed events on this "
                         f"connection"),
    "hist_mean_excess_ratio": ("History", lambda v, c: f"Median daily abstraction runs at "
                               f"{v:.2f}x the modelled requirement"),
    "elec_kwh_per_ha": ("Electricity", lambda v, c: f"{v:,.0f} kWh per hectare drawn this season"),
    "sat_ndvi_peak": ("Satellite", lambda v, c: f"Peak NDVI of {v:.2f} at this pixel"),
    "sat_eos04_coverage": ("Satellite", lambda v, c: f"EOS-04 returned a retrieval on "
                           f"{v*100:.0f}% of passes over this pixel"),
    "geo_over_exploited": ("Geography", lambda v, c: "Inside a CGWB over-exploited assessment unit"),
    "wx_heavy_rain_days": ("Weather", lambda v, c: f"{int(v)} days of 20 mm or more rainfall in the "
                           f"season"),
}


def _reason_text(name: str, value, conn: dict) -> tuple[str, str]:
    fam, fn = REASONS.get(name, ("Electricity", lambda v, c: f"{name} = {v}"))
    try:
        return fam, fn(value, conn)
    except Exception:
        return fam, f"{name} = {value}"


# ---------------------------------------------------------------------------
def run(bundle: dict, verbose: bool = True) -> dict:
    conns = {c["connection_id"]: c for c in db.query("SELECT * FROM connection")}
    for c in conns.values():
        c.update(json.loads(c["attrs_json"]))
        c["block_name"] = c["block_id"].split("-")[-1].title()

    rows, names = feat.build(verbose=verbose)
    conn_ids = [r["connection_id"] for r in rows]
    X = np.array([[r["features"][n] for n in names] for r in rows], dtype=float)
    Xs = StandardScaler().fit_transform(np.nan_to_num(X))

    # ---- detector 1: isolation forest -------------------------------------
    iso = IsolationForest(n_estimators=400, max_samples=256, contamination=0.12,
                          random_state=RANDOM_SEED).fit(Xs)
    iso_raw = -iso.score_samples(Xs)
    iso_score = _rank01(iso_raw)

    # ---- detector 2: temporal residual ------------------------------------
    temp_score, temporal_backend = temporal_residual(conn_ids, verbose=verbose)

    # ---- detector 3: supervised ranker on officer-labelled history --------
    y = np.array([1 if conns[cid]["truth_case"] in ANOMALOUS_TRUTH else 0 for cid in conn_ids])
    labelled = np.array([_previously_reviewed(cid) for cid in conn_ids])
    # Officer decisions recorded in the console are labels for the next run.
    # A confirmation is a positive, a clearance is a negative, and either one
    # overrides whatever the historical set said about that connection.
    officer = {r["connection_id"]: (1 if r["decision"] == "confirmed" else 0)
               for r in db.query("SELECT connection_id, decision FROM officer_feedback"
                                 " WHERE decision IN ('confirmed','cleared')"
                                 " ORDER BY feedback_id")}
    for i, cid in enumerate(conn_ids):
        if cid in officer:
            y[i] = officer[cid]
            labelled[i] = True
    import xgboost as xgb
    clf = xgb.XGBClassifier(n_estimators=260, max_depth=4, learning_rate=0.07,
                            subsample=0.9, colsample_bytree=0.85, reg_lambda=1.4,
                            eval_metric="logloss", random_state=RANDOM_SEED)
    clf.fit(X[labelled], y[labelled])
    sup_score = clf.predict_proba(X)[:, 1]

    # ---- SHAP reason codes -------------------------------------------------
    # TreeSHAP, from the shap package when its version agrees with the installed
    # XGBoost, and otherwise from XGBoost's own pred_contribs, which runs the
    # same algorithm inside the booster.  The reason codes are identical either
    # way; this only keeps the prototype from breaking on a version pairing.
    shap_backend = "shap.TreeExplainer"
    try:
        import shap
        sv = shap.TreeExplainer(clf).shap_values(X)
        if isinstance(sv, list):
            sv = sv[1]
        sv = np.array(sv)
    except Exception as exc:
        shap_backend = f"xgboost.pred_contribs (shap package unusable: {exc.__class__.__name__})"
        contribs = clf.get_booster().predict(xgb.DMatrix(X, feature_names=list(names)),
                                             pred_contribs=True)
        sv = np.array(contribs)[:, :-1]          # last column is the bias term

    # ---- blend, gate, band -------------------------------------------------
    fam_w = {k: v["weight"] for k, v in SIGNAL_FAMILIES.items()}
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []

    for i, cid in enumerate(conn_ids):
        c = conns[cid]
        f = rows[i]["features"]
        fam = feat.family_scores(f)
        model_blend = (MODEL_WEIGHTS["isolation_forest"] * iso_score[i]
                       + MODEL_WEIGHTS["temporal_residual"] * temp_score[i]
                       + MODEL_WEIGHTS["supervised"] * sup_score[i])
        # Corroboration counts for more than one loud signal: 60% of the family
        # component is the weighted mean, 40% is the mean of the three strongest
        # families, so evidence that lines up across families scores higher than
        # a single extreme reading.
        top3 = sorted(fam.values(), reverse=True)[:3]
        family_blend = (0.60 * sum(fam[k] * fam_w[k] for k in fam)
                        + 0.40 * (sum(top3) / 3.0))
        raw = 100.0 * (0.55 * model_blend + 0.45 * family_blend)

        triggered = [k for k, v in fam.items() if v >= FAMILY_TRIGGER]
        caps, score = [], raw
        if len(triggered) < GATE["SUSPICIOUS"]:
            if raw >= 55:
                caps.append(f"gate: only {len(triggered)} of 5 families over {FAMILY_TRIGGER}, "
                            f"{GATE['SUSPICIOUS']} needed to reach suspicious")
            score = min(score, 54.5)
        elif len(triggered) < GATE["HIGH_RISK"]:
            if raw >= 75:
                caps.append(f"gate: {len(triggered)} of 5 families over {FAMILY_TRIGGER}, "
                            f"{GATE['HIGH_RISK']} needed to reach high risk")
            score = min(score, 74.5)
        if c["whitelist_category"]:
            if raw >= 55:
                caps.append(f"whitelist: registered {c['whitelist_category'].replace('_', ' ')}, "
                            f"never escalated past {WHITELIST_CAP_BAND.lower()}")
            score = min(score, 54.5)
        capped_by = "; ".join(caps) if caps else None

        band = band_for(score)

        # Reason codes: one per family that carried the score, strongest family
        # first, each being that family's largest positive SHAP contribution.
        # Spanning the families is the point - an officer needs the evidence, not
        # the model's four favourite columns.
        contributions = [(names[j], float(sv[i][j])) for j in range(len(names))
                         if sv[i][j] > 0]
        contributions.sort(key=lambda x: -x[1])
        by_family: dict[str, list] = {}
        for name, val in contributions:
            fam_label, text = _reason_text(name, f[name], c)
            by_family.setdefault(fam_label, []).append(
                {"family": fam_label, "feature": name, "value": f[name],
                 "shap": round(val, 4), "text": text})

        order = sorted(fam, key=lambda k: -fam[k])
        reasons, used = [], set()
        if contributions:                              # lead with the single strongest
            name, val = contributions[0]
            fam_label, text = _reason_text(name, f[name], c)
            reasons.append({"family": fam_label, "feature": name, "value": f[name],
                            "shap": round(val, 4), "text": text})
            used.add(text)
        for k in order:
            label = SIGNAL_FAMILIES[k]["label"]
            for r in by_family.get(label, []):
                if r["text"] not in used:
                    reasons.append(r); used.add(r["text"])
                    break
            if len(reasons) == 4:
                break
        for name, val in contributions:                # top up if a family was silent
            if len(reasons) >= 4:
                break
            fam_label, text = _reason_text(name, f[name], c)
            if text not in used:
                reasons.append({"family": fam_label, "feature": name, "value": f[name],
                                "shap": round(val, 4), "text": text})
                used.add(text)

        conv = bundle["conversion_factors"][cid]
        excess = f["_season_excess_m3"]
        results.append({
            "connection_id": cid, "run_id": run_id, "ts": now,
            "risk_score": round(float(score), 1), "risk_raw": round(float(raw), 1),
            "band": band["key"], "band_colour": band["colour"],
            "families": fam, "triggered_families": triggered, "capped_by": capped_by,
            "detectors": {"isolation_forest": round(float(iso_score[i]), 3),
                          "temporal_residual": round(float(temp_score[i]), 3),
                          "supervised": round(float(sup_score[i]), 3)},
            "reasons": reasons, "features": f,
            "est_excess_m3": excess,
            "est_excess_kwh": round(excess / max(conv["m3_per_kwh"], 0.1), 0),
            "excess_m3_lo": round(max(0.0, f["_season_volume_m3"] * conv["m3_per_kwh_lo"] /
                                      max(conv["m3_per_kwh"], 0.1) - f["_season_expected_m3"]), 0),
            "excess_m3_hi": round(max(0.0, f["_season_volume_m3"] * conv["m3_per_kwh_hi"] /
                                      max(conv["m3_per_kwh"], 0.1) - f["_season_expected_m3"]), 0),
            "conversion": conv,
            "was_previously_reviewed": bool(labelled[i]),
            "_truth_case": c["truth_case"],
        })

    # ---- honest evaluation on connections the model never saw a label for --
    held = ~labelled
    metrics = _metrics(np.array([r["risk_score"] for r in results]), y, held)
    metrics.update({"temporal_backend": temporal_backend,
                    "shap_backend": shap_backend,
                    "n_labelled_history": int(labelled.sum()),
                    "n_officer_labels": len(officer),
                    "model_weights": MODEL_WEIGHTS,
                    "features": len(names)})

    db.insert_many("risk_feature", [{"connection_id": r["connection_id"],
                                     "features_json": json.dumps(r["features"]),
                                     "families_json": json.dumps(r["families"])} for r in results])
    db.append_audit("pipeline", "system", "score", f"run {run_id}",
                    {"detectors": list(MODEL_WEIGHTS), "gate": GATE,
                     "family_trigger": FAMILY_TRIGGER, "metrics": metrics})

    if verbose:
        counts = {}
        for r in results:
            counts[r["band"]] = counts.get(r["band"], 0) + 1
        print(f"[5/6] SCORE    : {counts}  |  temporal backend: {temporal_backend}")
        print(f"        holdout  : ROC-AUC {metrics['roc_auc']:.3f}, "
              f"precision@50 {metrics['precision_at_50']:.2f}, recall@50 {metrics['recall_at_50']:.2f}")

    bundle["results"] = results
    bundle["run_id"] = run_id
    bundle["metrics"] = metrics
    return bundle


def _metrics(scores: np.ndarray, y: np.ndarray, mask: np.ndarray) -> dict:
    s, t = scores[mask], y[mask]
    order = np.argsort(-s)
    k = min(50, len(order))
    top = order[:k]
    tp = int(t[top].sum())
    pos = int(t.sum())
    # ROC-AUC without sklearn.metrics import cost
    from sklearn.metrics import roc_auc_score, average_precision_score
    return {"n_holdout": int(mask.sum()), "n_positive_holdout": pos,
            "roc_auc": round(float(roc_auc_score(t, s)), 4) if pos else None,
            "average_precision": round(float(average_precision_score(t, s)), 4) if pos else None,
            "precision_at_50": round(tp / k, 4),
            "recall_at_50": round(tp / pos, 4) if pos else None}
