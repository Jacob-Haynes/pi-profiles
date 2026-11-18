"use strict";
// Engineer view. Helpers (status, $, esc, api, banner, viewerUrl, hubLink, rollup …) are in common.js.
let busy = false;

function stateClass(s) {
  const hb = s.heartbeat;
  if (!s.installed && hb) return hb.state === "live" ? (hb.status === "ok" ? "ok" : "warn") : hb.state === "stale" ? "warn" : "mute";
  if (!s.installed) return "mute";
  if (s.active === "active") return "ok";
  if (s.active === "activating" || s.active === "deactivating") return "warn";
  if (s.active === "failed") return "bad";
  return "mute";
}

function render(st) {
  status = st;
  ISR.setStatus(rollup(st));
  $("host").textContent = st.health.hostname ? st.health.hostname : "";
  $("active").textContent = st.active || "none";
  $("switching").innerHTML = st.switching ? '<span class="warn">switching…</span>' : "";
  const n = st.internet;
  $("net").innerHTML = n.gemini_reachable ? '<span class="ok">Internet (Gemini reachable)</span>'
    : n.online ? '<span class="warn">Internet, Gemini unreachable</span>'
    : '<span class="mute">No internet</span>';

  $("ai").textContent = st.ai_tier || "?";
  const ollama = (o, name) => !o || !o.configured ? '<span class="mute">' + name + " not set</span>"
    : o.reachable ? '<span class="' + (o.model_present === false ? "warn" : "ok") + '">' + name + " up"
      + (o.model_present === false ? " (model " + esc(o.model) + " missing)" : "") + "</span>"
    : '<span class="bad">' + name + " unreachable</span>";
  $("llm").innerHTML = ollama(st.llm, "LLM") + " · " + ollama(st.vlm, "VLM");
  $("mqtt").innerHTML = st.mqtt && st.mqtt.connected ? '<span class="ok">MQTT ok</span>'
    : '<span class="bad">MQTT down' + (st.mqtt && st.mqtt.error ? ": " + esc(st.mqtt.error) : "") + "</span>";

  const h = st.health, t = h.throttled || {};
  const temp = h.cpu_temp_c;
  const tcls = temp == null ? "mute" : temp >= 80 ? "bad" : temp >= 70 ? "warn" : "ok";
  const thr = t.raw == null ? '<b class="mute">n/a</b>' : t.ok && !t.flags.length ? '<b class="ok">none</b>'
    : '<b class="' + (t.ok ? "warn" : "bad") + '">' + esc(t.raw) + "</b>";
  $("health").innerHTML = [
    ['<b class="' + tcls + '">' + (temp == null ? "n/a" : temp + "°C") + "</b>", "CPU temp"],
    ["<b>" + (h.load ? h.load.join(" / ") : "n/a") + "</b>", "load 1/5/15 (" + (h.cpus || "?") + " cores)"],
    [thr, "throttling" + (t.flags && t.flags.length ? ": " + esc(t.flags.join(", ")) : "")],
    ["<b>" + (h.disk.free_gb == null ? "n/a" : h.disk.free_gb + " GB") + "</b>", "disk free (" + (h.disk.used_pct || "?") + "% used)"],
    ["<b>" + (h.mem.available_mb == null ? "n/a" : h.mem.available_mb + " MB") + "</b>", "RAM available"],
  ].map((x) => '<div class="stat">' + x[0] + "<span>" + x[1] + "</span></div>").join("");
  renderClock(st.clock || {});
  const pw = h.power || {};
  const pcls = { ok: "ok", warn: "warn", bad: "bad" }[pw.level] || "mute";
  $("power").className = "stat " + pcls;
  $("power").innerHTML = "<b>" + (pw.level === "ok" ? "5 A OK" : pw.level === "bad" ? "USB LIMITED"
    : pw.level === "warn" ? "Override" : "Power ?") + "</b><span>" + esc(pw.verdict || "") + "</span>";

  $("profiles").innerHTML = st.profiles.filter((p) => !p.hidden || p.active).map((p) => {
    const devs = p.devices.map((d) => '<span class="chip ' + (p.missing_devices.includes(d) ? "missing" : "present") + '">'
      + esc(d) + "</span>").join("")
      + (p.optional || []).map((d) => '<span class="chip' + ((p.missing_optional || []).includes(d) ? "" : " present") + '">'
      + esc(d) + " (opt)</span>").join("")
      + (p.remote || []).map((r) => '<span class="chip">' + esc(r.tool) + " @ " + esc(r.station) + "</span>").join("");
    return '<button class="prof' + (p.active ? " active" : "") + '" data-p="' + esc(p.name) + '"'
      + (st.switching || busy ? " disabled" : "") + ">"
      + "<b>" + esc(p.title) + "</b> <span class='mute'>" + esc(p.name) + (p.active ? " · active" : "") + "</span>"
      + (p.note ? "<small>" + esc(p.note) + "</small>" : "") + (devs ? "<div>" + devs + "</div>" : "") + "</button>";
  }).join("") || '<span class="mute">No profiles installed</span>';
  document.querySelectorAll(".prof").forEach((b) => b.addEventListener("click", () => doSwitch(b.dataset.p)));

  $("services").innerHTML = st.services.filter((s) => s.in_profile || s.active === "active" || s.installed).map((s) => {
    const cls = stateClass(s);
    const label = s.installed ? s.active + (s.sub && s.sub !== s.active ? " (" + s.sub + ")" : "") : "not installed";
    const hbUp = s.heartbeat && s.heartbeat.state === "live";
    const link = s.port && (s.active === "active" || hbUp) && s.unit !== "isr-launcher"
      ? '<a class="isr-btn" href="' + esc(hubLink(s.port)) + '" target="_blank" rel="noopener">Open :' + s.port + "</a>" : "";
    return '<div class="row"><div><b><span class="dot ' + cls + '"></span>' + esc(s.title) + "</b>"
      + (s.in_profile ? "" : ' <span class="mute">(not in profile)</span>')
      + '<br><small class="' + cls + '">' + esc(s.unit) + " · " + esc(label)
      + (s.heartbeat ? " · hb " + esc(s.heartbeat.state) + (s.heartbeat.mode ? " · " + esc(s.heartbeat.mode) : "")
        + (s.heartbeat.detail ? "<br>" + esc(s.heartbeat.detail) : "") : "")
      + "</small></div>" + link + "</div>";
  }).join("");

  const hbCls = (e) => !e ? "mute" : e.state === "live" ? (e.status === "ok" ? "ok" : "warn")
    : e.state === "stale" ? "warn" : "bad";
  const hbText = (e) => !e ? "no heartbeat" : e.state + (e.age_s != null ? " (" + Math.round(e.age_s) + " s ago)" : "")
    + " · " + esc(e.station_label) + (e.mode ? " · " + esc(e.mode) : "") + (e.status ? " · " + esc(e.status) : "")
    + (e.detail ? "<br>" + esc(e.detail) : "");
  $("remote").innerHTML = (st.remote || []).map((r) => {
    const e = r.heartbeat, cls = hbCls(e);
    const t = r.tunnel;
    const d = r.direct && r.direct.url;
    const link = t && e && e.state !== "offline"
      ? '<a class="isr-btn" href="' + esc(hubLink(t.port)) + '" target="_blank" rel="noopener">Open</a>'
      : d && e && e.state !== "offline"
      ? '<a class="isr-btn" href="' + esc(d) + '" target="_blank" rel="noopener">Open</a>'
      : e && e.url && e.state !== "offline"
      ? '<a class="isr-btn" href="' + esc(viewerUrl(e.url)) + '" target="_blank" rel="noopener">Open</a>' : "";
    return '<div class="row"><div><b><span class="dot ' + cls + '"></span>' + esc(r.title) + "</b>"
      + (r.expected_label ? ' <span class="mute">expected on ' + esc(r.expected_label) + "</span>" : "")
      + (t ? ' <span class="ok">' + (t.via === "relay" ? "via Pi relay" : "via Pi tunnel") + "</span>" : d ? ' <span class="mute">by IP</span>' : "")
      + '<br><small class="' + cls + '">' + esc(r.tool) + " · " + hbText(e) + "</small></div><div>" + link + "</div></div>";
  }).join("") || '<span class="mute">No Mac tools in this profile and no off-Pi heartbeats</span>';

  const mp = st.maps || {};
  $("maps").innerHTML = !mp.reachable ? '<span class="warn">Map server (isr-maps :8160) not reachable</span>'
    : !(mp.packs || []).length ? '<span class="mute">No map packs yet: build one on a Mac (laptop/build-map-pack.py) and push it (push-to-pi.sh --maps)</span>'
    : mp.packs.map((p) => '<div class="row"><div><b>' + esc(p.title) + " <span class='mute'>" + esc(p.name) + "</span></b><br>"
      + '<small class="mute">z' + esc(p.minzoom) + "–" + esc(p.maxzoom) + " · " + esc(p.size_mb) + " MB · bbox "
      + esc((p.bounds || []).join(", ")) + "<br>" + esc(p.attribution) + "</small></div><div class='btn-row'>"
      + '<button class="isr-btn pushmaps" type="button" data-pack="' + esc(p.name) + '">Push maps to all ATAK devices</button>'
      + (p.atak_xml ? '<a class="isr-btn" href="' + esc(viewerUrl(p.atak_xml)) + '" download>Add to ATAK</a>' : "")
      + (p.download ? '<a class="isr-btn" href="' + esc(viewerUrl(p.download)) + '" download>Download</a>' : "")
      + "</div></div>").join("");

  const d = st.devices, present = new Set(d.present);
  $("devices").innerHTML = Object.keys(d.labels).map((k) =>
    '<div class="row"><span><span class="dot ' + (present.has(k) ? "ok" : "") + '"></span> ' + esc(d.labels[k])
    + '</span><span class="' + (present.has(k) ? "ok" : "mute") + '">' + (present.has(k) ? "detected" : "—") + "</span></div>").join("")
    + '<details class="isr-more"><summary>Raw: ' + d.usb.length + " USB devices, serial " + esc(d.serial.join(" ") || "none") + "</summary><pre>"
    + esc(d.usb.map((u) => u.id + "  " + u.name + (u.kind ? "  [" + u.kind + "]" : "")).join("\n")) + "</pre></details>";

  $("log").textContent = (st.switch_log || []).join("\n") || "(no switches yet)";
  const ls = st.last_switch || {};
  if (!st.switching && !busy && ls.result && ls.result !== "ok" && ls.profile === st.active) {
    banner("Last switch to " + ls.profile + ": " + ls.result
      + (ls.missing ? " · not installed: " + ls.missing : "") + (ls.failed ? " · failed: " + ls.failed : ""), "err");
  }
}

