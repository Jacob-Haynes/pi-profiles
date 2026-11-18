"""Step-by-step demo (contract v6 demo triggers): the demo-step-mac profile, the Presenter panel's
aggregation of every tool's scenarios (with a tool offline), triggers, Reset calling every tool, the
heartbeat's demo_mode, and the Macs' tools following the Pi's demo style."""
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
import demo  # noqa: E402
import isr_launcher as L  # noqa: E402

PROFILES = REPO / "profiles.d"
H = {"X-Requested-With": "isr-launcher", "Content-Type": "application/json"}


# ── the profiles ─────────────────────────────────────────────────────────────

@pytest.fixture
def real_profiles(monkeypatch):
    monkeypatch.setattr(L, "PROFILES_DIR", PROFILES)
    monkeypatch.setattr(L, "SERVICES_CONF", REPO / "services.conf")
    monkeypatch.setattr(L, "SWITCH_SH", str(REPO / "switch.sh"))
    monkeypatch.setenv("PI_PROFILES_DIR", str(PROFILES))
    monkeypatch.setenv("PI_PROFILES_SERVICES", str(REPO / "services.conf"))


def test_step_profile(real_profiles):
    step, cont = L.parse_profile(PROFILES / "demo-step-mac.conf"), L.parse_profile(PROFILES / "demo-full-mac.conf")
    assert step["title"] == "Core demo (step by step)" and step["demo_mode"] == "on_demand"
    assert cont["title"] == "Core demo (Pi + 2 Macs)" and cont["demo_mode"] == "continuous"
    for u in ("drone-detector", "acoustic-detector", "rf-spectrum", "mesh-bridge"):     # mesh-bridge = blufor-tracker's BFT sim
        assert step["env"][u]["DEMO_MODE"] == "on_demand", u
    assert step["env"]["rf-spectrum"]["ALERT_COT"] == "1"                                # what the continuous profile sets, kept
    assert cont["env"] == {"rf-spectrum": {"ALERT_COT": "1"}}                           # the continuous demo is unchanged
    units = L.profile_units("demo-step-mac")
    assert units and units == L.profile_units("demo-full-mac")                          # the same tools
    assert {"drone-detector", "acoustic-detector", "rf-spectrum", "mesh-bridge", "opentakserver"} <= set(units)
    assert step["remote"] == cont["remote"] and step["devices"] == cont["devices"]
    env = subprocess.run(["bash", str(REPO / "switch.sh"), "--env", "demo-step-mac"], capture_output=True, text=True,
                         env=dict(os.environ, PI_PROFILES_DIR=str(PROFILES)), timeout=20).stdout.split("\n")
    assert {"mesh-bridge DEMO_MODE=on_demand", "drone-detector DEMO_MODE=on_demand", "rf-spectrum ALERT_COT=1"} <= set(env)


def test_heartbeat_demo_fields():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/drone-detector", json.dumps({"host": "isr-pi", "status": "ok", "demo_mode": "on_demand",
                                                                "demo_active": ["inbound", "bad id!"]}).encode(), False)
    st.on_message("isrkit/heartbeat/acoustic-detector", json.dumps({"host": "isr-pi", "status": "ok", "detail": {
        "text": "listening", "demo_mode": "continuous", "demo_active": []}}).encode(), False)
    st.on_message("isrkit/heartbeat/rf-spectrum", json.dumps({"host": "isr-pi", "demo_mode": "sometimes"}).encode(), False)
    s = st.snapshot()
    assert s["drone-detector"]["demo_mode"] == "on_demand" and s["drone-detector"]["demo_active"] == ["inbound"]
    assert s["acoustic-detector"]["demo_mode"] == "continuous" and s["acoustic-detector"]["detail"] == "listening"
    assert s["rf-spectrum"]["demo_mode"] is None


# ── aggregation, trigger, clear (demo.py) ────────────────────────────────────

def fake_call(table, log):
    def call(url, body=None, timeout=demo.TIMEOUT):
        log.append((url, body))
        for k, v in table.items():
            if url.startswith(k):
                return v(url, body) if callable(v) else v
        return 0, {"error": "ConnectionRefusedError: refused"}
    return call


