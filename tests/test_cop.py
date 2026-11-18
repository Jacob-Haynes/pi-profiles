"""The launcher's common operating picture (/map): CoT parsing into the picture, fading and removal, the
JSON and SSE API, the TAK viewer registration (contract v5) against the rehearsal relay that emulates OTS,
the tile proxy, and the page loading only local assets. Positions are placeholders, never a real OP."""
import hashlib
import http.client
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
sys.path.insert(0, str(REPO / "tools" / "rehearsal"))
import cop  # noqa: E402
import isr_launcher as L  # noqa: E402

LAT, LON = 51.2, -1.8          # placeholder site
STATIC = REPO / "launcher" / "static"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event(uid="DRONE-1", typ="a-h-A-M-F-Q", lat=LAT, lon=LON, stale=300, now=None, detail="", hae="9999999.0", how="m-g"):
    now = now or time.time()
    return (f'<event version="2.0" uid="{uid}" type="{typ}" how="{how}" time="{iso(now)}" start="{iso(now)}" '
            f'stale="{iso(now + stale)}"><point lat="{lat}" lon="{lon}" hae="{hae}" ce="15" le="9999999.0"/>'
            f'<detail>{detail}</detail></event>').encode()


# ── parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("typ,sidc,aff,layer", [
    ("a-h-A-M-F-Q", "SHAPMFQ--------", "h", "contact"),       # hostile UAV (drone-detector)
    ("a-u-A-M-F-Q", "SUAPMFQ--------", "u", "contact"),       # unknown UAV
    ("a-u-A-M-H", "SUAPMH---------", "u", "contact"),         # rotary-wing (op-sentry / fpv air watch)
    ("a-n-A-C", "SNAPC----------", "n", "contact"),           # civil aircraft
    ("a-f-G-E-S", "SFGPES---------", "f", "sensor"),          # a station's registration SA / sensor
    ("a-f-G-U-C", "SFGPUC---------", "f", "friend"),          # BFT mesh node
    ("a-u-G-U-C-I", "SUGPUCI--------", "u", "contact"),       # op-sentry person
    ("a-u-G", "SUGP-----------", "u", "contact"),             # change-detect / geo-locate
    ("a-s-G", "SSGP-----------", "h", "contact"),             # suspect folds into hostile
    ("a-f-G-U-C-Ixx", "SFGP-----------", "f", "friend"),      # odd function letters: generic symbol
])
def test_types_to_symbols(typ, sidc, aff, layer):
    it = cop.parse_event(event(typ=typ))
    assert it["sidc"] == sidc and len(it["sidc"]) == 15 and it["aff"] == aff and it["layer"] == layer


def test_track_stale_links_and_modes():
    now = time.time()
    it = cop.parse_event(event(now=now, stale=300, detail=(
        '<contact callsign="SIM DJI Mavic 3"/><track course="370.0" speed="21.0"/>'
        '<link uid="isrkit-drone-detector-isr-pi" relation="p-p" type="a-f-G-E-S"/>'
        '<remarks>SIMULATED: drone-detector: DRONE_CONFIRMED</remarks>')))
    assert it["course"] == 10.0 and it["speed"] == 21.0 and it["callsign"] == "SIM DJI Mavic 3"
    assert abs(it["stale"] - (now + 300)) < 0.01 and abs(it["time"] - now) < 0.01
    assert it["parent"] == "isrkit-drone-detector-isr-pi" and it["links"][0]["relation"] == "p-p"
    assert it["mode"] == "sim" and it["hae"] is None and it["ce"] == 15
    rec = cop.parse_event(event(detail="<remarks>RECORDED: 2x PERSON at bearing 045</remarks>"))
    assert rec["mode"] == "rec"
    assert cop.parse_event(event(detail="<remarks>[SIM] mesh</remarks>"))["mode"] == "sim"
    assert cop.parse_event(event(detail='<contact callsign="Alpha 1"/>'))["mode"] == "live"
    assert cop.parse_event(event(detail='<track course="90" />'))["course"] is None      # course without speed
    assert cop.parse_event(event(hae="120.5"))["hae"] == 120.5


