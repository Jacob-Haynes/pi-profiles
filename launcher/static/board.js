"use strict";
// Mission board: what a room watching the projector needs. Engineering detail lives on /engineer.

// One card per tool the active profile runs (toolInfo in common.js decides which and where).
function toolCard(st, t) {
  const extra = t.unit === "opentakserver" ? takBlock(st) : "";
  return '<div class="isr-card tool' + (t.state === "bad" ? " fault" : t.running ? "" : " quiet") + '" data-tool="' + esc(t.unit) + '">'
    + '<div class="tool-top"><span class="dot ' + t.state + '"></span><span class="tool-name">' + esc(t.name) + "</span>"
    + (t.running ? modeTag(t.e) : "") + "</div>"
    + '<div class="tool-purpose">' + (t.running ? esc(t.purpose) : '<span class="isr-muted">Not running</span>') + "</div>"
    + '<div class="isr-muted">' + esc(t.where) + "</div>" + extra
    + '<div class="tool-actions">'
    + (t.href ? openLink(t.href, t.unit)
              : '<span class="isr-btn off" aria-disabled="true">Open</span>')
    + (t.unit === "opentakserver" ? '<button class="isr-btn" id="qrbtn" type="button">Show QR</button>' : "")
    + "</div></div>";
}

function takBlock(st) {
  const n = st.tak && st.tak.clients;
  return n == null ? "" : '<div class="tak-count"><span class="isr-big">' + String(n).padStart(2, "0")
    + '</span><span class="isr-muted">connected</span></div>';
}

function render(st) {
  status = st;
  const active = (st.profiles || []).find((p) => p.active);
  $("sub").textContent = active ? active.title : (st.active || "No profile");
  // the demo style: "Continuous" (simulators run on their own) or "Step by step" (the presenter fires each event)
  const mode = $("mode");
  if (mode) {
    const dm = st.demo_mode || (active && active.demo_mode);
    mode.hidden = !dm;
    mode.textContent = dm === "on_demand" ? "Step by step" : "Continuous";
    mode.className = "isr-tag board-mode " + (dm === "on_demand" ? "live" : "sim");
  }
  ISR.setStatus(rollup(st));
  const act = mustAct(st);
  if (act) {
    banner(act.text, "err");
    if (act.action === "setclock") {
      const b = document.createElement("button");
      b.className = "isr-btn"; b.type = "button"; b.textContent = "Set Pi clock from this device";
      b.addEventListener("click", async () => { await setClock(); refresh(); });
      $("banner").appendChild(document.createTextNode(" ")); $("banner").appendChild(b);
    }
  } else if (!$("banner").dataset.keep) {
    banner("");
  }
  $("board").innerHTML = boardTools(st).map(([group, tools]) => {
    const cards = tools.map((t) => toolCard(st, t));
    return cards.length ? '<section class="board-group"><h2 class="isr-section">' + esc(group) + "</h2>"
      + '<div class="isr-grid board-grid">' + cards.join("") + "</div></section>" : "";
  }).join("") || '<div class="isr-card"><span class="isr-muted">No tools in this profile</span></div>';
  const q = $("qrbtn");
  if (q) q.addEventListener("click", openQr);
}

// QR overlay: this page, the hotspot Wi-Fi (PIN), each offline map.
function qrChoices() {
  const out = [["launcher", "This page"]];
  if (status && status.hotspot_qr) out.push(["wifi", "Join Wi-Fi"]);
  ((status && status.maps && status.maps.packs) || []).forEach((p) => out.push(["map:" + p.name, "Map " + (p.title || p.name)]));
  return out;
}
async function showQr(what) {
  const box = $("qr-img"), cap = $("qr-cap");
  box.innerHTML = ""; cap.textContent = "";
  let r;
  if (what === "wifi") {
    if (!pin()) { const p = prompt("PIN to show the Wi-Fi QR"); if (p == null) return; setPin(p); }
    r = await postJSON("/api/hotspot-qr", {});
    if (r.code === 401) setPin("");
  } else {
    r = await api("/api/qr?for=" + encodeURIComponent(what));
  }
  if (r.code !== 200) { cap.textContent = r.code === 401 ? "Wrong PIN" : (r.body.error || "QR not available"); return; }
  const img = new Image();
  img.alt = r.body.label; img.src = "data:image/svg+xml;base64," + btoa(r.body.svg);
  box.appendChild(img);
  cap.textContent = r.body.label;
}
function openQr() {
  $("qr-tabs").innerHTML = qrChoices().map(([k, label], i) =>
    '<button class="isr-btn' + (i === 0 ? " primary" : "") + '" type="button" data-q="' + esc(k) + '">' + esc(label) + "</button>").join("");
  $("qr-tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    $("qr-tabs").querySelectorAll("button").forEach((x) => x.classList.remove("primary"));
    b.classList.add("primary"); showQr(b.dataset.q);
  }));
  $("qr").hidden = false;
  showQr("launcher");
}
$("qr-close").addEventListener("click", () => { $("qr").hidden = true; });
$("qr").addEventListener("click", (e) => { if (e.target === $("qr")) $("qr").hidden = true; });

async function refresh() {
  try {
    const st = await fetchStatus();
    if (st) render(st);
  } catch (e) {
    ISR.setStatus([{ name: "Launcher", state: "bad", text: "no contact with the Pi" }]);
  }
}
refresh();
setInterval(refresh, 3000);