def test_scenarios_order_offline_and_cleaning():
    log = []
    call = fake_call({
        "http://op": (200, {"mode": "on_demand", "active": ["drone-on-bearing"], "scenarios": [
            {"id": "drone-on-bearing", "label": "Drone on a bearing", "hint": "clip at 045", "duration_s": 40},
            {"id": "bad id <script>", "label": "x"}, "junk"]}),
        "http://dd": (200, {"mode": "weird", "scenarios": [{"id": "inbound", "label": "Inbound"}, {"id": "swarm", "duration_s": -5}]}),
        "http://rf": (404, {"error": "HTTP 404"}),
    }, log)
    tools = [{"unit": "rf-spectrum", "where": "Pi", "base": "http://rf"},
             {"unit": "drone-detector", "where": "Pi", "base": "http://dd", "mode": "on_demand"},
             {"unit": "op-sentry", "where": "OP Mac", "base": "http://op"},
             {"unit": "fpv-vision", "where": "HQ Mac", "base": None, "why": "not running (no heartbeat)"},
             {"unit": "acoustic-detector", "where": "Pi", "base": "http://dead"},
             {"unit": "mesh-bridge", "where": "Pi", "base": None, "why": "no web port known for its demo API"}]
    rows = demo.scenarios(tools, call=call)
    assert [r["unit"] for r in rows] == ["mesh-bridge", "op-sentry", "drone-detector", "acoustic-detector", "rf-spectrum", "fpv-vision"]
    assert [r["group"] for r in rows] == ["BFT", "OP", "Counter-drone", "Counter-drone", "Counter-drone", "FPV"]
    by = {r["unit"]: r for r in rows}
    assert [s["id"] for s in by["op-sentry"]["scenarios"]] == ["drone-on-bearing"] and by["op-sentry"]["active"] == ["drone-on-bearing"]
    assert by["op-sentry"]["mode"] == "on_demand" and by["op-sentry"]["title"] == "OP sentry"
    assert by["drone-detector"]["mode"] == "on_demand"                       # junk mode in the reply: the heartbeat's
    assert by["drone-detector"]["scenarios"][1]["duration_s"] is None
    assert by["rf-spectrum"]["reachable"] and by["rf-spectrum"]["has_demo"] is False and by["rf-spectrum"]["error"] == ""
    assert not by["acoustic-detector"]["reachable"] and "refused" in by["acoustic-detector"]["error"]   # offline: shown, not fatal
    assert not by["fpv-vision"]["reachable"] and by["fpv-vision"]["error"] == "not running (no heartbeat)"
    assert len(log) == 4                                                    # nothing asked of tools with no route


def test_trigger_passes_only_safe_options():
    log = []
    call = fake_call({"http://op": (200, {"ok": True, "id": "drone-on-bearing", "until": 123})}, log)
    t = {"unit": "op-sentry", "base": "http://op"}
    code, d = demo.trigger(t, {"tool": "op-sentry", "id": "drone-on-bearing", "bearing": 45, "clip": "air-01",
                               "Evil-Key": 1, "nested": {"a": 1}, "long": "x" * 200}, call=call)
    assert code == 200 and d["ok"] and log == [("http://op/api/demo/trigger", {"id": "drone-on-bearing", "bearing": 45, "clip": "air-01"})]
    assert demo.trigger(t, {"id": "../../x"}, call=call)[0] == 400
    assert demo.trigger({"unit": "fpv-vision", "base": None, "why": "not running"}, {"id": "x"}, call=call) == (502, {"error": "not running"})


def test_reset_calls_every_tool_even_when_one_fails():
    log = []
    call = fake_call({"http://a": (200, {"ok": True, "cleared": 3}), "http://b": (500, {"error": "boom"}),
                      "http://c": (200, {"ok": True, "cleared": 0})}, log)
    tools = [{"unit": "drone-detector", "base": "http://a"}, {"unit": "rf-spectrum", "base": "http://b"},
             {"unit": "op-sentry", "base": "http://c"}, {"unit": "fpv-vision", "base": None, "why": "not running"}]
    res = demo.clear(tools, call=call)
    assert sorted(u for u, _ in log) == ["http://a/api/demo/clear", "http://b/api/demo/clear", "http://c/api/demo/clear"]
    assert [(r["unit"], r["ok"]) for r in res] == [("drone-detector", True), ("rf-spectrum", False), ("op-sentry", True), ("fpv-vision", False)]
    assert res[0]["cleared"] == 3 and res[1]["error"] == "boom" and res[3]["error"] == "not running"