def test_bearing_line_and_uncertainty_circle():
    line = cop.parse_event(event(uid="OPSENTRY-OP1-brg-7", typ="u-d-f", how="h-e", detail=(
        f'<link point="{LAT},{LON}"/><link point="51.2025,-1.7964"/><strokeColor value="-65536"/>'
        '<contact callsign="OP1 BRG 045 PERSON (range unknown)"/><remarks>Bearing only.</remarks>')))
    assert line["line"] == [[LAT, LON], [51.2025, -1.7964]] and line["layer"] == "bearing" and line["sidc"] is None
    circ = cop.parse_event(event(uid="GEO-abc-C", typ="u-d-c-c", how="h-e",
                                 detail='<shape><ellipse major="300" minor="300" angle="360"/></shape>'))
    assert circ["circle"] == 300.0 and circ["layer"] == "contact"
    assert cop.parse_event(event(typ="u-d-f", detail='<link point="51.2,-1.8"/>')) is None    # one point: no line
    assert cop.parse_event(event(typ="u-d-r")) is None                                        # a drawing we don't render


def test_not_for_the_map():
    # C5.2 "unknown position" registration (the viewer's own, or any tool without a site)
    assert cop.parse_event(event(typ="a-f-G-E-S", lat=0, lon=0)) is None
    for typ in ("t-x-c-t", "b-f-t-r", "b-t-f", "y-a-r"):          # pings, file shares, GeoChat, acks
        assert cop.parse_event(event(typ=typ)) is None
    assert cop.parse_event(event(lat=95)) is None
    assert cop.parse_event(b"<event uid='x' type='a-h-G'><point lat='1' lon='1'/>") is None   # cut off
    assert cop.parse_event(b'<!DOCTYPE x [<!ENTITY a "aaaa">]><event uid="x" type="a-h-G"><point lat="1" lon="1"/></event>') is None
    assert cop.parse_event(b"<event " + b"x" * (cop.MAX_EVENT + 1)) is None
    sos = cop.parse_event(event(typ="b-a-o-tbl", detail='<emergency type="911 Alert">Alpha</emergency>'))
    assert sos["emergency"] is True and sos["layer"] == "friend"


def test_no_range_contacts():
    """Contacts the tools place at the sensor because they have no range: flagged, with count and bearing."""
    op = cop.parse_event(event(uid="OPSENTRY-OP1-det-1", typ="a-u-G-U-C-I", how="m-r", detail=(
        '<contact callsign="OP1 2xPERSON BRG 045"/><remarks>RECORDED: 2x PERSON detected by OP1 at bearing 045 deg '
        '(true), est. range unknown. Marker is placed at the OP; the contact is somewhere along the bearing line.</remarks>')))
    assert op["norange"] and (op["count"], op["what"], op["bearing"]) == (2, "person", 45)
    rf = cop.parse_event(event(uid="RFSPEC-DRONE", typ="a-u-A", detail=(
        "<remarks>RF: 2.4 GHz burst, bearing unknown. Energy detection only; marker is at the sensor, not the drone.</remarks>")))
    assert rf["norange"] and rf["bearing"] is None and rf["count"] == 1
    ac = cop.parse_event(event(uid="ACOUSTIC-1-UAV", detail="<remarks>Marker is the SENSOR position: target is within ~150 m, bearing unknown.</remarks>"))
    assert ac["norange"]
    # a track with its own position and course is never "no range"; nor a friend, a line or a circle
    dd = cop.parse_event(event(detail='<track course="90" speed="5"/><remarks>range unknown</remarks>'))
    assert not dd["norange"]
    assert not cop.parse_event(event(typ="a-f-G-U-C", detail="<remarks>no range</remarks>"))["norange"]
    assert not cop.parse_event(event(typ="u-d-f", detail='<link point="51.2,-1.8"/><link point="51.21,-1.8"/><remarks>range unknown</remarks>'))["norange"]
    assert not cop.parse_event(event(uid="CHANGE-1", typ="a-u-G", detail="<remarks>Change detected: a car</remarks>"))["norange"]


