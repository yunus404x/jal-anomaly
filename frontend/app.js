/* ===========================================================================
   JAL-ANOMALY  |  GIS console
   React (via htm, no build step) + MapLibre GL + Recharts.

   Reads either the FastAPI service (/api/payload) or, in the packaged offline
   build, the scored run embedded as window.__JAL_DATA__.  The two paths render
   exactly the same console.
   =========================================================================== */
const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useMemo, useCallback } = React;
const RC = window.Recharts;

const OFFLINE = typeof window.__JAL_DATA__ !== "undefined";

/* ---------------------------------------------------------------- helpers */
const fmt = (n, d = 0) => (n === null || n === undefined || isNaN(n)) ? "-" :
  Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtCompact = (n) => n >= 1e7 ? (n / 1e7).toFixed(2) + " cr" :
  n >= 1e5 ? (n / 1e5).toFixed(2) + " lakh" : fmt(n);
const BAND_LABEL = { NORMAL: "Normal", MONITOR: "Monitor", SUSPICIOUS: "Suspicious", HIGH_RISK: "High risk" };
const bandColour = (bands, key) => (bands.find(b => b.key === key) || {}).colour || "#888";

function lerpColour(stops, v) {
  for (let i = 1; i < stops.length; i++) {
    if (v <= stops[i][0] || i === stops.length - 1) {
      const [v0, c0] = stops[i - 1], [v1, c1] = stops[i];
      const t = Math.max(0, Math.min(1, (v - v0) / (v1 - v0 || 1)));
      const p = (a, b) => Math.round(a + (b - a) * t);
      return `rgb(${p(c0[0], c1[0])},${p(c0[1], c1[1])},${p(c0[2], c1[2])})`;
    }
  }
  return "#ccc";
}
const SM_STOPS = [[0.10, [246, 232, 195]], [0.26, [199, 214, 200]], [0.34, [128, 186, 200]], [0.46, [33, 102, 172]]];
const NDVI_STOPS = [[0.05, [214, 199, 160]], [0.35, [173, 196, 120]], [0.60, [88, 160, 80]], [0.85, [21, 105, 50]]];
const RAIN_STOPS = [[0, [238, 245, 255]], [60, [158, 202, 225]], [140, [66, 146, 198]], [260, [8, 64, 129]]];