# ── through the launcher, against real fake tools ────────────────────────────

class FakeTool:
    def __init__(self, scenarios, no_api=False):
        self.calls = []
        tool = self

        class Hd(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                tool.calls.append(("GET", self.path, None))
                if no_api:
                    return self._out({"detail": "Not Found"}, 404)
                self._out({"mode": "on_demand", "scenarios": scenarios, "active": []})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                tool.calls.append(("POST", self.path, body))
                if no_api:
                    return self._out({"detail": "Not Found"}, 404)
                self._out({"ok": True, "id": body.get("id"), "until": 1} if self.path.endswith("trigger") else {"ok": True, "cleared": 2})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Hd)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


TAC = {}          # the panel's tac-advisor stand-in (no demo API), for the tests to inspect


@pytest.fixture
def panel(monkeypatch):
    dd = FakeTool([{"id": "inbound", "label": "Inbound drone", "duration_s": 60}, {"id": "swarm", "label": "Swarm"}])
    op = FakeTool([{"id": "drone-on-bearing", "label": "Drone on a bearing"}])
    ta = FakeTool([], no_api=True)                                    # tac-advisor: no demo API (404)
    TAC["tool"] = ta
    tools = [{"unit": "drone-detector", "where": "Pi", "base": dd.base},
             {"unit": "tac-advisor", "where": "Pi", "base": ta.base},
             {"unit": "op-sentry", "where": "OP Mac", "base": op.base},
             {"unit": "acoustic-detector", "where": "Pi", "base": "http://127.0.0.1:1"},          # offline
             {"unit": "fpv-vision", "where": "HQ Mac", "base": None, "why": "not running (no heartbeat)"}]
    monkeypatch.setattr(L, "demo_tools", lambda: tools)
    monkeypatch.setattr(L, "active_profile", lambda: "demo-step-mac")
    monkeypatch.setattr(L, "PROFILES_DIR", PROFILES)
    monkeypatch.setattr(L, "PIN", "")
    srv = L.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], dd, op
    srv.shutdown()
    for t in (dd, op, ta):
        t.srv.shutdown()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers or {})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_panel_aggregates_with_a_tool_offline(panel):
    port, dd, op = panel
    code, d = req(port, "GET", "/api/demo/scenarios")
    assert code == 200 and d["mode"] == "on_demand" and d["title"] == "Core demo (step by step)"
    by = {t["unit"]: t for t in d["tools"]}
    assert [s["id"] for s in by["drone-detector"]["scenarios"]] == ["inbound", "swarm"] and by["op-sentry"]["reachable"]
    assert not by["acoustic-detector"]["reachable"] and not by["fpv-vision"]["reachable"]
    assert by["tac-advisor"]["has_demo"] is False and by["tac-advisor"]["error"] == ""      # no API: not an error
    assert by["drone-detector"]["has_demo"] is True and by["acoustic-detector"]["has_demo"] is None
    assert [t["unit"] for t in d["tools"]] == ["op-sentry", "drone-detector", "acoustic-detector", "fpv-vision", "tac-advisor"]


def test_panel_trigger_and_reset(panel):
    port, dd, op = panel
    code, d = req(port, "POST", "/api/demo/trigger", {"tool": "op-sentry", "id": "drone-on-bearing", "bearing": 45}, H)
    assert code == 200 and d["ok"] and op.calls[-1] == ("POST", "/api/demo/trigger", {"id": "drone-on-bearing", "bearing": 45})
    assert req(port, "POST", "/api/demo/trigger", {"tool": "op-sentry", "id": "x"}, {"Content-Type": "application/json"})[0] == 403  # CSRF
    assert req(port, "POST", "/api/demo/trigger", {"tool": "change-detect", "id": "x"}, H)[0] == 404    # not in the profile
    assert req(port, "POST", "/api/demo/trigger", {"tool": "fpv-vision", "id": "x"}, H)[0] == 502       # no route
    code, d = req(port, "POST", "/api/demo/clear", {"all": True}, H)
    assert code == 200
    res = {r["unit"]: r for r in d["results"]}
    assert set(res) == {"drone-detector", "op-sentry", "acoustic-detector", "fpv-vision"}   # tac-advisor: no demo API
    assert res["drone-detector"]["ok"] and res["op-sentry"]["cleared"] == 2 and not res["acoustic-detector"]["ok"]
    assert not any(c[0] == "POST" for c in TAC["tool"].calls)                  # never sent a clear it would 404
    assert ("POST", "/api/demo/clear", {}) in dd.calls and ("POST", "/api/demo/clear", {}) in op.calls
    code, d = req(port, "POST", "/api/demo/clear", {"tool": "drone-detector"}, H)
    assert code == 200 and [r["unit"] for r in d["results"]] == ["drone-detector"]