def test_deletes_and_already_stale():
    """A CoT delete (t-x-d-d, ATAK's forcedelete) and an update that arrives stale end an item at once."""
    clk = Clock(1_000_000.0)
    p = cop.Picture(clock=clk)
    p.ingest(cop.parse_event(event(uid="OPSENTRY-OP1-det-9", now=clk.t)))
    p.ingest(cop.parse_event(event(uid="OPSENTRY-OP1-brg-9", typ="u-d-f", now=clk.t,
                                   detail='<link point="51.2,-1.8"/><link point="51.21,-1.8"/>')))
    delete = (b'<event version="2.0" uid="x-del" type="t-x-d-d" how="h-g-i-g-o" time="2026-11-17T12:00:00Z" '
              b'start="2026-11-17T12:00:00Z" stale="2026-11-17T12:00:20Z"><point lat="0" lon="0" hae="9999999"/>'
              b'<detail><link uid="OPSENTRY-OP1-det-9" relation="none" type="none"/><__forcedelete/></detail></event>')
    assert cop.delete_uids(delete) == ["OPSENTRY-OP1-det-9"] and cop.parse_event(delete) is None
    seq = p.seq
    assert p.remove(cop.delete_uids(delete)) == 1 and "OPSENTRY-OP1-det-9" not in p.items
    assert p.delta(seq)["remove"] == ["OPSENTRY-OP1-det-9"]                       # streamed to the page at once
    assert cop.delete_uids(event()) == [] and cop.delete_uids(b"<event t-x-d-d") == []
    # an update that is already stale on arrival: gone now, no fade
    assert not p.ingest(cop.parse_event(event(uid="OPSENTRY-OP1-brg-9", typ="u-d-f", now=clk.t - 30, stale=10,
                                              detail='<link point="51.2,-1.8"/><link point="51.21,-1.8"/>')))
    assert "OPSENTRY-OP1-brg-9" not in p.items


def test_split_events_across_reads():
    a, b = event(uid="A"), event(uid="B")
    stream = a + b"\n" + b + b"<event uid='C'"
    evs, rest = cop.split_events(stream[:len(a) - 10])
    assert evs == [] and rest == stream[:len(a) - 10]
    evs, rest = cop.split_events(stream)
    assert evs == [a, b] and rest == b"<event uid='C'"
    evs, rest = cop.split_events(b"garbage with no events" * 10)
    assert evs == [] and len(rest) <= 16


# ── the picture ──────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_picture_fade_remove_and_deltas():
    clk = Clock(1_000_000.0)
    p = cop.Picture(clock=clk, fade_s=60)
    assert p.ingest(cop.parse_event(event(uid="A", now=clk.t, stale=30)))
    assert p.ingest(cop.parse_event(event(uid="B", now=clk.t, stale=300)))
    s0 = p.snapshot()
    assert [i["uid"] for i in s0["items"]] == ["B", "A"] and all(i["state"] == "live" for i in s0["items"])
    clk.t += 45                                             # A is stale: fading, still there
    assert {i["uid"]: i["state"] for i in p.snapshot()["items"]} == {"A": "fading", "B": "live"}
    seq = p.seq
    clk.t += 60                                             # past stale + fade: gone
    d = p.delta(seq)
    assert d["remove"] == ["A"] and d["upsert"] == []
    assert [i["uid"] for i in p.snapshot()["items"]] == ["B"]
    # an update is an upsert after the last seq; an older copy arriving late is ignored
    seq = p.seq
    newer = cop.parse_event(event(uid="B", now=clk.t, stale=300))
    assert p.ingest(newer) and [i["uid"] for i in p.delta(seq)["upsert"]] == ["B"]
    assert not p.ingest(cop.parse_event(event(uid="B", now=clk.t - 100, stale=300)))
    # already gone on arrival
    assert not p.ingest(cop.parse_event(event(uid="old", now=clk.t - 1000, stale=10)))
    # removals forgotten: a client that far behind gets a snapshot instead
    p.oldest_removal = p.seq
    assert p.delta(0) is None


