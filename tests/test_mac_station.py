"""Mac station scripts: the drone-ops VLM (own Ollama on 127.0.0.1, VLM only), start-mac's Ollama check,
and the online-first order documented in setup-mac.sh."""
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SETUP = (REPO / "laptop" / "setup-mac.sh").read_text()


class FakeOllama:
    def __init__(self, models):
        names = [{"name": m} for m in models]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": names} if self.path == "/api/tags" else {"version": "0.12"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def start_mac(tmp_path, extra):
    conf, src = tmp_path / "conf", tmp_path / "src"
    conf.mkdir(exist_ok=True); src.mkdir(exist_ok=True)
    (conf / "station.env").write_text(f"ISR_STATION=droneops\nISR_HOST=test\nISR_REPOS=\nISR_SRC={src}\nISR_HUB=127.0.0.1\n" + extra)
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal")
    sh = str(REPO / "laptop" / "start-mac.sh")
    out = subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30).stdout
    subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=30)
    return out


def test_start_mac_checks_local_vlm(tmp_path):
    o = FakeOllama(["qwen3-vl:8b-instruct"])
    try:
        out = start_mac(tmp_path, f"VLM_URL={o.url}\nVLM_MODEL=qwen3-vl:8b-instruct\nLLM_URL=http://isr-hq.local:11434\n")
        assert f"Ollama at {o.url}: up, qwen3-vl:8b-instruct present" in out
        assert "isr-hq.local" not in out                                   # a remote LLM isn't this Mac's to check
        out = start_mac(tmp_path, f"VLM_URL={o.url}\nVLM_MODEL=qwen3-vl:4b-instruct\n")
        assert "model qwen3-vl:4b-instruct is missing" in out
    finally:
        o.srv.shutdown()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    out = start_mac(tmp_path, f"VLM_URL=http://127.0.0.1:{port}\nVLM_MODEL=qwen3-vl:8b-instruct\n")
    assert "is not answering" in out


def test_droneops_gets_its_own_vlm_on_loopback():
    # station.env: drone-ops VLM on this Mac unless --skip-ollama; the LLM stays on HQ
    assert 'case "$ROLE" in op|droneops) OWN_VLM=1 ;; *) OWN_VLM=0 ;; esac' in SETUP
    assert 'if [ "$OWN_VLM" -eq 1 ] && [ "$SKIP_OLLAMA" -eq 0 ]; then VLM_BASE="http://127.0.0.1:11434"; fi' in SETUP
    assert "VLM_URL=$VLM_BASE" in SETUP and "LLM_URL=$OLLAMA_BASE" in SETUP
    # Ollama: loopback only, the VLM only, one model loaded
    assert 'if [ "$TUNNEL" -eq 1 ] || [ "$OWN_VLM" -eq 1 ]; then OLLAMA_LISTEN="127.0.0.1:11434"' in SETUP
    assert 'if [ "$OWN_VLM" -eq 1 ]; then OLLAMA_MODELS="$VLM_MODEL"; MAX_LOADED=1' in SETUP
    assert 'if [ "$SKIP_OLLAMA" -eq 0 ]; then setup_ollama; fi' in SETUP                 # both roles, opt-out
    assert 'VLM_MODEL="${ISR_VLM_MODEL:-qwen3-vl:8b-instruct}"' in SETUP
    assert "for m in $OLLAMA_MODELS; do" in SETUP and 'for m in "$LLM_MODEL" "$VLM_MODEL"' not in SETUP


def test_online_first_order_is_documented():
    head = SETUP.split("set -uo pipefail")[0]
    i1, i2, i3 = head.index("1. On normal internet"), head.index("2. Join the BFT-TAK hotspot"), head.index("3. From Terminal.app")
    assert i1 < i2 < i3 and "gh auth login" in head and "--hub 192.168.4.1" in head and "--setup-only" in head
    assert "uv; do" in SETUP or "ffmpeg uv" in SETUP                                     # uv from brew
    assert "--skip-prefetch" in SETUP and 'grep -q -- "--setup-only"' in SETUP
    ops = (Path.home() / "Repos" / "isr-kit" / "isr-kit-ops" / "DEMO-RUNBOOK.md")
    if ops.exists():
        t = ops.read_text()
        assert "--setup-only" in t and "--hub 192.168.4.1" in t


