"""Station roles and links for Mac tools: the launcher learns each Mac's role (and hotspot address) from its
retained announce even when it (re)starts after the announce, as on the Pi's RabbitMQ (no retained replay
to wildcard subscriptions), and relays a hotspot Mac's tool page through the Pi."""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
import isr_launcher as L  # noqa: E402
from mqtt_mini import MiniMQTT  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def clean_stations(monkeypatch):
    monkeypatch.setattr(L, "ANNOUNCED", {})
    monkeypatch.setattr(L, "STATION_IPS", {})
    monkeypatch.setattr(L, "HUB_IP", "192.168.4.1")


def hb(host, tool="op-sentry", url=None):
    return json.dumps({"tool": tool, "host": host, "status": "ok", "mode": "mps",
                       "url": url or f"http://{host}.local:8120/"}).encode()


# ── which station topics the launcher subscribes to ──────────────────────────

def test_exact_station_topics_for_unknown_hosts(clean_stations):
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/op-sentry", hb("op-mac"), False)
    st.on_message("isrkit/heartbeat/change-detect", hb("Corp-Laptop-01.local", "change-detect"), False)
    st.on_message("isrkit/heartbeat/x", hb("bad/+#host"), False)
    assert st.station_topics() == ["isrkit/station/corp-laptop-01", "isrkit/station/op-mac"]
    assert st.station_topics() == []                                 # once per connection
    L.ANNOUNCED["newmac"] = "hq"
    st.on_message("isrkit/heartbeat/fpv-vision", hb("newmac", "fpv-vision"), False)
    assert st.station_topics() == []                                 # already known: nothing to ask
    st.station_subs = set()                                          # reconnect: ask again
    assert "isrkit/station/op-mac" in st.station_topics()


def test_announce_role_and_hotspot_ip(clean_stations):
    st = L.HeartbeatStore()
    st.on_message("isrkit/heartbeat/op-sentry", hb("op-mac"), False)
    st.on_message("isrkit/station/op-mac",
                  json.dumps({"host": "op-mac", "station": "op", "ip": "192.168.4.23"}).encode(), True)
    e = st.snapshot()["op-sentry"]
    assert e["station"] == "op" and e["station_label"] == "OP Mac" and L.STATION_IPS["op-mac"] == "192.168.4.23"
    # only a client address on the hotspot: never the hub, another network, or junk
    for bad in ("192.168.4.1", "10.0.0.5", "192.168.4.0", "192.168.4.255", "x", "192.168.4.300"):
        st.on_message("isrkit/station/op-mac",
                      json.dumps({"host": "op-mac", "station": "op", "ip": bad}).encode(), True)
        assert "op-mac" not in L.STATION_IPS, bad
    st.on_message("isrkit/station/corp-laptop-01", json.dumps({"host": "corp-laptop-01", "station": "hq"}).encode(), True)
    st.on_message("isrkit/heartbeat/change-detect", hb("corp-laptop-01", "change-detect"), False)
    assert st.snapshot()["change-detect"]["station_label"] == "HQ Mac"