def test_picture_cap():
    clk = Clock(1_000_000.0)
    p = cop.Picture(clock=clk)
    for i in range(cop.MAX_ITEMS + 5):
        p.ingest(cop.parse_event(event(uid=f"U{i}", now=clk.t, stale=100 + i)))
    assert len(p.items) == cop.MAX_ITEMS and "U0" not in p.items


# ── the TAK viewer against the OTS-emulating relay ───────────────────────────

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_registration_sa_is_a_viewer():
    sa = cop.registration_sa("isr-pi", now=1_800_000_000.0).decode()
    assert 'uid="isrkit-launcher-map-isr-pi"' in sa and 'callsign="COP-VIEW isr-pi"' in sa
    assert 'type="a-f-G-E-S"' in sa and 'platform="isr-kit"' in sa and 'endpoint="*:-1:stcp"' in sa
    t = re.search(r'start="([^"]+)"', sa).group(1)
    assert f'stale="{t}"' in sa and t.endswith("Z")              # stale = start: never drawn
    assert 'lat="0" lon="0" hae="9999999.0"' in sa                # the C5.2 unknown position
    assert cop.parse_event(sa.encode()) is None                   # and the COP itself never draws it
    assert cop.identity("ISR Pi.local")[0] == "isrkit-launcher-map-isr-pi-local"


