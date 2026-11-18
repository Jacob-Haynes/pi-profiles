"""Tunnelled HQ: the Mac's hq-tunnel.sh loop, start-mac's ISR_PUBLIC_URL, the launcher's hub-IP relay
for tunnelled Mac ports, isr-hq-mode, merge-env's stale-default update and setup-mac's MQTT check."""
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
import isr_launcher as L        # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Echo:
    """Stands in for sshd's -R listener on the Pi's 127.0.0.1:<port>."""

    def __init__(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    @staticmethod
    def _one(c):
        with c:
            while True:
                d = c.recv(4096)
                if not d:
                    return
                c.sendall(b"mac:" + d)

    def close(self):
        self.sock.close()


# ── launcher relay ───────────────────────────────────────────────────────────

def test_relay_listens_on_every_address_for_offset_tunnel():
    port, tport = free_port(), free_port()
    fw = L.TunnelForwarder(target="127.0.0.1", offset=tport - port)     # hq-tunnel: P -> 127.0.0.1:P+offset
    fw.sync({port: "photoAnalysis"})
    assert fw.active() == {}                                         # tunnel down: nothing listens
    echo = Echo(tport)
    try:
        fw.sync({port: "photoAnalysis"})
        assert fw.active() == {port: "photoAnalysis"}
        for host in ("127.0.0.1", "::1"):                            # any address: hotspot IP, eth0, ...
            c = socket.create_connection((host, port), timeout=3)
            c.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert c.recv(100).startswith(b"mac:GET /")
            c.close()
    finally:
        echo.close()
    fw.sync({port: "photoAnalysis"})
    assert fw.active() == {}                                         # tunnel gone: relay closed
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_relay_legacy_tunnel_binds_hub_ip_only(monkeypatch):
    """An older hq-tunnel.sh forwards P to 127.0.0.1:P: relay from the hub IP only (here ::1)."""
    port = free_port()
    monkeypatch.setattr(L, "HUB_IP", "::1")
    fw = L.TunnelForwarder(target="127.0.0.1", offset=0)
    echo = Echo(port)
    try:
        fw.sync({port: "photoAnalysis"})
        assert fw.active() == {port: "photoAnalysis"}
        c = socket.create_connection(("::1", port), timeout=3)
        c.sendall(b"x")
        assert c.recv(100) == b"mac:x"
        c.close()
    finally:
        echo.close()
    fw.sync({port: "photoAnalysis"})
    assert fw.active() == {}
    same = L.TunnelForwarder(bind="127.0.0.1", target="127.0.0.1", offset=0)
    Echo2 = Echo(port)
    try:
        same.sync({port: "x"})
        assert same.active() == {}                                   # never relay a port onto itself
    finally:
        Echo2.close()


def test_candidates_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "SERVICES_CONF", REPO / "services.conf")
    cat = L.load_catalog()
    by = {c["unit"]: c for c in cat}
    assert by["photoAnalysis"]["class"] == "mac"
    want = L.tunnel_candidates(cat, None)
    assert want == {8150: "photoAnalysis"}                       # only class mac without a profile
    meta = {"remote": [{"tool": "change-detect", "station": "hq"}, {"tool": "isr-launcher", "station": "hq"}]}
    want = L.tunnel_candidates(cat, meta)
    assert want == {8150: "photoAnalysis", 8130: "change-detect"}  # never an always/core port
    active = {8150: "photoAnalysis"}
    beat = {"url": "http://corp-laptop-01.local:8150/", "state": "ok"}
    assert L.tunnel_link("geo-locate", {}, beat, active) == {"port": 8150, "url": f"http://{L.HUB_IP}:8150/", "via": "tunnel"}
    assert L.tunnel_link("photoAnalysis", by["photoAnalysis"], None, active)["port"] == 8150
    assert L.tunnel_link("change-detect", by["change-detect"], {"url": "http://x.local:8130/"}, active) is None
    js = (REPO / "launcher" / "static" / "app.js").read_text()
    assert "via Pi tunnel" in js and "hubLink(t.port)" in js
    assert "location.hostname" in (REPO / "launcher" / "static" / "common.js").read_text()
    assert "viewerUrl(p.atak_xml)" in js and "viewerUrl(e.url)" in js


