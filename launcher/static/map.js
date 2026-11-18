"use strict";
// Common operating picture: the TAK picture the launcher sees (/api/cop, /api/cop/stream) on the offline
// map pack (isr-maps through /tiles/), with APP-6 symbols (milsymbol). Read-only: it never sends CoT.
// Loaded after leaflet.js, milsymbol.js, mgrs.js and common.js.

const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const COL = { f: css("--friend") || "#80e0ff", h: css("--hostile") || "#ff8080", u: css("--unknown") || "#ffff80",
  n: css("--neutral") || "#aaffaa" };
const LAYERS = ["friend", "contact", "sensor", "bearing"];
const LEAD_S = 60;                           // course leader: where it'll be in a minute
const LABEL_ZOOM = 17;                       // callsigns always shown from here (wide map); else on hover or selection
const AT_SENSOR_M = 25;                      // a contact this close to a sensor, with no range, is "at the OP"
const BEARING_MAX = 6;                       // bearing-only items per sensor: the newest few …
const BEARING_MAX_AGE = 180;                 // … for 3 min, fading, whatever their stale says
const CLEAR_AGE = 120;                       // "Clear old" hides what hasn't reported for 2 min (this browser only)
const CLUSTER_ZOOM = 16, CLUSTER_PX = 44;    // below zoom 16, symbols closer than this cluster
const items = new Map();                     // uid -> item from the server
const firstSeen = new Map();                 // uid -> server time we first saw it (list order)
const markerOf = new Map();                  // uid -> Leaflet layer with the popup
let map, base = null, baseName = "", meta = {}, fadeS = 120, offset = 0, viewSet = false, loaded = false;
let lastFollow = 0, renderTimer = null, poller = null, es = null;
let openUid = null, rendering = false;       // the open popup survives the rebuild every render
const prefs = loadPrefs();

function loadPrefs() {
  try { return Object.assign({ layers: {}, follow: false }, JSON.parse(localStorage.getItem("cop-prefs") || "{}")); }
  catch (e) { return { layers: {}, follow: false }; }
}
function savePrefs() { try { localStorage.setItem("cop-prefs", JSON.stringify(prefs)); } catch (e) { /* private mode */ } }
const serverNow = () => Date.now() / 1000 + offset;
const layerOn = (l) => prefs.layers[l] !== false;

// ── what things are ─────────────────────────────────────────────────────────
const TYPES = [
  ["a-?-A-M-F-Q", "UAV"], ["a-?-A-M-H", "Rotary-wing"], ["a-?-A-M-F", "Military aircraft"], ["a-?-A-C", "Civil aircraft"],
  ["a-?-A", "Air track"], ["a-?-G-U-C-I", "Person"], ["a-?-G-U-C", "Unit"], ["a-?-G-E-V", "Vehicle"],
  ["a-?-G-E-S", "Sensor"], ["a-?-G-I", "Installation"], ["a-?-G", "Ground contact"], ["a-?-S", "Surface vessel"],
  ["u-d-f", "Bearing line"], ["u-d-c-c", "Uncertainty circle"], ["b-a-o", "Emergency"], ["b-m-p", "Waypoint"],
];
const AFF_NAME = { f: "Friend", h: "Hostile", u: "Unknown", n: "Neutral" };
function typeLabel(t) {
  const g = t.replace(/^a-[a-z]-/, "a-?-");
  const hit = TYPES.find(([p]) => g === p || g.startsWith(p + "-"));
  return hit ? hit[1] : t;
}
function affOf(it) { return it.aff || (it.type.startsWith("b-") ? "f" : "u"); }
function colourOf(it) { return COL[affOf(it)] || COL.u; }
function tagOf(it) {
  return it.mode === "sim" ? '<span class="isr-tag sim">SIM</span>' : it.mode === "rec" ? '<span class="isr-tag rec">REC</span>' : "";
}
function grid(lat, lon, d) { return (window.MGRS && MGRS.toMGRS(lat, lon, d)) || "—"; }
function age(s) {
  s = Math.max(0, Math.round(s));
  return s < 90 ? s + " s" : s < 5400 ? Math.round(s / 60) + " min" : Math.round(s / 3600) + " h";
}
function dtg(t) {
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  const M = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][d.getUTCMonth()];
  return p(d.getUTCDate()) + p(d.getUTCHours()) + p(d.getUTCMinutes()) + "Z " + M + " " + String(d.getUTCFullYear()).slice(2);
}
// Fully there while live; after stale it fades over fade_s, then it's gone.
function alpha(it) {
  const past = serverNow() - it.stale;
  return past <= 0 ? 1 : past >= fadeS ? 0 : Math.max(0.12, 0.45 * (1 - past / fadeS));
}