# ── Wi-Fi keep-awake (start-mac.sh) ──────────────────────────────────────────

def test_wifi_keepalive_runs_and_stops(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "ping.log"
    (fakebin / "ping").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec sleep 30\n')
    (fakebin / "ping").chmod(0o755)
    conf, src = tmp_path / "conf", tmp_path / "src"
    conf.mkdir(); src.mkdir()
    base = f"ISR_STATION=droneops\nISR_HOST=t\nISR_REPOS=\nISR_SRC={src}\nLLM_URL=http://isr-hq.local:11434\n"
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal", PATH=f"{fakebin}:{os.environ['PATH']}")
    sh = str(REPO / "laptop" / "start-mac.sh")
    run = lambda *a: subprocess.run([sh, *a], capture_output=True, text=True, env=env, timeout=30).stdout

    (conf / "station.env").write_text(base + "ISR_HUB=192.168.4.1\n")
    out = run("start")
    assert "wifi keep-awake: ping -i 0.2 192.168.4.1" in out
    import time
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.1)
    assert log.read_text().strip() == "-i 0.2 -q 192.168.4.1"
    assert "wifi keep-awake: running" in run("status")
    assert "already running" in run("start")
    pid = int((tmp_path / "run" / "wifi-keepalive.pid").read_text())
    assert "wifi keep-awake: stopped" in run("stop")
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # tunnelled HQ on the cable, hub not on the hotspot: no keep-awake; and the off switch
    (conf / "station.env").write_text(base + "ISR_HUB=127.0.0.1\nISR_TUNNEL=1\nISR_CLOCK_SYNC=0\nISR_TUNNEL_PI=x@127.0.0.1\n")
    env["ISR_SSH"] = "/usr/bin/false"
    out = run("start"); run("stop")
    assert "keep-awake" not in out
    (conf / "station.env").write_text(base + "ISR_HUB=192.168.4.1\nISR_WIFI_KEEPALIVE=0\n")
    out = run("start"); run("stop")
    assert "keep-awake" not in out


# ── hotspot band (Pi) ────────────────────────────────────────────────────────

def nmcli_stub(tmp_path, band="bg", channel="7", exists=True):
    d = tmp_path / "nm"
    d.mkdir(exist_ok=True)
    (d / "state").write_text(f"{band} {channel}")
    (d / "nmcli").write_text(f"""#!/bin/bash
echo "$*" >> "{d}/calls"
case "$*" in
  "-t -f NAME connection show") {'echo bft-hotspot' if exists else 'echo other'} ;;
  "-g 802-11-wireless.band,802-11-wireless.channel connection show bft-hotspot") tr ' ' '\\n' < "{d}/state" ;;
  "connection modify bft-hotspot 802-11-wireless.band "*) set -- $*; echo "$5 $7" > "{d}/state" ;;
esac
""")
    (d / "nmcli").chmod(0o755)
    (d / "iw").write_text(f'#!/bin/sh\n[ "$1" = reg ] && [ "$2" = get ] && echo "country 00: DFS-UNSET"\n[ "$2" = set ] && echo "$*" >> "{d}/calls"\n')
    (d / "iw").chmod(0o755)
    return d


def band(tmp_path, d, *args):
    env = dict(os.environ, PATH=f"{d}:/usr/bin:/bin", ISR_SKIP_ROOT_CHECK="1", ISR_KIT_CONF=str(tmp_path / "etc"))
    return subprocess.run([str(REPO / "bin" / "isr-hotspot-band"), *args], capture_output=True, text=True, env=env)


