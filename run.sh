#!/usr/bin/env bash
# JAL-ANOMALY  |  one command to go from nothing to the risk map.
#
#   ./run.sh            install, generate the example data, score it, serve the console
#   ./run.sh pipeline   just regenerate and re-score
#   ./run.sh offline    just rebuild dist/jal_anomaly_demo.html
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
PORT=${PORT:-8000}

ensure_env() {
  if [ ! -d .venv ]; then
    echo "→ creating .venv"
    "$PY" -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  if ! python -c "import xgboost, shap, fastapi" >/dev/null 2>&1; then
    echo "→ installing dependencies (one time, ~1 min)"
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
  fi
}

case "${1:-all}" in
  pipeline)
    ensure_env; cd backend && python run_pipeline.py ;;
  offline)
    ensure_env; python scripts/build_offline.py ;;
  all)
    ensure_env
    (cd backend && python run_pipeline.py)
    python scripts/build_offline.py
    echo
    echo "→ console at http://127.0.0.1:${PORT}   (ctrl-c to stop)"
    echo "→ offline copy at dist/jal_anomaly_demo.html — open it in any browser, no server needed"
    cd backend && python -m uvicorn app:api --port "$PORT" ;;
  *)
    echo "usage: ./run.sh [all|pipeline|offline]"; exit 1 ;;
esac