async function refresh() {
  try {
    const st = await fetchStatus();
    if (st) render(st);
  } catch (e) {
    banner("Lost contact with the Pi, retrying", "err");
  }
}

async function doSwitch(name) {
  if (!status || busy || status.switching) return;
  if (name === status.active && !confirm("Re-apply profile " + name + "? (restarts anything stopped)")) return;
  if (name !== status.active && !confirm("Switch to " + name + "?")) return;
  if (status.pin_required && !pin()) {
    const p = prompt("PIN to switch profile");
    if (p == null) return;
    setPin(p);
  }
  busy = true;
  banner("Switching to " + name, "info");
  try {
    const r = await api("/api/switch", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: name }) });
    if (r.code === 401) { setPin(""); banner("Wrong PIN", "err"); }
    else if (r.code !== 202) banner(r.body.error || ("Switch failed (" + r.code + ")"), "err");
    else setTimeout(() => banner(""), 8000);
  } finally {
    busy = false;
    refresh();
  }
}

async function showChannel() {
  const box = $("channel");
  if (box.dataset.shown) { box.innerHTML = ""; delete box.dataset.shown; $("chbtn").textContent = "Show channel QR"; return; }
  if (!pin()) { const p = prompt("PIN to show the mesh channel"); if (p == null) return; setPin(p); }
  const r = await api("/api/channel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (r.code === 401) { setPin(""); box.innerHTML = '<span class="bad">Wrong PIN</span>'; return; }
  if (r.code !== 200) { box.innerHTML = '<span class="warn">' + esc(r.body.error || ("error " + r.code)) + "</span>"; return; }
  const img = new Image();
  img.alt = "Meshtastic channel QR";
  img.src = "data:image/svg+xml;base64," + btoa(r.body.svg);
  box.innerHTML = "";
  box.appendChild(img);
  const a = document.createElement("a");
  a.href = r.body.url; a.textContent = "Open in the Meshtastic app (on this phone)";
  a.className = "isr-btn";
  box.appendChild(a);
  const m = document.createElement("div");
  m.className = "isr-muted";
  m.textContent = "Cached " + r.body.updated + ". Hide it when done.";
  box.appendChild(m);
  box.dataset.shown = "1";
  $("chbtn").textContent = "Hide channel QR";
}
$("chbtn").addEventListener("click", showChannel);

async function selfCheck(mesh) {
  const box = $("checkout");
  if (mesh && !confirm("Run the checks and transmit a short radio check on the mesh?")) return;
  if (status && status.pin_required && !pin()) { const p = prompt("PIN"); if (p == null) return; setPin(p); }
  box.innerHTML = '<span class="isr-muted">Running checks (up to a minute)</span>';
  ["chk", "chkmesh"].forEach((id) => { $(id).disabled = true; });
  try {
    const r = await api("/api/check", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mesh: !!mesh }) });
    if (r.code === 401) { setPin(""); box.innerHTML = '<span class="bad">Wrong PIN</span>'; return; }
    if (r.code !== 200) { box.innerHTML = '<span class="warn">' + esc(r.body.error || ("error " + r.code)) + "</span>"; return; }
    const cls = { PASS: "ok", OK: "ok", FAIL: "bad", WARN: "warn", INFO: "mute" };
    box.innerHTML = '<div class="' + (r.body.ok ? "ok" : "bad") + '"><b>' + (r.body.ok ? "All required checks pass" : "Checks failed")
      + "</b> <span class='mute'>" + esc(r.body.ran_at) + "</span></div>"
      + r.body.lines.map((l) => '<div class="row"><small class="' + (cls[l.level] || "") + '">'
        + (l.level ? "[" + esc(l.level) + "] " : "") + esc(l.text) + "</small></div>").join("");
  } finally {
    ["chk", "chkmesh"].forEach((id) => { $(id).disabled = false; });
  }
}
$("chk").addEventListener("click", () => selfCheck(false));
$("chkmesh").addEventListener("click", () => selfCheck(true));