def test_viewer_registers_and_receives_via_relay(tmp_path):
    """Contract v5: wait 3 s, register, get bound; OTS-style routing then delivers others' CoT."""
    import cot_relay
    port = free_port()
    log = tmp_path / "relay.jsonl"
    relay = cot_relay.Relay(port, str(log), bind_delay=2.5)
    threading.Thread(target=relay.serve, daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    pic = cop.Picture()
    client = cop.CopClient(pic, "127.0.0.1", port, me="isr-pi", reg_every=1.0)
    stop = threading.Event()
    threading.Thread(target=client.run, args=(stop,), daemon=True).start()
    # a sender that registers like every kit tool, then sends one drone
    s = socket.create_connection(("127.0.0.1", port))
    time.sleep(3.0)
    s.sendall(event(uid="isrkit-drone-detector-test", typ="a-f-G-E-S",
                    detail='<contact callsign="DRONE-DET test" endpoint="*:-1:stcp"/><takv platform="isr-kit"/>'))
    for _ in range(80):
        if client.status()["registered_at"]:
            break
        time.sleep(0.1)
    time.sleep(0.8)
    # the sender re-sends its SA (tools do every 60 s): the viewer is bound by now, so it gets that too
    s.sendall(event(uid="isrkit-drone-detector-test", typ="a-f-G-E-S",
                    detail='<contact callsign="DRONE-DET test" endpoint="*:-1:stcp"/><takv platform="isr-kit"/>'))
    s.sendall(event(uid="DRONE-TEST", detail='<contact callsign="Mavic"/><track course="90" speed="5"/>'))
    for _ in range(50):
        if "DRONE-TEST" in pic.items:
            break
        time.sleep(0.1)
    time.sleep(1.5)                                      # one SA re-send (reg_every=1 s)
    stop.set()
    s.close()
    assert "DRONE-TEST" in pic.items and pic.items["DRONE-TEST"]["course"] == 90.0
    assert "isrkit-drone-detector-test" in pic.items      # the sender's SA is a sensor on the map
    st = client.status()
    assert st["connected"] or st["registered_at"]
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    mine = [r for r in rows if r["conn_uid"] == "isrkit-launcher-map-isr-pi"]
    reg = [r for r in mine if r["registration"]]
    assert reg and reg[0]["registration"] == "registered isrkit-launcher-map-isr-pi as 'COP-VIEW isr-pi'"
    assert reg[0]["reg_delay_s"] >= 2.9 and reg[0]["bound"] is True
    # it only ever sent its own registration SA: never a marker, never anything forwarded
    assert len(mine) >= 2 and all(r["uid"] == "isrkit-launcher-map-isr-pi" and r["type"] == "a-f-G-E-S" for r in mine)
    assert all('lat="0" lon="0"' in r["raw"] for r in mine)


def test_viewer_reconnects_with_backoff():
    pic = cop.Picture()
    calls = []

    def refuse(addr, timeout):
        calls.append(time.time())
        raise ConnectionRefusedError("no OTS")
    client = cop.CopClient(pic, "127.0.0.1", 1, connect=refuse)
    stop = threading.Event()
    t = threading.Thread(target=client.run, args=(stop,), daemon=True)
    t.start()
    time.sleep(2.5)
    stop.set()
    t.join(5)
    assert len(calls) == 2 and not client.status()["connected"] and "no OTS" in client.status()["error"]


# ── the HTTP API ─────────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    site = tmp_path / "site.env"
    site.write_text(f"ISR_HOST=isr-pi\nSENSOR_LAT={LAT}\nSENSOR_LON={LON}\n")
    monkeypatch.setattr(L, "SITE_ENV", site)
    pic = cop.Picture()
    monkeypatch.setattr(L, "picture", pic)
    monkeypatch.setattr(L.probes, "data", {"maps": {"reachable": True, "packs": [
        {"name": "exercise", "title": "Exercise area", "minzoom": 12, "maxzoom": 17, "bounds": [-1.82, 51.18, -1.78, 51.22],
         "attribution": "Synthetic", "format": "png", "size_mb": 1}]}})
    srv = L.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], pic
    srv.shutdown()
    srv.server_close()


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_api_cop_snapshot(api):
    port, pic = api
    pic.ingest(cop.parse_event(event(uid="DRONE-1")))
    pic.ingest(cop.parse_event(event(uid="OPSENTRY-OP1-brg-1", typ="u-d-f", detail='<link point="51.2,-1.8"/><link point="51.21,-1.79"/>')))
    code, h, body = get(port, "/api/cop")
    d = json.loads(body)
    assert code == 200 and h["Cache-Control"] == "no-store"
    assert {i["uid"] for i in d["items"]} == {"DRONE-1", "OPSENTRY-OP1-brg-1"}
    assert d["site"] == {"lat": LAT, "lon": LON} and d["maps"]["packs"][0]["name"] == "exercise"
    assert d["maps"]["packs"][0]["format"] == "png" and d["tak"]["uid"].startswith("isrkit-launcher-map-")
    assert d["seq"] == pic.seq and d["fade_s"] == cop.FADE_S and "now" in d


def test_api_cop_stream(api):
    port, pic = api
    pic.ingest(cop.parse_event(event(uid="A")))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/api/cop/stream")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")

    def next_event():
        name, data = None, None
        while True:
            line = r.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line == "" and name:
                return name, data
    name, snap = next_event()
    assert name == "snapshot" and [i["uid"] for i in snap["items"]] == ["A"]
    pic.ingest(cop.parse_event(event(uid="B")))
    name, d = next_event()
    assert name == "delta" and [i["uid"] for i in d["upsert"]] == ["B"] and d["seq"] == pic.seq
    c.close()