def test_hotspot_band_default_5ghz_idempotent_and_saved(tmp_path):
    d = nmcli_stub(tmp_path)                                   # as blufor-tracker's installer leaves it
    p = band(tmp_path, d)
    assert p.returncode == 0 and "hotspot set to 5 GHz channel 36 (was bg/7)" in p.stdout
    assert (d / "state").read_text().split() == ["a", "36"]
    assert "reg set GB" in (d / "calls").read_text()           # 5 GHz needs a country; only set when unset
    assert "applies when the hotspot next starts" in p.stdout and "connection up" not in (d / "calls").read_text()
    p = band(tmp_path, d)
    assert "already 5 GHz channel 36" in p.stdout              # idempotent (e.g. set by hand already)
    assert (d / "calls").read_text().count("connection modify") == 1
    # 2.4 GHz is saved and kept on re-runs (bootstrap --hotspot-2ghz)
    p = band(tmp_path, d, "2.4")
    assert "2.4 GHz channel 7" in p.stdout and (d / "state").read_text().split() == ["bg", "7"]
    assert "ISR_HOTSPOT_BAND=bg" in (tmp_path / "etc" / "hotspot.env").read_text()
    assert "already 2.4 GHz channel 7" in band(tmp_path, d).stdout
    p = band(tmp_path, d, "5", "--apply")
    assert "restarting bft-hotspot" in p.stdout and "connection up bft-hotspot" in (d / "calls").read_text()
    assert band(tmp_path, d, "--status").stdout.strip() == "bft-hotspot: band a channel 36"
    assert band(tmp_path, d, "6").returncode == 64


def test_hotspot_band_without_the_connection(tmp_path):
    d = nmcli_stub(tmp_path, exists=False)
    p = band(tmp_path, d)
    assert p.returncode == 0 and "unchanged" in p.stdout and "modify" not in (d / "calls").read_text()


def test_bootstrap_sets_band_after_the_repos():
    b = (REPO / "bootstrap.sh").read_text()
    assert b.index("install_repo \"$repo\"") < b.index("/opt/pi-profiles/bin/isr-hotspot-band $HOTSPOT_BAND")
    assert "--hotspot-2ghz) HOTSPOT_BAND=2.4" in b
    assert '"$INSTALL_DIR/bin/isr-hotspot-band"' in (REPO / "install.sh").read_text()


# ── op-sentry split mode (Pi camera stream, Mac detection) ───────────────────

class FakeLauncher:
    def __init__(self):
        self.services = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"services": fake.services}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


def test_op_sentry_source_in_split_mode(tmp_path):
    import time
    fl = FakeLauncher()
    conf, src = tmp_path / "conf", tmp_path / "src"
    (src / "op-sentry" / "scripts").mkdir(parents=True)
    conf.mkdir()
    out = tmp_path / "source.out"
    (src / "op-sentry" / "scripts" / "run-laptop.sh").write_text(f'echo "src=${{OPSENTRY_SOURCE:-}}" > "{out}"\nsleep 30\n')
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal", ISR_LAUNCHER_PORT=str(fl.port), ISR_WIFI_KEEPALIVE="0")
    sh = str(REPO / "laptop" / "start-mac.sh")

    def source(extra=""):
        if out.exists():
            out.unlink()
        (conf / "station.env").write_text(f"ISR_STATION=droneops\nISR_HOST=t\nISR_REPOS=op-sentry\nISR_SRC={src}\n"
                                          f"ISR_HUB=127.0.0.1\n{extra}")
        log = subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30).stdout
        for _ in range(50):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.1)
        subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=60)
        return out.read_text().strip(), log
    try:
        fl.services = [{"unit": "op-sentry-stream", "in_profile": True}]
        s, log = source()
        assert s == "src=http://127.0.0.1:8120/raw.mjpg"             # the Pi's profile is split mode
        fl.services = [{"unit": "op-sentry-stream", "in_profile": False}, {"unit": "op-sentry", "in_profile": True}]
        assert source()[0] == "src="                                   # not split: op-sentry's own default (auto)
        fl.services = [{"unit": "op-sentry-stream", "in_profile": True}]
        assert source("OPSENTRY_SOURCE=sim360\n")[0] == "src=sim360"   # an explicit setting wins
    finally:
        fl.srv.shutdown()
    assert source()[0] == "src="                                       # launcher unreachable: no guess


