"""Launcher tests. Run on a Mac or the Pi:  python3 -m pytest tests/ (or: uv run --with pytest pytest tests/)"""
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
import isr_launcher as L  # noqa: E402

LSUSB = """\
Bus 004 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 003: ID 10c4:ea60 Silicon Labs CP210x UART Bridge
Bus 001 Device 004: ID 0bda:8812 Realtek Semiconductor Corp. RTL8812AU 802.11a/b/g/n/ac 2T2R DB WLAN Adapter
Bus 001 Device 005: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T
Bus 001 Device 006: ID 534d:2109 MacroSilicon USB Video
Bus 001 Device 007: ID 2e1a:4c02 Arashi Vision Insta360 X3
"""


class FakeRun:
    """Stands in for L.run: records calls, answers from a table."""

    def __init__(self, states=None):
        self.calls = []
        self.states = states or {}
        self.helper_rc = 0
        self.switching = False

    def __call__(self, cmd, timeout=5.0):
        self.calls.append(list(cmd))
        if cmd[0] == "lsusb":
            return 0, LSUSB
        if cmd[0] == "vcgencmd":
            return 0, "throttled=0x50000\n"
        if cmd[0] in ("rpicam-hello", "libcamera-hello"):
            return 127, ""
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, ("active\n" if self.switching else "inactive\n")
        if cmd[:2] == ["systemctl", "show"]:
            units = cmd[cmd.index("--") + 1:]
            blocks = []
            for u in units:
                name = u[:-8] if u.endswith(".service") else u
                st = self.states.get(name)
                if st is None:
                    blocks.append(f"Id={u}\nLoadState=not-found\nActiveState=inactive\nSubState=dead")
                else:
                    blocks.append(f"Id={u}\nLoadState=loaded\nActiveState={st}\nSubState=running")
            return 0, "\n\n".join(blocks) + "\n"
        if cmd[0] == "sudo":
            return self.helper_rc, ""
        if cmd[0] == L.SWITCH_SH:
            return subprocess_run(cmd)
        return 127, ""


def subprocess_run(cmd):
    p = subprocess.run(["bash"] + cmd, capture_output=True, text=True)
    return p.returncode, p.stdout


@pytest.fixture
def kit(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "profiles.d").mkdir(parents=True)
    shutil.copy(REPO / "services.conf", root / "services.conf")
    for p in (REPO / "profiles.d").glob("*.conf"):
        shutil.copy(p, root / "profiles.d" / p.name)
    # test-only profiles (the shipped set is three demo profiles plus bft)
    (root / "profiles.d" / "t-threat.conf").write_text(
        "# Counter-UAS segment\n# @title Drone threat (test)\n# @devices heltec-v3, usb-mic\n# @optional alfa-wifi, rtl-sdr\n"
        "include bft\ndrone-detector\nacoustic-detector\nrf-spectrum\n")
    (root / "profiles.d" / "t-op.conf").write_text("# @title OP (test)\ninclude bft\nop-sentry\ntac-advisor\n")
    (root / "active").write_text("t-threat\n")
    (root / "last-switch").write_text("profile=t-threat\nresult=partial\nmissing=rf-spectrum\nfailed=\n")
    monkeypatch.setenv("PI_PROFILES_ROOT", str(root))
    monkeypatch.setattr(L, "PROFILES_DIR", root / "profiles.d")
    monkeypatch.setattr(L, "SERVICES_CONF", root / "services.conf")
    monkeypatch.setattr(L, "ACTIVE_FILE", root / "active")
    monkeypatch.setattr(L, "STATE_FILE", root / "last-switch")
    monkeypatch.setattr(L, "SWITCH_SH", str(REPO / "switch.sh"))
    monkeypatch.setattr(L, "SWITCH_LOG", tmp_path / "switch.log")
    monkeypatch.setattr(L, "PIN", "")
    monkeypatch.setattr(L, "PIN_FOR_VIEW", False)
    monkeypatch.setattr(L, "internet", lambda: {"online": False, "gemini_reachable": False, "mode": "offline"})
    monkeypatch.setattr(L, "heartbeats", L.HeartbeatStore())
    monkeypatch.setattr(L, "probes", L.Probes())
    monkeypatch.setattr(L, "pin_guard", L.PinGuard())
    fake = FakeRun({"opentakserver": "active", "mesh-bridge": "active", "isr-launcher": "active",
                    "drone-detector": "active", "acoustic-detector": "failed"})
    monkeypatch.setattr(L, "run", fake)
    L.cache.clear()
    yield fake
    L.cache.clear()