def test_status_marks_tunnelled_remote(monkeypatch):
    monkeypatch.setattr(L, "SERVICES_CONF", REPO / "services.conf")
    monkeypatch.setattr(L.tunnels, "active", lambda: {8150: "photoAnalysis"})
    monkeypatch.setattr(L.heartbeats, "snapshot", lambda: {"geo-locate": {
        "tool": "geo-locate", "station": "hq", "url": "http://corp-laptop-01.local:8150/", "state": "ok"}})
    st = L.build_status()
    r = [x for x in st["remote"] if x["tool"] == "geo-locate"][0]
    assert r["tunnel"]["url"] == f"http://{L.HUB_IP}:8150/" and st["tunnel"]["relayed"] == {"8150": "photoAnalysis"}
    assert {"192.168.4.1", "isr-pi.local"} <= set(st["hub_hosts"])


# ── Mac side ─────────────────────────────────────────────────────────────────

def test_hq_tunnel_loop(tmp_path):
    log = tmp_path / "ssh.log"
    stub = tmp_path / "ssh"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nsleep 0.3\nexit 255\n')
    stub.chmod(0o755)
    env = dict(os.environ, ISR_CONF_DIR=str(tmp_path / "none"), ISR_RUN_DIR=str(tmp_path / "run"),
               ISR_LOG_DIR=str(tmp_path / "logs"), ISR_SSH=str(stub), ISR_TUNNEL_PI="pi@isr-pi.local",
               ISR_TUNNEL_PORTS="8130 8150", ISR_CLOCK_SYNC="0")        # clock sync: tests/test_clock.py
    sh = str(REPO / "laptop" / "hq-tunnel.sh")
    p = subprocess.run([sh, "--print"], capture_output=True, text=True, env=env)
    for want in ("ExitOnForwardFailure=yes", "ServerAliveInterval=15", "BatchMode=yes",
                 "-R 127.0.0.1:11434:127.0.0.1:11434", "-R 127.0.0.1:18130:127.0.0.1:8130",
                 "-R 127.0.0.1:18150:127.0.0.1:8150", "pi@isr-pi.local"):
        assert want in p.stdout
    assert "0.0.0.0" not in p.stdout and "GatewayPorts" not in p.stdout
    assert subprocess.run([sh, "start"], capture_output=True, text=True, env=env).returncode == 0
    time.sleep(3.0)                                  # first try + 2 s backoff + second try
    st = subprocess.run([sh, "status"], capture_output=True, text=True, env=env).stdout
    assert "running" in st
    subprocess.run([sh, "stop"], capture_output=True, text=True, env=env)
    assert log.read_text().count("ExitOnForwardFailure") >= 2          # it reconnected
    assert "retrying in 2s" in (tmp_path / "logs" / "hq-tunnel.log").read_text()
    assert "not running" in subprocess.run([sh, "status"], capture_output=True, text=True, env=env).stdout
    bad = dict(env, ISR_TUNNEL_PORTS="8130;rm")
    assert subprocess.run([sh, "--print"], capture_output=True, text=True, env=bad).returncode == 64


