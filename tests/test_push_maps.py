"""isr-push-maps: TAK data package build, upload to a fake OTS (1.8.3 Marti routes), fileshare CoT over a
fake TAK port, the launcher endpoint and the optional auto mode. Real zips, real manifest parsing."""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "maps"))
sys.path.insert(0, str(REPO / "launcher"))
sys.path.insert(0, str(REPO / "tests"))

import isr_push_maps as P           # noqa: E402
from test_maps import make_pack     # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeOTS:
    """The OTS 1.8.3 data-package routes we use, on plain HTTP like nginx's :8080."""

    def __init__(self, limit=None):
        self.store, self.uploads, self.limit = {}, [], limit
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                u = urlsplit(self.path)
                ok = u.path == "/Marti/sync/content" and parse_qs(u.query).get("hash", [""])[0] in fake.store
                self.send_response(200 if ok else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                u = urlsplit(self.path)
                h = parse_qs(u.query).get("hash", [""])[0]
                if u.path != "/Marti/sync/content" or h not in fake.store:
                    self.send_response(404); self.end_headers(); return
                body = fake.store[h]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers["Content-Length"])
                if fake.limit and n > fake.limit:
                    self.rfile.read(n)
                    self.send_response(413); self.end_headers(); return
                raw = self.rfile.read(n)
                b = re.search(r"boundary=(\S+)", self.headers["Content-Type"]).group(1).encode()
                part = raw.split(b"--" + b)[1]
                head, data = part.split(b"\r\n\r\n", 1)
                data = data[:-2]                                     # trailing CRLF before the boundary
                fn = re.search(rb'filename="([^"]+)"', head).group(1).decode()
                assert b'name="assetfile"' in head and fn.endswith(".zip")
                assert u.path == "/Marti/sync/missionupload" and re.fullmatch(r"[a-f0-9]{64}", q["hash"])
                fake.uploads.append(dict(q, file=fn, size=len(data)))
                fake.store[q["hash"]] = data
                body = f"https://127.0.0.1:8443/Marti/api/sync/metadata/{q['hash']}/tool".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


class FakeTAK:
    """Accepts TCP clients; records what each sends and when; can push events to them."""

    def __init__(self, greet=b""):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.clients, self.greet = [], greet
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            rec = {"t0": time.monotonic(), "data": b"", "first_at": None}
            self.clients.append(rec)
            threading.Thread(target=self._read, args=(c, rec), daemon=True).start()

    def _read(self, c, rec):
        sent_greet = False
        c.settimeout(0.2)
        while True:
            try:
                d = c.recv(65536)
                if not d:
                    return
                if rec["first_at"] is None:
                    rec["first_at"] = time.monotonic() - rec["t0"]
                rec["data"] += d
                if self.greet and not sent_greet and b"a-f-G-E-S" in rec["data"]:
                    c.sendall(self.greet)
                    sent_greet = True
            except socket.timeout:
                continue
            except OSError:
                return

    def events(self, i=0):
        return re.findall(rb"<event\b.*?</event>", self.clients[i]["data"], re.S)