/* ------------------------------------------------------------- data layer */
const api = {
  async payload() {
    if (OFFLINE) return window.__JAL_DATA__;
    const r = await fetch("/api/payload");
    if (!r.ok) throw new Error("API " + r.status + " - has run_pipeline.py been run?");
    return r.json();
  },
  async feedback(alertId, body, role) {
    if (OFFLINE) {
      return { alert_id: alertId, status: body.decision, offline: true,
               audit_hash: "offline-" + Math.random().toString(16).slice(2, 10) };
    }
    const r = await fetch(`/api/alerts/${alertId}/feedback`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Role": role },
      body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error((await r.text()).slice(0, 200));
    return r.json();
  },
  async retrain(role) {
    if (OFFLINE) throw new Error("Retraining needs the API. Start it with: python -m uvicorn app:api --port 8000");
    const r = await fetch("/api/retrain", { method: "POST", headers: { "X-Role": role } });
    if (!r.ok) throw new Error((await r.text()).slice(0, 300));
    return r.json();
  }
};

/* ==================================================================== MAP */
function MapView({ data, filters, selected, onSelect, layers }) {
  const ref = useRef(null);
  const mapRef = useRef(null);
  const markers = useRef([]);
  const [ready, setReady] = useState(false);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    if (mapRef.current || !ref.current) return;
    const c = data.meta.district.centre;
    const map = new maplibregl.Map({
      container: ref.current,
      style: {
        version: 8, sources: {}, layers: [
          { id: "bg", type: "background", paint: { "background-color": "#e9eef3" } }]
      },
      center: c, zoom: 9.3, attributionControl: false
    });
    const b = new maplibregl.LngLatBounds();
    data.map.connections.features.forEach(f => b.extend(f.geometry.coordinates));
    map.fitBounds(b, { padding: { top: 60, bottom: 40, left: 40, right: 220 }, duration: 0 });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");
    mapRef.current = map;

    map.on("load", () => {
      const M = data.map;
      const gridPaint = (stops) => ({
        "fill-color": ["interpolate", ["linear"], ["get", "value"],
          ...stops.flatMap(([v, c]) => [v, `rgb(${c.join(",")})`])],
        "fill-opacity": 0.72
      });

      map.addSource("sm", { type: "geojson", data: M.soil_moisture });
      map.addLayer({ id: "sm", type: "fill", source: "sm", layout: { visibility: "none" },
                     paint: gridPaint(SM_STOPS) });
      map.addSource("ndvi", { type: "geojson", data: M.ndvi });
      map.addLayer({ id: "ndvi", type: "fill", source: "ndvi", layout: { visibility: "none" },
                     paint: gridPaint(NDVI_STOPS) });
      map.addSource("rain", { type: "geojson", data: M.rainfall_7d });
      map.addLayer({ id: "rain", type: "fill", source: "rain", layout: { visibility: "none" },
                     paint: gridPaint(RAIN_STOPS) });

      map.addSource("blocks", { type: "geojson", data: M.blocks });
      map.addLayer({ id: "blocks-fill", type: "fill", source: "blocks", paint: {
        "fill-color": ["match", ["get", "category"],
          "Over-exploited", "#c62828", "Critical", "#e2711d", "Semi-critical", "#e0a217", "#1f9d8f"],
        "fill-opacity": 0.055 } });
      map.addLayer({ id: "blocks-line", type: "line", source: "blocks", paint: {
        "line-color": "#7d93a8", "line-width": 1.1, "line-dasharray": [3, 2] } });

      map.addSource("feeders", { type: "geojson", data: M.feeder_lines });
      map.addLayer({ id: "feeders", type: "line", source: "feeders", layout: { visibility: "none" },
                     paint: { "line-color": "#7a5cff", "line-width": 1.2, "line-opacity": 0.65 } });
      map.addSource("dts", { type: "geojson", data: M.dts });
      map.addLayer({ id: "dts", type: "circle", source: "dts", layout: { visibility: "none" },
                     paint: { "circle-radius": 3.4, "circle-color": "#5b3fd6",
                              "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });

      map.addSource("conns", { type: "geojson", data: M.connections });
      map.addLayer({ id: "conns", type: "circle", source: "conns", paint: {
        "circle-color": ["get", "colour"],
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          8, ["interpolate", ["linear"], ["get", "risk_score"], 0, 2.4, 100, 5.5],
          11, ["interpolate", ["linear"], ["get", "risk_score"], 0, 4.2, 100, 10],
          14, ["interpolate", ["linear"], ["get", "risk_score"], 0, 7, 100, 16]],
        "circle-opacity": 0.92,
        "circle-stroke-width": ["case", [">=", ["get", "risk_score"], 75], 1.6, 0.7],
        "circle-stroke-color": ["case", [">=", ["get", "risk_score"], 75], "#5c0f0f", "#ffffff"] } });
      map.addLayer({ id: "conns-halo", type: "circle", source: "conns",
        filter: ["==", ["get", "band"], "HIGH_RISK"], paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 9, 11, 17, 14, 26],
          "circle-color": "#c62828", "circle-opacity": 0.14 } }, "conns");
      map.addLayer({ id: "conns-sel", type: "circle", source: "conns",
        filter: ["==", ["get", "connection_id"], "__none__"], paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 8, 11, 15, 14, 23],
          "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#0d6efd",
          "circle-stroke-width": 2.6 } });

      // block name labels as DOM markers (no glyph server needed - works offline)
      M.blocks.features.forEach(f => {
        const ring = f.geometry.coordinates[0];
        const cx = ring[0][0] + (ring[2][0] - ring[0][0]) * 0.5, cy = ring[2][1] - 0.006;
        const el = document.createElement("div");
        el.className = "blocklabel";
        el.textContent = `${f.properties.name.toUpperCase()} · ${f.properties.category} · ${f.properties.stage_of_extraction}%`;
        markers.current.push(new maplibregl.Marker({ element: el }).setLngLat([cx, cy]).addTo(map));
      });

      map.on("click", "conns", e => onSelect(e.features[0].properties.connection_id));
      map.on("mouseenter", "conns", e => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mousemove", "conns", e => setHover(e.features[0].properties));
      map.on("mouseleave", "conns", () => { map.getCanvas().style.cursor = ""; setHover(null); });
      setReady(true);
    });
    return () => { markers.current.forEach(m => m.remove()); map.remove(); mapRef.current = null; };
  }, []);

  /* filters -> map source */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const feats = data.map.connections.features.filter(f => {
      const p = f.properties;
      if (filters.bands.size && !filters.bands.has(p.band)) return false;
      if (filters.feeder && p.feeder_id !== filters.feeder) return false;
      if (filters.hideWhitelist && p.whitelist) return false;
      return true;
    });
    map.getSource("conns").setData({ type: "FeatureCollection", features: feats });
  }, [ready, filters, data]);

  /* layer visibility */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const vis = (id, on) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    vis("sm", layers.sm); vis("ndvi", layers.ndvi); vis("rain", layers.rain);
    vis("feeders", layers.network); vis("dts", layers.network);
    if (layers.basemap && !map.getSource("osm")) {
      map.addSource("osm", { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                             tileSize: 256, attribution: "(c) OpenStreetMap contributors" });
      map.addLayer({ id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.55 } }, "sm");
    } else if (map.getLayer("osm")) {
      map.setLayoutProperty("osm", "visibility", layers.basemap ? "visible" : "none");
    }
  }, [ready, layers]);

  /* selection */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setFilter("conns-sel", ["==", ["get", "connection_id"], selected || "__none__"]);
    if (selected) {
      const f = data.map.connections.features.find(x => x.properties.connection_id === selected);
      if (f) map.easeTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), 11.6), duration: 600 });
    }
  }, [ready, selected]);

  const shown = useMemo(() => data.map.connections.features.filter(f => {
    const p = f.properties;
    if (filters.bands.size && !filters.bands.has(p.band)) return false;
    if (filters.feeder && p.feeder_id !== filters.feeder) return false;
    if (filters.hideWhitelist && p.whitelist) return false;
    return true;
  }).length, [filters, data]);

  const scaleBar = (stops) => html`<div class="scale">${stops.map((s, i) =>
    html`<div key=${i} style=${{ flex: 1, background: `linear-gradient(90deg,rgb(${s[1].join(",")}),rgb(${(stops[i + 1] || s)[1].join(",")}))` }}></div>`)}</div>`;

  return html`
    <div class="mapwrap">
      <div id="map" ref=${ref}></div>
      <div class="mapcard mapstat">
        <b>${fmt(shown)}</b> connections shown${filters.feeder ? ` · ${filters.feeder}` : ""}
        ${hover && html`<div style=${{ marginTop: 4, color: "#6b7c8c" }}>
          ${hover.connection_id} · ${hover.crop_label} · ${hover.area_ha} ha ·
          <b style=${{ color: hover.colour }}>${BAND_LABEL[hover.band]} ${hover.risk_score}</b></div>`}
      </div>
      <div class="mapcard layers">
        <div style=${{ fontWeight: 700, fontSize: 10.5, letterSpacing: ".05em", color: "#6b7c8c", marginBottom: 4 }}>
          MAP LAYERS</div>
        ${[["sm", "Soil moisture (EOS-04 + SMAP)"], ["ndvi", "NDVI (Sentinel-2)"],
           ["rain", "Rainfall, 7-day (IMD)"], ["network", "Feeders and DTs"],
           ["basemap", "OSM basemap (needs internet)"]].map(([k, label]) => html`
          <label class="lrow" key=${k}>
            <input type="checkbox" checked=${!!layers[k]} onChange=${() => layers.set(k, !layers[k])} />
            <span>${label}</span>
          </label>`)}
      </div>
      ${(layers.sm || layers.ndvi || layers.rain) && html`
        <div class="mapcard maplegend">
          ${layers.sm && html`<div><b style=${{ fontSize: 10.5 }}>Soil moisture m³/m³</b>
            ${scaleBar(SM_STOPS)}<div style=${{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#6b7c8c" }}><span>0.10 dry</span><span>0.46 saturated</span></div></div>`}
          ${layers.ndvi && html`<div><b style=${{ fontSize: 10.5 }}>NDVI</b>${scaleBar(NDVI_STOPS)}
            <div style=${{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#6b7c8c" }}><span>0.05 bare</span><span>0.85 vigorous</span></div></div>`}
          ${layers.rain && html`<div><b style=${{ fontSize: 10.5 }}>Rainfall, 7-day mm</b>${scaleBar(RAIN_STOPS)}
            <div style=${{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#6b7c8c" }}><span>0</span><span>260</span></div></div>`}
        </div>`}
    </div>`;
}