@pytest.fixture
def server(kit):
    srv = L.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], kit
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    c.request(method, path, body=data, headers=headers or {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode()


SW_HEADERS = {"X-Requested-With": "isr-launcher", "Content-Type": "application/json"}


# ── pure functions ───────────────────────────────────────────────────────────

def test_parse_lsusb_classifies_kit_devices():
    kinds = {d["kind"] for d in L.parse_lsusb(LSUSB)}
    assert {"heltec-v3", "alfa-wifi", "rtl-sdr", "capture-card", "360-camera"} <= kinds
    root_hub = [d for d in L.parse_lsusb(LSUSB) if d["id"] == "1d6b:0003"][0]
    assert root_hub["kind"] is None


def test_decode_throttled():
    assert L.decode_throttled(0) == {"raw": "0x0", "flags": [], "ok": True}
    d = L.decode_throttled(0x50005)
    assert d["ok"] is False
    assert "under-voltage now" in d["flags"] and "throttling has occurred" in d["flags"]
    assert L.decode_throttled(None)["ok"] is None


def test_catalog_matches_contract_ports(kit):
    ports = {c["unit"]: c["port"] for c in L.load_catalog()}
    assert ports == {
        "isr-launcher": 8050, "opentakserver": 80, "mesh-bridge": 8170, "mesh-deadrop": 8070,
        "drone-detector": 8060, "tac-advisor": 8090, "acoustic-detector": 8100, "fpv-vision": 8110,
        "op-sentry": 8120, "op-sentry-stream": 8120, "change-detect": 8130, "rf-spectrum": 8140,
        "photoAnalysis": 8150, "isr-maps": 8160,
    }
    cams = [c["unit"] for c in L.load_catalog() if c["class"] == "camera"]
    assert sorted(cams) == ["fpv-vision", "op-sentry", "op-sentry-stream"]


def test_profiles_metadata(kit):
    profs = {p["name"]: p for p in L.list_profiles()}
    shipped = {n for n in profs if not n.startswith("t-")}
    assert shipped == {"bft", "demo-full", "demo-full-mac", "demo-step-mac"}          # the 2026-11 prune
    assert profs["bft"]["hidden"] and not any(profs[n]["hidden"] for n in shipped - {"bft"})
    assert profs["demo-full"]["title"] == "Pi only (fallback: no Macs)" and not profs["demo-full"]["remote"]
    assert profs["demo-full-mac"]["title"] == "Core demo (Pi + 2 Macs)"
    assert profs["demo-full-mac"]["devices"] == ["heltec-v3", "usb-mic"]
    assert profs["demo-full-mac"]["optional"] == ["alfa-wifi", "rtl-sdr"]
    remote = {(r["tool"], r["station"]) for r in profs["demo-full-mac"]["remote"]}
    assert remote == {("fpv-vision", "hq"), ("op-sentry", "op"), ("change-detect", "hq"),   # the demo layout
                      ("photoAnalysis", "hq")}
    assert profs["demo-step-mac"]["remote"] == profs["demo-full-mac"]["remote"]


def test_profile_units_uses_switch_sh_includes(kit):
    assert L.profile_units("t-op") == ["opentakserver", "mesh-bridge", "op-sentry", "tac-advisor"]
    assert L.profile_units("../etc") == []
    assert L.profile_units("nope") == []


def test_unit_states_handles_missing_units(kit):
    st = L.unit_states(["opentakserver", "rf-spectrum"])
    assert st["opentakserver"] == {"installed": True, "active": "active", "sub": "running"}
    assert st["rf-spectrum"]["installed"] is False


def test_build_status_shape(kit):
    s = L.build_status()
    assert s["active"] == "t-threat"
    units = {x["unit"]: x for x in s["services"]}
    assert units["drone-detector"]["in_profile"] and units["drone-detector"]["active"] == "active"
    assert units["acoustic-detector"]["active"] == "failed"
    assert units["rf-spectrum"]["installed"] is False
    assert units["isr-launcher"]["in_profile"]          # always-on
    assert units["fpv-vision"]["in_profile"] is False
    dt = [p for p in s["profiles"] if p["name"] == "t-threat"][0]
    # usb-mic is not in the fake lsusb and there is no /proc/asound on a Mac
    assert dt["active"] and "usb-mic" in dt["missing_devices"] and "rtl-sdr" not in dt["missing_devices"]
    assert s["last_switch"]["result"] == "partial"
    assert s["health"]["throttled"]["flags"]


# ── HTTP ─────────────────────────────────────────────────────────────────────

def test_index_and_status(server):
    port, _ = server
    code, html = req(port, "GET", "/")
    assert code == 200 and "ISTAR kit" in html and "/board.js" in html          # mission board
    code, html = req(port, "GET", "/engineer")
    assert code == 200 and "Engineer" in html and "/app.js" in html            # engineer view
    code, js = req(port, "GET", "/app.js")
    assert code == 200 and "doSwitch" in js
    code, st = req(port, "GET", "/api/status")
    assert code == 200 and st["active"] == "t-threat" and st["pin_required"] is False


def test_switch_happy_path_calls_only_the_helper(server):
    port, fake = server
    code, body = req(port, "POST", "/api/switch", {"profile": "demo-step-mac"}, SW_HEADERS)
    assert code == 202 and body["profile"] == "demo-step-mac"
    sudo = [c for c in fake.calls if c[0] == "sudo"]
    assert sudo == [["sudo", "-n", L.HELPER, "demo-step-mac"]]


@pytest.mark.parametrize("name", ["../../etc/passwd", "bft;reboot", "bft && id", "-h", "", "A" * 60, "BFT"])
def test_switch_rejects_bad_names(server, name):
    port, fake = server
    code, _ = req(port, "POST", "/api/switch", {"profile": name}, SW_HEADERS)
    assert code == 400
    assert not [c for c in fake.calls if c[0] == "sudo"]


def test_switch_unknown_profile_404(server):
    port, fake = server
    code, _ = req(port, "POST", "/api/switch", {"profile": "no-such-profile"}, SW_HEADERS)
    assert code == 404 and not [c for c in fake.calls if c[0] == "sudo"]


def test_switch_requires_csrf_header(server):
    port, fake = server
    code, _ = req(port, "POST", "/api/switch", {"profile": "bft"}, {"Content-Type": "application/json"})
    assert code == 403 and not [c for c in fake.calls if c[0] == "sudo"]


def test_switch_bad_bodies(server):
    port, _ = server
    assert req(port, "POST", "/api/switch", b"not json", SW_HEADERS)[0] == 400
    assert req(port, "POST", "/api/switch", b"[1,2]", SW_HEADERS)[0] == 400
    assert req(port, "POST", "/api/switch", b"x" * 5000, SW_HEADERS)[0] == 400
    assert req(port, "POST", "/api/other", {"profile": "bft"}, SW_HEADERS)[0] == 404


def test_switch_conflict_while_switching(server):
    port, fake = server
    fake.switching = True
    code, _ = req(port, "POST", "/api/switch", {"profile": "bft"}, SW_HEADERS)
    assert code == 409


def test_helper_failure_reported(server):
    port, fake = server
    fake.helper_rc = 67
    code, body = req(port, "POST", "/api/switch", {"profile": "bft"}, SW_HEADERS)
    assert code == 500 and "camera" in body["error"]


def test_pin_and_lockout(server, monkeypatch):
    port, fake = server
    monkeypatch.setattr(L, "PIN", "4321")
    code, _ = req(port, "POST", "/api/switch", {"profile": "bft"}, SW_HEADERS)
    assert code == 401
    code, _ = req(port, "POST", "/api/switch", {"profile": "bft"}, dict(SW_HEADERS, **{"X-PIN": "4321"}))
    assert code == 202
    for _ in range(L.MAX_PIN_FAILS):
        assert req(port, "POST", "/api/switch", {"profile": "bft"}, dict(SW_HEADERS, **{"X-PIN": "0000"}))[0] == 401
    # locked out even with the right PIN now
    code, body = req(port, "POST", "/api/switch", {"profile": "bft"}, dict(SW_HEADERS, **{"X-PIN": "4321"}))
    assert code == 429
    assert len([c for c in fake.calls if c[0] == "sudo"]) == 1
    # status stays readable unless PIN_FOR_VIEW
    assert req(port, "GET", "/api/status")[0] == 200
    monkeypatch.setattr(L, "PIN_FOR_VIEW", True)
    monkeypatch.setattr(L, "pin_guard", L.PinGuard())
    assert req(port, "GET", "/api/status")[0] == 401
    assert req(port, "GET", "/api/status", headers={"X-PIN": "4321"})[0] == 200


def test_security_headers(server):
    port, _ = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/")
    r = c.getresponse()
    r.read()
    assert r.getheader("X-Frame-Options") == "DENY"
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    c.close()


# ── privileged helper and Gemini key helper (shell, run unprivileged here) ─────

def test_switch_helper_rejects_bad_names():
    helper = REPO / "bin" / "isr-switch-helper"
    for bad in ["../x", "a;b", "-rf", "", "UPPER"]:
        p = subprocess.run(["bash", str(helper), bad], capture_output=True, text=True)
        assert p.returncode == 65, (bad, p.returncode, p.stderr)
    p = subprocess.run(["bash", str(helper), "a", "b"], capture_output=True, text=True)
    assert p.returncode == 64
    p = subprocess.run(["bash", str(helper), "definitely-not-a-profile"], capture_output=True, text=True)
    assert p.returncode == 66


def test_set_gemini_key(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "tac-advisor.env").write_text("FOO=1\nGEMINI_API_KEY=old\nGEMINI_MODEL=keep-me\n")
    env = dict(os.environ, ISR_SKIP_ROOT_CHECK="1", ISR_ETC_DIR=str(etc), ISR_REPOS_CONF=str(REPO / "repos.conf"))
    tool = str(REPO / "bin" / "isr-set-gemini-key")
    p = subprocess.run(["bash", tool], input="AIzaTEST_key-123\n", capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "AIzaTEST" not in p.stdout + p.stderr
    repos = [l.split()[0] for l in (REPO / "repos.conf").read_text().splitlines()
             if l.strip() and not l.startswith("#") and l.split()[3:4] != ["mac"]]
    assert not (etc / "photoAnalysis.env").exists()                       # Mac-only repo: no /etc env on the Pi
    for r in repos:
        f = etc / f"{r}.env"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert "GEMINI_API_KEY=AIzaTEST_key-123" in f.read_text().splitlines()
    ta = (etc / "tac-advisor.env").read_text().splitlines()
    assert "FOO=1" in ta and "GEMINI_MODEL=keep-me" in ta and "GEMINI_API_KEY=old" not in ta
    # --model replaces the model line; a second run does not duplicate the key
    p = subprocess.run(["bash", tool, "--model", "gemini-3.6-flash"], input="AIzaNEW\n",
                       capture_output=True, text=True, env=env)
    ta = (etc / "tac-advisor.env").read_text().splitlines()
    assert ta.count("GEMINI_API_KEY=AIzaNEW") == 1 and "GEMINI_MODEL=gemini-3.6-flash" in ta
    assert "GEMINI_MODEL=keep-me" not in ta
    # junk is refused (no shell/env injection into env files)
    p = subprocess.run(["bash", tool], input="abc\nEVIL=1\n", capture_output=True, text=True, env=env)
    assert "EVIL" not in (etc / "tac-advisor.env").read_text()
    p = subprocess.run(["bash", tool], input="bad key$(id)\n", capture_output=True, text=True, env=env)
    assert p.returncode == 1
    assert (etc / "isr-kit" / "gemini.configured").exists()
    assert "AIza" not in (etc / "isr-kit" / "gemini.configured").read_text()
    p = subprocess.run(["bash", tool, "--clear"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "GEMINI_API_KEY" not in (etc / "tac-advisor.env").read_text()
    assert not (etc / "isr-kit" / "gemini.configured").exists()


def test_set_site(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "op-sentry.env").write_text("FOO=1\nSENSOR_LAT=0\nSENSOR_LON=0\nGEMINI_API_KEY=k\n")
    env = dict(os.environ, ISR_SKIP_ROOT_CHECK="1", ISR_ETC_DIR=str(etc), ISR_REPOS_CONF=str(REPO / "repos.conf"))
    tool = str(REPO / "bin" / "isr-set-site")
    for bad in ["0,0", "91,1", "abc,1", "51.1;rm,1"]:
        assert subprocess.run(["bash", tool, "--sensor", bad], capture_output=True, text=True, env=env).returncode == 64
    p = subprocess.run(["bash", tool, "--sensor", "51.0000,-1.0000"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    site = (etc / "isr-kit" / "site.env").read_text()
    assert "ISR_HOST=isr-pi" in site and "SENSOR_LAT=51.0000" in site and "SENSOR_LON=-1.0000" in site
    os_env = (etc / "op-sentry.env").read_text().splitlines()
    assert "FOO=1" in os_env and "GEMINI_API_KEY=k" in os_env and "SENSOR_LAT=51.0000" in os_env
    assert "SENSOR_LAT=0" not in os_env and stat.S_IMODE((etc / "op-sentry.env").stat().st_mode) == 0o600
    assert (etc / "rf-spectrum.env").exists()        # created for every repo in repos.conf
    p = subprocess.run(["bash", tool, "--host", "isr-hub", "--sync"], capture_output=True, text=True, env=env)
    assert "SENSOR_LAT=51.0000" in (etc / "isr-kit" / "site.env").read_text()   # kept on re-sync
    assert "ISR_HOST=isr-hub" in (etc / "rf-spectrum.env").read_text()


# ── heartbeats, MQTT client, Ollama probe ─────────────────────────────────────

import socket as _socket
import struct as _struct
import time as _time
from http.server import BaseHTTPRequestHandler as _BH, ThreadingHTTPServer as _TS

import mqtt_mini as M  # noqa: E402


def hb(tool="fpv-vision", host="isr-droneops", **kw):
    d = {"tool": tool, "host": host, "url": f"http://{host}.local:8110/", "mode": "mps", "status": "ok", "ts": 1}
    d.update(kw)
    return json.dumps(d).encode()


def test_heartbeat_states():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/fpv-vision", hb(), retained=False, now=100.0)
    assert st.snapshot(now=110)["fpv-vision"]["state"] == "live"
    assert st.snapshot(now=140)["fpv-vision"]["state"] == "stale"
    assert st.snapshot(now=300)["fpv-vision"]["state"] == "offline"
    e = st.snapshot(now=110)["fpv-vision"]
    assert e["station"] == "droneops" and e["station_label"] == "Drone-ops Mac" and e["mode"] == "mps"


def test_cleared_retained_heartbeat_removes_tool():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/old", hb("old"), retained=True, now=1)
    st.on_message("isrkit/heartbeat/old", b"", retained=True, now=2)
    assert "old" not in st.snapshot(now=3)


def test_retained_heartbeat_is_never_fresh():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/op-sentry", hb("op-sentry"), retained=True, now=100.0)
    assert st.snapshot(now=100.5)["op-sentry"]["state"] == "offline"
    st.on_message("isrkit/heartbeat/op-sentry", hb("op-sentry"), retained=False, now=101.0)
    assert st.snapshot(now=102)["op-sentry"]["state"] == "live"


def test_heartbeat_sanitising():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/x", hb("x", url="javascript:alert(1)"), retained=False, now=1)
    st.on_message("isrkit/heartbeat/bad tool", hb(), retained=False, now=1)
    st.on_message("isrkit/heartbeat/y", b"not json", retained=False, now=1)
    st.on_message("isrkit/heartbeat/z", b"[1]", retained=False, now=1)
    st.on_message("other/topic", hb(), retained=False, now=1)
    st.on_message("isrkit/heartbeat/s", hb("s", status="stopped"), retained=False, now=1)
    snap = st.snapshot(now=2)
    assert set(snap) == {"x", "s"}
    assert snap["x"]["url"] == ""
    assert snap["s"]["state"] == "stopped"


def test_station_of():
    assert L.station_of("isr-hq") == "hq"
    assert L.station_of("isr-hq.local") == "hq"
    assert L.station_of("isr-droneops") == "droneops"
    assert L.station_of(L.HUB_NAME) == "pi"
    assert L.station_of("192.168.4.1") == "pi"
    assert L.station_of("my-laptop") == "my-laptop"
    # The OP Mac (remote sensor post): its announce/heartbeat role, or the isr-op name
    assert L.station_of("op-mac", "op") == "op" and L.STATION_LABELS["op"] == "OP Mac"
    assert L.station_of("isr-op") == "op" and L.station_of("isr-op.local") == "op" and L.station_of("kit-op") == "op"
    assert L.station_of("ops-mac") == "droneops" and L.station_of("desktop") == "desktop"
    L.ANNOUNCED["some-mac"] = "op"
    try:
        assert L.station_of("some-mac") == "op"
    finally:
        L.ANNOUNCED.pop("some-mac", None)


def test_status_remote_section(kit):
    (L.PROFILES_DIR.parent / "active").write_text("demo-full-mac\n")
    L.heartbeats.on_message("isrkit/heartbeat/fpv-vision", hb(), retained=False)
    L.heartbeats.on_message("isrkit/heartbeat/drone-detector", hb("drone-detector", host=L.HUB_NAME, mode="simulate"),
                            retained=False)
    s = L.build_status()
    rem = {r["tool"]: r for r in s["remote"]}
    assert set(rem) == {"fpv-vision", "op-sentry", "change-detect", "photoAnalysis"}
    assert rem["photoAnalysis"]["title"].startswith("Geo-locate") and rem["photoAnalysis"]["expected_label"] == "HQ Mac"
    assert "photoAnalysis" not in {x["unit"] for x in s["services"]}      # Mac-only: never a Pi service row
    assert rem["fpv-vision"]["heartbeat"]["state"] == "live"
    assert rem["op-sentry"]["heartbeat"] is None and rem["op-sentry"]["expected_label"] == "OP Mac"
    assert rem["fpv-vision"]["expected_label"] == "HQ Mac"
    dd = [x for x in s["services"] if x["unit"] == "drone-detector"][0]
    assert dd["heartbeat"]["mode"] == "simulate"
    assert s["ai_tier"] == "offline/template"


def test_repo_named_heartbeat_maps_to_unit(kit):
    L.heartbeats.on_message("isrkit/heartbeat/blufor-tracker", hb("blufor-tracker", host=L.HUB_NAME, mode="simulate"),
                            retained=False)
    s = L.build_status()
    mb = [x for x in s["services"] if x["unit"] == "mesh-bridge"][0]
    assert mb["heartbeat"]["tool"] == "blufor-tracker"
    assert "blufor-tracker" not in {r["tool"] for r in s["remote"]}
    L.heartbeats.on_message("isrkit/heartbeat/mystery-tool", hb("mystery-tool", host=L.HUB_NAME), retained=False)
    assert "mystery-tool" in {r["tool"] for r in L.build_status()["remote"]}   # unknown Pi tools still shown


def test_topic_matches():
    assert M.topic_matches("isrkit/heartbeat/+", "isrkit/heartbeat/op-sentry")
    assert not M.topic_matches("isrkit/heartbeat/+", "isrkit/heartbeat/a/b")
    assert M.topic_matches("meshbus/#", "meshbus/tx")
    assert not M.topic_matches("meshbus/#", "isrkit/x")


class FakeBroker:
    """Just enough of an MQTT broker: CONNACK (optionally refusing), SUBACK, then one
    retained and one live PUBLISH; records what the client publishes."""

    def __init__(self, refuse_code=0):
        self.srv = _socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.refuse = refuse_code
        self.got = []
        self.login = None
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        conn, _ = self.srv.accept()
        c = M.MiniMQTT("x")
        c.sock = conn
        ptype, _, body = c.read_packet()
        assert ptype == M.CONNECT
        self.login = body
        conn.sendall(M.packet(M.CONNACK, 0, bytes([0, self.refuse])))
        if self.refuse:
            conn.close()
            return
        conn.settimeout(3)
        try:
            while True:
                ptype, flags, body = c.read_packet()
                if ptype == M.SUBSCRIBE:
                    conn.sendall(M.packet(M.SUBACK, 0, body[:2] + b"\x00"))
                    rp = M.publish_packet("isrkit/heartbeat/op-sentry", hb("op-sentry"), retain=True)
                    # split a packet across two sends to exercise partial reads
                    conn.sendall(rp[:5]); _time.sleep(0.05); conn.sendall(rp[5:])
                    conn.sendall(M.publish_packet("isrkit/heartbeat/fpv-vision", hb()))
                elif ptype == M.PUBLISH:
                    self.got.append(M.parse_publish(flags, body))
        except (OSError, M.MQTTError):
            pass


def test_mini_mqtt_roundtrip():
    b = FakeBroker()
    c = M.MiniMQTT("127.0.0.1", b.port, "isrkit", "s3cret", client_id="t")
    c.connect()
    assert b"isrkit" in b.login and b"s3cret" in b.login
    c.subscribe("isrkit/heartbeat/+")
    msgs = []
    deadline = _time.monotonic() + 3

    def tick():
        if len(msgs) >= 2 or _time.monotonic() > deadline:
            raise M.MQTTError("done")
        c.publish("isrkit/heartbeat/isr-launcher", b"{}", retain=True)

    with pytest.raises(M.MQTTError):
        c.loop(lambda t, p, r: msgs.append((t, r)), tick, tick_every=0.2)
    c.close()
    assert ("isrkit/heartbeat/op-sentry", True) in msgs and ("isrkit/heartbeat/fpv-vision", False) in msgs
    _time.sleep(0.1)
    assert any(t == "isrkit/heartbeat/isr-launcher" and r for t, _, r in b.got)


def test_check_login_reports_bad_password():
    b = FakeBroker(refuse_code=4)
    assert M.check_login("127.0.0.1", b.port, "isrkit", "wrong") == "bad username or password"
    assert M.check_login("127.0.0.1", 1, "u", "p") is not None   # nothing listening


class _Ollama(_BH):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": "qwen3.5:27b"}, {"name": "qwen3-vl:8b-instruct"}]}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_probe_ollama():
    srv = _TS(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = L.probe_ollama(base, "qwen3-vl:8b-instruct")
        assert r["reachable"] and r["model_present"] is True
        assert L.probe_ollama(base + "/v1", "qwen3.5:27b")["model_present"] is True
        assert L.probe_ollama(base, "llama9:400b")["model_present"] is False
    finally:
        srv.shutdown()
    assert L.probe_ollama("http://127.0.0.1:1", "x")["reachable"] is False
    assert L.probe_ollama("", "x") == {"configured": False}
    assert L.probe_ollama("file:///etc/passwd")["reachable"] is False


def test_ai_ladder():
    vlm_ok = {"vlm": {"reachable": True, "model_present": True}, "internet": {"gemini_reachable": True}}
    assert L.ai_ladder(vlm_ok) == "Mac VLM"
    net = {"vlm": {"reachable": False}, "internet": {"gemini_reachable": True}}
    assert L.ai_ladder(net, gemini_key=True) == "Gemini"
    assert L.ai_ladder(net, gemini_key=False) == "offline/template"    # reachable but no key: not Gemini
    assert L.ai_ladder({"vlm": {"reachable": True, "model_present": False}, "internet": {}}, gemini_key=True) == "offline/template"


# ── Mac station setup (runs only on macOS) ───────────────────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_setup_mac_writes_station_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    pw = tmp_path / "mqtt.env"
    pw.write_text("MQTT_USER=isrkit\nMQTT_PASS=abcDEF123\n")
    # Never touch the real Mac's services: --skip-ollama, and brew/launchctl/ollama/osascript/open/sudo
    # stubs that only log, so a call is visible and harmless.
    fake, calls = tmp_path / "fakebin", tmp_path / "calls.log"
    fake.mkdir()
    for c in ("brew", "launchctl", "ollama", "osascript", "open", "sudo"):
        (fake / c).write_text(f'#!/bin/sh\necho "{c} $*" >> "{calls}"\nexit 1\n')
        (fake / c).chmod(0o755)
    env = dict(os.environ, HOME=str(home), ISR_MAC_SRC=str(tmp_path / "src"), PATH=f"{fake}:{os.environ['PATH']}")
    env.pop("ISR_ALLOW_SERVICE_CHANGES", None)
    p = subprocess.run(["bash", str(REPO / "laptop" / "setup-mac.sh"), "--role", "droneops", "--hub", "127.0.0.1",
                        "--hq-host", "127.0.0.1", "--mqtt-pass-file", str(pw), "--skip-brew", "--skip-clone", "--skip-camera-probe",
                        "--skip-prefetch", "--skip-ollama", "--sensor", "51.0000,-1.0000"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    f = home / ".config" / "isr-kit" / "station.env"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    kv = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l and not l.startswith("#"))
    assert kv["ISR_STATION"] == "droneops" and kv["MQTT_USER"] == "isrkit" and kv["MQTT_PASS"] == "abcDEF123"
    assert kv["TAK_HOST"] == "127.0.0.1" and kv["TAK_PORT"] == "8087" and kv["MQTT_PORT"] == "1883"
    assert kv["VLM_URL"] == "http://127.0.0.1:11434" and kv["ISR_REPOS"] == '"fpv-vision op-sentry"'
    assert "abcDEF123" not in p.stdout
    assert kv["SENSOR_LAT"] == "51.0000" and kv["SENSOR_LON"] == "-1.0000" and kv["ISR_HOST"] == kv["ISR_HOST"].lower()
    bad = subprocess.run(["bash", str(REPO / "laptop" / "setup-mac.sh")], capture_output=True, text=True, env=env)
    assert bad.returncode != 0 and "--role" in bad.stderr
    assert not calls.exists(), calls.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_setup_mac_never_touches_services_under_a_fake_home(tmp_path):
    """Even without --skip-ollama, a fake HOME (any test) must not reach brew services, launchctl or Ollama."""
    home = tmp_path / "home"
    home.mkdir()
    pw = tmp_path / "mqtt.env"
    pw.write_text("MQTT_USER=isrkit\nMQTT_PASS=abcDEF123\n")
    fake, calls = tmp_path / "fakebin", tmp_path / "calls.log"
    fake.mkdir()
    for c in ("brew", "launchctl", "ollama", "osascript", "open", "sudo"):
        (fake / c).write_text(f'#!/bin/sh\necho "{c} $*" >> "{calls}"\nexit 1\n')
        (fake / c).chmod(0o755)
    env = dict(os.environ, HOME=str(home), ISR_MAC_SRC=str(tmp_path / "src"), PATH=f"{fake}:{os.environ['PATH']}")
    env.pop("ISR_ALLOW_SERVICE_CHANGES", None)
    for role in ("droneops", "hq"):
        p = subprocess.run(["bash", str(REPO / "laptop" / "setup-mac.sh"), "--role", role, "--hub", "127.0.0.1",
                            "--hq-host", "127.0.0.1", "--mqtt-pass-file", str(pw), "--skip-brew", "--skip-clone",
                            "--skip-camera-probe", "--skip-prefetch", "--sensor", "51.0000,-1.0000"],
                           capture_output=True, text=True, env=env, timeout=60)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "leaving Ollama, brew services and launchctl alone" in p.stdout + p.stderr
    assert not calls.exists(), calls.read_text()
    assert not (home / "Library" / "LaunchAgents").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_start_mac_runs_run_laptop_with_env(tmp_path):
    conf, src = tmp_path / "conf", tmp_path / "src"
    (src / "fpv-vision" / "scripts").mkdir(parents=True)
    out = tmp_path / "seen.env"
    (src / "fpv-vision" / "scripts" / "run-laptop.sh").write_text(
        f'#!/bin/bash\nenv | grep -E "^(TAK_HOST|MQTT_USER|VLM_URL|EXTRA)=" | sort > "{out}"\n'
        'trap "exit 0" TERM\nwhile true; do sleep 0.2; done\n')
    conf.mkdir()
    (conf / "station.env").write_text(
        f'ISR_STATION=droneops\nISR_HOST=t\nISR_REPOS="fpv-vision op-sentry"\nISR_SRC={src}\nISR_HUB=isr-pi.local\n'
        'TAK_HOST=isr-pi.local\nMQTT_USER=isrkit\nVLM_URL=http://isr-hq.local:11434\nLLM_URL=http://127.0.0.1:1\n')
    (conf / "fpv-vision.env").write_text("EXTRA=1\n")
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"))
    tool = str(REPO / "laptop" / "start-mac.sh")
    try:
        p = subprocess.run(["bash", tool, "start"], capture_output=True, text=True, env=env, timeout=30)
        assert "fpv-vision: started" in p.stdout and "op-sentry: MISSING" in p.stdout
        for _ in range(50):
            if out.exists() and out.read_text():
                break
            _time.sleep(0.1)
        assert out.read_text().split() == ["EXTRA=1", "MQTT_USER=isrkit", "TAK_HOST=isr-pi.local",
                                           "VLM_URL=http://isr-hq.local:11434"]
        p = subprocess.run(["bash", tool, "status"], capture_output=True, text=True, env=env, timeout=30)
        assert "fpv-vision: running" in p.stdout and "unreachable" in p.stdout
    finally:
        p = subprocess.run(["bash", tool, "stop"], capture_output=True, text=True, env=env, timeout=60)
    assert "fpv-vision: stopped" in p.stdout
    assert not (tmp_path / "run" / "fpv-vision.pid").exists()


# ── power check, mesh channel QR ─────────────────────────────────────────────

import shutil as _shutil  # noqa: E402

import qr as Q  # noqa: E402


def _dt(tmp_path, ma, override=None):
    d = tmp_path / "power"
    d.mkdir(exist_ok=True)
    (d / "max_current").write_bytes(int(ma).to_bytes(4, "big"))
    if override is not None:
        (d / "usb_max_current_enable").write_bytes(int(override).to_bytes(4, "big"))
    return d


def test_power_state(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "run", lambda cmd, timeout=5: (127, ""))
    monkeypatch.setattr(L, "POWER_DT", _dt(tmp_path, 5000))
    assert L.power_state()["level"] == "ok" and L.power_state()["usb_limited"] is False
    (tmp_path / "a").mkdir()
    monkeypatch.setattr(L, "POWER_DT", _dt(tmp_path / "a", 3000))
    st = L.power_state()
    assert st["level"] == "bad" and st["usb_limited"] is True and "CURRENT-LIMITED" in st["verdict"] and "3.0 A" in st["verdict"]
    monkeypatch.setattr(L, "POWER_DT", _dt(tmp_path / "a", 3000, override=1))
    assert L.power_state()["level"] == "warn"
    monkeypatch.setattr(L, "POWER_DT", tmp_path / "missing")
    assert L.power_state()["level"] == "unknown"


CH_URL = "https://meshtastic.org/e/#CgMSAQESDAgBOAFAA0gBUBtoAQ"


@pytest.fixture
def channel_file(tmp_path, monkeypatch):
    f = tmp_path / "mesh-channel.url"
    f.write_text(CH_URL + "\n")
    monkeypatch.setattr(L, "CHANNEL_FILES", [tmp_path / "absent", f])
    return f


def test_channel_needs_pin_configured(server, channel_file):
    port, _ = server
    code, body = req(port, "POST", "/api/channel", b"{}", SW_HEADERS)
    assert code == 403 and "LAUNCHER_PIN" in body["error"]
    assert CH_URL not in json.dumps(req(port, "GET", "/api/status")[1])   # status never leaks it


def test_channel_pin_gated(server, channel_file, monkeypatch):
    port, _ = server
    monkeypatch.setattr(L, "PIN", "2468")
    assert req(port, "POST", "/api/channel", b"{}", SW_HEADERS)[0] == 401
    assert req(port, "POST", "/api/channel", b"{}", {"X-PIN": "2468", "Content-Type": "application/json"})[0] == 403
    code, body = req(port, "POST", "/api/channel", b"{}", dict(SW_HEADERS, **{"X-PIN": "2468"}))
    assert code == 200 and body["url"] == CH_URL and body["svg"].startswith("<svg")
    assert req(port, "GET", "/api/status")[1]["channel_available"] is True


def test_channel_rejects_junk(tmp_path, monkeypatch):
    f = tmp_path / "c"
    for junk in ["javascript:alert(1)", "https://evil.example/e/#abc", "https://meshtastic.org/e/#<script>"]:
        f.write_text(junk)
        monkeypatch.setattr(L, "CHANNEL_FILES", [f])
        assert L.mesh_channel() is None


def test_qr_structure():
    q = Q.QR(CH_URL.encode(), "M")
    assert q.size == q.version * 4 + 17
    # finder patterns in three corners, timing pattern alternates
    for x0, y0 in ((0, 0), (q.size - 7, 0), (0, q.size - 7)):
        assert all(q.m[y0][x0 + i] for i in range(7)) and not q.m[y0 + 1][x0 + 1] and q.m[y0 + 3][x0 + 3]
    assert [q.m[6][x] for x in range(8, q.size - 8)] == [x % 2 == 0 for x in range(8, q.size - 8)]
    with pytest.raises(ValueError):
        Q.QR(b"x" * 3000, "H")


@pytest.mark.skipif(sys.platform != "darwin" or not _shutil.which("swift"), reason="needs macOS Vision")
def test_qr_decodes_with_macos_vision(tmp_path):
    import random
    import string
    import struct as st
    import zlib

    def png(q, path, scale=6, border=4):
        n = q.size + 2 * border
        w = n * scale
        rows = []
        for yy in range(w):
            y = yy // scale - border
            row = bytearray([0])
            for xx in range(w):
                x = xx // scale - border
                row.append(0 if (0 <= x < q.size and 0 <= y < q.size and q.m[y][x]) else 255)
            rows.append(bytes(row))

        def chunk(t, d):
            return st.pack(">I", len(d)) + t + d + st.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", st.pack(">IIBBBBB", w, w, 8, 0, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))

    random.seed(7)
    cases = []
    for i, n in enumerate([10, 60, 150, 300]):
        t = "https://meshtastic.org/e/#" + "".join(random.choice(string.ascii_letters + "-_") for _ in range(n))
        p = tmp_path / f"q{i}.png"
        png(Q.QR(t.encode(), "M"), p)
        cases.append(f"{p}\t{t}")
    (tmp_path / "cases.tsv").write_text("\n".join(cases))
    swift = tmp_path / "dec.swift"
    swift.write_text('''import Foundation
import Vision
let lines = try! String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8).split(separator: "\\n")
var bad = 0
for l in lines {
  let f = l.split(separator: "\\t").map(String.init)
  let r = VNDetectBarcodesRequest(); r.symbologies = [.qr]
  try? VNImageRequestHandler(url: URL(fileURLWithPath: f[0])).perform([r])
  if (r.results ?? []).first?.payloadStringValue != f[1] { bad += 1; print("FAIL \\(f[0])") }
}
print("bad=\\(bad)")
''')
    p = subprocess.run(["swift", str(swift), str(tmp_path / "cases.tsv")], capture_output=True, text=True, timeout=300)
    assert "bad=0" in p.stdout, p.stdout + p.stderr


def test_isr_mesh_channel_caches_url(tmp_path):
    cli = tmp_path / "meshtastic"
    cli.write_text("#!/bin/sh\necho 'Owner: Bridge (BRDG)'\n"
                   f"echo 'Primary channel URL: {CH_URL}'\n"
                   f"echo 'Complete URL (includes all channels): {CH_URL}EiA'\n")
    cli.chmod(0o755)
    out = tmp_path / "etc" / "mesh-channel.url"
    env = dict(os.environ, ISR_SKIP_ROOT_CHECK="1", ISR_MESHTASTIC_CLI=str(cli), ISR_CHANNEL_FILE=str(out),
               MESH_PORT="/dev/null", PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-mesh-channel")], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert out.read_text().strip() == CH_URL + "EiA"          # prefers the all-channels URL
    assert CH_URL not in p.stdout                              # never printed without --print
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    cli.write_text("#!/bin/sh\necho nothing useful\n")
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-mesh-channel")], capture_output=True, text=True, env=env)
    assert p.returncode == 6


# ── self-check button, heartbeat detail ──────────────────────────────────────

def test_heartbeat_detail_shown():
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/tac-advisor", hb("tac-advisor", host=L.HUB_NAME, mode="llm",
                                                     detail="LLM qwen3.5:27b @ isr-hq"), retained=False, now=1)
    e = st.snapshot(now=2)["tac-advisor"]
    assert e["mode"] == "llm" and e["detail"] == "LLM qwen3.5:27b @ isr-hq" and e["station"] == "pi"
    st.on_message("isrkit/heartbeat/x", hb("x", detail="y" * 500), retained=False, now=1)
    assert len(st.snapshot(now=2)["x"]["detail"]) == 160


@pytest.fixture
def fake_check(tmp_path, monkeypatch):
    log = tmp_path / "args.txt"
    py = tmp_path / "python"
    py.write_text(f"""#!/bin/sh
echo "$@" > "{log}"
env | grep -E '^(TAK_HOST|MQTT_USER|MQTT_PASS|HOME)=' | sort >> "{log}"
echo "[PASS] TAK: OTS at 127.0.0.1:8087 routed a sender's CoT to a listener"
echo "[PASS] MQTT: logged in to 127.0.0.1:1883 as guest"
echo "[OK  ] fpv-vision         ok        mps          3s ago  http://isr-droneops.local:8110/"
echo "[WARN] op-sentry          ok        vlm         95s ago"
exit 0
""")
    py.chmod(0o755)
    monkeypatch.setattr(L, "CHECK_PY", str(py))
    monkeypatch.setattr(L, "CHECK_DIR", str(tmp_path))
    return log


def test_self_check_endpoint(server, fake_check):
    port, _ = server
    assert req(port, "POST", "/api/check", {"mesh": False}, {"Content-Type": "application/json"})[0] == 403
    code, body = req(port, "POST", "/api/check", {"mesh": False}, SW_HEADERS)
    assert code == 200 and body["ok"] is True
    assert [l["level"] for l in body["lines"]] == ["PASS", "PASS", "OK", "WARN"]
    seen = fake_check.read_text()
    assert seen.startswith("-m bridge.check\n") and "TAK_HOST=127.0.0.1" in seen and "MQTT_USER=guest" in seen
    assert "HOME=" not in seen                       # minimal environment, no launcher secrets
    code, body = req(port, "POST", "/api/check", {"mesh": "yes please"}, SW_HEADERS)
    assert "--mesh-text radio check from HQ" in fake_check.read_text()   # fixed text, never user input


def test_self_check_pin_and_missing(server, fake_check, monkeypatch):
    port, _ = server
    monkeypatch.setattr(L, "PIN", "1111")
    assert req(port, "POST", "/api/check", {"mesh": True}, SW_HEADERS)[0] == 401
    assert req(port, "POST", "/api/check", {"mesh": True}, dict(SW_HEADERS, **{"X-PIN": "1111"}))[0] == 200
    monkeypatch.setattr(L, "CHECK_PY", "/nonexistent/python")
    assert req(port, "POST", "/api/check", {}, dict(SW_HEADERS, **{"X-PIN": "1111"}))[0] in (400, 404)


# ── stop-heartbeat gate harness ──────────────────────────────────────────────

def _fake_tool(root, name, on_term):
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / ".git").mkdir()
    (d / "scripts" / "run-laptop.sh").write_text(f"""#!/bin/bash
exec /usr/bin/env python3 - "$@" <<'PY'
import json, os, signal, sys, time
sys.path.insert(0, {str(REPO / "launcher")!r})
from mqtt_mini import MiniMQTT
c = MiniMQTT(os.environ["MQTT_HOST"], int(os.environ["MQTT_PORT"]), os.environ["MQTT_USER"], os.environ["MQTT_PASS"], client_id="t")
c.connect()
def hb(status, retain=True):
    c.publish("isrkit/heartbeat/{name}", json.dumps({{"tool": "{name}", "host": "t", "url": "", "mode": "simulate",
              "status": status, "ts": int(time.time())}}).encode(), retain=retain)
def term(*a):
    {on_term}
    c.close(); sys.exit(0)
signal.signal(signal.SIGTERM, term)
hb("ok")
while True:
    time.sleep(0.2)
PY
""")
    return d


def test_stop_heartbeat_harness(tmp_path):
    _fake_tool(tmp_path, "good-tool", 'hb("stopped")')
    _fake_tool(tmp_path, "unretained-tool", 'hb("stopped", retain=False)')
    _fake_tool(tmp_path, "silent-tool", "pass")
    p = subprocess.run([sys.executable, str(REPO / "tools" / "stop_heartbeat_check.py"), "--src", str(tmp_path),
                        "--first-heartbeat-timeout", "20", "good-tool", "unretained-tool", "silent-tool"],
                       capture_output=True, text=True, timeout=120)
    out = p.stdout
    assert "[PASS] good-tool" in out, out + p.stderr
    assert "[FAIL] unretained-tool" in out and "NOT retained" in out
    assert "[FAIL] silent-tool" in out and "no stopped heartbeat" in out
    assert p.returncode == 1



# ── boot ordering and wired Ethernet (local-first) ───────────────────────────

def test_boot_unit_does_not_wait_for_network_online():
    unit = (REPO / "pi-profiles.service").read_text()
    deps = [l for l in unit.splitlines() if l.startswith(("After=", "Wants=", "Requires="))]
    assert deps and not any("network-online" in l for l in deps)
    assert any("NetworkManager.service" in l for l in deps)


def _fake_nmcli(tmp_path, version, profiles):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    log = tmp_path / "nmcli.log"
    (b / "nmcli").write_text(f"""#!/bin/sh
echo "$@" >> "{log}"
case "$1" in
  --version) echo "nmcli tool, version {version}" ;;
  -t) printf '%s' "{profiles}" ;;
esac
exit 0
""")
    (b / "nmcli").chmod(0o755)
    return b, log


@pytest.mark.parametrize("version", ["1.42.4", "1.52.0"])
def test_wired_setup_default_is_link_local(tmp_path, version):
    b, log = _fake_nmcli(tmp_path, version, "Wired connection 1:802-3-ethernet\nbft-hotspot:802-11-wireless\n")
    sysd = tmp_path / "systemd"
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", ISR_SKIP_ROOT_CHECK="1", ISR_SYSTEMD_DIR=str(sysd))
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-wired-setup")], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    mod = [c for c in log.read_text().splitlines() if c.startswith("connection modify")]
    assert mod == ["connection modify Wired connection 1 ipv4.method link-local ipv4.dhcp-timeout 0 ipv4.may-fail yes "
                   "ipv6.method link-local ipv6.may-fail yes ipv4.link-local auto"]
    assert not any("connection up" in c for c in log.read_text().splitlines())       # no --apply: no re-activation
    assert "NM_ONLINE_TIMEOUT=15" in (sysd / "NetworkManager-wait-online.service.d" / "isr-kit.conf").read_text()


@pytest.mark.parametrize("version,ll", [("1.42.4", "enabled"), ("1.52.0", "fallback"), ("2.0.1", "fallback")])
def test_wired_setup_dhcp_opt_in(tmp_path, version, ll):
    b, log = _fake_nmcli(tmp_path, version, "Wired connection 1:802-3-ethernet\nbft-hotspot:802-11-wireless\n")
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", ISR_SKIP_ROOT_CHECK="1", ISR_SYSTEMD_DIR=str(tmp_path / "s"))
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-wired-setup"), "--dhcp"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    mod = [c for c in log.read_text().splitlines() if c.startswith("connection modify")]
    assert mod == [f"connection modify Wired connection 1 ipv4.method auto ipv4.dhcp-timeout 10 ipv4.may-fail yes "
                   f"ipv6.method auto ipv6.may-fail yes ipv4.link-local {ll}"]
    assert not any("bft-hotspot" in c for c in mod)


def test_wired_setup_creates_profile_when_none(tmp_path):
    b, log = _fake_nmcli(tmp_path, "1.42.4", "bft-hotspot:802-11-wireless\n")
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", ISR_SKIP_ROOT_CHECK="1", ISR_SYSTEMD_DIR=str(tmp_path / "s"))
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-wired-setup"), "--iface", "eth0"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    add = [c for c in log.read_text().splitlines() if c.startswith("connection add")]
    assert len(add) == 1 and "con-name isr-wired" in add[0] and "ipv4.method link-local" in add[0] and "ipv6.method link-local" in add[0]
    d = subprocess.run(["bash", str(REPO / "bin" / "isr-wired-setup"), "--dry-run"], capture_output=True, text=True, env=env)
    assert "[dry-run]" in d.stdout


# ── offline update: laptop/push-to-pi.sh (stub ssh runs the remote side locally) ──

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tar / COPYFILE_DISABLE path")
def test_push_to_pi_offline(tmp_path):
    mirror, pi_home, opt = tmp_path / "mirror", tmp_path / "pihome", tmp_path / "opt"
    mirror.mkdir(); pi_home.mkdir()
    # mirrors: pi-profiles (this repo) + a tiny drone-detector with a requirement the venv lacks
    subprocess.run(["git", "clone", "-q", "--mirror", str(REPO), str(mirror / "pi-profiles.git")], check=True)
    work = tmp_path / "dd"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "requirements.txt").write_text("definitely-not-installed-pkg==1.0\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(work), str(mirror / "drone-detector.git")], check=True)
    (mirror / "drone-detector.git" / "._junk").write_text("appledouble")       # must not survive
    subprocess.run([sys.executable, "-m", "venv", str(opt / "drone-detector" / ".venv")], check=True)
    stub = tmp_path / "ssh"
    stub.write_text(f"""#!/bin/bash
# fake ssh: skip options and host, run the command locally as the "Pi" user
while [ "${{1#-}}" != "$1" ]; do shift 2; done
shift
case "$*" in *"rsync --server"*) [ -f "{tmp_path}/fail-rsync" ] && exit 12 ;; esac
cd "{pi_home}" && HOME="{pi_home}" exec bash -c "$*"
""")
    stub.chmod(0o755)
    env = dict(os.environ, ISR_SSH=str(stub), ISR_MIRROR_DIR=str(mirror), ISR_PI_OPT=str(opt),
               ISR_PIP_CHECK_URL="http://127.0.0.1:9/", GIT_CONFIG_NOSYSTEM="1")
    p = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@test", "--no-fetch"],
                       capture_output=True, text=True, env=env, timeout=180)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert (pi_home / "isr-mirror" / "pi-profiles.git").is_dir() and not list((pi_home / "isr-mirror").rglob("._*"))
    gcfg = (pi_home / ".gitconfig").read_text()
    assert 'insteadOf = https://github.com/Jacob-Haynes/' in gcfg and str(pi_home / "isr-mirror") in gcfg
    assert (pi_home / "pi-profiles" / "bootstrap.sh").exists()                 # cloned through insteadOf
    assert "drone-detector: MISSING offline" in out and "definitely-not-installed-pkg" in out, out[out.find("── 4"):]
    assert "fpv-vision: no requirements.txt" in out or "fpv-vision" not in out   # no mirror for it: reported, not fatal
    # second run replaces the mirrors atomically and fast-forwards the checkout
    p2 = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@test", "--no-fetch"],
                        capture_output=True, text=True, env=env, timeout=180)
    assert p2.returncode == 0 and "mirrors in place" in p2.stdout and "pi-profiles:" in p2.stdout
    assert (pi_home / "isr-mirror.staging" / "pi-profiles.git").is_dir()        # resumable staging kept
    # a copy that keeps failing leaves the live mirrors untouched and says so
    marker = pi_home / "isr-mirror" / "pi-profiles.git" / "HEAD"
    before = marker.read_text()
    (tmp_path / "fail-rsync").write_text("1")
    f = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@test", "--no-fetch"],
                       capture_output=True, text=True, env=dict(env, ISR_COPY_RETRIES="2", ISR_COPY_BACKOFF="0"), timeout=120)
    assert f.returncode != 0 and "the Pi is UNCHANGED" in f.stderr and "attempt 2/2" in f.stdout
    assert marker.read_text() == before and (pi_home / "isr-mirror" / "drone-detector.git").is_dir()
    (tmp_path / "fail-rsync").unlink()
    # tar fallback (no rsync): no ._ files, no xattr headers complaint, same swap
    t = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@test", "--no-fetch"],
                       capture_output=True, text=True, env=dict(env, ISR_COPY_METHOD="tar"), timeout=120)
    assert t.returncode == 0 and "falling back to one tar stream" in t.stdout and "mirrors in place" in t.stdout, t.stdout + t.stderr
    assert not list((pi_home / "isr-mirror").rglob("._*"))
    bad = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@x;rm -rf /", "--no-fetch"],
                         capture_output=True, text=True, env=env)
    assert bad.returncode != 0 and "unsafe" in bad.stderr


# ── offline pip/uv (bin/isr-pip-offline) ─────────────────────────────────────

def test_pip_offline_backup_restore_and_fast_fail(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    orig = "[global]\nextra-index-url=https://www.piwheels.org/simple\n"
    (etc / "pip.conf").write_text(orig)                       # what Pi OS ships
    env = dict(os.environ, ISR_ETC_DIR=str(etc))
    tool = str(REPO / "bin" / "isr-pip-offline")
    assert subprocess.run(["bash", tool, "on"], env=env, capture_output=True, text=True).returncode == 0
    conf = (etc / "pip.conf").read_text()
    assert "no-index = true" in conf and "retries = 0" in conf and "piwheels" not in conf
    assert (etc / "uv" / "uv.toml").read_text().strip().endswith("offline = true")
    assert "OFFLINE" in subprocess.run(["bash", tool, "status"], env=env, capture_output=True, text=True).stdout
    # idempotent: a second "on" must not overwrite the backup with the offline file
    subprocess.run(["bash", tool, "on"], env=env, check=True, capture_output=True)
    # pip with this config: already-satisfied passes, a missing package fails fast (no network)
    venv = tmp_path / "v"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    penv = dict(os.environ, PIP_CONFIG_FILE=str(etc / "pip.conf"))
    import time as _t
    t0 = _t.time()
    up = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "--upgrade", "pip"],
                        env=penv, capture_output=True, text=True, timeout=60)
    assert up.returncode == 0 and "already satisfied" in (up.stdout + up.stderr).lower()
    miss = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "definitely-not-a-real-pkg-xyz"],
                          env=penv, capture_output=True, text=True, timeout=60)
    assert miss.returncode != 0 and _t.time() - t0 < 30
    # off restores the original byte for byte and removes the uv file it created
    subprocess.run(["bash", tool, "off"], env=env, check=True, capture_output=True)
    assert (etc / "pip.conf").read_text() == orig and not (etc / "uv" / "uv.toml").exists()
    assert "normal" in subprocess.run(["bash", tool, "status"], env=env, capture_output=True, text=True).stdout