def test_launcher_restarted_after_the_announce_still_learns_roles(clean_stations, tmp_path, monkeypatch):
    """The regression: announces are retained, but RabbitMQ only replays them to exact-topic subscribers."""
    port = free_port()
    log = tmp_path / "broker.log"
    broker = subprocess.Popen([sys.executable, str(REPO / "tools" / "rehearsal" / "broker.py"), str(port), str(log),
                               "--rabbit-retained"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        pub = MiniMQTT("127.0.0.1", port, "isrkit", "pw", client_id="mac")
        pub.connect()
        pub.publish("isrkit/station/corp-laptop-01", json.dumps({"host": "corp-laptop-01", "station": "hq"}).encode(), retain=True)
        pub.publish("isrkit/station/op-mac",
                    json.dumps({"host": "op-mac", "station": "op", "ip": "192.168.4.23"}).encode(), retain=True)
        time.sleep(0.3)
        # the emulation: a wildcard subscriber gets no retained announce
        w = MiniMQTT("127.0.0.1", port, "x", "y", client_id="w")
        w.connect()
        w.subscribe("isrkit/station/+")
        w.sock.settimeout(0.8)
        got = []
        try:
            while True:
                got.append(w.read_packet()[0])
        except (socket.timeout, TimeoutError):
            pass
        w.close()
        assert 3 not in got                       # SUBACK only, no PUBLISH
        # now the launcher starts, after the announces
        store = L.HeartbeatStore()
        monkeypatch.setattr(L, "heartbeats", store)
        monkeypatch.setattr(L, "MQTT_HOST", "127.0.0.1")
        monkeypatch.setattr(L, "MQTT_PORT", port)
        threading.Thread(target=L.heartbeat_thread, args=(stop,), daemon=True).start()
        time.sleep(0.8)
        pub.publish("isrkit/heartbeat/op-sentry", hb("op-mac"))
        pub.publish("isrkit/heartbeat/change-detect", hb("corp-laptop-01", "change-detect"))
        snap = {}
        for _ in range(60):
            time.sleep(0.1)
            snap = store.snapshot()
            if snap.get("op-sentry", {}).get("station") == "op" and snap.get("change-detect", {}).get("station") == "hq":
                break
        assert snap["op-sentry"]["station_label"] == "OP Mac" and snap["change-detect"]["station_label"] == "HQ Mac"
        assert L.STATION_IPS.get("op-mac") == "192.168.4.23"
        pub.close()
    finally:
        stop.set()
        broker.terminate()
        broker.wait(5)


def test_start_mac_re_announces_and_stops(tmp_path):
    """start-mac.sh announces at start and then every ISR_ANNOUNCE_EVERY seconds; stop ends the announcer."""
    port = free_port()
    log = tmp_path / "broker.log"
    broker = subprocess.Popen([sys.executable, str(REPO / "tools" / "rehearsal" / "broker.py"), str(port), str(log)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    conf, src, run = tmp_path / "conf", tmp_path / "src", tmp_path / "run"
    conf.mkdir(); src.mkdir()
    (conf / "station.env").write_text(f"ISR_STATION=op\nISR_HOST=op-test\nISR_REPOS=\nISR_SRC={src}\nISR_HUB=127.0.0.1\n"
                                      f"MQTT_HOST=127.0.0.1\nMQTT_PORT={port}\nMQTT_USER=isrkit\nMQTT_PASS=pw\n"
                                      "LLM_URL=http://127.0.0.1:1\nISR_WIFI_KEEPALIVE=0\n")
    env = dict(os.environ, ISR_CONF_DIR=str(conf), ISR_RUN_DIR=str(run), ISR_LOG_DIR=str(tmp_path / "logs"),
               TERM_PROGRAM="Apple_Terminal", ISR_ANNOUNCE_EVERY="1")
    sh = str(REPO / "laptop" / "start-mac.sh")
    try:
        time.sleep(0.4)
        subprocess.run([sh, "start"], capture_output=True, text=True, env=env, timeout=30)
        pid = int((run / "station-announce.pid").read_text())
        time.sleep(2.6)
        t0 = time.time()
        subprocess.run([sh, "stop"], capture_output=True, text=True, env=env, timeout=30)
        assert time.time() - t0 < 5                                    # the announcer stops at once
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        msgs = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        ann = [m for m in msgs if m["topic"] == "isrkit/station/op-test"]
        assert len(ann) >= 3 and all(m["retain"] for m in ann)
        assert json.loads(ann[0]["payload"]) == {"host": "op-test", "station": "op"}   # hub 127.0.0.1: no hotspot ip
        n = len(ann)
        time.sleep(1.5)
        assert len([m for m in (json.loads(x) for x in log.read_text().splitlines() if x.strip())
                    if m["topic"] == "isrkit/station/op-test"]) == n            # nothing after stop
    finally:
        broker.terminate()
        broker.wait(5)


def test_start_mac_announces_hotspot_ip():
    text = (REPO / "laptop" / "start-mac.sh").read_text()
    assert 'hub.startswith("192.168.4.") and ip.startswith("192.168.4.") and ip != hub' in text
    assert 'os.environ.get("ISR_TUNNEL", "0") != "1"' in text            # a tunnelled HQ links via the tunnel


# ── links and the Pi relay for a hotspot Mac ─────────────────────────────────

CAT = [{"unit": "op-sentry", "port": 8120, "class": "camera"}, {"unit": "op-sentry-stream", "port": 8120, "class": "camera"},
       {"unit": "change-detect", "port": 8130, "class": "mac"}, {"unit": "drone-detector", "port": 8060, "class": "light"}]


def test_relay_wanted_never_takes_a_pi_units_port():
    meta = {"remote": [{"tool": "op-sentry", "station": "op"}]}
    assert L.relay_wanted(CAT, meta, ["drone-detector"]) == {8120: "op-sentry", 8130: "change-detect"}
    # split OP: the Pi's op-sentry-stream owns 8120
    assert L.relay_wanted(CAT, meta, ["op-sentry-stream"]) == {8130: "change-detect"}


def test_direct_targets_and_link(clean_stations):
    L.STATION_IPS["op-mac"] = "192.168.4.23"
    hbs = {"op-sentry": {"host": "op-mac", "state": "live", "url": "http://op-mac.local:8120/"},
           "change-detect": {"host": "corp-laptop-01", "state": "live", "url": "http://192.168.4.1:8130/"}}
    wanted = {8120: "op-sentry", 8130: "change-detect"}
    assert L.direct_targets(CAT, wanted, hbs) == {8120: ("192.168.4.23", 8120)}
    assert L.direct_targets(CAT, wanted, {"op-sentry": dict(hbs["op-sentry"], state="offline")}) == {}
    assert L.direct_targets(CAT, {8130: "change-detect"}, hbs) == {}        # not a port the Pi may relay
    assert L.direct_link(CAT[0], hbs["op-sentry"]) == {"url": "http://192.168.4.23:8120/"}
    assert L.direct_link(CAT[2], hbs["change-detect"]) is None             # no hotspot address known


def test_pi_relays_a_hotspot_macs_page():
    """A phone opens http://<hub>:<port>/ and the Pi relays it to the Mac's hotspot address."""

    class Mac(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"op-sentry page"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    mac = ThreadingHTTPServer(("127.0.0.1", 0), Mac)
    threading.Thread(target=mac.serve_forever, daemon=True).start()
    relay_port = free_port()
    fw = L.TunnelForwarder(bind="127.0.0.1", target="127.0.0.9", offset=10000)   # no tunnel up
    try:
        fw.sync({relay_port: "op-sentry"}, {relay_port: ("127.0.0.1", mac.server_address[1])})
        assert fw.active() == {relay_port: "op-sentry"} and fw.via() == {relay_port: "relay"}
        c = http.client.HTTPConnection("127.0.0.1", relay_port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        assert r.status == 200 and r.read() == b"op-sentry page"
        fw.sync({relay_port: "op-sentry"}, {})                                # Mac gone: relay closes
        assert fw.active() == {}
    finally:
        fw.stop()
        mac.shutdown()


def test_hotspot_relay_does_not_flap(monkeypatch):
    """The bug on the Pi: with no HQ tunnel listener, the station relay on [::]:P answered the legacy-tunnel
    probe of 127.0.0.1:P itself, so every 5 s sync closed it and reopened it in the other mode. A port
    with a live hotspot station belongs to the station relay alone: same listener, sync after sync."""

    class Mac(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    mac = ThreadingHTTPServer(("127.0.0.1", 0), Mac)
    threading.Thread(target=mac.serve_forever, daemon=True).start()
    port = free_port()
    monkeypatch.setattr(L, "HUB_IP", "127.0.0.1")              # the legacy bind would be the hub IP
    fw = L.TunnelForwarder(bind=None, target="127.0.0.1", offset=10000)   # the Pi's defaults; no tunnel at port+10000
    direct = {port: ("127.0.0.1", mac.server_address[1])}
    opened = []
    real_listen = fw._listen
    monkeypatch.setattr(fw, "_listen", lambda *a, **k: (opened.append(a[2]), real_listen(*a, **k)))
    try:
        fw.sync({port: "op-sentry"}, direct)
        first = fw.listeners[port][0]
        for _ in range(4):
            fw.sync({port: "op-sentry"}, direct)
            assert fw.listeners[port][0] is first and fw.via() == {port: "relay"}
        assert opened == [direct[port]]                             # opened once, never in tunnel mode
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        assert c.getresponse().read() == b"ok"
        # the station goes (heartbeat offline): the relay closes, and only then may a tunnel take the port
        fw.sync({port: "op-sentry"}, {})
        assert port not in fw.listeners or fw.via()[port] == "tunnel"
    finally:
        fw.stop()
        mac.shutdown()


def test_board_links_a_hotspot_mac_by_relay_or_ip():
    sys.path.insert(0, str(Path(__file__).parent))
    from test_ui import FAKE, _node
    st = json.loads(json.dumps(FAKE))
    e = {"state": "live", "status": "ok", "station": "op", "station_label": "OP Mac", "host": "op-mac",
         "url": "http://op-mac.local:8120/"}
    st["remote"] = [{"tool": "op-sentry", "expected_station": "op", "expected_label": "OP Mac", "port": 8120, "heartbeat": e,
                     "direct": {"url": "http://192.168.4.23:8120/"}, "tunnel": None},
                    {"tool": "fpv-vision", "expected_station": "hq", "expected_label": "HQ Mac", "port": 8110,
                     "heartbeat": dict(e, station="hq"), "direct": {"url": "http://192.168.4.23:8110/"},
                     "tunnel": {"port": 8110, "via": "relay"}}]
    r = _node("status = " + json.dumps(st) + """;
console.log(JSON.stringify({op: toolInfo(status, "op-sentry", "OP sentry", ""), fpv: toolInfo(status, "fpv-vision", "FPV", "")}));""")
    assert r["op"]["href"] == "http://192.168.4.23:8120/" and r["op"]["where"] == "OP Mac"   # by IP, never .local
    assert r["fpv"]["href"] == "http://isr-pi.local:8110/"                                   # the Pi's relay wins