/* ============================================================ LEFT PANEL */
function Kpis({ k, meta }) {
  return html`
    <div class="kpis">
      <div class="kpi"><div class="v">${fmt(k.connections_monitored)}</div>
        <div class="l">Connections</div><div class="s">${fmt(k.meter_days)} meter-days</div></div>
      <div class="kpi"><div class="v">${fmt(k.alerts_open)}</div>
        <div class="l">Open alerts</div><div class="s">${k.shortlist_pct}% shortlisted for a visit</div></div>
      <div class="kpi"><div class="v">${k.estimated_excess_mcm} <span style=${{ fontSize: 12 }}>MCM</span></div>
        <div class="l">Excess abstraction</div><div class="s">against modelled crop demand</div></div>
      <div class="kpi"><div class="v">₹${fmtCompact(k.estimated_subsidy_value_rs)}</div>
        <div class="l">Subsidised energy</div><div class="s">${fmt(k.estimated_excess_kwh / 1000)} MWh at ₹${meta.subsidy_rs_per_kwh}/kWh</div></div>
    </div>`;
}

function Legend({ bands, counts, active, toggle }) {
  return html`<div class="legend">
    ${bands.map(b => html`
      <div class="row ${active.has(b.key) ? "on" : ""}" key=${b.key} onClick=${() => toggle(b.key)}
           title=${b.meaning}>
        <span class="dot" style=${{ background: b.colour }}></span>
        <span class="nm">${BAND_LABEL[b.key]}</span>
        <span class="ct">${fmt(counts[b.key] || 0)}</span>
      </div>`)}
  </div>`;
}