@pytest.mark.parametrize("resolved,wlan_has_hub,expect", [
    ("192.168.4.1", True, "ok    isr-pi.local -> 192.168.4.1"),
    ("169.254.95.138", True, "note  isr-pi.local -> 169.254.95.138 here (this Pi's eth0 address"),
    ("169.254.95.138", False, "FAIL  isr-pi.local -> 169.254.95.138 (eth0), and wlan0 doesn't have"),
    ("10.9.9.9", True, "FAIL  isr-pi.local resolves to '10.9.9.9'"),
])
def test_netcheck_mdns_per_interface(tmp_path, resolved, wlan_has_hub, expect):
    b = tmp_path / "bin"
    b.mkdir()
    def stub(name, body):
        (b / name).write_text("#!/bin/sh\n" + body + "\n")
        (b / name).chmod(0o755)
    stub("avahi-resolve", f'echo "isr-pi.local\t{resolved}"')
    wlan = "192.168.4.1/24" if wlan_has_hub else "10.0.0.5/24"
    stub("ip", f'''case "$*" in
  "-4 -o addr show") printf '1: lo    inet 127.0.0.1/8 scope host lo\\n3: wlan0    inet {wlan} brd x scope global wlan0\\n2: eth0    inet 169.254.95.138/16 brd x scope link eth0\\n' ;;
  *) printf '    inet {wlan} brd x scope global wlan0\\n' ;;
esac''')
    stub("systemctl", 'exit 0')
    stub("timeout", 'shift; exec "$@"')
    for n in ("nmcli", "pgrep", "ss", "ufw", "nft", "iptables"):
        stub(n, "exit 1")
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin")
    p = subprocess.run(["bash", str(REPO / "bin" / "isr-netcheck")], capture_output=True, text=True, env=env)
    assert expect in p.stdout, p.stdout