// ── symbols ─────────────────────────────────────────────────────────────────
const symCache = new Map();
function symbol(sidc, colour, size) {
  const key = sidc + colour + size;
  if (!symCache.has(key)) {
    let out = null;
    try {
      const s = new ms.Symbol(sidc, { size: size, fillColor: colour, outlineColor: "#0e1012", outlineWidth: 2 });
      if (s.isValid()) {
        const a = s.getAnchor(), z = s.getSize();
        out = { svg: s.asSVG(), ax: a.x, ay: a.y, w: z.width, h: z.height };
      }
    } catch (e) { out = null; }
    symCache.set(key, out);
  }
  return symCache.get(key);
}
function dotSymbol(colour) {
  return { svg: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14"><circle cx="7" cy="7" r="5" fill="' + colour
    + '" stroke="#0e1012" stroke-width="2"/></svg>', ax: 7, ay: 7, w: 14, h: 14 };
}
function icon(it, dx, dy, extra, size) {
  const sym = (it.sidc && symbol(it.sidc, colourOf(it), size || 24)) || dotSymbol(colourOf(it));
  const html = '<div class="s" style="left:' + dx + 'px;top:' + dy + 'px">' + sym.svg + "</div>";
  return { sym, icon: L.divIcon({ className: "cop-sym" + (extra || ""), html: html, iconSize: [sym.w, sym.h], iconAnchor: [sym.ax, sym.ay] }) };
}
// The callsign as a tooltip beside the symbol: on hover, on selection, or always from LABEL_ZOOM.
function labelsOn() { return map.getZoom() >= LABEL_ZOOM && map.getSize().x >= 600; }
function label(m, html, sym, dx, dy, uid) {
  if (!html) return;
  const left = dx < -4;      // fanned out to the left: the label goes on the outer side
  m.bindTooltip(html, { className: "cop-tip", direction: left ? "left" : "right", opacity: 1,
    permanent: labelsOn() || uid === openUid,
    offset: left ? [dx - sym.ax - 2, dy + sym.h / 2 - sym.ay] : [dx + sym.w - sym.ax + 2, dy + sym.h / 2 - sym.ay] });
}

// ── popups ──────────────────────────────────────────────────────────────────
function popup(it, others) {
  const rows = [["Grid", grid(it.lat, it.lon)], ["Type", typeLabel(it.type) + (AFF_NAME[it.aff] ? " · " + AFF_NAME[it.aff] : "")],
    ["Seen", dtg(it.time) + " (" + age(serverNow() - it.time) + " ago)"]];
  if (it.course != null && it.speed != null) rows.push(["Course", String(Math.round(it.course)).padStart(3, "0") + "° · " + it.speed.toFixed(1) + " m/s"]);
  if (it.hae != null) rows.push(["Height", Math.round(it.hae) + " m HAE"]);
  if (it.ce != null) rows.push(["Accuracy", "±" + Math.round(it.ce) + " m"]);
  if (alpha(it) < 1) rows.push(["Status", "Stale since " + age(serverNow() - it.stale)]);
  return '<div class="cop-pop"><b>' + esc(it.callsign || it.uid) + " " + tagOf(it) + "</b>"
    + '<div class="kv">' + rows.map(([k, v]) => "<span>" + esc(k) + ' <b class="isr-mono">' + esc(v) + "</b></span>").join("") + "</div>"
    + (it.remarks ? '<div class="rem">' + esc(it.remarks) + "</div>" : "")
    + (others ? '<div class="rem">' + others + "</div>" : "") + "</div>";
}