function AlertList({ alerts, bands, selected, onSelect, feedback }) {
  if (!alerts.length) return html`<div class="empty">No alerts match this filter.</div>`;
  return html`<div>${alerts.map(a => {
    const c = bandColour(bands, a.band);
    const fb = feedback[a.alert_id];
    return html`
      <div class="alert ${selected === a.connection_id ? "sel" : ""}" key=${a.alert_id}
           style=${{ borderLeftColor: c }} onClick=${() => onSelect(a.connection_id)}>
        <span class="sc" style=${{ color: c }}>${a.risk_score}</span>
        <div class="id">${a.connection_id}</div>
        <div class="meta">${a.alert_id} · ${a.crop} · ${a.area_ha} ha · ${a.feeder_id}
          ${a.whitelist ? html` · <span class="tag">${a.whitelist.replace("_", " ")}</span>` : null}</div>
        <div class="why">${(a.reasons[0] || {}).text || ""}</div>
        <div style=${{ marginTop: 4 }}>
          <span class="pill" style=${{ background: c }}>${BAND_LABEL[a.band]}</span>
          <span class="tag" style=${{ marginLeft: 5 }}>${a.triggered_families}/5 families</span>
          ${fb && html`<span class="tag" style=${{ background: "#dff0ea", color: "#12695f" }}>${fb.status}</span>`}
        </div>
      </div>`;
  })}</div>`;
}

function FeederPanel({ k }) {
  return html`
    <div>
      <div class="sec">Agricultural feeders</div>
      <table class="tbl">
        <thead><tr><th>Feeder</th><th>Conn</th><th>MWh</th><th>MCM</th><th>Alerts</th></tr></thead>
        <tbody>${k.feeders.map(f => html`<tr key=${f.feeder_id}>
          <td><b>${f.feeder_id}</b><div style=${{ color: "#6b7c8c" }}>${f.name} · ${f.supply_window}</div></td>
          <td>${f.connections}</td><td>${f.energy_mwh}</td><td>${f.volume_mcm}</td>
          <td>${f.alerts}${f.high_risk ? html` <span style=${{ color: "#c62828", fontWeight: 700 }}>(${f.high_risk})</span>` : null}</td>
        </tr>`)}</tbody>
      </table>
      <div class="sec">CGWB assessment blocks</div>
      <table class="tbl">
        <thead><tr><th>Block</th><th>Category</th><th>SoE</th><th>Alerts</th><th>Excess MCM</th></tr></thead>
        <tbody>${k.blocks.map(b => html`<tr key=${b.block_id}>
          <td><b>${b.name}</b></td><td>${b.category}</td><td>${b.stage_of_extraction}%</td>
          <td>${b.alerts}</td><td>${b.excess_mcm}</td></tr>`)}</tbody>
      </table>
      <div class="note">Stage of extraction and block category are CGWB 2025 categories; a block above
        100% is drawing more than it recharges. Excess is the modelled gap between abstraction and
        crop water demand for connections in that block.</div>
    </div>`;
}

