# JAL-ANOMALY — working prototype

**AI-powered groundwater pumping anomaly detection**
Smart India Hackathon 2026 · Problem statement **SIH26015** · Team **SyntaxError**

A decision-support prototype that scores every agricultural electricity connection in a
district for abnormal groundwater abstraction, by fusing DISCOM smart-meter data with
satellite soil moisture, rainfall and vegetation, and puts the result on a colour-coded
risk map an officer can act on.

Pilot area in the example data: **four assessment blocks of Sangrur district, Punjab**
(Sunam and Lehragaga are CGWB over-exploited), 612 agricultural connections on four
11 kV feeders, Kharif 2026, 90 days, 55,080 meter-days.

---

## Run it

```bash
./run.sh                      # installs, generates data, scores, serves the console
```

Then open <http://127.0.0.1:8000>.

Or step by step:

```bash
pip install -r requirements.txt
cd backend && python run_pipeline.py          # 1 INGEST .. 6 ACT, about 8 seconds
python -m uvicorn app:api --port 8000         # the GIS console
```

**Presentation insurance** — `dist/jal_anomaly_demo.html` is the same console with the
scored run, all libraries and the map baked in. Double-click it. No server, no install,
no internet. Rebuild it after any re-scoring with `python scripts/build_offline.py`.

---

## What the demo shows

| Slide claim | Where you see it |
|---|---|
| Colour-coded risk map, each alert carries its evidence | The map. Four bands, four colours. Click any dot. |
| Five signal families, no single factor raises an alert | Detail panel → *Evidence families*, with the 0.55 trigger line and the gate note |
| Risk score = f(power anomaly, soil moisture, rainfall, historical pattern, geography) | Detail panel → *Why this score*, SHAP reason codes in the units of the evidence |
| kWh translated to pumped volume | *Abstraction against modelled crop demand*, with the conversion factor, head and a confidence band |
| Expected crop water demand vs observed abstraction | Same chart: red area is abstraction, dashed line is the FAO-56 demand baseline |
| Soil moisture corroborates, it never triggers | Map layer *Soil moisture*, and the satellite chart in the detail panel |
| Anomaly, not accusation | *Officer decision* — confirm, queue a field visit, or clear. Nothing is decided by the model. |
| Feedback retrains the model | Switch role to **admin** → *Retrain on feedback*. Confirmations and clearances become labels for the next run. |
| Whitelist registry suppresses known exceptions | Nurseries, dairies and fish ponds are capped at MONITOR; the cap is shown in the gate note |
| Immutable audit log | `GET /api/audit` — hash-chained, verified on every run |
| Feeder-level energy accounting | *Network* tab, and `GET /api/energy-accounting` for DT input vs the sum of consumer meters |
| Portable to another district by configuration | Everything district-specific is in `backend/config.py` |

---

## How it works

```
1 INGEST   HES/MDM load profiles (DLMS-COSEM, IS 15959) + geo-tagged pump index
2 ENRICH   EOS-04 500 m/17-day fused with SMAP 9 km daily, Sentinel-1/2, IMD rainfall, Bhuvan LULC
3 CONVERT  V = E × 3.6e6 × η / (ρ g H),  H from CGWB DWLR + drawdown + delivery + friction
4 MODEL    FAO-56 ETc = Kc × ET0, effective rainfall, percolation, NDVI crop-presence check
5 SCORE    27 features → 5 families → 3 detectors → gate → whitelist → band
6 ACT      map, review queue, officer decision, audit log, retraining
```

**Stage 3, conversion.** The engine uses *nameplate* pump efficiency and the block water
level — what a utility can actually know — not the per-pump truth. The resulting error
(about 6% against the ground truth in the example data) is carried through to the alert
as a confidence band, because an anomaly has to survive it before anyone is sent to a field.

**Stage 4, the demand baseline.** Two corrections keep it honest. The declared crop is
checked against Sentinel-2 NDVI, so a parcel that never leaves bare-soil values loses its
water entitlement and its draw has nothing to justify it. And where fused soil moisture is
already at field capacity, the crop's need is met from storage, so expected irrigation for
that day falls — which is what makes post-rain pumping visible.

**Stage 5, the score.** Three detectors look at the same feature matrix from different
angles: an Isolation Forest for patterns nobody labelled, a temporal residual against the
connection's own seasonal baseline (PyTorch LSTM-autoencoder if `torch` is installed, an
EWMA baseline otherwise — same interface, the console reports which ran), and an XGBoost
ranker trained on connections a field officer already confirmed or cleared. Their blend is
combined with the five weighted family scores; 60% of the family component is the weighted
mean and 40% is the mean of the three strongest families, so corroboration counts for more
than one loud signal. SHAP over the XGBoost model produces the reason codes.

Then two rules apply, both visible in the console:

- **Gate** — SUSPICIOUS needs two families over 0.55, HIGH RISK needs three. No single
  family can raise an alert on its own. Where the gate holds a score back, the panel says so.
- **Whitelist** — registered exceptions (nursery, dairy, fish pond, sandy-soil block,
  second crop) are never escalated past MONITOR.

Every threshold in `pipeline/features.py` was placed with `scripts/calibrate.py`: the low
end of each ramp is the 85th percentile of ordinary connections, the high end the 90th
percentile of confirmed ones.

