"use strict";
// Presenter panel (contract v6): one button per demo scenario, in run-of-show order, for driving a
// step-by-step demo from a phone while the projector shows /map. Loaded after common.js.

const SENT_MS = 4000;
const sent = new Map();          // "unit|id" -> time the trigger was accepted (the "sent" state)
let panel = null, busy = false;

const modeName = (m) => (m === "on_demand" ? "Step by step" : m === "continuous" ? "Continuous" : "");

async function post(path, body) {
  let r = await postJSON(path, body);
  if (r.code === 401) {                          // the launcher has a PIN: ask once, then retry
    const p = prompt("Launcher PIN");
    if (p == null) return r;
    setPin(p);
    r = await postJSON(path, body);
  }
  return r;
}

// A scenario's options as inputs above its button: degrees/number (bearing 045), or a choice (clip 08).
function optionInputs(t, s) {
  const o = s.options || {};
  const names = Object.keys(o);
  if (!names.length) return "";
  return '<div class="pres-opts">' + names.map((n) => {
    const x = o[n], id = "opt-" + t.unit + "-" + s.id + "-" + n;
    const keep = optValue(t.unit, s.id, n);
    const v = keep != null ? keep : x.default;
    let input;
    if (x.type === "choice") {
      input = '<select id="' + esc(id) + '" data-opt="' + esc(n) + '">' + x.choices.map((c) => '<option value="' + esc(c.value) + '"'
        + (String(c.value) === String(v) ? " selected" : "") + ">" + esc(c.label) + "</option>").join("") + "</select>";
    } else if (x.type === "degrees" || x.type === "number") {
      input = '<input id="' + esc(id) + '" data-opt="' + esc(n) + '" type="number" inputmode="numeric"'
        + (x.type === "degrees" ? ' min="0" max="359" step="5"' : "") + ' value="' + esc(v == null ? "" : v) + '">'
        + (x.type === "degrees" ? '<span class="isr-muted">°</span>' : "");
    } else {
      input = '<input id="' + esc(id) + '" data-opt="' + esc(n) + '" type="text" value="' + esc(v == null ? "" : v) + '">';
    }
    return '<label class="pres-opt" for="' + esc(id) + '"><span>' + esc(x.label) + "</span>" + input + "</label>";
  }).join("") + "</div>";
}
const optKeep = new Map();       // what the presenter set, kept across refreshes
function optValue(unit, id, n) { return optKeep.get(unit + "|" + id + "|" + n); }

function scenarioButton(t, s) {
  const key = t.unit + "|" + s.id;
  const fresh = sent.has(key) && Date.now() - sent.get(key) < SENT_MS;
  const running = (t.active || []).includes(s.id);
  return '<div class="pres-scn" data-scn="' + esc(key) + '">' + optionInputs(t, s)
    + '<button class="isr-btn pres-go' + (fresh ? " sent" : running ? " running" : "") + '" type="button" data-unit="'
    + esc(t.unit) + '" data-id="' + esc(s.id) + '"' + (t.reachable ? "" : " disabled") + ">"
    + '<span class="pres-label">' + esc(fresh ? "Sent → watch the map" : s.label) + "</span>"
    + '<span class="pres-hint">' + esc(running && !fresh ? "Running" + (s.duration_s ? " · " + Math.round(s.duration_s) + " s" : "")
      : (s.hint || "") + (s.duration_s && !fresh ? (s.hint ? " · " : "") + Math.round(s.duration_s) + " s" : "")) + "</span></button></div>";
}

function toolCard(t) {
  // the tool's own mode; when it differs from the profile's (a Mac tool not restarted yet, or DEMO_MODE
  // pinned in its station.env), say so rather than show two modes that disagree
  const off = t.mode && panel.mode && t.mode !== panel.mode;
  const mode = modeName(t.mode) + (off ? " (profile: " + modeName(panel.mode).toLowerCase() + ")" : "");
  const state = !t.reachable ? '<span class="dot bad"></span>' : '<span class="dot ok"></span>';
  return '<div class="isr-card pres-tool' + (t.reachable ? "" : " quiet") + '">'
    + '<div class="pres-head">' + state + '<span class="tool-name">' + esc(t.title) + "</span>"
    + (mode ? '<span class="isr-tag ' + (off ? "rec" : t.mode === "on_demand" ? "live" : "sim") + '">' + esc(mode) + "</span>" : "") + "</div>"
    + '<div class="isr-muted">' + esc(t.where || "") + (t.error ? " · " + esc(t.error) : "") + "</div>"
    + '<div class="pres-btns">' + (t.scenarios || []).map((s) => scenarioButton(t, s)).join("") + "</div>"
    + (t.reachable && (t.scenarios || []).length
      ? '<button class="isr-btn pres-clear" type="button" data-clear="' + esc(t.unit) + '">Clear demo</button>' : "")
    + "</div>";
}