@pytest.fixture
def kit(tmp_path, monkeypatch):
    maps = tmp_path / "maps"
    maps.mkdir()
    make_pack(maps / "op-area.mbtiles", [(1, 0, 0), (1, 1, 0)], name="OP area")
    import isr_maps as M
    monkeypatch.setattr(M, "catalog", M.Catalog(maps))
    monkeypatch.setattr(M, "PUBLIC_BASE", "http://192.168.4.1:8160")
    msrv = M.make_server("127.0.0.1", 0)
    threading.Thread(target=msrv.serve_forever, daemon=True).start()
    ots, tak = FakeOTS(), FakeTAK()
    conf = tmp_path / "etc"
    conf.mkdir()
    (conf / "site.env").write_text("ISR_HOST=isr-pi\n")          # no SENSOR_* set
    env = {"ISR_KIT_CONF": str(conf), "ISR_MAPS_DIR": str(maps), "ISR_MAPS_URL": f"http://127.0.0.1:{msrv.server_address[1]}",
           "ISR_OTS_URL": ots.url, "ISR_OTS_PUBLIC_URL": "http://192.168.4.1:8080",
           "TAK_HOST": "127.0.0.1", "TAK_PORT": str(tak.port)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(P, "REG_WAIT_S", 0.3)
    yield {"maps": maps, "ots": ots, "tak": tak, "env": env, "conf": conf, "tmp": tmp_path}
    msrv.shutdown(); msrv.server_close(); ots.srv.shutdown(); tak.sock.close()


def fast_link(sleeps):
    return lambda s: P.TakLink(s, sleep=lambda x: (sleeps.append(x), time.sleep(min(x, 0.3))))


# ── package ──────────────────────────────────────────────────────────────────

def test_package_is_a_valid_v2_data_package_with_the_map_source(kit, tmp_path):
    s = P.settings()
    xml, entry = P.fetch_map_source(s["ISR_MAPS_URL"], "op-area")
    pkg = P.build_package("op-area", xml, tmp_path, None)
    with zipfile.ZipFile(pkg["path"]) as z:
        names = z.namelist()
        m = ET.fromstring(z.read("MANIFEST/manifest.xml"))
        assert m.tag == "MissionPackageManifest" and m.get("version") == "2"
        params = {p.get("name"): p.get("value") for p in m.find("Configuration")}
        assert re.fullmatch(r"[0-9a-f-]{36}", params["uid"]) and params["name"] == "op-area-map"
        entries = [c.get("zipEntry") for c in m.find("Contents")]
        assert entries == pkg["entries"] and all(e in names for e in entries)
        assert all(c.get("ignore") == "false" for c in m.find("Contents"))
        assert len(entries) == 1 and entries[0].endswith("/op-area.xml")
        src = ET.fromstring(z.read(entries[0]))
        assert src.tag == "customMapSource"
        assert src.findtext("url") == "http://192.168.4.1:8160/tiles/op-area/{$z}/{$x}/{$y}.png"
        assert src.findtext("minZoom") == "1" and src.findtext("tileType") == "png"
    assert pkg["sha256"] == hashlib.sha256(pkg["path"].read_bytes()).hexdigest()
    # deterministic: rebuilding gives the same bytes (so OTS never gets duplicates)
    again = P.build_package("op-area", xml, tmp_path / "..", None)
    assert again["sha256"] == pkg["sha256"] and again["uid"] == pkg["uid"]


def test_package_with_mbtiles(kit, tmp_path):
    s = P.settings()
    xml, _ = P.fetch_map_source(s["ISR_MAPS_URL"], "op-area")
    pkg = P.build_package("op-area", xml, tmp_path, kit["maps"] / "op-area.mbtiles")
    with zipfile.ZipFile(pkg["path"]) as z:
        assert pkg["entries"][1].endswith("/op-area.mbtiles")
        assert z.read(pkg["entries"][1]) == (kit["maps"] / "op-area.mbtiles").read_bytes()
        assert z.getinfo(pkg["entries"][1]).compress_type == zipfile.ZIP_STORED
    assert pkg["name"] == "op-area-map-with-tiles"


def test_validate_rejects_broken_packages(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("MANIFEST/manifest.xml", P.manifest_xml("u", "n", ["missing/x.xml"]))
    with pytest.raises(P.PushError, match="zipEntry"):
        P.validate_package(bad)
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("MANIFEST/manifest.xml", b'<MissionPackageManifest version="1"/>')
    with pytest.raises(P.PushError, match="version 2"):
        P.validate_package(bad)


# ── push: upload + fileshare ─────────────────────────────────────────────────

def test_push_uploads_then_sends_fileshare_after_registration(kit):
    sleeps, log = [], []
    r = P.push("op-area", out=log.append, link_factory=fast_link(sleeps))
    ots, tak = kit["ots"], kit["tak"]
    assert r["sent"] and r["uploaded"] and len(ots.uploads) == 1
    up = ots.uploads[0]
    assert up["hash"] == r["sha256"] and up["filename"] == "op-area-map.zip" and up["creatorUid"] == "isrkit-maps-isr-pi"
    assert hashlib.sha256(ots.store[r["sha256"]]).hexdigest() == r["sha256"]        # bytes arrived intact
    assert P.REG_WAIT_S in sleeps and sleeps.index(P.REG_WAIT_S) == 0                # C5.2 wait first
    time.sleep(0.3)
    evs = tak.events()
    assert len(evs) == 2
    reg, fs = ET.fromstring(evs[0]), ET.fromstring(evs[1])
    assert reg.get("uid") == "isrkit-maps-isr-pi" and reg.get("type") == "a-f-G-E-S"
    assert reg.find("detail/contact").get("callsign") == "MAPS isr-pi"
    assert reg.find("detail/takv").get("platform") == "isr-kit"
    assert reg.find("point").get("lat") == "0" and reg.get("stale") == reg.get("start")    # position unset
    assert tak.clients[0]["first_at"] >= P.REG_WAIT_S
    assert fs.get("type") == "b-f-t-r" and fs.find("detail/marti") is None              # to everyone
    f = fs.find("detail/fileshare").attrib
    assert f["senderUrl"] == f"http://192.168.4.1:8080/Marti/sync/content?hash={r['sha256']}"
    assert f["sha256"] == r["sha256"] and f["sizeInBytes"] == str(r["size"]) and f["filename"] == "op-area-map.zip"
    assert f["senderUid"] == "isrkit-maps-isr-pi" and f["senderCallsign"] == "MAPS isr-pi" and f["name"] == "op-area-map"
    # a second push reuses what OTS already has, and still sends
    r2 = P.push("op-area", out=log.append, link_factory=fast_link([]))
    assert r2["uploaded"] is False and r2["sha256"] == r["sha256"] and len(ots.uploads) == 1
    assert any("already has" in m for m in log)


def test_push_with_site_position_and_errors(kit):
    (kit["conf"] / "site.env").write_text("ISR_HOST=isr-pi\nSENSOR_LAT=12.34567\nSENSOR_LON=-12.34567\n")
    P.push("op-area", out=lambda m: None, link_factory=fast_link([]))
    time.sleep(0.3)
    reg = ET.fromstring(kit["tak"].events()[0])
    assert reg.find("point").get("lat") == "12.34567" and reg.get("stale") > reg.get("start")
    with pytest.raises(P.PushError, match="no pack 'nope'"):
        P.push("nope", out=lambda m: None)
    with pytest.raises(P.PushError, match="bad pack"):
        P.push("../etc", out=lambda m: None)
    kit["ots"].limit = 10
    with pytest.raises(P.PushError, match="413"):
        P.push("op-area", with_mbtiles=True, out=lambda m: None, link_factory=fast_link([]))


def test_dry_run_cli(kit):
    env = dict(os.environ, **kit["env"])
    p = subprocess.run([str(REPO / "bin" / "isr-push-maps"), "op-area", "--dry-run"], capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    assert "dry run" in p.stdout and re.search(r"sha256 [0-9a-f]{64}", p.stdout) and not kit["ots"].uploads
    p = subprocess.run([str(REPO / "bin" / "isr-push-maps")], capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and "op-area" in p.stdout
    p = subprocess.run([str(REPO / "bin" / "isr-push-maps"), "nope"], capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 1 and "no pack" in p.stderr


def test_unreachable_tak_or_ots(kit, monkeypatch):
    monkeypatch.setenv("ISR_OTS_URL", "http://127.0.0.1:1")
    with pytest.raises(P.PushError, match="OTS not reachable"):
        P.push("op-area", out=lambda m: None)
    monkeypatch.setenv("ISR_OTS_URL", kit["ots"].url)
    monkeypatch.setenv("TAK_PORT", "1")
    with pytest.raises(P.PushError, match="TAK server not reachable"):
        P.push("op-area", out=lambda m: None)


# ── launcher ─────────────────────────────────────────────────────────────────

def test_launcher_push_endpoint(kit, monkeypatch):
    import isr_launcher as L
    import http.client
    monkeypatch.setattr(L, "PIN", "4321")
    srv = L.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(body, pin):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", "/api/push-maps", body=json.dumps(body),
                  headers={"Content-Type": "application/json", L.CSRF_HEADER: L.CSRF_VALUE, "X-PIN": pin})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    try:
        assert post({"pack": "op-area"}, "0000")[0] == 401
        assert post({"pack": "../x"}, "4321")[0] == 400
        code, body = post({"pack": "op-area"}, "4321")
        assert code == 200 and body["sent"] and body["filename"] == "op-area-map.zip"
        assert len(kit["ots"].uploads) == 1
        code, body = post({"pack": "nope"}, "4321")
        assert code == 502 and "no pack" in body["error"]
    finally:
        srv.shutdown(); srv.server_close()
    js = (REPO / "launcher" / "static" / "app.js").read_text()
    assert "Push maps to all ATAK devices" in js and "/api/push-maps" in js


# ── auto mode ────────────────────────────────────────────────────────────────

PHONE_SA = (b'<event version="2.0" uid="ANDROID-abc123" type="a-f-G-U-C" how="h-e" time="t" start="t" stale="t">'
            b'<point lat="0" lon="0" hae="0" ce="9" le="9"/><detail><contact callsign="HANGOVER" endpoint="*:-1:stcp"/>'
            b'<takv platform="ATAK-CIV" device="Pixel" os="34" version="5.4"/></detail></event>')


def test_auto_sends_once_per_new_eud(kit, tmp_path):
    tak = FakeTAK(greet=PHONE_SA + PHONE_SA)            # the phone's SA arrives twice
    os.environ["TAK_PORT"] = str(tak.port)
    stop, log = threading.Event(), []
    state = tmp_path / "state.json"

    def out(m):
        log.append(m)
        if "sent op-area-map.zip to ANDROID-abc123" in m:
            threading.Timer(1.0, stop.set).start()
    t = threading.Thread(target=P.auto, args=("op-area", False), daemon=True,
                         kwargs={"out": out, "state_file": state, "stop": stop, "link_factory": fast_link([])})
    t.start()
    t.join(20)
    assert not t.is_alive()
    evs = [ET.fromstring(e) for e in tak.events()]
    shares = [e for e in evs if e.get("type") == "b-f-t-r"]
    assert len(shares) == 1 and shares[0].find("detail/marti/dest").get("uid") == "ANDROID-abc123"
    sha = shares[0].find("detail/fileshare").get("sha256")
    assert json.loads(state.read_text()) == {sha: ["ANDROID-abc123"]}
    assert P.eud_uid(PHONE_SA) == "ANDROID-abc123"
    assert P.eud_uid(P.registration_sa(P.settings())) is None           # never to ourselves or other kit tools
    tak.sock.close()