// ── drawing ─────────────────────────────────────────────────────────────────
const groups = {};
function dest(lat, lon, brg, m) {
  const R = 6371008.8, d = m / R, b = brg * Math.PI / 180, p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 * 180 / Math.PI, ((l2 * 180 / Math.PI + 540) % 360) - 180];
}
function siteItem() {
  const s = meta.site;
  return s ? { uid: "__site", type: "a-f-G-E-S", sidc: "SFGPES---------", aff: "f", lat: s.lat, lon: s.lon, callsign: "Sensor site",
    layer: "sensor", time: serverNow(), stale: serverNow() + 3600, mode: "live", remarks: "The kit's sensor position (isr-set-site).",
    links: [] } : null;
}
function render() {
  renderTimer = null;
  rendering = true;
  Object.values(groups).forEach((g) => g.clearLayers());
  markerOf.clear();
  const now = serverNow();
  for (const [u, it] of items) if (now - it.stale >= fadeS) items.delete(u);
  const all = [...items.values()];
  const site = siteItem();
  if (site) all.push(site);
  const sensors = all.filter((x) => x.layer === "sensor");
  const z = map.getZoom(), size = symSize(z);
  capped.clear();
  // 1. Bearing-only items (lines, and detections with no range): per sensor, the newest few, fading by age.
  const bySensor = new Map();
  const points = [];
  for (const it of all) {
    if (!shown(it)) continue;
    const s = bearingSensor(it, sensors);
    if (s) {
      const k = s.lat.toFixed(4) + "," + s.lon.toFixed(4);
      if (!bySensor.has(k)) bySensor.set(k, { at: s, lines: [], dets: [] });
      bySensor.get(k)[it.line ? "lines" : "dets"].push(it);
      continue;
    }
    if (it.line) { drawLine(it, 1); continue; }               // other drawn lines (not from a sensor)
    if (it.circle) {
      if (layerOn("contact")) {
        L.circle([it.lat, it.lon], { radius: it.circle, color: colourOf(it), weight: 1, opacity: 0.8 * alpha(it), dashArray: "4 4",
          fillOpacity: 0.05 * alpha(it), interactive: false }).addTo(groups.contact);
      }
      if (it.type.startsWith("u-d-")) continue;
    }
    points.push(it);
  }
  for (const [k, g] of bySensor) {
    const newest = (l) => l.sort((p, q) => q.time - p.time).slice(0, BEARING_MAX);   // sorts in place
    if (layerOn("bearing")) newest(g.lines).forEach((it) => drawLine(it, bearingFade(it)));
    const dets = newest(g.dets);
    g.dets.slice(BEARING_MAX).forEach((it) => capped.add(it.uid));
    if (dets.length && layerOn("contact")) drawBadge(k, g.at, dets);
  }
  // 2. Positioned contacts: one symbol each, sized to the zoom; overlapping ones cluster when zoomed out.
  const singles = [], sensorAt = new Map();
  for (const it of points) {
    if (!layerOn(it.layer === "other" ? "contact" : it.layer)) continue;
    if (it.layer === "sensor") {                      // the stations registered at one site: one symbol
      const k = it.lat.toFixed(4) + "," + it.lon.toFixed(4);
      if (!sensorAt.has(k)) sensorAt.set(k, []);
      sensorAt.get(k).push(it);
      continue;
    }
    if (it.course != null && it.speed > 0.2) {
      const m = Math.max(25, Math.min(2000, it.speed * LEAD_S));
      L.polyline([[it.lat, it.lon], dest(it.lat, it.lon, it.course, m)], { color: colourOf(it), weight: 1.5, opacity: alpha(it),
        interactive: false }).addTo(groups[it.layer] || groups.contact);
    }
    singles.push(it);
  }
  for (const list of sensorAt.values()) drawSensor(list, size);
  for (const c of clusters(singles, z)) {
    if (c.length > 1) drawCluster(c);
    else drawSymbol(c[0], size);
  }
  rendering = false;
  const again = openUid && markerOf.get(openUid);
  if (again) again.openPopup();
  renderList();
}
// Shown at all: not gone, not cleared in this browser, a bearing-only item not past its 3 min.
function shown(it) {
  if (alpha(it) <= 0) return false;
  if (it.uid === "__site") return true;
  if (prefs.hideBefore && it.time < prefs.hideBefore) return false;
  if (isBearingOnly(it) && serverNow() - it.time > BEARING_MAX_AGE) return false;
  return true;
}
function isBearingOnly(it) { return !!(it.line && it.layer === "bearing") || !!it.norange; }
function bearingFade(it) { return Math.max(0.25, 1 - 0.75 * (serverNow() - it.time) / BEARING_MAX_AGE) * alpha(it); }
// The sensor a bearing-only item belongs to: a bearing line starts at it; a no-range detection sits on it.
function bearingSensor(it, sensors) {
  if (it.line && it.layer === "bearing") {
    const p = it.line[0];
    return sensors.find((s) => map.distance([s.lat, s.lon], p) <= AT_SENSOR_M) || { lat: p[0], lon: p[1] };
  }
  return isNoRange(it, sensors);
}
// A contact with no range of its own, drawn at the sensor that saw it: the server's flag (from the tools'
// remarks), or a contact sitting on a sensor with no course and no useful accuracy. Returns that sensor.
function isNoRange(it, sensors) {
  if (it.layer !== "contact" || it.line || it.course != null) return null;
  const near = sensors.find((s) => map.distance([s.lat, s.lon], [it.lat, it.lon]) <= AT_SENSOR_M);
  if (it.norange) return near || { lat: it.lat, lon: it.lon };
  return near && (it.ce == null || it.ce >= 300) ? near : null;
}
function symSize(z) { return Math.max(14, Math.min(26, Math.round(10 + (z - 10) * 2.4))); }   // z10 14 px … z17 26 px
function drawLine(it, fade) {
  const l = L.polyline(it.line, { color: it.layer === "bearing" ? COL.u : colourOf(it), weight: 1.5, opacity: 0.9 * fade,
    dashArray: "6 5" });
  l.bindPopup(popup(it), { autoPan: false }).addTo(groups.bearing);
  l.bindTooltip(esc(it.callsign || it.uid) + tagOf(it), { className: "cop-tip", sticky: true, opacity: 1 });
  l.options.uid = it.uid;
  markerOf.set(it.uid, l);
}
function drawSymbol(it, size) {
  const cls = (alpha(it) < 1 ? " fading" : "") + (it.emergency ? " emergency" : "");
  const ic = icon(it, 0, 0, cls, size);
  const m = L.marker([it.lat, it.lon], { icon: ic.icon, opacity: Math.min(1, alpha(it) * 2.2), zIndexOffset: it.layer === "contact" ? 500 : 0 });
  m.options.uid = it.uid;
  label(m, esc((it.callsign || it.uid).slice(0, 32)) + tagOf(it), ic.sym, 0, 0, it.uid);
  m.bindPopup(popup(it), { autoPan: false }).addTo(groups[it.layer] || groups.contact);
  markerOf.set(it.uid, m);
}
function drawSensor(list, size) {
  const first = list.find((x) => x.uid === "__site") || list[0];
  const names = list.filter((x) => x.uid !== "__site").map((x) => esc(x.callsign || x.uid));
  const ic = icon(first, 0, 0, "", size);
  const m = L.marker([first.lat, first.lon], { icon: ic.icon, zIndexOffset: -100 });
  m.options.uid = first.uid;
  label(m, esc(first.uid === "__site" ? "Sensor site" : first.callsign || "Sensors") + (list.length > 1 ? " ×" + list.length : ""),
    ic.sym, 0, 0, first.uid);
  m.bindPopup(popup(first, names.length ? "Registered here: " + names.join(", ") : ""), { autoPan: false }).addTo(groups.sensor);
  list.forEach((x) => markerOf.set(x.uid, m));
}
// Grid clusters below CLUSTER_ZOOM: contacts whose symbols would overlap (same CLUSTER_PX cell) become one
// count at their centre, in the colour of the commonest affiliation (hostile wins a tie); click to zoom in.
// Only positioned contacts cluster: a bearing-only detection is never given a position.
function clusters(list, z) {
  if (z >= CLUSTER_ZOOM) return list.map((it) => [it]);
  const cells = new Map();
  for (const it of list) {
    const p = map.project([it.lat, it.lon], z);
    const k = Math.floor(p.x / CLUSTER_PX) + ":" + Math.floor(p.y / CLUSTER_PX);
    if (!cells.has(k)) cells.set(k, []);
    cells.get(k).push(it);
  }
  return [...cells.values()];
}
function dominantAff(list) {
  const n = {};
  list.forEach((it) => { const a = affOf(it); n[a] = (n[a] || 0) + 1; });
  return ["h", "u", "n", "f"].reduce((best, a) => ((n[a] || 0) > (n[best] || 0) ? a : best), "h");
}
function drawCluster(list) {
  const aff = dominantAff(list);
  const lat = list.reduce((s, it) => s + it.lat, 0) / list.length, lon = list.reduce((s, it) => s + it.lon, 0) / list.length;
  const html = '<div class="cop-cluster" style="border-color:' + (COL[aff] || COL.u) + ";color:" + (COL[aff] || COL.u) + '">'
    + list.length + "</div>";
  const m = L.marker([lat, lon], { icon: L.divIcon({ className: "cop-cluster-icon", html: html, iconSize: [30, 30], iconAnchor: [15, 15] }),
    zIndexOffset: 700, opacity: Math.max(...list.map((it) => Math.min(1, alpha(it) * 2.2))) });
  m.bindTooltip(list.slice(0, 8).map((it) => esc(it.callsign || it.uid)).join("<br>") + (list.length > 8 ? "<br>…" : ""),
    { className: "cop-tip", direction: "right", offset: [16, 0], opacity: 1 });
  m.on("click", () => map.fitBounds(list.map((it) => [it.lat, it.lon]), { padding: [60, 60], maxZoom: CLUSTER_ZOOM + 1 }));
  m.addTo(groups[list.every((it) => it.layer === "friend") ? "friend" : "contact"]);
  list.forEach((it) => markerOf.set(it.uid, m));
}
function whatOf(it) {            // "person", "vehicle", "air track"; acronyms stay ("UAV")
  const w = it.what || typeLabel(it.type);
  return w === w.toUpperCase() && w.length <= 4 ? w : w.toLowerCase();
}
function opName(list, at) {      // "OP1" from op-sentry's callsigns, else the sensor's own name
  for (const it of list) { const m = /\b(OP\d{1,3})\b/.exec(it.callsign || ""); if (m) return m[1]; }
  return at.uid === "__site" || !at.callsign ? "OP" : at.callsign.replace(/^SIM /, "").split(" ")[0];
}
// One compact badge on the sensor: "OP1 · 3 person · 1 vehicle". Its popup lists the detections with bearings.
function badgeText(list, at) {
  const rows = new Map();
  for (const it of list) {
    const k = whatOf(it);
    rows.set(k, (rows.get(k) || 0) + (it.count || 1));
  }
  return [opName(list, at), ...[...rows].sort((p, q) => q[1] - p[1]).map(([w, n]) => n + " " + w)].join(" · ");
}
function drawBadge(key, at, list) {
  const aff = dominantAff(list), sim = list.some((it) => it.mode === "sim"), rec = list.some((it) => it.mode === "rec");
  const html = '<div class="cop-badge-box"><span class="aff ' + ({ f: "friend", h: "hostile", u: "unknown", n: "neutral" }[aff] || "unknown")
    + '"></span>' + esc(badgeText(list, at)) + (sim ? ' <span class="isr-tag sim">SIM</span>' : rec ? ' <span class="isr-tag rec">REC</span>' : "")
    + "</div>";
  const m = L.marker([at.lat, at.lon], { icon: L.divIcon({ className: "cop-badge", html: html, iconSize: null, iconAnchor: [-14, 26] }),
    zIndexOffset: 600 });
  m.options.uid = "__badge:" + key;
  const lines = list.map((it) =>
    "<div><b>" + esc(it.callsign || it.uid) + "</b> " + tagOf(it) + "<br>" + esc(typeLabel(it.type))
    + (it.bearing != null ? " · BRG " + String(it.bearing).padStart(3, "0") : " · no bearing") + " · " + age(serverNow() - it.time) + " ago</div>").join("");
  m.bindPopup('<div class="cop-pop"><b>' + esc(opName(list, at)) + ": bearings only</b><div class=\"rem\">Each contact is somewhere along its "
    + "bearing line; the sensor doesn't measure range. Newest " + BEARING_MAX + ", for " + Math.round(BEARING_MAX_AGE / 60) + " min.</div>"
    + '<div class="cop-badge-list">' + lines + "</div></div>", { autoPan: false, maxWidth: 320 });
  m.addTo(groups.contact);
  markerOf.set(m.options.uid, m);             // so an open badge popup survives the next render
  list.forEach((it) => markerOf.set(it.uid, m));
}
function scheduleRender() { if (!renderTimer) renderTimer = setTimeout(render, 250); }