---

## The example data

`backend/data_gen.py` builds a reproducible, physically consistent season. It is not noise
dressed up as data: rainfall drives a root-zone soil-moisture bucket, the bucket and the
crop coefficient drive irrigation demand, demand and pump head drive energy, and the
satellite streams are sampled from the truth with each sensor's real revisit interval,
retrieval rate and noise (EOS-04 every 17 days at 92%, SMAP daily, Sentinel-2 every 5 days
with monsoon cloud losses). Ground truth is written alongside so the prototype can be
scored honestly.

Seven behaviours are planted, with deliberate overlap between them so the classes are not
trivially separable:

| case | share | what it looks like |
|---|---|---|
| `normal` | 70% | pumping tracks crop stage and backs off after rain |
| `excess_extraction` | 8.5% | 1.35–2.6× crop demand, long hours, much of it outside the supply window |
| `no_crop_draw` | 4.5% | steady weather-blind draw, NDVI flat, land use not cropland |
| `post_rain_pumping` | 6% | ignores rainfall and wet soil |
| `step_change` | 4% | sustained jump partway through the season |
| `legit_exception` | 4.5% | nursery, dairy or fish pond — high use, legitimately |
| `under_reporting` | 2.5% | meter under-registers; shows up in the DT energy balance |

### Honest note on the accuracy figures

The console reports ROC-AUC and precision@50 on connections whose labels the model never
saw. Those numbers are high because the example data is synthetic: the anomalies were
generated from known behaviours, so they are cleanly separable in a way real connections
are not. **They demonstrate that the pipeline works end to end. They are not a claim about
field accuracy.** Real accuracy can only come from the CGWB DWLR back-test and officer
feedback described on the feasibility slide. The console says this on the Model tab too.

---

## Layout

```
backend/
  config.py            district, crops, pump specs, bands, family weights, gate  ← port here
  data_gen.py          the example dataset
  db.py                storage; same table names as deploy/sql/schema.sql
  app.py               FastAPI service, role-based access, audit
  run_pipeline.py      the six stages, end to end
  pipeline/
    ingest.py enrich.py convert.py model.py score.py act.py
    features.py        the 27 features and the five family scores
frontend/
  index.html app.css app.js        React + MapLibre GL + Recharts, no build step
  vendor/                          the libraries, vendored so it runs offline
scripts/
  build_offline.py     packages the single-file demo
  calibrate.py         prints the separation behind every threshold
  screenshot.py        headless smoke test of the console
deploy/
  docker-compose.yml   the pilot stack: TimescaleDB, PostGIS, MinIO, Kafka, Keycloak
  sql/schema.sql       production DDL, hypertables and GIST indexes
```

## API

| endpoint | purpose |
|---|---|
| `GET /api/meta`, `/api/kpis`, `/api/bands` | run metadata, headline numbers, band definitions |
| `GET /api/map/connections?band=&feeder_id=` | the risk map as GeoJSON |
| `GET /api/map/layers/{soil_moisture\|ndvi\|rainfall_7d\|blocks\|dts\|feeder_lines}` | map layers |
| `GET /api/alerts?band=&feeder_id=&limit=` | the review queue, highest risk first |
| `GET /api/connections/{id}` | full evidence for one connection |
| `POST /api/alerts/{id}/feedback` | officer decision (`X-Role: officer` or `admin`) |
| `POST /api/retrain` | re-score with officer labels folded in (`X-Role: admin`) |
| `GET /api/energy-accounting` | DT input energy vs the sum of consumer meters |
| `GET /api/audit` | the hash-chained ledger, with verification |

## If something goes wrong on demo day

| symptom | fix |
|---|---|
| `sqlite3.OperationalError: disk I/O error` | The project folder is on a network or cloud-synced drive where SQLite cannot lock. Run with the database on local disk: `JAL_DB_PATH=/tmp/jal.db python run_pipeline.py` |
| `shap` raises on `TreeExplainer` | Nothing to do — the pipeline falls back to XGBoost's own TreeSHAP and says so on the Model tab. The reason codes are identical. |
| Anything at all fails on the laptop | Open `dist/jal_anomaly_demo.html`. It is the same console with the scored run baked in and needs nothing but a browser. |
| Map is blank | The offline build draws no basemap by design, so it works without internet. The blocks, grids and connections are all drawn from local GeoJSON. Tick *OSM basemap* only if you have wifi. |
| `./run.sh` cannot find python | `PYTHON=python3.11 ./run.sh`, or install dependencies yourself with `pip install -r requirements.txt`. |

Verified on Python 3.10 and 3.11, macOS and Linux, from a clean virtual environment.

---

## What this prototype is not

- The satellite and meter streams are **generated**, not live. Every field matches the
  real source it stands for, but no ISRO, IMD or DISCOM endpoint is called.
- Block boundaries are illustrative rectangles over the Sangrur bounding box. The CGWB
  categories and stages of extraction attached to them are real.
- SQLite stands in for TimescaleDB and PostGIS, and an `X-Role` header stands in for
  Keycloak. Both are one configuration change away, and `deploy/` holds the target.
- An anomaly is not an offence. Nothing here decides anything: the output is a graded
  score with its reasons attached, and a human makes the call.