function ModelPanel({ meta, k }) {
  const m = meta.metrics;
  const stages = [
    ["1 INGEST", "Meter load profiles and the geo-tagged pump index from the utility's HES/MDM",
     `${fmt(k.meter_days)} meter-days, DLMS-COSEM / IS 15959`],
    ["2 ENRICH", "Soil moisture, rainfall, NDVI and land use sampled for each pump pixel",
     "EOS-04 500 m/17 d fused with SMAP 9 km daily, Sentinel-2 NDVI, IMD 0.25°"],
    ["3 CONVERT", "kWh translated to pumped volume using pump curve, head and efficiency",
     "V = E × 3.6e6 × η / (ρ g H), head from CGWB DWLR + drawdown"],
    ["4 MODEL", "Expected crop water demand compared against observed abstraction",
     "FAO-56 Kc curve, NDVI crop-presence check, field-capacity correction"],
    ["5 SCORE", "Multi-factor risk score across the five signal families",
     `${m.features} features, 3 detectors, gate: 2 families for suspicious, 3 for high risk`],
    ["6 ACT", "Colour-coded risk map, officer review, and feedback that retrains the model",
     `${fmt(k.alerts_open)} alerts, append-only audit log`]];
  return html`
    <div>
      <div class="sec">Pipeline</div>
      ${stages.map(([n, d, s]) => html`<div class="card" key=${n} style=${{ margin: "7px 0", padding: "8px 10px" }}>
        <b style=${{ letterSpacing: ".05em", fontSize: 11 }}>${n}</b>
        <div style=${{ fontSize: 11.5, marginTop: 2 }}>${d}</div>
        <div style=${{ fontSize: 10.5, color: "#6b7c8c", marginTop: 3 }}>${s}</div></div>`)}

      <div class="sec">Detectors and weights</div>
      <table class="tbl"><tbody>
        <tr><td>Isolation Forest (unsupervised)</td><td>${meta.metrics.model_weights.isolation_forest}</td></tr>
        <tr><td>Temporal residual <code class="sm">${m.temporal_backend}</code></td><td>${meta.metrics.model_weights.temporal_residual}</td></tr>
        <tr><td>XGBoost ranker on officer labels</td><td>${meta.metrics.model_weights.supervised}</td></tr>
        <tr><td>Reason codes <code class="sm">${(m.shap_backend || "").split(" ")[0]}</code></td><td>TreeSHAP</td></tr>
      </tbody></table>
      <div class="note">Reason codes come from SHAP values over the XGBoost model. The family score is
        60% weighted mean and 40% the mean of the three strongest families, so corroboration across
        families counts for more than one loud signal.</div>

      <div class="sec">Holdout performance</div>
      <table class="tbl"><tbody>
        <tr><td>Connections held out (never labelled)</td><td>${m.n_holdout}</td></tr>
        <tr><td>Officer labels used for training</td><td>${m.n_labelled_history}</td></tr>
        <tr><td>ROC-AUC</td><td>${m.roc_auc}</td></tr>
        <tr><td>Average precision</td><td>${m.average_precision}</td></tr>
        <tr><td>Precision @ top 50</td><td>${m.precision_at_50}</td></tr>
        <tr><td>Recall @ top 50</td><td>${m.recall_at_50}</td></tr>
      </tbody></table>
      <div class="warn" style=${{ marginTop: 8 }}>These figures are measured on the synthetic example
        dataset, where the anomaly cases were generated from known behaviours. They show the pipeline
        works end to end; they are not a claim about field accuracy. Real accuracy can only come from
        the DWLR back-test and officer feedback described on the feasibility slide.</div>

      <div class="sec">Data sources</div>
      ${meta.data_sources.map(s => html`<div key=${s} style=${{ fontSize: 11, padding: "2px 0", color: "#33475b" }}>· ${s}</div>`)}
    </div>`;
}

/* =========================================================== DETAIL PANEL */
function FamilyBars({ families, meta, triggered }) {
  return html`<div>${Object.keys(meta.families).map(k => {
    const v = families[k], on = triggered.includes(k);
    return html`<div class="fam" key=${k} title=${meta.families[k].desc}>
      <span class="nm">${meta.families[k].label}</span>
      <span class="bar"><span class="fill" style=${{ width: (v * 100) + "%",
        background: on ? "#c62828" : "#8fa9c2" }}></span><span class="trig"></span></span>
      <span class="v" style=${{ color: on ? "#c62828" : "#48607a", fontWeight: on ? 700 : 400 }}>${v.toFixed(2)}</span>
    </div>`;
  })}</div>`;
}

function AbstractionChart({ series }) {
  const rows = series.date.map((d, i) => ({
    d: d.slice(5), obs: series.volume_m3[i], exp: series.expected_m3[i], rain: series.rain_mm[i]
  }));
  const { ResponsiveContainer, ComposedChart, Area, Line, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Legend } = RC;
  return html`
    <${ResponsiveContainer} width="100%" height=${170}>
      <${ComposedChart} data=${rows} margin=${{ top: 4, right: 4, bottom: 0, left: -18 }}>
        <${CartesianGrid} stroke="#eef2f6" />
        <${XAxis} dataKey="d" interval=${14} tick=${{ fontSize: 9 }} />
        <${YAxis} tick=${{ fontSize: 9 }} />
        <${YAxis} yAxisId="r" orientation="right" reversed=${true} tick=${{ fontSize: 9 }} width=${26} />
        <${Tooltip} contentStyle=${{ fontSize: 11 }} />
        <${Legend} wrapperStyle=${{ fontSize: 10 }} />
        <${Bar} yAxisId="r" dataKey="rain" name="Rainfall mm" fill="#9ecae1" />
        <${Area} dataKey="obs" name="Abstraction m³" stroke="#c62828" fill="#c62828" fillOpacity=${0.16} />
        <${Line} dataKey="exp" name="Crop demand m³" stroke="#12304f" dot=${false} strokeWidth=${1.6}
                 strokeDasharray="4 2" />
      <//>
    <//>`;
}