// ── contacts list ───────────────────────────────────────────────────────────
const recent = new Set();
const capped = new Set();                    // bearing-only detections past the newest BEARING_MAX at a sensor
// Contacts newest report first, with the age of that report. Hidden: stale, past 3 min (bearing-only),
// over the per-sensor cap, or cleared in this browser.
function renderList() {
  const now = serverNow();
  const all = [...items.values()].filter((it) => !it.line && !(it.circle && it.type.startsWith("u-d-")) && it.layer !== "sensor");
  const rows = all.filter((it) => shown(it) && alpha(it) === 1 && !capped.has(it.uid)).sort((p, q) => q.time - p.time);
  const hidden = all.length - rows.length;
  $("count").textContent = String(rows.length).padStart(2, "0") + (hidden ? " (" + hidden + " stale hidden)" : "");
  $("clear").textContent = prefs.hideBefore ? "Show all" : "Clear old";
  $("list").innerHTML = rows.slice(0, 200).map((it) => {
    const aff = { f: "friend", h: "hostile", u: "unknown", n: "neutral" }[affOf(it)] || "unknown";
    return '<li data-uid="' + esc(it.uid) + '" class="' + (alpha(it) < 1 ? "fading " : "") + (recent.has(it.uid) ? "new" : "") + '">'
      + '<span class="aff ' + aff + '"></span><span class="who"><span class="cs">' + esc(it.callsign || it.uid) + "</span>"
      + '<span class="what">' + esc(typeLabel(it.type)) + " · " + (it.norange ? "at OP" + (it.bearing != null
        ? ", BRG " + String(it.bearing).padStart(3, "0") : ", no range") : esc(grid(it.lat, it.lon, 4))) + "</span></span>"
      + tagOf(it) + '<span class="t">' + age(now - it.time) + "</span></li>";
  }).join("") || '<li class="isr-muted">No contacts yet</li>';
}
function centreOn(uid) {
  const it = items.get(uid);
  if (!it) return;
  map.setView([it.lat, it.lon], Math.max(map.getZoom(), 16));
  const m = markerOf.get(uid);
  if (m) m.openPopup();
}

