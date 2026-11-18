"use strict";
// Shared by the mission board (board.js) and the engineer view (app.js). Loaded after /ui/isr-ui.js.
let status = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function pin() { try { return sessionStorage.getItem("pin") || ""; } catch (e) { return ""; } }
function setPin(p) { try { sessionStorage.setItem("pin", p); } catch (e) {} }
function askPin(why) {
  if (status && status.pin_required && !pin()) { const p = prompt(why || "PIN"); if (p == null) return false; setPin(p); }
  return true;
}

// One line under the header. "err" = the operator must act (.isr-banner.bad); "info" = feedback.
function banner(msg, kind) {
  const b = $("banner");
  if (!b) return;
  if (!msg) { b.className = ""; b.textContent = ""; b.hidden = true; return; }
  b.hidden = false;
  b.className = kind === "err" ? "isr-banner bad" : kind === "warn" ? "isr-banner" : "isr-note";
  b.textContent = msg;
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Requested-With": "isr-launcher", "X-PIN": pin() }, opts.headers || {});
  const r = await fetch(path, opts);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  return { code: r.status, body };
}
const postJSON = (path, obj) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(obj || {}) });

// Links for the person looking at this page: a URL that names the hub (192.168.4.1, isr-pi.local)
// is rewritten to the host this page was opened with, so it works from a phone on the hotspot
// (192.168.4.1) and from a Mac on the cable (isr-pi.local) alike. Content that leaves the page
// (the ATAK XML's tile URL, CoT) keeps the hub IP.
function viewerUrl(url) {
  if (!url) return url;
  try {
    const u = new URL(url, location.href);
    const hub = (status && status.hub_hosts) || [];
    if (hub.includes(u.hostname.toLowerCase())) { u.protocol = location.protocol; u.hostname = location.hostname; }
    return u.toString();
  } catch (e) { return url; }
}
function hubLink(port) { return location.protocol + "//" + location.hostname + (port && port !== 80 ? ":" + port : "") + "/"; }

// Status fetch, remembering the browser time halfway through it for the clock skew.
let fetchMid = null;
async function fetchStatus() {
  const t0 = Date.now();
  const r = await api("/api/status");
  fetchMid = (t0 + Date.now()) / 2;
  if (r.code === 401) { const p = prompt("Launcher PIN"); if (p != null) setPin(p); return null; }
  return r.code === 200 ? r.body : null;
}
function clockSkew(c) { return c && c.now != null && fetchMid != null ? fetchMid / 1000 - c.now : null; }  // + = Pi behind
function ago(now, ts) {
  const s = Math.max(0, now - ts);
  return s < 90 ? Math.round(s) + " s ago" : s < 5400 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago";
}

// The tools the board knows, by group: [unit, title, purpose].
const BOARD = [
  ["Detect", [["drone-detector", "Drone detector", "Wi-Fi drone signals"],
              ["acoustic-detector", "Acoustic", "Hears drone motors"],
              ["rf-spectrum", "RF spectrum", "Radio emissions"]]],
  ["Watch", [["fpv-vision", "FPV vision", "Drone video detections"],
             ["op-sentry", "OP sentry", "360° camera watch"]]],
  ["Analyse", [["change-detect", "Change detect", "Before and after"],
               ["photoAnalysis", "Geo-locate", "Where was it taken"]]],
  ["Share", [["opentakserver", "TAK server", "Shares to ATAK"],
             ["mesh-deadrop", "Mesh dead-drop", "Messages over mesh"],
             ["tac-advisor", "AI advisor", "SITREP and advice"]]],
];
const ROLE = { pi: "Pi", hq: "HQ Mac", op: "OP Mac", droneops: "Drone-ops Mac" };

function unitState(s) {
  if (!s || !s.installed) return "idle";
  return s.active === "active" ? "ok" : s.active === "failed" ? "bad" : /activating|deactivating/.test(s.active) ? "warn" : "idle";
}

// What the board needs about one tool, or null when the active profile doesn't run it.
function toolInfo(st, unit, name, purpose) {
  const svc = (st.services || []).find((s) => s.unit === unit);
  const rem = (st.remote || []).find((r) => r.tool === unit && r.expected_station);
  let e, where, running, state, href = null, why = "";
  if (rem) {
    e = rem.heartbeat;
    running = !!e && (e.state === "live" || e.state === "stale");
    // Where it runs now: the live heartbeat's station (role, else host name). The profile's plan
    // only when there's no live heartbeat (a tool planned for the OP Mac may be running on HQ today).
    where = running ? (ROLE[e.station] || e.station_label || "Mac") : (rem.expected_label || "Mac");
    state = running ? hbState(e) : "idle";
    why = running ? "" : "no heartbeat";
    // Open stays clickable: the Pi's relay (HQ tunnel, or a hotspot Mac), the Mac's hotspot address,
    // the last heartbeat's URL, else the tool's port on the hub.
    if (rem.tunnel) href = hubLink(rem.tunnel.port);
    else if (rem.direct && rem.direct.url) href = rem.direct.url;
    else if (e && e.url) href = viewerUrl(e.url);
    else if (rem.port) href = hubLink(rem.port);
  } else if (svc && svc.in_profile) {
    e = svc.heartbeat;
    running = svc.active === "active" || (!!e && e.state === "live");
    where = (e && ROLE[e.station]) || "Pi";
    state = running ? (e && e.state === "live" ? hbState(e) : "ok") : svc.active === "failed" ? "bad" : "idle";
    why = running ? "" : svc.active === "failed" ? "failed" : svc.installed ? "not running" : "not installed";
    if (svc.port) href = hubLink(svc.port);
  } else {
    return null;
  }
  return { unit, name, purpose, e, where, running, state, href, why };
}
function boardTools(st) {
  return BOARD.map(([group, tools]) => [group, tools.map((t) => toolInfo(st, t[0], t[1], t[2])).filter(Boolean)]);
}