def test_start_mac_sets_public_url_and_runs_tunnel(tmp_path):
    src, conf = tmp_path / "src", tmp_path / "conf"
    (src / "photoAnalysis" / "scripts").mkdir(parents=True)
    conf.mkdir()
    out = tmp_path / "env.out"
    (src / "photoAnalysis" / "scripts" / "run-laptop.sh").write_text(f'echo "$ISR_PUBLIC_URL $PUBLIC_URL" > "{out}"\nsleep 30\n')
    calls = tmp_path / "tunnel.calls"
    (conf / "station.env").write_text(
        f"ISR_STATION=hq\nISR_HOST=testmac\nISR_REPOS=photoAnalysis\nISR_SRC={src}\nISR_HUB=isr-pi.local\n"
        "LLM_URL=http://127.0.0.1:1\nISR_TUNNEL=0\nSTATION_PUBLIC_BASE=http://192.168.4.1\n")
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(tmp_path / "run"), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal")
    sh = str(REPO / "laptop" / "start-mac.sh")
    try:
        subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30)
        for _ in range(50):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.1)
        assert out.read_text().strip() == "http://192.168.4.1:8150/ http://192.168.4.1:8150/"
    finally:
        subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=60)
    assert calls.exists() is False
    text = (REPO / "laptop" / "start-mac.sh").read_text()
    assert 'hq-tunnel.sh" start' in text and 'hq-tunnel.sh" stop' in text


def _mqtt_snippet():
    s = (REPO / "laptop" / "setup-mac.sh").read_text()
    m = re.search(r"/usr/bin/python3 -c '\n(.*?)\n' \"\$HUB\"", s, re.S)
    assert m, "MQTT check snippet not found"
    assert "mqtt_mini" not in m.group(1) and "import" in m.group(1)
    return m.group(1)