// ── data ────────────────────────────────────────────────────────────────────
function upsert(list, initial) {
  for (const it of list) {
    const isNew = !items.has(it.uid) && !firstSeen.has(it.uid);
    if (!firstSeen.has(it.uid)) firstSeen.set(it.uid, initial ? it.time : serverNow());
    items.set(it.uid, it);
    if (isNew && !initial && loaded) {
      recent.add(it.uid); setTimeout(() => recent.delete(it.uid), 3000);
      if (prefs.follow && it.layer === "contact" && (it.aff === "h" || it.aff === "u") && !it.line && Date.now() - lastFollow > 4000) {
        lastFollow = Date.now();
        map.flyTo([it.lat, it.lon], Math.max(map.getZoom(), 16), { duration: 1.2 });
      }
    }
  }
}
function applyMeta(m) {
  if (m.tak) meta.tak = m.tak;
  if ("site" in m) meta.site = m.site;
  if (m.maps) meta.maps = m.maps;
  if (m.fade_s) fadeS = m.fade_s;
  if (m.now) offset = m.now - Date.now() / 1000;
  if (!viewSet) initialView();               // before the base layer, so it never tiles the whole world
  setBase();
  setStatus();
}
function applySnapshot(s) {
  const keep = new Set(s.items.map((it) => it.uid));
  for (const u of [...items.keys()]) if (!keep.has(u)) items.delete(u);
  upsert(s.items, !loaded);
  applyMeta(s);
  loaded = true;
  scheduleRender();
}
function applyDelta(d) {
  if (d.now) offset = d.now - Date.now() / 1000;
  upsert(d.upsert || [], false);
  (d.remove || []).forEach((u) => items.delete(u));
  scheduleRender();
  setStatus();
}
async function fetchSnapshot() {
  const r = await api("/api/cop");
  if (r.code === 401) { const p = prompt("Launcher PIN"); if (p != null) setPin(p); return false; }
  if (r.code !== 200) { note("Launcher not answering (" + r.code + ")"); return false; }
  applySnapshot(r.body);
  return true;
}
function startStream() {
  // ?poll=1 forces polling (a proxy that buffers SSE; headless screenshots)
  if (!window.EventSource || new URLSearchParams(location.search).get("poll") === "1") return startPolling();
  es = new EventSource("/api/cop/stream");
  es.addEventListener("snapshot", (e) => applySnapshot(JSON.parse(e.data)));
  es.addEventListener("delta", (e) => applyDelta(JSON.parse(e.data)));
  es.addEventListener("status", (e) => { const s = JSON.parse(e.data); applyMeta({ tak: s.tak, now: s.now }); });
  es.onerror = () => { if (es.readyState === 2) { es.close(); es = null; startPolling(); } };
}
function startPolling() {                    // no SSE (or the stream cap reached): the snapshot every second
  if (poller) return;
  poller = setInterval(fetchSnapshot, 1000);
}

