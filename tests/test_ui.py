"""Shared UI kit (ui/) and the launcher's two views: the mission board (/) and the engineer view (/engineer)."""
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "launcher" / "static"
sys.path.insert(0, str(REPO / "launcher"))
sys.path.insert(0, str(REPO / "maps"))
import isr_launcher as L        # noqa: E402

# The kit as handed over (ui-kit v2). Changing ui/ is deliberate: update these and re-sync every repo.
KIT_SHA256 = {
    "isr-ui.css": "287c3df0938d25c25b04b751007095c3cf08e1df0e2689d3dd8df8b3e9eb832c",
    "isr-ui.js": "79a61cb2a427302e45b62cbebe2d022eff2dcd0549df5edea77b13a6b478fe7d",
    "fonts/barlow-condensed-500.woff2": "31ff5e6e96a096fde4d78bc838719e69b093db2d0b062b96e423c065a6d1b2b1",
    "fonts/barlow-condensed-600.woff2": "5bc49d41f32810a114765314392332ce2acae2276635eee265a50319a12c1b49",
    "fonts/ibm-plex-mono-400.woff2": "52de70091213c9eab340e2a7ac59e7b6b2b94e089bf69ef38479b22cfde4274c",
    "fonts/ibm-plex-mono-500.woff2": "f17f51948d10b67d907393ccbac551912f2c2bd2c5af19be3aabe2f456f8fe54",
    "fonts/OFL-BarlowCondensed.txt": "186d750eb496a4c17a76385f82be6aea2ac1cf2de074a811d63786cf374ea73f",
    "fonts/OFL-IBMPlexMono.txt": "7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da",
}
sha = lambda b: hashlib.sha256(b).hexdigest()