// Hub health as one list for the header pill (ISR.setStatus). Only a hub essential (MQTT, the TAK
// server, contact with the launcher) can make it Offline; anything else is Degraded at most.
const ESSENTIAL = new Set(["MQTT", "TAK server", "Launcher"]);
function rollup(st) {
  const out = [];
  const add = (name, state, text, optional) => out.push({ name, state: state === "bad" && !ESSENTIAL.has(name) ? "warn" : state,
    text, optional: !!optional });
  add("MQTT", st.mqtt && st.mqtt.connected ? "ok" : "bad", st.mqtt && st.mqtt.connected ? "connected" : "down");
  const ots = (st.services || []).find((s) => s.unit === "opentakserver");
  const n = st.tak && st.tak.clients;
  if (ots && ots.installed) {
    add("TAK server", ots.active === "active" ? "ok" : ots.active === "activating" ? "warn" : "bad",
      ots.active === "active" ? (n != null ? n + " connected" : "running") : ots.active);
  } else {
    add("TAK server", "idle", "not installed here", true);
  }
  const tier = st.ai_tier || "";
  add("AI", tier === "Mac VLM" ? "ok" : tier === "Gemini" ? "warn" : "warn", tier === "offline/template" ? "templates only" : tier, true);
  const ol = (o, name) => !o || !o.configured ? add(name, "idle", "not set", true)
    : add(name, !o.reachable ? "bad" : o.model_present === false ? "warn" : "ok",
      !o.reachable ? "unreachable" : o.model_present === false ? "model missing" : "up", true);
  ol(st.llm, "LLM"); ol(st.vlm, "VLM");
  const c = st.clock || {}, sk = clockSkew(c);
  const big = sk != null && Math.abs(sk) > (c.browser_min_skew || 5);
  const ls = c.last_set;
  add("Clock", big ? "bad" : c.trusted ? "ok" : "warn",
    big ? "off by " + Math.abs(sk).toFixed(0) + " s" : c.ntp_synced ? "NTP"
      : c.trusted && ls ? (ls.action === "verified" ? "verified " : "set ") + ago(c.now, ls.ts) : "not synced");
  const h = st.health || {}, pw = h.power || {}, t = h.throttled || {};
  const uv = (t.flags || []).includes("under-voltage now");
  add("Power", uv || pw.level === "bad" ? "bad" : pw.level === "warn" ? "warn" : pw.level === "ok" ? "ok" : "idle",
    uv ? "under-voltage" : pw.level === "ok" ? "5 A" : pw.level === "bad" ? "USB limited" : pw.level === "warn" ? "override" : "unknown",
    pw.level === "unknown" && !uv);
  const free = h.disk && h.disk.free_gb;
  if (free != null) add("Disk", free < 2 ? "bad" : free < 5 ? "warn" : "ok", free + " GB free");
  const temp = h.cpu_temp_c;
  if (temp != null) add("Temperature", temp >= 80 ? "bad" : temp >= 70 ? "warn" : "ok", temp + " °C");
  if (st.switching) add("Profile", "warn", "switching");
  // Tools the profile runs that aren't running, or are unhappy (never worse than Degraded).
  boardTools(st).forEach(([, tools]) => tools.forEach((t) => {
    if (t.unit === "opentakserver") return;                       // already a hub check above
    if (!t.running) add(t.name, "warn", t.why);
    else if (t.state === "warn" || t.state === "bad") add(t.name, "warn", (t.e && t.e.state === "stale") ? "stale" : (t.e && t.e.status) || "degraded");
  }));
  return out;
}

// Things the operator must act on now: shown as a banner, not just in the pill.
function mustAct(st) {
  const c = st.clock || {}, sk = clockSkew(c);
  const t = (st.health || {}).throttled || {};
  if ((t.flags || []).includes("under-voltage now")) return { text: "Under-voltage: plug the Pi into the 27 W PSU.", action: null };
  if (!c.trusted && sk != null && Math.abs(sk) > (c.browser_min_skew || 5)) {
    return { text: "Pi clock is " + Math.abs(sk).toFixed(0) + " s " + (sk > 0 ? "behind" : "ahead") + ": CoT times on the phones are wrong.",
      action: "setclock" };
  }
  return null;
}

async function setClock() {
  if (!confirm("Set the Pi's clock to this device's time (" + new Date().toLocaleTimeString() + ")?")) return;
  if (!askPin()) return;
  const r = await postJSON("/api/clock", { epoch: Date.now() / 1000 });
  if (r.code === 401) { setPin(""); banner("Wrong PIN", "err"); return; }
  banner(r.code === 200 ? (r.body.message || "Clock set") : (r.body.error || ("error " + r.code)), r.code === 200 ? "info" : "err");
}

// SIM / REC / LIVE from a heartbeat (contract labels).
function modeTag(e) {
  if (!e || e.state === "offline") return "";
  const d = String(e.detail || "").toUpperCase();
  if (e.mode === "simulate" || d.startsWith("SIMULATED")) return '<span class="isr-tag sim">SIM</span>';
  if (e.mode === "replay" || d.startsWith("RECORDED")) return '<span class="isr-tag rec">REC</span>';
  return e.state === "live" ? '<span class="isr-tag live">LIVE</span>' : "";
}
function hbState(e) {
  return !e ? "idle" : e.state === "live" ? (e.status === "ok" ? "ok" : e.status === "error" ? "bad" : "warn")
    : e.state === "stale" ? "warn" : "bad";
}