// ── base map ────────────────────────────────────────────────────────────────
function pickPack() {
  const packs = (meta.maps && meta.maps.packs) || [];
  const want = new URLSearchParams(location.search).get("pack");
  return packs.find((p) => p.name === want) || packs[0] || null;
}
function setBase() {
  const p = pickPack();
  if (!p) {
    if (base) { map.removeLayer(base); base = null; baseName = ""; }
    note(meta.maps && meta.maps.reachable ? "No offline map pack installed: contacts on a blank background."
      : "Offline maps (isr-maps) not reachable: contacts on a blank background.");
    return;
  }
  if (p.name === baseName) return;
  if (base) map.removeLayer(base);
  const fmt = p.format === "jpeg" ? "jpeg" : p.format === "jpg" ? "jpg" : "png";
  // Only the pack's zooms and area: below minzoom nothing (no up-scaling a z12 pack across the world),
  // above maxzoom the deepest tiles enlarged.
  const b = p.bounds && p.bounds.length === 4 ? L.latLngBounds([[p.bounds[1], p.bounds[0]], [p.bounds[3], p.bounds[2]]]) : null;
  base = L.tileLayer("/tiles/" + encodeURIComponent(p.name) + "/{z}/{x}/{y}." + fmt, Object.assign({
    minZoom: p.minzoom || 0, maxNativeZoom: p.maxzoom || 18, maxZoom: Math.min(20, (p.maxzoom || 18) + 2),
    attribution: esc(p.attribution || p.title || p.name), keepBuffer: 2,
  }, b ? { bounds: b.pad(0.05) } : {})).addTo(map);
  baseName = p.name;
  $("sub").textContent = p.title || p.name;
  note("");
}
function initialView() {
  const p = pickPack(), s = meta.site;
  const maxZ = p ? Math.min(p.maxzoom || 17, 16) : 16;
  if (s) {
    // the OP area: the site and whatever the sensors see within 5 km of it
    const near = [...items.values()].filter((it) => alpha(it) > 0 && map.distance([s.lat, s.lon], [it.lat, it.lon]) < 5000)
      .map((it) => [it.lat, it.lon]);
    if (near.length) map.fitBounds([[s.lat, s.lon], ...near], { padding: [50, 50], maxZoom: maxZ });
    else map.setView([s.lat, s.lon], Math.max(p && p.minzoom || 0, Math.min(15, maxZ)));
  } else if (p && p.bounds && p.bounds.length === 4) map.fitBounds([[p.bounds[1], p.bounds[0]], [p.bounds[3], p.bounds[2]]]);
  else if (items.size) fitAll();
  else return;
  viewSet = true;
}
function fitAll() {
  const pts = [...items.values()].filter((it) => alpha(it) > 0 && layerOn(it.layer)).map((it) => [it.lat, it.lon]);
  if (meta.site) pts.push([meta.site.lat, meta.site.lon]);
  if (pts.length === 1) map.setView(pts[0], Math.max(map.getZoom(), 16));
  else if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
}