def get(port, path, headers=None, method="GET", body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setattr(L, "SERVICES_CONF", REPO / "services.conf")
    srv = L.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_kit_files_are_the_pinned_kit():
    for name, want in KIT_SHA256.items():
        assert sha((REPO / "ui" / name).read_bytes()) == want, f"ui/{name} changed: update KIT_SHA256 and re-sync"
    assert (REPO / "ui" / "UI-SPEC.md").exists()


def test_launcher_serves_the_kit_byte_identical(launcher):
    for name in KIT_SHA256:
        s, h, b = get(launcher, f"/ui/{name}?v=3")
        assert s == 200 and sha(b) == KIT_SHA256[name], name
    assert "text/css" in get(launcher, "/ui/isr-ui.css")[1]["Content-Type"]
    assert get(launcher, "/ui/fonts/ibm-plex-mono-400.woff2")[1]["Content-Type"] == "font/woff2"
    assert get(launcher, "/ui/fonts/x.py")[0] == 404
    assert get(launcher, "/ui/../launcher/isr_launcher.py")[0] == 404
    assert get(launcher, "/ui/secret.txt")[0] == 404
    for name in ("common.js", "board.js", "app.js", "launcher.css"):
        assert get(launcher, "/" + name)[0] == 200


def test_maps_serves_the_kit(tmp_path, monkeypatch):
    import isr_maps as M
    monkeypatch.setattr(M, "catalog", M.Catalog(tmp_path))
    srv = M.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for name in KIT_SHA256:
            s, _, b = get(port, f"/ui/{name}")
            assert s == 200 and sha(b) == KIT_SHA256[name], name
        s, _, page = get(port, "/")
        page = page.decode()
        assert s == 200 and "/ui/isr-ui.css?v=3" in page and "/ui/isr-ui.js?v=3" in page and 'class=\'isr-bar\'' in page
        assert "id='isr-status'" in page and "isr-more" in page and "No packs" in page
    finally:
        srv.shutdown(); srv.server_close()


def test_board_page(launcher):
    s, _, html = get(launcher, "/")
    html = html.decode()
    assert s == 200
    assert html.index("/ui/isr-ui.css?v=3") < html.index("/launcher.css?v=")   # kit first, own CSS after
    assert "/ui/isr-ui.js?v=3" in html and 'id="isr-status"' in html and 'href="/engineer"' in html
    assert "<h1>ISTAR kit</h1>" in html and "/board.js?v=" in html and "/app.js" not in html
    board = (STATIC / "board.js").read_text()
    assert "/api/switch" not in board and "doSwitch" not in board             # switching is engineer-only
    common = (STATIC / "common.js").read_text()
    for g in ("Detect", "Watch", "Analyse", "Share"):
        assert f'["{g}"' in common
    assert '"AI advisor"' in common and '"Mesh dead-drop"' in common and "Tac advisor" not in common
    assert "<p>" not in html                                                   # no paragraphs on the board


def test_engineer_page(launcher):
    s, _, html = get(launcher, "/engineer")
    html = html.decode()
    assert s == 200 and "/ui/isr-ui.css?v=3" in html and "/ui/isr-ui.js?v=3" in html and 'id="isr-status"' in html
    assert "<h1>ISTAR kit</h1>" in html
    assert 'href="/"' in html and "/common.js" in html and "/app.js" in html
    for sect in ("Pi health", "Profiles", "Self-check", "Services on the Pi", "Mac stations", "Offline maps",
                 "Devices", "Add a mesh node", "Last switch log"):
        assert sect in html
    assert html.count('details class="isr-more"') >= 4
    assert 'class="mute" style="font-size:13px' not in html                      # old inline help text gone
    app = (STATIC / "app.js").read_text()
    assert "doSwitch" in app and "ISR.setStatus(rollup(st))" in app


# ── board logic, run in node with a fake status ──────────────────────────────

def _node(code):
    if not shutil.which("node"):
        pytest.skip("no node")
    common = (STATIC / "common.js").read_text()
    board = (STATIC / "board.js").read_text()
    funcs = board[board.index("function toolCard"):board.index("function render(st)")]
    pre = 'const location = {protocol: "http:", hostname: "isr-pi.local", href: "http://isr-pi.local:8050/"};\n'
    p = subprocess.run(["node", "-e", pre + common + funcs + code], capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


FAKE = {
    "hub_hosts": ["192.168.4.1", "isr-pi", "isr-pi.local"], "active": "demo-full-mac", "switching": False,
    "mqtt": {"connected": True}, "ai_tier": "Mac VLM", "tak": {"clients": 3},
    "llm": {"configured": True, "reachable": True}, "vlm": {"configured": True, "reachable": False},
    "clock": {"now": 1000.0, "trusted": True, "ntp_synced": False, "browser_min_skew": 5,
              "last_set": {"ts": 940.0, "source": "hq", "action": "verified", "skew_s": 0.7}},
    "health": {"power": {"level": "ok"}, "throttled": {"flags": []}, "disk": {"free_gb": 40}, "cpu_temp_c": 61},
    "services": [
        {"unit": "opentakserver", "port": 80, "installed": True, "active": "active", "in_profile": True, "class": "core"},
        {"unit": "drone-detector", "port": 8060, "installed": True, "active": "active", "in_profile": True,
         "heartbeat": {"state": "live", "status": "ok", "mode": "simulate", "station": "pi"}},
        {"unit": "acoustic-detector", "port": 8100, "installed": True, "active": "inactive", "in_profile": False},
        {"unit": "rf-spectrum", "port": 8140, "installed": True, "active": "failed", "in_profile": True}],
    "remote": [
        {"tool": "change-detect", "expected_station": "hq", "expected_label": "HQ Mac", "tunnel": {"port": 8130},
         "heartbeat": {"state": "live", "status": "ok", "mode": "mps", "url": "http://192.168.4.1:8130/",
                       "station": "hq", "station_label": "HQ Mac", "host": "corp-laptop-01"}},
        {"tool": "fpv-vision", "expected_station": "op", "expected_label": "OP Mac", "port": 8110,
         "heartbeat": {"state": "live", "status": "ok", "mode": "replay", "url": "http://corp-laptop-01.local:8110/",
                       "station": "hq", "station_label": "HQ Mac"}},
        {"tool": "op-sentry", "expected_station": "op", "expected_label": "OP Mac", "port": 8120,
         "heartbeat": None},
        {"tool": "photoAnalysis", "expected_station": None, "heartbeat": {"state": "live", "status": "ok"}}],
}


def test_board_cards():
    r = _node("status = " + json.dumps(FAKE) + """;
const out = {};
for (const [g, tools] of BOARD) for (const t of tools) { const i = toolInfo(status, t[0], t[1], t[2]); out[t[0]] = i ? toolCard(status, i) : null; }
console.log(JSON.stringify(out));""")
    assert r["acoustic-detector"] is None                    # not in the profile: hidden, not greyed out
    assert r["photoAnalysis"] is None                        # an off-profile heartbeat isn't a card
    dd = r["drone-detector"]
    assert 'class="dot ok"' in dd and "isr-tag sim" in dd and ">Pi<" in dd and 'href="http://isr-pi.local:8060/"' in dd
    assert "Wi-Fi drone signals" in dd and "Open" in dd
    rf = r["rf-spectrum"]
    assert 'class="dot bad"' in rf and "fault" in rf and "Not running" in rf and 'href="http://isr-pi.local:8140/"' in rf
    cd = r["change-detect"]
    assert ">HQ Mac<" in cd and "corp-laptop" not in cd                     # the role, not the Mac's hostname
    assert "isr-tag live" in cd and 'href="http://isr-pi.local:8130/"' in cd
    fpv = r["fpv-vision"]                                        # planned for the OP Mac, running on HQ today
    assert ">HQ Mac<" in fpv and "OP Mac" not in fpv and "isr-tag rec" in fpv
    ops = r["op-sentry"]                                          # no heartbeat: the plan, muted, Open clickable
    assert ">OP Mac<" in ops and 'class="dot idle"' in ops and "quiet" in ops and "Not running" in ops
    assert "isr-tag" not in ops and "360° camera watch" not in ops
    assert '<a class="isr-btn" href="http://isr-pi.local:8120/"' in ops and "isr-btn off" not in ops
    tak = r["opentakserver"]
    assert "isr-big\">03<" in tak and "Show QR" in tak and 'href="http://isr-pi.local/"' in tak


def test_board_shows_the_op_mac_role():
    """The OP Mac's live heartbeat (station op) reads "OP Mac", and an old droneops one "Drone-ops Mac"."""
    st = json.loads(json.dumps(FAKE))
    st["remote"] = [{"tool": "op-sentry", "expected_station": "op", "expected_label": "OP Mac", "port": 8120,
                     "heartbeat": {"state": "live", "status": "ok", "station": "op", "station_label": "OP Mac",
                                   "host": "op-mac", "url": "http://op-mac.local:8120/"}},
                    {"tool": "fpv-vision", "expected_station": "hq", "expected_label": "HQ Mac", "port": 8110,
                     "heartbeat": {"state": "live", "status": "ok", "station": "droneops", "host": "x"}}]
    r = _node("status = " + json.dumps(st) + """;
console.log(JSON.stringify({op: toolInfo(status, "op-sentry", "OP sentry", "").where,
                            fpv: toolInfo(status, "fpv-vision", "FPV vision", "").where}));""")
    assert r == {"op": "OP Mac", "fpv": "Drone-ops Mac"}


def test_pill_rollup_and_must_act():
    r = _node("""fetchMid = 1000.4 * 1000;
const st = """ + json.dumps(FAKE) + """;
const a = rollup(st);
const st2 = JSON.parse(JSON.stringify(st)); st2.clock.trusted = false; st2.clock.last_set = null; fetchMid = 1060 * 1000;
const st3 = JSON.parse(JSON.stringify(st)); st3.health.throttled.flags = ["under-voltage now"];
const st4 = JSON.parse(JSON.stringify(st)); st4.mqtt.connected = false;
console.log(JSON.stringify({a, b: rollup(st2), act2: mustAct(st2), act1: mustAct(st), act3: mustAct(st3), down: rollup(st4)}));""")
    by = {c["name"]: c for c in r["a"]}
    assert by["MQTT"]["state"] == "ok" and by["TAK server"]["text"] == "3 connected"
    assert by["AI"]["state"] == "ok" and by["VLM"]["state"] == "warn" and by["VLM"]["optional"]
    assert by["OP sentry"] == {"name": "OP sentry", "state": "warn", "text": "no heartbeat", "optional": False}
    assert "FPV vision" not in by
    assert by["RF spectrum"]["state"] == "warn" and by["RF spectrum"]["text"] == "failed"
    assert not any(c["state"] == "bad" for c in r["a"])                   # tools down: Degraded, never Offline
    assert by["Clock"]["state"] == "ok" and by["Clock"]["text"].startswith("verified")
    assert by["Power"]["text"] == "5 A" and by["Disk"]["state"] == "ok"
    clk = {c["name"]: c for c in r["b"]}["Clock"]
    assert clk["state"] == "warn" and "off by 60 s" in clk["text"]           # banner, not Offline
    assert {c["name"]: c for c in r["down"]}["MQTT"]["state"] == "bad"        # a hub essential: Offline
    assert r["act1"] is None and r["act2"]["action"] == "setclock" and "behind" in r["act2"]["text"]
    assert "Under-voltage" in r["act3"]["text"]


# ── QR endpoints and TAK client count ────────────────────────────────────────

def test_qr_endpoints(launcher, monkeypatch):
    monkeypatch.setattr(L.probes, "get", lambda: {"maps": {"packs": [
        {"name": "op-area", "title": "OP area", "atak_xml": "http://192.168.4.1:8160/atak/op-area.xml"}]}})
    s, _, b = get(launcher, "/api/qr?for=launcher")
    q = json.loads(b)
    assert s == 200 and q["text"] == f"http://{L.HUB_IP}:{L.PORT}/" and q["svg"].startswith("<svg")
    s, _, b = get(launcher, "/api/qr?for=map:op-area")
    assert s == 200 and json.loads(b)["text"].endswith("/atak/op-area.xml")
    assert get(launcher, "/api/qr?for=map:nope")[0] == 404
    assert get(launcher, "/api/qr?for=http://evil")[0] == 404
    hdr = {"Content-Type": "application/json", L.CSRF_HEADER: L.CSRF_VALUE}
    s, _, b = get(launcher, "/api/hotspot-qr", hdr, "POST", "{}")
    assert s == 404 and "LAUNCHER_HOTSPOT_PSK" in json.loads(b)["error"]
    monkeypatch.setattr(L, "HOTSPOT_PSK", "pa;ss")
    monkeypatch.setattr(L, "PIN", "4321")
    assert get(launcher, "/api/hotspot-qr", dict(hdr, **{"X-PIN": "0"}), "POST", "{}")[0] == 401
    s, _, b = get(launcher, "/api/hotspot-qr", dict(hdr, **{"X-PIN": "4321"}), "POST", "{}")
    assert s == 200 and json.loads(b)["ssid"] == "BFT-TAK" and "pa;ss" not in b.decode()
    assert L._wifi_escape('a;b:c\\d"e,f') == 'a\\;b\\:c\\\\d\\"e\\,f'


def test_tak_clients(monkeypatch):
    out = ("0 0 192.168.4.1:8087 192.168.4.23:40112\n0 0 192.168.4.1:8087 192.168.4.23:40113\n"
           "0 0 127.0.0.1:8087 127.0.0.1:51000\n0 0 [::ffff:169.254.10.2]:8087 [::ffff:169.254.7.9]:5000\n")
    monkeypatch.setattr(L, "run", lambda cmd, timeout=5: (0, out))
    assert L.tak_clients() == {"clients": 2}                 # one phone (two sockets) + the Mac; not loopback
    monkeypatch.setattr(L, "run", lambda cmd, timeout=5: (127, ""))
    assert L.tak_clients() is None


# ── tools/sync-ui-kit.sh ─────────────────────────────────────────────────────

def test_kit_v3_hidden_rule_and_targets():
    css = (REPO / "ui" / "isr-ui.css").read_text()
    assert css.startswith("/* isr-ui.css v3") and "[hidden] { display: none !important; }" in css
    assert (REPO / "ui" / "isr-ui.js").read_text().startswith("/* isr-ui.js v3")
    assert "blufor-tracker:bridge/web/static" in (REPO / "tools" / "sync-ui-kit.sh").read_text()
    spec = (REPO / "ui" / "UI-SPEC.md").read_text()
    assert "isr-ui.css?v=3" in spec and "[hidden]" in spec


def test_sync_ui_kit_check_and_copy(tmp_path):
    root = tmp_path / "isr-kit"
    good = root / "drone-detector" / "detector" / "web" / "static"
    bad = root / "tac-advisor" / "advisor" / "web" / "static"
    good.mkdir(parents=True); bad.mkdir(parents=True)
    (root / "change-detect").mkdir()
    for f in ("isr-ui.css", "isr-ui.js"):
        shutil.copy(REPO / "ui" / f, good / f)
        shutil.copy(REPO / "ui" / f, bad / f)
    shutil.copytree(REPO / "ui" / "fonts", good / "fonts")
    (bad / "isr-ui.css").write_text("/* edited copy */")
    page = root / "tac-advisor" / "advisor" / "web" / "templates" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text('<link rel="stylesheet" href="/static/isr-ui.css"><script src=\'/static/isr-ui.js?v=1\'></script>')
    route = root / "tac-advisor" / "advisor" / "web" / "app.py"
    route.write_text('KIT = ["isr-ui.css", "isr-ui.js"]\nHTML = \'<link href="/static/isr-ui.css?v=3">\'\n')
    sh = str(REPO / "tools" / "sync-ui-kit.sh")
    p = subprocess.run([sh, "--check", "--root", str(root)], capture_output=True, text=True)
    assert p.returncode == 1 and "DRIFT    tac-advisor/advisor/web/static/isr-ui.css" in p.stdout
    assert "LINK     tac-advisor/advisor/web/templates/index.html" in p.stdout and "want ?v=3" in p.stdout
    assert "app.py" not in p.stdout                                             # already ?v=3; bare names ignored
    assert "ok       drone-detector/detector/web/static/isr-ui.js" in p.stdout
    assert "missing  change-detect/changedetect/web/static" in p.stdout
    p = subprocess.run([sh, "--root", str(root), "tac-advisor", "change-detect"], capture_output=True, text=True)
    assert p.returncode == 0 and "copied into change-detect/changedetect/web/static" in p.stdout
    assert page.read_text() == ('<link rel="stylesheet" href="/static/isr-ui.css?v=3">'
                                "<script src='/static/isr-ui.js?v=3'></script>")
    assert route.read_text().startswith('KIT = ["isr-ui.css", "isr-ui.js"]')        # file names untouched
    p = subprocess.run([sh, "--check", "--root", str(root)], capture_output=True, text=True)
    assert p.returncode == 0 and "DRIFT" not in p.stdout and "missing" not in p.stdout and "LINK" not in p.stdout
    assert (root / "change-detect/changedetect/web/static/isr-ui.js").read_bytes() == (REPO / "ui/isr-ui.js").read_bytes()
    assert (root / "tac-advisor/advisor/web/static/fonts/ibm-plex-mono-400.woff2").exists()
    (good / "fonts" / "ibm-plex-mono-400.woff2").write_bytes(b"x")
    p = subprocess.run([sh, "--check", "--root", str(root)], capture_output=True, text=True)
    assert p.returncode == 1 and "DRIFT    drone-detector/detector/web/static/fonts/ibm-plex-mono-400.woff2" in p.stdout


# ── dual-stack listeners (a Mac on the cable and on Wi-Fi reaches the Pi only over IPv6) ──

@pytest.mark.parametrize("modname", ["isr_launcher", "isr_maps"])
def test_default_bind_is_dual_stack(modname, monkeypatch, tmp_path):
    import importlib
    import socket
    M = importlib.import_module(modname)
    assert M.BIND == "::"
    if modname == "isr_maps":
        monkeypatch.setattr(M, "catalog", M.Catalog(tmp_path))
    srv = M.make_server("::", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert srv.address_family == socket.AF_INET6
        assert srv.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
        path = "/healthz" if modname == "isr_launcher" else "/api/packs"
        for host in ("127.0.0.1", "::1"):                          # IPv4 and IPv6 on one socket
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", path)
            assert c.getresponse().status == 200
            c.close()
    finally:
        srv.shutdown(); srv.server_close()
    # IPv6 disabled on this Pi: fall back to 0.0.0.0
    def no_v6(*a, **k):
        raise OSError("Address family not supported by protocol")
    monkeypatch.setattr(M, "DualStackServer", no_v6)
    srv = M.make_server("::", 0)
    try:
        assert srv.address_family == socket.AF_INET and srv.server_address[0] == "0.0.0.0"
    finally:
        srv.server_close()


def test_launcher_client_ip_unmaps_ipv4():
    h = L.Handler.__new__(L.Handler)
    h.client_address = ("::ffff:192.168.4.23", 5000, 0, 0)
    assert h._client_ip() == "192.168.4.23"
    h.client_address = ("fe80::1", 5000, 0, 2)
    assert h._client_ip() == "fe80::1"


def test_station_role_from_heartbeat_or_map(monkeypatch):
    assert L.station_of("corp-laptop-01", "hq") == "hq"                      # the heartbeat's own field wins
    assert L.station_of("corp-laptop-01") == "corp-laptop-01"
    monkeypatch.setattr(L, "STATION_MAP", {"corp-laptop-01": "hq"})
    assert L.station_of("corp-laptop-01.local") == "hq"
    assert L.station_of("isr-droneops") == "droneops" and L.station_of("whatever", "bogus") == "whatever"
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/change-detect", json.dumps({"host": "mac-x", "station": "hq", "status": "ok"}).encode(), False)
    assert st.snapshot()["change-detect"]["station_label"] == "HQ Mac"
    # start-mac.sh's retained station announce: applies to heartbeats already stored, too
    monkeypatch.setattr(L, "ANNOUNCED", {})
    st.on_message("isrkit/heartbeat/fpv-vision", json.dumps({"host": "mac-y", "status": "ok"}).encode(), False)
    assert st.snapshot()["fpv-vision"]["station"] == "mac-y"
    st.on_message("isrkit/station/mac-y", json.dumps({"host": "mac-y", "station": "hq"}).encode(), True)
    assert st.snapshot()["fpv-vision"]["station_label"] == "HQ Mac"
    st.on_message("isrkit/station/mac-y", b"", True)                   # cleared
    assert st.snapshot()["fpv-vision"]["station"] == "mac-y"
    st.on_message("isrkit/station/mac-y", json.dumps({"host": "mac-y", "station": "bogus"}).encode(), True)
    assert st.snapshot()["fpv-vision"]["station"] == "mac-y"


def test_start_mac_announces_station(tmp_path):
    """start-mac.sh publishes a retained isrkit/station/<host> with its role (broker: tools/rehearsal)."""
    import socket as so
    s0 = so.socket(); s0.bind(("127.0.0.1", 0)); port = s0.getsockname()[1]; s0.close()
    log = tmp_path / "broker.log"
    broker = subprocess.Popen([sys.executable, str(REPO / "tools" / "rehearsal" / "broker.py"), str(port), str(log)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    conf, src = tmp_path / "conf", tmp_path / "src"
    conf.mkdir(); src.mkdir()
    (conf / "station.env").write_text(f"ISR_STATION=hq\nISR_HOST=mac-test\nISR_REPOS=\nISR_SRC={src}\nISR_HUB=127.0.0.1\n"
                                      f"MQTT_HOST=127.0.0.1\nMQTT_PORT={port}\nMQTT_USER=isrkit\nMQTT_PASS=pw\nLLM_URL=http://127.0.0.1:1\n")
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal")
    try:
        import time
        time.sleep(0.5)
        subprocess.run([str(REPO / "laptop" / "start-mac.sh"), "start"], capture_output=True, text=True, env=env, timeout=30)
        subprocess.run([str(REPO / "laptop" / "start-mac.sh"), "stop"], capture_output=True, text=True, env=env, timeout=30)
        msgs = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        ann = [m for m in msgs if m["topic"] == "isrkit/station/mac-test"]
        assert ann and ann[0]["retain"] and json.loads(ann[0]["payload"]) == {"host": "mac-test", "station": "hq"}
    finally:
        broker.kill()