function renderClock(c) {
  const el = $("clock");
  if (c.now == null) { el.className = "stat"; el.innerHTML = "<b class='mute'>Clock unknown</b>"; return; }
  const skew = clockSkew(c);
  const big = skew != null && Math.abs(skew) > (c.browser_min_skew || 5);
  const ls = c.last_set;
  const last = c.ntp_synced ? "NTP-synced"
    : ls ? (ls.action === "verified" ? "verified by " : "set from ") + esc(ls.source) + " " + ago(c.now, ls.ts)
      + (ls.skew_s != null ? " (" + (ls.action === "verified" ? "skew " : "corrected ") + esc(ls.skew_s) + " s)" : "")
    : "never set or verified by a station";
  el.className = "stat " + (big ? "bad" : !c.trusted ? "warn" : "ok");
  el.innerHTML = "<b>" + (big ? "Clock skew" : !c.trusted ? "Clock not synced" : "Clock OK") + "</b><span>Pi "
    + esc(new Date(c.now * 1000).toLocaleTimeString())
    + (skew == null ? "" : ", " + (skew >= 0 ? "behind" : "ahead of") + " this device by " + Math.abs(skew).toFixed(1) + " s")
    + " · " + last + "</span>"
    + (big ? '<button class="isr-btn" id="setclock" type="button">Set Pi clock from this device</button>' : "");
  const b = $("setclock");
  if (b) b.addEventListener("click", async () => { await setClock(); refresh(); });
}