// ── header pill, notes ──────────────────────────────────────────────────────
function note(t) { const n = $("note"); n.hidden = !t; n.textContent = t || ""; }
function setStatus() {
  const t = meta.tak || {}, p = pickPack();
  const live = [...items.values()].filter((it) => alpha(it) === 1 && !it.line).length;
  ISR.setStatus([
    { name: "TAK feed", state: t.enabled === false ? "idle" : t.connected && t.registered_at ? "ok" : "bad",
      text: t.enabled === false ? "off (LAUNCHER_COP=0)" : t.connected ? (t.registered_at ? "receiving" : "registering") : "not connected" },
    { name: "Map", state: p ? "ok" : "warn", text: p ? (p.title || p.name) : "no offline map", optional: true },
    { name: "Contacts", state: "ok", text: String(live) + " live", optional: true },
  ]);
  if (t.enabled !== false && !t.connected && loaded) note("Not connected to the TAK server: the picture isn't updating.");
  else if ($("note").textContent.startsWith("Not connected")) note("");
}

// ── start ───────────────────────────────────────────────────────────────────
function setup() {
  map = L.map("map", { zoomControl: true, attributionControl: true, worldCopyJump: true, center: [0, 0], zoom: 2, minZoom: 2,
    fadeAnimation: false });   // no fade: the open popup is rebuilt with the picture every render
  map.attributionControl.setPrefix("Leaflet");
  L.control.scale({ metric: true, imperial: false, maxWidth: 140 }).addTo(map);
  ["friend", "contact", "sensor", "bearing"].forEach((l) => { groups[l] = L.layerGroup(); if (layerOn(l)) groups[l].addTo(map); });
  let hovering = false;
  const showCentre = () => { if (!hovering) { const c = map.getCenter(); $("grid").textContent = grid(c.lat, c.lng); } };
  map.on("mousemove", (e) => { hovering = true; $("grid").textContent = grid(e.latlng.lat, e.latlng.lng); });
  map.on("mouseout", () => { hovering = false; showCentre(); });
  map.on("moveend", showCentre);
  map.on("zoomend resize", scheduleRender);
  map.on("popupopen", (e) => { if (!rendering) { openUid = e.popup._source && e.popup._source.options.uid; scheduleRender(); } });
  map.on("popupclose", () => { if (!rendering) { openUid = null; scheduleRender(); } });
  document.querySelectorAll("#layers input").forEach((cb) => {
    const l = cb.dataset.layer;
    cb.checked = layerOn(l);
    cb.addEventListener("change", () => {
      prefs.layers[l] = cb.checked; savePrefs();
      if (cb.checked) groups[l].addTo(map); else map.removeLayer(groups[l]);
      scheduleRender();
    });
  });
  const f = $("follow");
  f.checked = !!prefs.follow;
  f.addEventListener("change", () => { prefs.follow = f.checked; savePrefs(); });
  $("fit").addEventListener("click", fitAll);
  // "Clear old": hide, in this browser only, what hasn't reported for CLEAR_AGE (OTS is never touched).
  // Anything that reports again shows again. "Show all" undoes it.
  $("clear").addEventListener("click", () => {
    prefs.hideBefore = prefs.hideBefore ? 0 : serverNow() - CLEAR_AGE;
    savePrefs();
    scheduleRender();
  });
  $("list").addEventListener("click", (e) => { const li = e.target.closest("li[data-uid]"); if (li) centreOn(li.dataset.uid); });
  setInterval(scheduleRender, 2000);           // ages and fading
  setInterval(() => { if (es) fetchSnapshot(); }, 30000);   // resync and refresh packs/site
  fetchSnapshot().then(startStream);
}
document.addEventListener("DOMContentLoaded", setup);