def test_panel_needs_the_pin(panel, monkeypatch):
    port, dd, op = panel
    monkeypatch.setattr(L, "PIN", "4321")
    assert req(port, "POST", "/api/demo/trigger", {"tool": "op-sentry", "id": "drone-on-bearing"}, H)[0] == 401
    assert req(port, "POST", "/api/demo/trigger", {"tool": "op-sentry", "id": "drone-on-bearing"}, dict(H, **{"X-PIN": "4321"}))[0] == 200


def test_demo_tools_routes(real_profiles, monkeypatch):
    """Pi tools by loopback port (blufor-tracker by its heartbeat's demo_url), Mac tools via the relay, else the hotspot IP."""
    monkeypatch.setattr(L, "active_profile", lambda: "demo-step-mac")
    monkeypatch.setattr(L, "STATION_IPS", {"op-mac": "192.168.4.192"})
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/blufor-tracker", json.dumps({"host": "isr-pi", "url": "http://isr-pi.local/",   # the OTS UI
                                                                "demo_url": "http://isr-pi.local:8171/",
                                                                "status": "ok", "demo_mode": "on_demand"}).encode(), False)
    st.on_message("isrkit/heartbeat/op-sentry", json.dumps({"host": "op-mac", "status": "ok",
                                                           "url": "http://op-mac.local:8120/"}).encode(), False)
    monkeypatch.setattr(L, "heartbeats", st)

    class Relay:
        def local_url(self, port):
            return "http://127.0.0.1:8110" if port == 8110 else None
    monkeypatch.setattr(L, "tunnels", Relay())
    t = {x["unit"]: x for x in L.demo_tools()}
    assert t["drone-detector"]["base"] == "http://127.0.0.1:8060" and t["rf-spectrum"]["base"] == "http://127.0.0.1:8140"
    assert t["mesh-bridge"]["base"] == "http://127.0.0.1:8171" and t["mesh-bridge"]["mode"] == "on_demand"   # demo_url wins
    assert t["fpv-vision"]["base"] == "http://127.0.0.1:8110"                              # the HQ tunnel relay
    assert t["op-sentry"]["base"] == "http://192.168.4.192:8120"                           # no relay up: its hotspot address
    assert "opentakserver" not in t and "mesh-deadrop" not in t
    st.tools.clear()
    t = {x["unit"]: x for x in L.demo_tools()}
    assert t["op-sentry"]["base"] is None and "not running" in t["op-sentry"]["why"]
    assert t["mesh-bridge"]["base"] == "http://127.0.0.1:8170"                             # services.conf: blufor's DEMO_PORT


# ── pages ─────────────────────────────────────────────────────────────────────

def test_presenter_page_and_board_header():
    html = (REPO / "launcher" / "static" / "presenter.html").read_text()
    assert 'id="reset"' in html and "/presenter.js?v=" in html and "/common.js?v=" in html
    assert all(r.startswith("/") for r in __import__("re").findall(r'(?:src|href)="([^"]+)"', html))
    js = (REPO / "launcher" / "static" / "presenter.js").read_text()
    assert "/api/demo/trigger" in js and "/api/demo/clear" in js and "{ all: true }" in js and "Sent → watch the map" in js
    board = (REPO / "launcher" / "static" / "board.html").read_text()
    assert 'href="/presenter"' in board and 'id="mode"' in board
    assert '"Step by step" : "Continuous"' in (REPO / "launcher" / "static" / "board.js").read_text()
    assert "presenter.js" in L.STATIC_FILES


# ── the Macs follow the Pi's demo style ──────────────────────────────────────