async function pushMaps(pack, btn) {
  const box = $("pushout"), tiles = $("pushtiles").checked;
  if (!confirm("Send map '" + pack + "' to every ATAK device connected to the TAK server"
    + (tiles ? " (with the full .mbtiles: large)" : "") + "?")) return;
  if (!askPin()) return;
  box.innerHTML = '<span class="isr-muted">Building the data package and sending it (about 10 s' + (tiles ? ", longer with tiles" : "") + ")</span>";
  btn.disabled = true;
  try {
    const r = await api("/api/push-maps", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pack: pack, with_mbtiles: tiles }) });
    if (r.code === 401) { setPin(""); box.innerHTML = '<span class="bad">Wrong PIN</span>'; return; }
    if (r.code !== 200) { box.innerHTML = '<span class="bad">' + esc(r.body.error || ("error " + r.code)) + "</span>"; return; }
    box.innerHTML = '<div class="ok"><b>Sent ' + esc(r.body.filename) + "</b> (" + esc((r.body.size / 1e6).toFixed(1))
      + " MB) to all connected ATAK devices.</div><details class='isr-more'><summary>Details</summary>Each phone shows a"
      + " file-transfer notification; the map then appears in Maps &amp; Favorites. " + esc((r.body.log || []).join(" · "))
      + "</details>";
  } finally {
    btn.disabled = false;
  }
}
$("maps").addEventListener("click", (ev) => {
  const b = ev.target.closest("button.pushmaps");
  if (b) pushMaps(b.dataset.pack, b);
});

refresh();
setInterval(refresh, 3000);
