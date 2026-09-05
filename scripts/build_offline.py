"""
Package the console as a single self-contained HTML file.

    python scripts/build_offline.py

Everything is inlined - React, MapLibre GL, Recharts, the stylesheet, the app
and the scored run itself - so dist/jal_anomaly_demo.html opens with no server,
no install and no internet.  Presentation insurance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
VENDOR = FRONTEND / "vendor"
DATA = ROOT / "data" / "api_payload.json"
OUT = ROOT / "dist" / "jal_anomaly_demo.html"

VENDOR_JS = ["react.js", "react-dom.js", "prop-types.js", "htm.js", "maplibre-gl.js",
             "recharts.js"]


def main() -> None:
    if not DATA.exists():
        sys.exit("No scored run found. Run backend/run_pipeline.py first.")
    payload = json.loads(DATA.read_text())

    css = (VENDOR / "maplibre-gl.css").read_text() + "\n" + (FRONTEND / "app.css").read_text()
    js = "\n;\n".join((VENDOR / f).read_text() for f in VENDOR_JS)
    app = (FRONTEND / "app.js").read_text()

    meta = payload["meta"]
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JAL-ANOMALY | {meta['district']['name']} risk map (offline demo)</title>
<style>
{css}
</style>
</head>
<body>
<div id="root"><div class="loading">Loading the scored run…</div></div>
<script>{js}</script>
<script>window.__JAL_DATA__ = {json.dumps(payload, separators=(',', ':'))};</script>
<script>{app}</script>
</body>
</html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1e6:.1f} MB)")
    print(f"  run {meta['run_id']} · {payload['kpis']['connections_monitored']} connections · "
          f"{payload['kpis']['alerts_open']} alerts")


if __name__ == "__main__":
    main()