function SatelliteChart({ series }) {
  const rows = series.date.map((d, i) => ({
    d: d.slice(5), sm: series.sm_fused[i], ndvi: series.ndvi[i] }));
  const { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, Legend, ReferenceLine } = RC;
  return html`
    <${ResponsiveContainer} width="100%" height=${140}>
      <${LineChart} data=${rows} margin=${{ top: 4, right: 4, bottom: 0, left: -18 }}>
        <${CartesianGrid} stroke="#eef2f6" />
        <${XAxis} dataKey="d" interval=${14} tick=${{ fontSize: 9 }} />
        <${YAxis} domain=${[0, 1]} tick=${{ fontSize: 9 }} />
        <${Tooltip} contentStyle=${{ fontSize: 11 }} />
        <${Legend} wrapperStyle=${{ fontSize: 10 }} />
        <${ReferenceLine} y=${0.32} stroke="#8fa3b6" strokeDasharray="3 3" />
        <${Line} dataKey="sm" name="Soil moisture" stroke="#2166ac" dot=${false} strokeWidth=${1.5} />
        <${Line} dataKey="ndvi" name="NDVI" stroke="#2e7d32" dot=${false} strokeWidth=${1.5} />
      <//>
    <//>`;
}

function ProfileChart({ profile, window_ }) {
  const rows = profile.map((v, i) => ({
    t: `${String(Math.floor(i / 4)).padStart(2, "0")}:${String((i % 4) * 15).padStart(2, "0")}`,
    kw: v, off: (window_ === "night" ? (i >= 24 && i < 76) : (i < 24 || i >= 76)) ? v : 0
  }));
  const { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Legend } = RC;
  return html`
    <${ResponsiveContainer} width="100%" height=${130}>
      <${BarChart} data=${rows} margin=${{ top: 4, right: 4, bottom: 0, left: -18 }}>
        <${CartesianGrid} stroke="#eef2f6" />
        <${XAxis} dataKey="t" interval=${11} tick=${{ fontSize: 9 }} />
        <${YAxis} tick=${{ fontSize: 9 }} />
        <${Tooltip} contentStyle=${{ fontSize: 11 }} />
        <${Legend} wrapperStyle=${{ fontSize: 10 }} />
        <${Bar} dataKey="kw" name="kW in supply window" fill="#8fa9c2" />
        <${Bar} dataKey="off" name="kW outside window" fill="#c62828" />
      <//>
    <//>`;
}