class FakeStatus:
    def __init__(self):
        self.mode = "on_demand"
        fake = self

        class Hd(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                b = json.dumps({"demo_mode": fake.mode, "services": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Hd)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


def test_mac_tools_follow_the_pis_demo_style(tmp_path):
    fl = FakeStatus()
    conf, src = tmp_path / "conf", tmp_path / "src"
    (src / "op-sentry" / "scripts").mkdir(parents=True)
    conf.mkdir()
    out = tmp_path / "demo.out"
    (src / "op-sentry" / "scripts" / "run-laptop.sh").write_text(f'echo "mode=${{DEMO_MODE:-}}" > "{out}"\nsleep 30\n')
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal", ISR_LAUNCHER_PORT=str(fl.port), ISR_WIFI_KEEPALIVE="0")
    sh = str(REPO / "laptop" / "start-mac.sh")

    def mode(extra=""):
        if out.exists():
            out.unlink()
        (conf / "station.env").write_text(f"ISR_STATION=op\nISR_HOST=t\nISR_REPOS=op-sentry\nISR_SRC={src}\nISR_HUB=127.0.0.1\n{extra}")
        subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30)
        for _ in range(50):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.1)
        subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=60)
        return out.read_text().strip()
    try:
        assert mode() == "mode=on_demand"                               # the Pi's profile is step by step
        fl.mode = "continuous"
        assert mode() == "mode="                                        # continuous: the tool's own default
        fl.mode = "on_demand"
        assert mode("DEMO_MODE=continuous\n") == "mode=continuous"      # station.env pins it
        (conf / "op-sentry.env").write_text("DEMO_MODE=continuous\n")
        assert mode() == "mode=continuous"                              # so does the tool's own env file
    finally:
        fl.srv.shutdown()


# ── scenario options (op-sentry a6f7897: drone-on-bearing, bearing + clip) ────

OPSENTRY = {"mode": "on_demand", "active": [], "scenarios": [{
    "id": "drone-on-bearing", "label": "Drone on bearing", "hint": "Plays a drone clip on that bearing", "duration_s": 40,
    "options": {"bearing": {"type": "degrees", "default": 45},
                "clip": {"choices": [{"clip": "08", "label": "V-Bat"}, {"clip": "07", "label": "Quad"}, {"clip": "01", "label": "Air 01"}],
                         "default": "08"},
                "Bad Name": {"type": "text"}, "evil": "not a dict"}}]}


def test_scenario_options_cleaned():
    s = demo.clean_scenarios(OPSENTRY)["scenarios"][0]
    assert s["options"] == {
        "bearing": {"type": "degrees", "default": 45, "label": "bearing", "choices": []},
        "clip": {"type": "choice", "default": "08", "label": "clip",
                 "choices": [{"value": "08", "label": "V-Bat"}, {"value": "07", "label": "Quad"}, {"value": "01", "label": "Air 01"}]}}
    assert demo.clean_options({"n": {"type": "number", "default": True}})["n"]["default"] is None   # a bool isn't a number
    log = []
    call = fake_call({"http://op": (200, {"ok": True})}, log)
    demo.trigger({"unit": "op-sentry", "base": "http://op"}, {"tool": "op-sentry", "id": "drone-on-bearing", "bearing": 120, "clip": "07"}, call=call)
    assert log[-1][1] == {"id": "drone-on-bearing", "bearing": 120, "clip": "07"}


def test_heartbeat_demo_fields_top_level_first():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/op-sentry", json.dumps({"host": "m", "detail": {"demo_mode": "continuous"},
                                                           "demo_mode": "on_demand", "demo_active": ["drone-on-bearing"]}).encode(), False)
    e = st.snapshot()["op-sentry"]
    assert e["demo_mode"] == "on_demand" and e["demo_active"] == ["drone-on-bearing"]


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
def test_presenter_renders_options():
    common = (REPO / "launcher" / "static" / "common.js").read_text()
    js = (REPO / "launcher" / "static" / "presenter.js").read_text()
    js = js[:js.index("document.addEventListener(\"input\"")]           # the functions, no DOM wiring
    panel = {"tools": [{"unit": "op-sentry", "reachable": True, "scenarios": demo.clean_scenarios(OPSENTRY)["scenarios"], "active": []}]}
    code = ("global.document = { getElementById: () => null };\n" + common + js
            + "\nconsole.log(scenarioButton(" + json.dumps(panel["tools"][0]) + ", " + json.dumps(panel["tools"][0]["scenarios"][0]) + "));")
    p = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    html = p.stdout
    assert 'data-opt="bearing" type="number"' in html and 'value="45"' in html and 'max="359"' in html
    assert '<select' in html and '<option value="08" selected>V-Bat</option>' in html and 'value="07"' in html


