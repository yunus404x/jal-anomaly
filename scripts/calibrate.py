"""Print per-feature separation between ordinary and anomalous connections.

Used once, while tuning, to place each ramp() threshold on evidence rather than
on taste.  The thresholds it produces are written into pipeline/features.py.
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
from pipeline import features as feat  # noqa: E402

ANOM = {"excess_extraction", "no_crop_draw", "post_rain_pumping", "step_change", "under_reporting"}

rows, names = feat.build(verbose=False)
truth = {c["connection_id"]: json.loads(c["attrs_json"])["truth_case"]
         for c in db.query("SELECT connection_id, attrs_json FROM connection")}
cases = {c["connection_id"]: truth[c["connection_id"]] for c in db.query("SELECT connection_id FROM connection")}

X = {n: np.array([r["features"][n] for r in rows], dtype=float) for n in names}
lab = np.array([truth[r["connection_id"]] in ANOM for r in rows])

print(f"{'feature':34s} {'normal p50':>10s} {'normal p85':>10s} {'anom p50':>10s} {'anom p90':>10s}  suggest ramp")
for n in names:
    a, b = X[n][~lab], X[n][lab]
    lo, hi = float(np.percentile(a, 85)), float(np.percentile(b, 90))
    print(f"{n:34s} {np.median(a):10.3f} {lo:10.3f} {np.median(b):10.3f} {hi:10.3f}  ({lo:.2f}, {hi:.2f})")

print()
print("per-case family scores")
fam_names = ["electricity", "weather", "satellite", "geography", "history"]
by_case = {}
for r in rows:
    fs = feat.family_scores(r["features"])
    by_case.setdefault(cases[r["connection_id"]], []).append([fs[f] for f in fam_names])
print(f"{'case':22s}" + "".join(f"{f:>13s}" for f in fam_names) + "   n")
for k, v in sorted(by_case.items()):
    m = np.array(v).mean(axis=0)
    print(f"{k:22s}" + "".join(f"{x:13.2f}" for x in m) + f"  {len(v):4d}")