function Detail({ data, id, role, feedback, setFeedback }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const d = data.details[id];
  const alert = data.alerts.find(a => a.connection_id === id);
  const meta = data.meta;
  if (!d) return html`
    <div class="dt">
      <div class="card">
        <div class="sec" style=${ { margin: "0 0 6px" } }>Start here</div>
        <div style=${ { fontSize: 12 } }>
          Every dot on the map is one agricultural connection, coloured by its risk band.
          Click a dot, or an alert in the queue on the left, to see the evidence behind its score:
          the five signal families, the SHAP reason codes, abstraction against modelled crop demand,
          the satellite record for that pixel, and the 15-minute load profile.
        </div>
        <div class="note">Turn on the soil moisture, NDVI or rainfall layers from the map panel to see
          what the risk engine is reading. Use the band rows on the left to filter the map.</div>
      </div>
      <div class="card">
        <div class="sec" style=${ { margin: "0 0 6px" } }>What the bands mean</div>
        ${data.meta.bands.map(b => html`<div key=${b.key} style=${ { margin: "7px 0" } }>
          <span class="pill" style=${ { background: b.colour } }>${BAND_LABEL[b.key]}</span>
          <span style=${ { fontSize: 11, color: "#6b7c8c", marginLeft: 6 } }>${b.min}–${Math.min(b.max, 100)}</span>
          <div style=${ { fontSize: 11.5, marginTop: 3 } }>${b.meaning}</div>
        </div>`)}
      </div>
    </div>`;
  const c = d.connection, r = d.risk;
  const feeder = data.kpis.feeders.find(f => f.feeder_id === c.feeder_id) || {};
  const fb = alert ? feedback[alert.alert_id] : null;

  const act = async (decision) => {
    if (!alert) return;
    setBusy(true); setErr(null);
    try {
      const res = await api.feedback(alert.alert_id, { decision, note: "reviewed in console",
        officer: "field.officer@pspcl" }, role);
      setFeedback(f => ({ ...f, [alert.alert_id]: res }));
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };

  return html`
    <div class="dt">
      <div style=${{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style=${{ flex: 1 }}>
          <h2>${c.connection_id}</h2>
          <div class="sub">${c.crop_label} · ${c.area_ha} ha · ${c.pump_hp} hp · ${c.dt_id} · ${(data.kpis.blocks.find(b => b.block_id === c.block_id) || {}).name} block</div>
        </div>
        <div style=${{ textAlign: "right" }}>
          <div style=${{ fontSize: 28, fontWeight: 800, lineHeight: 1, color: r.colour }}>${r.score}</div>
          <span class="pill" style=${{ background: r.colour }}>${BAND_LABEL[r.band]}</span>
        </div>
      </div>
      <div class="scorebar"><span class="mk" style=${{ left: `calc(${r.score}% - 1px)` }}></span></div>
      <div style=${{ display: "flex", justifyContent: "space-between", fontSize: 9.5, color: "#6b7c8c" }}>
        <span>0</span><span>30 monitor</span><span>55 suspicious</span><span>75 high</span><span>100</span></div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 6px" }}>Evidence families</div>
        <${FamilyBars} families=${r.families} meta=${meta} triggered=${r.triggered} />
        <div class="note">
          ${r.triggered.length} of 5 families over the 0.55 trigger.
          ${r.capped_by ? html` Score held back — <b>${r.capped_by}</b>.` :
            " No single family can raise an alert on its own: two are needed for suspicious, three for high risk."}
        </div>
      </div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 4px" }}>Why this score</div>
        ${r.reasons.length ? r.reasons.map((x, i) => html`
          <div class="reason" key=${i}>
            <span class="sh">SHAP ${x.shap.toFixed(2)}</span>
            <span class="tag">${x.family}</span>${x.text}.
          </div>`) : html`<div class="note">Nothing pushed this connection above its peers.</div>`}
      </div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 6px" }}>Abstraction against modelled crop demand</div>
        <${AbstractionChart} series=${d.series} />
        <div class="kv" style=${{ marginTop: 8 }}>
          <span><b>Season abstraction</b> ${fmt(r.season_volume_m3)} m³</span>
          <span><b>Crop demand</b> ${fmt(r.season_expected_m3)} m³</span>
          <span><b>Excess</b> ${fmt(r.excess_m3)} m³</span>
          <span><b>Confidence band</b> ${fmt(r.excess_lo)}–${fmt(r.excess_hi)} m³</span>
          <span><b>Energy</b> ${fmt(r.season_energy_kwh)} kWh</span>
          <span><b>Conversion</b> ${r.conversion.m3_per_kwh} m³/kWh at ${r.conversion.assumed_head_m} m head</span>
        </div>
      </div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 6px" }}>Soil moisture and vegetation at this pixel</div>
        <${SatelliteChart} series=${d.series} />
        <div class="note">Fused EOS-04 (500 m, 17-day) and SMAP (9 km, daily); dashed line is field
          capacity. NDVI is Sentinel-2, forward-filled through cloud gaps.</div>
      </div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 6px" }}>15-minute load profile · ${d.series.date[d.series.date.length - 1]}</div>
        <${ProfileChart} profile=${d.load_profile} window_=${feeder.supply_window} />
        <div class="note">Declared supply window for ${c.feeder_id} is <b>${feeder.supply_window || "-"}</b>.
          Red blocks are running outside it.</div>
      </div>

      <div class="card">
        <div class="sec" style=${{ margin: "0 0 6px" }}>Connection record</div>
        <div class="kv">
          <span><b>Feeder</b> ${c.feeder_id}</span><span><b>DT</b> ${c.dt_id}</span>
          <span><b>Sanctioned</b> ${c.sanctioned_load_kw} kW</span><span><b>Connected</b> ${c.connected_load_kw} kW</span>
          <span><b>Land use</b> ${c.lulc_class}</span><span><b>Water level</b> ${c.static_water_level_m} m</span>
          <span><b>Block</b> ${c.block_category} (${c.stage_of_extraction}%)</span>
          <span><b>Nearest AP</b> ${c.distance_to_registered_ap_m} m</span>
          <span><b>Tariff</b> ${c.tariff}</span>
          <span><b>Age</b> ${c.connection_age_years} yr</span>
          ${c.whitelist_category ? html`<span style=${{ gridColumn: "1/3" }}><b>Registered exception</b>
            ${c.whitelist_category.replace("_", " ")} — never escalated past monitor</span>` : null}
        </div>
      </div>

      ${alert ? html`
        <div class="card">
          <div class="sec" style=${{ margin: "0 0 4px" }}>Officer decision · ${alert.alert_id}</div>
          <div class="note" style=${{ marginTop: 0 }}>An anomaly is not a confirmed offence. The system
            flags what needs looking at; the officer decides what it means, and the decision is fed back
            as a training label.</div>
          <div class="acts">
            <button class="confirm" disabled=${busy} onClick=${() => act("confirmed")}>Confirm anomaly</button>
            <button class="visit" disabled=${busy} onClick=${() => act("field_visit")}>Queue field visit</button>
            <button class="clear" disabled=${busy} onClick=${() => act("cleared")}>Clear</button>
          </div>
          ${fb && html`<div class="ok">Recorded as <b>${fb.status}</b>. Audit entry
            <code class="sm">${(fb.audit_hash || "").slice(0, 16)}</code>${fb.offline ? " (offline demo - not written to the ledger)" : ""}.
            This decision joins the training labels for the next run.</div>`}
          ${err && html`<div class="warn" style=${{ marginTop: 8 }}>${err}</div>`}
        </div>` : html`<div class="note">No open alert on this connection — it is in the normal band.</div>`}
    </div>`;
}

/* ==================================================================== APP */
function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState("alerts");
  const [bandFilter, setBandFilter] = useState(new Set());
  const [feeder, setFeeder] = useState("");
  const [hideWhitelist, setHideWhitelist] = useState(false);
  const [role, setRole] = useState("officer");
  const [feedback, setFeedback] = useState({});
  const [layerState, setLayerState] = useState({ sm: false, ndvi: false, rain: false, network: false, basemap: false });
  const [retraining, setRetraining] = useState(null);

  useEffect(() => { api.payload().then(setData).catch(e => setError(String(e.message || e))); }, []);

  const toggleBand = useCallback(k => setBandFilter(s => {
    const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n;
  }), []);

  const layers = useMemo(() => ({ ...layerState, set: (k, v) => setLayerState(s => ({ ...s, [k]: v })) }),
    [layerState]);
  const filters = useMemo(() => ({ bands: bandFilter, feeder, hideWhitelist }),
    [bandFilter, feeder, hideWhitelist]);

  if (error) return html`<div class="loading" style=${{ color: "#c62828", padding: 40, textAlign: "center" }}>${error}</div>`;
  if (!data) return html`<div class="loading">Loading the scored run…</div>`;

  const alerts = data.alerts.filter(a => {
    if (bandFilter.size && !bandFilter.has(a.band)) return false;
    if (feeder && a.feeder_id !== feeder) return false;
    if (hideWhitelist && a.whitelist) return false;
    return true;
  });

  const doRetrain = async () => {
    setRetraining("running");
    try { const r = await api.retrain("admin"); setRetraining(`re-scored with ${r.officer_labels_used} officer labels`); location.reload(); }
    catch (e) { setRetraining(String(e.message || e)); }
  };

  return html`
    <${React.Fragment}>
      <header class="top">
        <div class="brand">JAL-ANOMALY<small>AI-powered groundwater pumping anomaly detection</small></div>
        <span class="chip">SIH26015 · SyntaxError</span>
        <span class="chip">${data.meta.district.name}, ${data.meta.district.state} · ${data.meta.district.season}</span>
        <span class="chip">${data.meta.district.discom} pilot</span>
        <div class="spacer"></div>
        ${OFFLINE ? html`<span class="chip">offline demo build</span>` : null}
        <select value=${feeder} onChange=${e => setFeeder(e.target.value)}>
          <option value="">All feeders</option>
          ${data.kpis.feeders.map(f => html`<option key=${f.feeder_id} value=${f.feeder_id}>${f.feeder_id} — ${f.name}</option>`)}
        </select>
        <select value=${role} onChange=${e => setRole(e.target.value)} title="Role-based access">
          <option value="viewer">viewer</option><option value="officer">officer</option>
          <option value="admin">admin</option>
        </select>
        ${role === "admin" && html`<button class="btn ghost" onClick=${doRetrain}
          disabled=${retraining === "running"}>${retraining === "running" ? "Re-scoring…" : "Retrain on feedback"}</button>`}
      </header>

      <div class="shell">
        <div class="left">
          <div class="tabs">
            ${[["alerts", "Alerts"], ["feeders", "Network"], ["model", "Model"]].map(([k, l]) =>
              html`<button key=${k} class=${tab === k ? "on" : ""} onClick=${() => setTab(k)}>${l}</button>`)}
          </div>
          <div class="panel">
            ${tab === "alerts" && html`
              <${React.Fragment}>
                <${Kpis} k=${data.kpis} meta=${data.meta} />
                <${Legend} bands=${data.meta.bands} counts=${data.kpis.band_counts}
                           active=${bandFilter} toggle=${toggleBand} />
                <label style=${{ display: "flex", gap: 6, alignItems: "center", fontSize: 11.5, marginBottom: 6 }}>
                  <input type="checkbox" checked=${hideWhitelist} onChange=${e => setHideWhitelist(e.target.checked)} />
                  Hide registered exceptions (nursery, dairy, fish pond)
                </label>
                <div class="sec">Review queue · ${alerts.length} alerts, highest risk first</div>
                <${AlertList} alerts=${alerts.slice(0, 120)} bands=${data.meta.bands}
                              selected=${selected} onSelect=${setSelected} feedback=${feedback} />
              <//>`}
            ${tab === "feeders" && html`<${FeederPanel} k=${data.kpis} />`}
            ${tab === "model" && html`<${ModelPanel} meta=${data.meta} k=${data.kpis} />`}
          </div>
        </div>

        <${MapView} data=${data} filters=${filters} selected=${selected}
                    onSelect=${setSelected} layers=${layers} />

        <div class="right">
          <${Detail} data=${data} id=${selected} role=${role}
                     feedback=${feedback} setFeedback=${setFeedback} />
        </div>
      </div>
    <//>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