# ── the pruned profile set ───────────────────────────────────────────────────

def test_switcher_hides_the_include_base_and_install_prunes():
    app = (REPO / "launcher" / "static" / "app.js").read_text()
    assert "st.profiles.filter((p) => !p.hidden || p.active)" in app
    inst = (REPO / "install.sh").read_text()
    assert 'if [ ! -f "$SRC/profiles.d/$(basename "$conf")" ]; then' in inst and 'rm -f "$conf"' in inst
    assert "DEFAULT_PROFILE=demo-full-mac" in inst and '"$INSTALL_DIR/switch.sh" "$DEFAULT_PROFILE"' in inst
    assert 'for fb in demo-full-mac bft' in (REPO / "switch.sh").read_text()


# ── Reset: only tools with a demo API (real-kit finding, acbba28) ────────────

def test_clear_all_skips_tools_without_a_demo_api():
    log = []
    call = fake_call({"http://dd/api/demo/scenarios": (200, {"scenarios": [{"id": "inbound"}]}),
                      "http://dd/api/demo/clear": (200, {"ok": True, "cleared": 4}),
                      "http://ta": (404, {"error": "Not Found"}), "http://cd": (404, {"error": "Not Found"})}, log)
    tools = [{"unit": "drone-detector", "base": "http://dd"}, {"unit": "tac-advisor", "base": "http://ta"},
             {"unit": "change-detect", "base": "http://cd"}, {"unit": "acoustic-detector", "base": "http://off"}]
    res = {r["unit"]: r for r in demo.clear_all(tools, call=call)}
    assert set(res) == {"drone-detector", "acoustic-detector"}
    assert res["drone-detector"]["ok"] and res["drone-detector"]["cleared"] == 4 and not res["acoustic-detector"]["ok"]
    posts = [u for u, b in log if b is not None]
    assert posts == ["http://dd/api/demo/clear"]                           # tac-advisor / change-detect never cleared


def test_presenter_lists_tools_without_a_demo_api_quietly():
    js = (REPO / "launcher" / "static" / "presenter.js").read_text()
    assert "has_demo === false" in js and "No demo buttons" in js and "(profile: " in js
    assert "/presenter.js?v=3" in (REPO / "launcher" / "static" / "presenter.html").read_text()


# ── after a switch into step by step ─────────────────────────────────────────

def test_after_switch_clears_once_each_tool_is_back_in_step_mode():
    a = demo.AfterSwitch()
    assert not a.observe("demo-full-mac", "continuous", 100)               # first look (launcher start): nothing
    assert not a.observe("demo-full-mac", "continuous", 105)
    assert a.observe("demo-step-mac", "on_demand", 110)                    # the switch
    replies = {"dd": "continuous", "op": "on_demand"}                     # drone-detector still the old process
    log = []

    def call(url, body=None, timeout=demo.TIMEOUT):
        log.append((url, body))
        host = url.split("/")[2]
        if host == "ta":
            return 404, {"error": "Not Found"}
        if url.endswith("/scenarios"):
            return 200, {"mode": replies[host], "scenarios": [{"id": "x"}]}
        return 200, {"ok": True, "cleared": 3}
    tools = [{"unit": "drone-detector", "base": "http://dd"}, {"unit": "op-sentry", "base": "http://op"},
             {"unit": "tac-advisor", "base": "http://ta"}, {"unit": "fpv-vision", "base": "http://fp"}]
    a.note_trigger("fpv-vision", 115)                                      # the presenter fired it since: hands off
    res = a.sweep(tools, 120, call=call)
    assert [r["unit"] for r in res] == ["op-sentry"]
    assert ("http://op/api/demo/clear", {"background": True}) in log and not any("fp/" in u for u, _ in log)
    replies["dd"] = "on_demand"                                            # it restarted
    log.clear()
    assert [r["unit"] for r in a.sweep(tools, 125, call=call)] == ["drone-detector"]
    assert not any(u.startswith("http://op") or u.startswith("http://ta") for u, _ in log)   # done once each
    assert a.sweep(tools, 130, call=call) == []
    assert a.observe("demo-step-mac", "on_demand", 140) is False           # no change: no new sweep
    a2 = demo.AfterSwitch()
    a2.observe("demo-full-mac", "continuous", 0)
    a2.observe("demo-step-mac", "on_demand", 1)
    assert a2.sweep(tools, 1 + demo.AfterSwitch.SETTLE_S + 1, call=call) == []   # gives up after SETTLE_S
    assert not a2.observe("demo-full-mac", "continuous", 300)              # into continuous: nothing to clear