def test_setup_mac_op_split_writes_the_stream():
    assert '--op-split) OP_SPLIT=1' in SETUP
    assert '"OPSENTRY_SOURCE=http://$HUB:8120/raw.mjpg"' in SETUP and '"# OPSENTRY_SOURCE=http://$HUB:8120/raw.mjpg"' in SETUP
    # no shipped profile runs op-sentry-stream since the 2026-11 prune: split mode is set by hand
    assert not any("op-sentry-stream" in p.read_text() for p in (REPO / "profiles.d").glob("*.conf"))


# ── the demo layout: HQ (tunnelled, + fpv-vision) and the OP Mac ─────────────────────────────────

def _setup(tmp_path, *args):
    """Run the real setup-mac.sh under a fake HOME, with brew/launchctl/ollama/ssh stubbed (they only log)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    pw = tmp_path / "mqtt.env"
    pw.write_text("MQTT_USER=isrkit\nMQTT_PASS=abcDEF123\n")
    fake, calls = tmp_path / "fakebin", tmp_path / "calls.log"
    fake.mkdir(exist_ok=True)
    for c in ("brew", "launchctl", "ollama", "osascript", "open", "sudo", "ssh"):
        (fake / c).write_text(f'#!/bin/sh\necho "{c} $*" >> "{calls}"\nexit 1\n')
        (fake / c).chmod(0o755)
    env = dict(os.environ, HOME=str(home), ISR_MAC_SRC=str(tmp_path / "src"), PATH=f"{fake}:{os.environ['PATH']}")
    env.pop("ISR_ALLOW_SERVICE_CHANGES", None)
    p = subprocess.run(["bash", str(REPO / "laptop" / "setup-mac.sh"), "--hub", "127.0.0.1", "--hq-host", "127.0.0.1",
                        "--mqtt-pass-file", str(pw), "--skip-brew", "--skip-clone", "--skip-camera-probe",
                        "--skip-prefetch", "--skip-ollama", "--sensor", "51.0000,-1.0000", *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not calls.exists() or not any(l.split()[0] in ("brew", "launchctl", "ollama") for l in calls.read_text().splitlines())
    f = home / ".config" / "isr-kit" / "station.env"
    text = f.read_text()
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#")), text, p.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_setup_op_role(tmp_path):
    kv, text, _ = _setup(tmp_path, "--role", "op")
    assert kv["ISR_STATION"] == "op" and kv["ISR_REPOS"] == '"op-sentry"'
    assert kv["LLM_URL"] == "http://127.0.0.1:11434"                    # --skip-ollama here: HQ's (the --hq-host)
    assert "FPV_FOOTAGE_DIRS" not in text                                # no fpv-vision on the OP Mac
    # without --skip-ollama the OP Mac runs its own VLM only, on loopback
    assert 'op) REPOS="op-sentry" ; HOSTNAME_WANT="isr-op" ;;' in SETUP


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_setup_hq_with_fpv_vision(tmp_path):
    kv, _, _ = _setup(tmp_path, "--role", "hq", "--tunnel", "--extra", "fpv-vision", "--extra", "change-detect",
                      "--fpv-footage", "~/Desktop/ISR-demo/fpv")
    assert kv["ISR_STATION"] == "hq" and kv["ISR_REPOS"] == '"change-detect photoAnalysis fpv-vision"'   # no duplicate
    assert kv["ISR_TUNNEL_PORTS"] == '"8130 8150 8110"'                  # fpv-vision's port joins the tunnel
    assert kv["FPV_FOOTAGE_DIRS"] == "~/Desktop/ISR-demo/fpv"
    kv2, _, _ = _setup(tmp_path, "--role", "hq", "--tunnel", "--extra", "fpv-vision")   # a re-run keeps the folder
    assert kv2["FPV_FOOTAGE_DIRS"] == "~/Desktop/ISR-demo/fpv"
    kv3, text3, _ = _setup(tmp_path, "--role", "hq", "--tunnel")
    assert "FPV_FOOTAGE_DIRS=" not in text3.replace("# FPV_FOOTAGE_DIRS", "") and kv3["ISR_TUNNEL_PORTS"] == '"8130 8150"'