def test_api_cop_stream_cap(api, monkeypatch):
    port, _ = api
    monkeypatch.setattr(L, "cop_streams", threading.BoundedSemaphore(1))
    L.cop_streams.acquire()
    try:
        code, _, body = get(port, "/api/cop/stream")
        assert code == 503 and "poll" in json.loads(body)["error"]
    finally:
        L.cop_streams.release()


def test_api_cop_pin(api, monkeypatch):
    port, _ = api
    monkeypatch.setattr(L, "PIN", "4321")
    monkeypatch.setattr(L, "PIN_FOR_VIEW", True)
    assert get(port, "/api/cop")[0] == 401
    assert get(port, "/api/cop", {"X-PIN": "4321"})[0] == 200


def test_tile_proxy(api, monkeypatch):
    port, _ = api
    png = b"\x89PNG\r\n\x1a\n" + b"tile"
    seen = []

    class Maps(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            if self.path == "/tiles/exercise/15/16300/10900.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(png)
            else:
                self.send_response(404)
                self.end_headers()
    ms = ThreadingHTTPServer(("127.0.0.1", 0), Maps)
    threading.Thread(target=ms.serve_forever, daemon=True).start()
    monkeypatch.setattr(L, "MAPS_URL", f"http://127.0.0.1:{ms.server_address[1]}")
    code, h, body = get(port, "/tiles/exercise/15/16300/10900.png")
    assert code == 200 and body == png and h["Content-Type"] == "image/png" and "max-age" in h["Cache-Control"]
    assert get(port, "/tiles/exercise/15/1/1.png")[0] == 404
    assert get(port, "/tiles/../etc/1/1/1.png")[0] == 404 and get(port, "/tiles/x/1/1/1.gif")[0] == 404
    assert seen == ["/tiles/exercise/15/16300/10900.png", "/tiles/exercise/15/1/1.png"]   # nothing else proxied
    monkeypatch.setattr(L, "MAPS_URL", "http://127.0.0.1:1")
    assert get(port, "/tiles/exercise/15/1/1.png")[0] == 502
    ms.shutdown()


def test_site_position(tmp_path, monkeypatch):
    f = tmp_path / "site.env"
    monkeypatch.setattr(L, "SITE_ENV", f)
    assert L.site_position() is None                         # no file
    f.write_text('SENSOR_LAT="51.2"\nSENSOR_LON=-1.8\n')
    assert L.site_position() == {"lat": 51.2, "lon": -1.8}
    f.write_text("SENSOR_LAT=0\nSENSOR_LON=0\n")
    assert L.site_position() is None                         # unset (0,0) is never a site


# ── the page ─────────────────────────────────────────────────────────────────

def test_page_routes_and_local_assets(api):
    port, _ = api
    code, h, html = get(port, "/map")
    html = html.decode()
    assert code == 200 and "<title>COP · ISTAR kit</title>" in html
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs and all(r.startswith("/") and not r.startswith("//") for r in refs), refs   # same host only
    assert all("?v=" in r for r in refs if not r.startswith("/engineer") and r not in ("/",)), refs
    for r in refs:
        if r.startswith(("/engineer",)) or r == "/":
            continue
        c, hh, body = get(port, r)
        assert c == 200 and body, r
    assert "default-src 'self'" in h["Content-Security-Policy"]          # the browser enforces it too
    # our own scripts fetch nothing off-host (the SVG namespace isn't a request)
    for f in ("map.js", "mgrs.js"):
        text = (STATIC / f).read_text()
        assert not [u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if not u.startswith("http://www.w3.org/2000/svg")], f
    assert "url(http" not in (STATIC / "vendor" / "leaflet.css").read_text()
    assert get(port, "/vendor/../isr_launcher.py")[0] == 404 and get(port, "/vendor/nope.js")[0] == 404
    # the board and the engineer view link to it
    for page in ("board.html", "index.html"):
        assert 'href="/map"' in (STATIC / page).read_text()


VENDOR = {   # unchanged copies of the npm releases (see launcher/static/vendor/README.md)
    "leaflet.js": "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    "leaflet.css": "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
    "milsymbol.js": "16227943002e45506db5555f6015a67e42b3ed263df61653ec07b0f9c04a10c4",
}


def test_vendored_libraries_and_licences():
    v = STATIC / "vendor"
    for name, want in VENDOR.items():
        assert hashlib.sha256((v / name).read_bytes()).hexdigest() == want, name
    assert "BSD 2-Clause" in (v / "LICENSE-leaflet.txt").read_text()
    assert "MIT License" in (v / "LICENSE-milsymbol.txt").read_text()
    html = (STATIC / "map.html").read_text()
    assert "/vendor/leaflet.js?v=1.9.4" in html and "/vendor/milsymbol.js?v=3.0.4" in html
    assert set(L.VENDOR_FILES) == set(VENDOR) | {"LICENSE-leaflet.txt", "LICENSE-milsymbol.txt"}
    assert 'install -m 644 -o root -g root "$SRC"/launcher/static/vendor/*' in (REPO / "install.sh").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_mgrs_and_symbols_in_node():
    js = f"""
const M = require({json.dumps(str(STATIC / "mgrs.js"))});
const ms = require({json.dumps(str(STATIC / "vendor" / "milsymbol.js"))});
const out = {{
  usng: M.toMGRS(38.8895, -77.0352, 4),         // the USNG reference example (Washington Monument)
  full: M.toMGRS(38.8895, -77.0352),
  south: M.toMGRS(-33.8568, 151.2153),
  norway: M.toMGRS(60.0, 5.0).slice(0, 3),      // the 32V exception
  svalbard: M.toMGRS(78.0, 15.0).slice(0, 3),   // 33X
  polar: M.toMGRS(85, 0),
  valid: ["SHAPMFQ--------", "SUAPMH---------", "SNAPC----------", "SFGPES---------", "SFGPUC---------", "SUGP-----------"]
    .map((s) => new ms.Symbol(s, {{ size: 24, fillColor: "#ff8080" }}).isValid()),
}};
console.log(JSON.stringify(out));"""
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    assert r["usng"] == "18S UJ 2348 0648" and r["full"].startswith("18S UJ 2348")
    assert r["south"].startswith("56H ") and r["norway"] == "32V" and r["svalbard"] == "33X" and r["polar"] is None
    assert all(r["valid"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_map_js_labels():
    """map.js's type labels, fade and course leader, run in node with stand-ins for the browser."""
    src = (STATIC / "map.js").read_text()
    js = """
global.document = { documentElement: {}, addEventListener() {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
global.localStorage = { getItem: () => null, setItem() {} };
""" + src + """
console.log(JSON.stringify({
  uav: typeLabel("a-h-A-M-F-Q"), heli: typeLabel("a-u-A-M-H"), civ: typeLabel("a-n-A-C"), person: typeLabel("a-u-G-U-C-I"),
  sensor: typeLabel("a-f-G-E-S"), unit: typeLabel("a-f-G-U-C"), line: typeLabel("u-d-f"), odd: typeLabel("x-y-z"),
  live: alpha({ stale: serverNow() + 10 }), half: alpha({ stale: serverNow() - fadeS / 2 }), gone: alpha({ stale: serverNow() - fadeS - 1 }),
  lead: dest(51.2, -1.8, 90, 1000).map((v) => +v.toFixed(5)),
  what: [whatOf({ what: "person", type: "a-u-G-U-C-I" }), whatOf({ type: "a-u-A-M-F-Q" }), whatOf({ type: "a-u-A" })],
  size: [symSize(10), symSize(14), symSize(17), symSize(20)],
  dom: [dominantAff([{ aff: "f" }, { aff: "f" }, { aff: "h" }]), dominantAff([{ aff: "u" }, { aff: "h" }]), dominantAff([{ aff: "n" }])],
  badge: badgeText([{ callsign: "OP1 2xPERSON BRG 045", what: "person", count: 2 }, { callsign: "OP1 1xPERSON BRG 080", what: "person", count: 1 },
                    { callsign: "OP1 1xVEHICLE BRG 300", what: "vehicle", count: 1 }], { uid: "__site" }),
  badge2: badgeText([{ callsign: "SIM RF 2.4 GHz", type: "a-u-A" }], { uid: "isrkit-rf", callsign: "SIM RF-SPEC isr-pi" }),
  shown: (() => {
    offset = 0; const now = serverNow();
    const r = [shown({ uid: "a", time: now - 10, stale: now + 60 }),                                  // fresh
               shown({ uid: "b", line: [[0, 0], [1, 1]], layer: "bearing", time: now - 200, stale: now + 600 }),   // bearing past 3 min
               shown({ uid: "c", norange: true, time: now - 200, stale: now + 600 }),                // no-range detection past 3 min
               shown({ uid: "d", time: now - 200, stale: now + 600 })];                              // positioned: its stale decides
    prefs.hideBefore = now - 120;                                                                    // "Clear old"
    r.push(shown({ uid: "e", time: now - 130, stale: now + 600 }), shown({ uid: "f", time: now - 5, stale: now + 600 }),
           shown({ uid: "__site", time: 0, stale: now + 600 }));
    prefs.hideBefore = 0;
    return r;
  })(),
  cells: (() => {
    map = { project: (ll, z) => ({ x: ll[1] * 1000 * (z - 9), y: -ll[0] * 1000 * (z - 9) }) };
    const pts = [{ lat: 51.2, lon: -1.8 }, { lat: 51.2001, lon: -1.8001 }, { lat: 51.3, lon: -1.7 }];
    return [clusters(pts, 12).map((c) => c.length).sort(), clusters(pts, 16).map((c) => c.length)];
  })(),
  near: (() => {
    map = { distance: (a, b) => Math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 69700) };
    const s = [{ lat: 51.2, lon: -1.8 }];
    return [!!isNoRange({ layer: "contact", lat: 51.2, lon: -1.8, ce: null }, s),          // on the sensor, no accuracy
            !!isNoRange({ layer: "contact", lat: 51.2, lon: -1.8, ce: 15 }, s),            // on it, but precise: a fix
            !!isNoRange({ layer: "contact", lat: 51.21, lon: -1.8, norange: true }, s),    // flagged: its own spot
            !!isNoRange({ layer: "contact", lat: 51.2, lon: -1.8, course: 90 }, s),        // a track
            !!isNoRange({ layer: "friend", lat: 51.2, lon: -1.8 }, s)];
  })(),
}));"""
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    assert (r["uav"], r["heli"], r["civ"], r["person"], r["sensor"], r["unit"], r["line"], r["odd"]) == \
        ("UAV", "Rotary-wing", "Civil aircraft", "Person", "Sensor", "Unit", "Bearing line", "x-y-z")
    assert r["live"] == 1 and 0.12 <= r["half"] < 0.45 and r["gone"] == 0
    assert r["what"] == ["person", "UAV", "air track"]
    assert r["size"] == [14, 20, 26, 26]                                    # smaller zoomed out, with a minimum
    assert r["dom"] == ["f", "h", "n"]                                      # commonest affiliation; hostile wins a tie
    assert r["badge"] == "OP1 · 3 person · 1 vehicle" and r["badge2"] == "RF-SPEC · 1 air track"
    assert r["shown"] == [True, False, False, True, False, True, True]
    assert r["cells"] == [[1, 2], [1, 1, 1]]                                # overlapping symbols cluster below zoom 16
    assert r["near"] == [True, False, True, False, False]
    assert r["lead"][0] == pytest.approx(51.2, abs=1e-4) and r["lead"][1] == pytest.approx(-1.8 + 1000 / (111320 * 0.6266), abs=2e-4)