def test_after_switch_tick_drops_old_sim_from_the_map(monkeypatch):
    import cop
    pic = cop.Picture(clock=lambda: 1000.0)
    monkeypatch.setattr(L, "picture", pic)
    for uid, mode, rec in (("SIM-1", "sim", 1), ("live-1", "live", 1)):
        pic.items[uid] = {"uid": uid, "mode": mode, "received": 900.0 + rec, "stale": 2000.0, "seq": 1}
    state = {"p": ("demo-full-mac", "continuous")}
    monkeypatch.setattr(L, "switch_in_progress", lambda: False)
    monkeypatch.setattr(L, "demo_mode_now", lambda: state["p"])
    monkeypatch.setattr(L, "demo_tools", lambda: [])
    monkeypatch.setattr(L, "after_switch", demo.AfterSwitch())
    L.after_switch_tick(950)
    assert set(pic.items) == {"SIM-1", "live-1"}                           # first look: untouched
    state["p"] = ("demo-step-mac", "on_demand")
    L.after_switch_tick(960)
    assert set(pic.items) == {"live-1"}                                    # old simulated CoT gone, real stays
    monkeypatch.setattr(L, "switch_in_progress", lambda: True)
    state["p"] = ("demo-full-mac", "continuous")
    L.after_switch_tick(970)                                               # mid-switch: wait
    assert L.after_switch.last == ("demo-step-mac", "on_demand")


def test_mac_follower_restarts_demo_tools_when_the_pi_switches(tmp_path):
    fl = FakeStatus()
    fl.mode = "continuous"
    conf, src = tmp_path / "conf", tmp_path / "src"
    out = tmp_path / "demo.out"
    for r in ("op-sentry", "tac-advisor"):
        (src / r / "scripts").mkdir(parents=True)
        (src / r / "scripts" / "run-laptop.sh").write_text(f'echo "{r} mode=${{DEMO_MODE:-}}" >> "{out}"\nsleep 60\n')
    conf.mkdir()
    (conf / "station.env").write_text(f"ISR_STATION=op\nISR_HOST=t\nISR_REPOS='op-sentry tac-advisor'\nISR_SRC={src}\nISR_HUB=127.0.0.1\n")
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal", ISR_LAUNCHER_PORT=str(fl.port), ISR_WIFI_KEEPALIVE="0", ISR_DEMO_FOLLOW_EVERY="1")
    sh = str(REPO / "laptop" / "start-mac.sh")

    def lines(n):
        for _ in range(100):
            got = out.read_text().splitlines() if out.exists() else []
            if len(got) >= n:
                return got
            time.sleep(0.1)
        return got
    try:
        subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30)
        assert sorted(lines(2)) == ["op-sentry mode=", "tac-advisor mode="]
        assert (tmp_path / "run" / "demo-follow.pid").exists()
        fl.mode = "on_demand"                                              # the Pi switches to step by step
        got = lines(3)
        assert got[2:] == ["op-sentry mode=on_demand"]                     # restarted in the new mode, only the demo tool
        time.sleep(2.5)
        assert len(out.read_text().splitlines()) == 3                      # and only once
        assert "restarting it" in (tmp_path / "logs" / "demo-follow.log").read_text()
    finally:
        r = subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=90)
        fl.srv.shutdown()
    assert not (tmp_path / "run" / "demo-follow.pid").exists() and not (tmp_path / "run" / "op-sentry.mode").exists(), r.stdout