def test_setup_mac_mqtt_check_is_standalone(tmp_path):
    code = _mqtt_snippet()
    port = free_port()
    broker = subprocess.Popen([sys.executable, str(REPO / "tools" / "rehearsal" / "broker.py"), str(port),
                               str(tmp_path / "b.log")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        run = lambda c: subprocess.run([sys.executable, "-I", "-c", c.replace("1883", str(port)), "127.0.0.1"],
                                       capture_output=True, text=True, env=dict(os.environ, MQTT_PASS="pw_1"),
                                       cwd=str(tmp_path), timeout=10).stdout.strip()
        assert run(code) == "ok"
    finally:
        broker.kill()
    # a broker that says "bad user/password"
    deny = socket.socket()
    deny.bind(("127.0.0.1", 0))
    deny.listen(1)

    def refuse():
        c, _ = deny.accept()
        c.recv(1024)
        c.sendall(b"\x20\x02\x00\x04")
        c.close()
    threading.Thread(target=refuse, daemon=True).start()
    out = subprocess.run([sys.executable, "-I", "-c", code.replace("1883", str(deny.getsockname()[1])), "127.0.0.1"],
                         capture_output=True, text=True, env=dict(os.environ, MQTT_PASS="x"), timeout=10).stdout.strip()
    assert out == "bad user/password"
    out = subprocess.run([sys.executable, "-I", "-c", code.replace("1883", "1"), "127.0.0.1"],
                         capture_output=True, text=True, env=dict(os.environ, MQTT_PASS="x"), timeout=10).stdout
    assert "cannot connect" in out


def test_setup_mac_tunnel_and_managed_mac_texts():
    s = (REPO / "laptop" / "setup-mac.sh").read_text()
    assert "NoMulticastAdvertisements" in s and "--tunnel" in s
    assert 'OLLAMA_LISTEN="127.0.0.1:11434"' in s and "STATION_PUBLIC_BASE=" in s and "ISR_TUNNEL_PORTS=" in s


# ── Pi side ──────────────────────────────────────────────────────────────────

def test_merge_env_updates_only_stale_defaults(tmp_path):
    ex, stale, live = tmp_path / "ex", tmp_path / "stale", tmp_path / "live"
    ex.write_text("# c\nVLM_MODEL=qwen3-vl:8b-instruct\nLLM_MODEL=qwen3.5:27b\nNEW_KEY=1\n")
    stale.write_text("# old\nVLM_MODEL=qwen3-vl:8b\n")
    live.write_text("LLM_MODEL=my-own:7b\nVLM_MODEL=qwen3-vl:8b\nLLM_URL=http://127.0.0.1:11434\n")
    p = subprocess.run(["bash", str(REPO / "deploy" / "merge-env.sh"), str(ex), str(stale), str(live)],
                       capture_output=True, text=True)
    assert p.returncode == 0 and "old default" in p.stdout
    got = live.read_text().splitlines()
    assert got == ["LLM_MODEL=my-own:7b", "VLM_MODEL=qwen3-vl:8b-instruct", "LLM_URL=http://127.0.0.1:11434", "NEW_KEY=1"]
    p = subprocess.run(["bash", str(REPO / "deploy" / "merge-env.sh"), str(ex), str(stale), str(live)],
                       capture_output=True, text=True)
    assert p.stdout == "" and live.read_text().splitlines() == got          # idempotent
    assert "VLM_MODEL=qwen3-vl:8b" in (REPO / "deploy" / "isr-launcher.env.stale").read_text()


def test_isr_hq_mode(tmp_path):
    etc, stub = tmp_path / "etc", tmp_path / "stub"
    etc.mkdir(); stub.mkdir()
    (etc / "isr-launcher.env").write_text("LAUNCHER_PIN=1\nLLM_URL=http://isr-hq.local:11434\nVLM_URL=http://isr-hq.local:11434\n")
    (etc / "tac-advisor.env").write_text("LLM_URL=http://isr-hq.local:11434/v1\nOTHER=http://isr-hq.local:9999\n")
    env = dict(os.environ, ISR_ETC_DIR=str(etc), PI_PROFILES_ROOT=str(REPO), ISR_SKIP_ROOT_CHECK="1",
               STUB_DIR=str(stub), PATH=f"{REPO / 'tests' / 'stubs'}:{os.environ['PATH']}")
    sh = str(REPO / "bin" / "isr-hq-mode")
    p = subprocess.run([sh, "tunnel"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert (etc / "isr-launcher.env").read_text() == "LAUNCHER_PIN=1\nLLM_URL=http://127.0.0.1:11434\nVLM_URL=http://127.0.0.1:11434\n"
    assert (etc / "tac-advisor.env").read_text() == "LLM_URL=http://127.0.0.1:11434/v1\nOTHER=http://isr-hq.local:9999\n"
    calls = (stub / "calls").read_text()
    assert "try-restart isr-launcher.service" in calls and "try-restart tac-advisor.service" in calls
    assert "already tunnel" in subprocess.run([sh, "tunnel"], capture_output=True, text=True, env=env).stdout
    st = subprocess.run([sh, "status"], capture_output=True, text=True, env=env).stdout
    assert "tac-advisor.env: LLM_URL=http://127.0.0.1:11434/v1" in st
    subprocess.run([sh, "mdns"], capture_output=True, text=True, env=env)
    assert "LLM_URL=http://isr-hq.local:11434\n" in (etc / "isr-launcher.env").read_text()


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="no node")
def test_links_follow_the_viewers_host():
    js = (REPO / "launcher" / "static" / "common.js").read_text()
    start = js.index("function viewerUrl(url)")
    end = js.index("// Status fetch")
    code = js[start:end] + r"""
const out = {};
for (const host of ["192.168.4.1", "isr-pi.local"]) {
  location.hostname = host;
  out[host] = [viewerUrl("http://192.168.4.1:8150/"), viewerUrl("http://isr-pi.local:8160/atak/op.xml"),
               viewerUrl("http://isr-droneops.local:8110/"), hubLink(8130)];
}
console.log(JSON.stringify(out));
"""
    pre = ('const location = {protocol: "http:", hostname: "", href: "http://x/"};'
           'const status = {hub_hosts: ["192.168.4.1", "isr-pi", "isr-pi.local"]};')
    p = subprocess.run(["node", "-e", pre + code], capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    import json
    r = json.loads(p.stdout)
    assert r["isr-pi.local"] == ["http://isr-pi.local:8150/", "http://isr-pi.local:8160/atak/op.xml",
                                 "http://isr-droneops.local:8110/", "http://isr-pi.local:8130/"]
    assert r["192.168.4.1"][0] == "http://192.168.4.1:8150/" and r["192.168.4.1"][3] == "http://192.168.4.1:8130/"