function render() {
  if (!panel) return;
  $("sub").textContent = panel.title || panel.profile || "No profile";
  const m = $("mode");
  m.hidden = !panel.mode;
  m.textContent = modeName(panel.mode);
  m.className = "isr-tag " + (panel.mode === "on_demand" ? "live" : "sim");
  $("how").textContent = panel.mode === "on_demand"
    ? "Step by step: nothing simulated runs on its own. Each button fires one event; watch it arrive on the map and ATAK."
    : "Continuous: the simulators already run on their own. A button adds one more event on top.";
  const groups = [];
  const none = (panel.tools || []).filter((t) => t.has_demo === false);     // no demo API: no card, no error
  for (const t of (panel.tools || []).filter((x) => x.has_demo !== false)) {
    let g = groups.find((x) => x.name === t.group);
    if (!g) groups.push((g = { name: t.group, tools: [] }));
    g.tools.push(t);
  }
  $("groups").innerHTML = groups.map((g) => '<section class="pres-group"><h2 class="isr-section">' + esc(g.name) + "</h2>"
    + g.tools.map(toolCard).join("") + "</section>").join("")
    + (none.length ? '<p class="isr-muted pres-none">No demo buttons (they show real results only): '
      + esc(none.map((t) => t.title).join(", ")) + "</p>" : "")
    || '<div class="isr-card"><span class="isr-muted">No tool in this profile has demo scenarios.</span></div>';
}

async function refresh() {
  if (busy || editing) return;
  const r = await api("/api/demo/scenarios");
  if (r.code === 401) { const p = prompt("Launcher PIN"); if (p != null) setPin(p); return; }
  if (r.code !== 200) { banner("Launcher not answering (" + r.code + ")", "err"); return; }
  if ($("banner").textContent.startsWith("Launcher not")) banner("");
  panel = r.body;
  render();
}

async function fire(unit, id, btn) {
  busy = true;
  btn.disabled = true;
  const body = { tool: unit, id: id };
  const box = btn.closest(".pres-scn");
  const scn = ((panel && panel.tools) || []).find((t) => t.unit === unit);
  const opts = ((scn && scn.scenarios.find((s) => s.id === id)) || {}).options || {};
  (box ? box.querySelectorAll("[data-opt]") : []).forEach((el) => {
    const n = el.dataset.opt, x = opts[n] || {};
    if (el.value === "") return;
    body[n] = x.type === "degrees" || x.type === "number" ? Number(el.value) : el.value;
  });
  const r = await post("/api/demo/trigger", body);
  busy = false;
  if (r.code === 200 && r.body.ok !== false) {
    sent.set(unit + "|" + id, Date.now());
    banner("");
    setTimeout(render, SENT_MS + 50);
  } else {
    banner(unit + ": " + (r.body.error || r.body.detail || "error " + r.code), "err");
  }
  render();
  setTimeout(refresh, 800);
}

async function clearTools(body, what) {
  busy = true;
  const r = await post("/api/demo/clear", body);
  busy = false;
  const res = (r.body && r.body.results) || [];
  const bad = res.filter((x) => !x.ok);
  const n = res.reduce((s, x) => s + (x.cleared || 0), 0);
  if (r.code === 200 && !bad.length) banner(what + ": cleared" + (n ? " " + n + " item" + (n === 1 ? "" : "s") : ""), "info");
  else banner(what + ": " + (bad.map((x) => x.unit + " (" + (x.error || "failed") + ")").join(", ") || r.body.error || "error " + r.code), "err");
  sent.clear();
  refresh();
}

// keep what the presenter typed or picked across the 4 s refresh (which rebuilds the cards)
document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-opt]");
  const box = el && el.closest(".pres-scn");
  if (box) optKeep.set(box.dataset.scn + "|" + el.dataset.opt, el.value);
});
// no refresh (which rebuilds the cards) while an option input has the focus
let editing = false;
document.addEventListener("focusin", (e) => { editing = !!e.target.closest(".pres-opt"); });
document.addEventListener("focusout", () => { editing = false; });
document.addEventListener("click", (e) => {
  const go = e.target.closest("button[data-id]");
  if (go) return fire(go.dataset.unit, go.dataset.id, go);
  const cl = e.target.closest("button[data-clear]");
  if (cl) return clearTools({ tool: cl.dataset.clear }, cl.dataset.clear);
  if (e.target.closest("#reset")) {
    if (confirm("Reset picture: clear every tool's demo events now? Real detections stay.")) clearTools({ all: true }, "Reset picture");
  }
});
refresh();
setInterval(refresh, 4000);
