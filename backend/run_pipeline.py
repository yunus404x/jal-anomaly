"""
JAL-ANOMALY  |  End-to-end run.

    python run_pipeline.py            regenerate example data and score it
    python run_pipeline.py --keep     rescore the existing example data

Stages follow the Technical Approach slide exactly:
    1 INGEST -> 2 ENRICH -> 3 CONVERT -> 4 MODEL -> 5 SCORE -> 6 ACT
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data_gen
from config import DATA_DIR, DISTRICT
from pipeline import act, convert, enrich, ingest, model, score


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="reuse data/raw_bundle.json")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 78)
    print(f"JAL-ANOMALY  |  SIH26015  |  {DISTRICT['name']}, {DISTRICT['state']}  |  "
          f"{DISTRICT['season']}")
    print("=" * 78)

    raw = DATA_DIR / "raw_bundle.json"
    if args.keep and raw.exists():
        bundle = json.loads(raw.read_text())
        print(f"[0/6] DATA     : reusing {raw.name} "
              f"({bundle['meta']['n_connections']} connections)")
    else:
        print("[0/6] DATA     : generating example dataset ...")
        bundle = data_gen.build(verbose=False)
        print(f"                 {bundle['meta']['n_connections']} connections, "
              f"{bundle['meta']['n_meter_rows']} meter-days, "
              f"{bundle['meta']['n_satellite_rows']} satellite samples")

    bundle = ingest.run(bundle)
    bundle = enrich.run(bundle)
    bundle = convert.run(bundle)
    bundle = model.run(bundle)
    bundle = score.run(bundle)
    act.build_payload(bundle)

    import db
    chain = db.verify_audit_chain()
    print(f"        audit    : {chain['entries']} entries, chain valid={chain['valid']}, "
          f"head {chain.get('head')}")
    print("-" * 78)
    print(f"done in {time.time() - t0:.1f}s   ->   start the console with:  "
          f"python -m uvicorn app:api --port 8000")


if __name__ == "__main__":
    main()
