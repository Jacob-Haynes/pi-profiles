"""Offline maps: the Pi's isr-maps server, the Mac's build-map-pack.py, the launcher's Maps card.
No real downloads: the builder's HTTP fetcher is stubbed."""
import http.client
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "maps"))
sys.path.insert(0, str(REPO / "launcher"))


def load_builder():
    spec = importlib.util.spec_from_file_location("build_map_pack", REPO / "laptop" / "build-map-pack.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


B = load_builder()
PNG = b"\x89PNG\r\n\x1a\n"


def tile_bytes(z, x, y, jpg=False):
    return (b"\xff\xd8\xff" if jpg else PNG) + f"tile-{z}-{x}-{y}".encode()


def make_pack(path: Path, tiles, fmt="png", name="Test Pack", minzoom=1, maxzoom=2):
    db = B.mbtiles_open(path)
    for z, x, y in tiles:
        B.put_tile(db, z, x, y, tile_bytes(z, x, y, jpg=(fmt == "jpg")))
    B.write_metadata(db, name=name, format=fmt, minzoom=minzoom, maxzoom=maxzoom, bounds="-1.1,50.9,-0.9,51.1",
                     attribution="Test attribution ©", source="test")
    db.commit()
    db.close()


@pytest.fixture
def maps_server(tmp_path, monkeypatch):
    d = tmp_path / "maps"
    d.mkdir()
    make_pack(d / "venue.mbtiles", [(1, 0, 0), (1, 1, 0), (2, 1, 1)])
    make_pack(d / "photo.mbtiles", [(1, 0, 1)], fmt="jpg", name="JPEG pack")
    (d / "Bad Name.mbtiles").write_bytes(b"x")                     # ignored: name not allowed
    (d / "broken.mbtiles").write_bytes(b"not sqlite")              # skipped, not fatal
    import isr_maps as M
    monkeypatch.setattr(M, "catalog", M.Catalog(d))
    monkeypatch.setattr(M, "PUBLIC_BASE", "http://192.168.4.1:8160")
    monkeypatch.setattr(M, "RESCAN_S", 0)
    srv = M.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], d, M
    srv.shutdown()
    srv.server_close()


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_tiles_xyz_with_tms_flip(maps_server):
    port, _, _ = maps_server
    # stored via put_tile(z,x,y) as XYZ; MBTiles row is TMS-flipped: the server must flip it back
    s, h, b = get(port, "/tiles/venue/1/1/0.png")
    assert s == 200 and b == tile_bytes(1, 1, 0) and h["Content-Type"] == "image/png"
    assert "max-age" in h["Cache-Control"] and h["ETag"]
    s, h2, _ = get(port, "/tiles/venue/1/1/0.png", {"If-None-Match": h["ETag"]})
    assert s == 304
    assert get(port, "/tiles/venue/2/1/1.png")[2] == tile_bytes(2, 1, 1)
    # the TMS row really is flipped in the file (z=1, y=0 is stored as row 1)
    db = sqlite3.connect(str(maps_server[1] / "venue.mbtiles"))
    assert db.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level=1 AND tile_column=1 AND tile_row=1").fetchone()[0] == 1


def test_outside_coverage_is_transparent_404(maps_server):
    port, _, M = maps_server
    for path in ("/tiles/venue/1/0/1.png", "/tiles/venue/9/0/0.png", "/tiles/venue/1/5/5.png", "/tiles/nope/1/0/0.png"):
        s, h, b = get(port, path)
        assert s == 404 and h["Content-Type"] == "image/png" and b == M.EMPTY_PNG
    assert M.EMPTY_PNG.startswith(PNG) and len(M.EMPTY_PNG) < 2000


def test_jpeg_pack(maps_server):
    port, _, _ = maps_server
    s, h, b = get(port, "/tiles/photo/1/0/1.jpg")
    assert s == 200 and h["Content-Type"] == "image/jpeg"


def test_atak_xml_download_and_api(maps_server):
    port, d, _ = maps_server
    s, h, b = get(port, "/atak/venue.xml")
    x = b.decode()
    assert s == 200 and h["Content-Type"] == "application/xml" and "attachment" in h["Content-Disposition"]
    assert "<customMapSource>" in x and "<minZoom>1</minZoom>" in x and "<maxZoom>2</maxZoom>" in x
    assert "<url>http://192.168.4.1:8160/tiles/venue/{$z}/{$x}/{$y}.png</url>" in x and "<tileType>png</tileType>" in x
    assert "<tileType>jpeg</tileType>" in get(port, "/atak/photo.xml")[2].decode()
    assert "<name>Test Pack (offline)</name>" in x and "<!-- Test attribution © -->" in x
    import xml.etree.ElementTree as ET
    assert ET.fromstring(b).find("name") is not None
    s, h, b = get(port, "/download/venue.mbtiles")
    assert s == 200 and b == (d / "venue.mbtiles").read_bytes()
    packs = {p["name"]: p for p in json.loads(get(port, "/api/packs")[2])["packs"]}
    assert set(packs) == {"venue", "photo"}                           # bad name + broken file ignored
    v = packs["venue"]
    assert v["tiles"] == 3 and v["minzoom"] == 1 and v["attribution"] == "Test attribution ©"
    assert v["atak_xml"] == "http://192.168.4.1:8160/atak/venue.xml"
    for bad in ("/atak/../etc.xml", "/download/Bad Name.mbtiles", "/tiles/venue/1/0/0.gif", "/download/nope.mbtiles"):
        assert get(port, bad.replace(" ", "%20"))[0] == 404


def test_atak_xml_carries_credit(maps_server):
    port, d, M = maps_server
    db = B.mbtiles_open(d / "topo.mbtiles")
    B.put_tile(db, 1, 0, 0, tile_bytes(1, 0, 0))
    B.write_metadata(db, name="OP", format="png", minzoom=10, maxzoom=16, bounds="-1.1,50.9,-0.9,51.1",
                     attribution=B.SOURCES["opentopomap"]["attribution"], credit=B.SOURCES["opentopomap"]["short_credit"])
    db.commit(); db.close()
    x = get(port, "/atak/topo.xml")[2].decode()
    assert "<name>OP (offline, © OpenStreetMap contributors, © OpenTopoMap (CC-BY-SA))</name>" in x
    assert "Map style: © OpenTopoMap (CC-BY-SA) -->" in x


def test_new_pack_appears_without_restart(maps_server):
    port, d, _ = maps_server
    make_pack(d / "balcony.mbtiles", [(1, 0, 0)])
    assert "balcony" in {p["name"] for p in json.loads(get(port, "/api/packs")[2])["packs"]}


# ── builder ──────────────────────────────────────────────────────────────────

def args(**kw):
    base = ["--source", "otm", "--name", "t", "--center", "51.0,-1.0", "--radius-km", "0.5", "--zoom", "10-12"]
    for k, v in kw.items():
        base += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
    return B.parse(base)


# Placeholder positions only (never a real site): somewhere plausible, nowhere in particular.
PLACEHOLDER_LAT, PLACEHOLDER_LON = 51.0, -1.0


def site_env(lat=PLACEHOLDER_LAT, lon=PLACEHOLDER_LON):
    return f"# isr-kit site\nISR_HOST=test\nSENSOR_LAT={lat}\nSENSOR_LON={lon}\n"


def test_tile_maths():
    assert B.lonlat_to_tile(0, 0, 1) == (1, 1)
    assert B.lonlat_to_tile(-180, 85, 2) == (0, 0)
    bb = B.bbox_from_center(51.0, -1.0, 1.0)
    assert abs((bb[2] - bb[0]) * 111.32 - 2.0) < 0.01
    assert B.count_tiles((50.99, -1.01, 51.01, -0.99), 10, 10) >= 1


def test_defaults_alias_and_no_terms_flag_for_opentopomap(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    a = B.parse(["--name", "t", "--center", "51,-1", "--radius-km", "0.5", "--zoom", "10-12"])
    assert a.source == "opentopomap" and args().source == "opentopomap"           # default, and otm alias
    out = []
    assert B.build_download(a, "opentopomap", fetch=None, out=out.append) is None  # estimate only, no --yes
    assert any("tiles for z10-12" in o for o in out) and any("opentopomap.org/about" in o for o in out)
    assert any("short note" in o for o in out) and any("OS_API_KEY" in o for o in out)
    assert not any("confirm-terms" in o for o in out)


def test_opentopomap_cap_and_operator_override(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    out = []
    big = B.parse(["--name", "big", "--center", "51,-1", "--radius-km", "20", "--zoom", "10-17", "--yes"])
    assert B.count_tiles(big.bbox, 10, 17) > 5000
    with pytest.raises(B.Refused, match="exceeds the cap of 5000.*i-asked-the-operator"):
        B.build_download(big, "opentopomap", fetch=None, out=out.append)
    ok = B.parse(["--name", "big", "--center", "51,-1", "--radius-km", "20", "--zoom", "10-17",
                  "--i-asked-the-operator", "--max-tiles", "100000"])
    assert B.build_download(ok, "opentopomap", fetch=None, out=lambda m: None) is None   # passes the cap; no --yes
    with pytest.raises(B.Refused, match="cap of 1"):
        B.build_download(args(yes=True, max_tiles=1), "opentopomap", fetch=None, out=out.append)


def test_default_zoom_reduces_to_fit_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    a = B.parse(["--name", "d", "--center", "51,-1", "--radius-km", "3"])
    assert (a.zmin, a.zmax) == B.DEFAULT_ZOOM and a.zoom_defaulted
    out = []
    B.build_download(a, "opentopomap", fetch=None, out=out.append)
    n17 = B.count_tiles(a.bbox, 10, 17)
    if n17 > 5000:
        assert a.zmax == 16 and any("reduced to z10-16" in o for o in out)
    else:
        assert a.zmax == 17
    # an explicit --zoom is never changed behind your back
    e = B.parse(["--name", "d", "--center", "51,-1", "--radius-km", "6", "--zoom", "10-17", "--yes"])
    with pytest.raises(B.Refused, match="cap of 5000"):
        B.build_download(e, "opentopomap", fetch=None, out=lambda m: None)


def test_from_site_reads_pi_over_ssh_and_masks_coordinates(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    stub = tmp_path / "ssh"
    log = tmp_path / "ssh.log"
    stub.write_text(f"#!/bin/sh\necho \"$@\" > {log}\ncat <<'X'\n{site_env(12.34567, -12.34567)}X\n")
    stub.chmod(0o755)
    monkeypatch.setenv("ISR_SSH", str(stub))
    a = B.parse(["--name", "op", "--from-site", "pi@isr-pi.local"])
    assert "pi@isr-pi.local" in log.read_text() and "/etc/isr-kit/site.env" in log.read_text()
    assert a.center_from == "Pi site" and a.zoom_defaulted
    assert a.bbox == B.bbox_from_center(12.34567, -12.34567, B.DEFAULT_RADIUS_KM)
    out = []
    B.build_download(a, "opentopomap", fetch=None, out=out.append)
    text = "\n".join(out)
    assert "coordinates not shown" in text
    assert "12.3" not in text and "12.34567" not in text
    # missing / unset position
    stub.write_text("#!/bin/sh\necho ISR_HOST=x\n")
    with pytest.raises(SystemExit):
        B.parse(["--name", "op", "--from-site", "pi@isr-pi.local"])
    stub.write_text("#!/bin/sh\nexit 255\n")
    with pytest.raises(SystemExit):
        B.parse(["--name", "op", "--from-site", "pi@isr-pi.local"])
    with pytest.raises(SystemExit):
        B.parse(["--name", "op", "--from-site", "pi@host;rm -rf /"])


def test_from_station(tmp_path, monkeypatch):
    (tmp_path / "station.env").write_text(site_env())
    monkeypatch.setenv("ISR_CONF_DIR", str(tmp_path))
    a = B.parse(["--name", "op", "--from-station", "--radius-km", "1"])
    assert a.center_from == "station"
    assert a.bbox == B.bbox_from_center(PLACEHOLDER_LAT, PLACEHOLDER_LON, 1.0)
    (tmp_path / "station.env").write_text("SENSOR_LAT=0\nSENSOR_LON=0\n")
    with pytest.raises(SystemExit):
        B.parse(["--name", "op", "--from-station"])


def test_backoff_on_5xx_and_stop_after_repeated_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    sleeps, calls = [], []

    def flaky(url):                       # one 502, then fine
        calls.append(url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 502, "bad gateway", {}, None)
        return 200, PNG + url.encode()
    a = args(yes=True)
    final = B.build_download(a, "opentopomap", fetch=flaky, sleep=sleeps.append, out=lambda m: None)
    assert final.exists() and 10 in sleeps
    assert all(any(f"//{s}.tile.opentopomap.org/" in u for s in "abc") for u in calls)
    assert {u.split("//")[1][0] for u in calls} <= set("abc") and len({u.split("//")[1][0] for u in calls}) > 1
    assert min(s for s in sleeps if s < 10) >= 0.5                          # <= 2 req/s pacing

    final.unlink()
    good = [0]

    def dying(url):                       # first 2 tiles ok, then the server keeps failing
        if good[0] < 2:
            good[0] += 1
            return 200, PNG + url.encode()
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)
    with pytest.raises(B.Refused, match="in a row"):
        B.build_download(args(yes=True, zoom="14-16"), "opentopomap", fetch=dying, sleep=lambda s: None, out=lambda m: None)
    partial = tmp_path / "t.mbtiles.partial"
    assert sqlite3.connect(str(partial)).execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == 2


def test_os_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    monkeypatch.delenv("OS_API_KEY", raising=False)
    a = B.parse(["--source", "os", "--name", "v", "--center", "51,-1", "--radius-km", "0.3", "--zoom", "10-17",
                 "--confirm-terms", "--yes"])
    with pytest.raises(B.Refused, match="Premium"):
        B.build_download(a, "os", fetch=None, out=lambda m: None)
    a.zmax = 16
    with pytest.raises(B.Refused, match="OS_API_KEY"):
        B.build_download(a, "os", fetch=None, out=lambda m: None)
    monkeypatch.setenv("OS_API_KEY", "SECRETKEY123")
    seen, logs = [], []

    def fake(url):
        seen.append(url)
        if len(seen) == 1:
            raise urllib.error.HTTPError(url, 429, "slow down", {}, None)
        return 200, PNG + b"os"
    a.zmin = a.zmax = 14
    final = B.build_download(a, "os", fetch=fake, sleep=lambda s: None, out=logs.append)
    assert final and final.exists() and not Path(str(final) + ".partial").exists()
    assert all(u.startswith("https://api.os.uk/maps/raster/v1/zxy/Outdoor_3857/14/") and u.endswith("key=SECRETKEY123") for u in seen)
    assert not any("SECRETKEY123" in m for m in logs)
    db = sqlite3.connect(str(final))
    meta = dict(db.execute("SELECT name, value FROM metadata"))
    assert "SECRETKEY123" not in json.dumps(meta) and meta["attribution"].startswith("Contains OS data © Crown copyright")
    assert meta["minzoom"] == "14" and db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == len(seen) - 1


def test_opentopomap_download_resume_user_agent_and_credit(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path)
    calls = []

    def fake(url):
        calls.append(url)
        return 200, PNG + url.encode()
    a = args(yes=True)
    n = B.count_tiles(a.bbox, a.zmin, a.zmax)
    final = B.build_download(a, "opentopomap", fetch=fake, sleep=lambda s: None, out=lambda m: None)
    assert len(calls) == n and all(".tile.opentopomap.org/" in u for u in calls)
    # resume: a leftover .partial with every tile means nothing is fetched again
    final.rename(tmp_path / "t.mbtiles.partial")
    calls.clear()
    B.build_download(a, "opentopomap", fetch=fake, sleep=lambda s: None, out=lambda m: None)
    assert calls == []
    assert B.USER_AGENT == f"isr-kit-map-pack/{B.VERSION} (one-off small offline pack; contact via project repo)"
    assert "@" not in B.USER_AGENT
    meta = dict(sqlite3.connect(str(final)).execute("SELECT name, value FROM metadata"))
    assert meta["attribution"] == "Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)"
    assert "OpenTopoMap" in meta["credit"]


def test_file_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "OUT_DIR", tmp_path / "out")
    src = tmp_path / "unit.mbtiles"
    make_pack(src, [(1, 0, 0)])
    a = B.parse(["--source", f"file:{src}", "--name", "unit"])
    assert B.build_file(a, src, out=lambda m: None).read_bytes() == src.read_bytes()
    empty = tmp_path / "empty.mbtiles"
    B.mbtiles_open(empty).close()
    with pytest.raises(B.Refused, match="no tiles"):
        B.build_file(B.parse(["--source", f"file:{empty}", "--name", "e"]), empty, out=lambda m: None)
    # GeoPackage with a standard EPSG:3857 pyramid (top-origin rows) converts with the TMS flip
    g = tmp_path / "area.gpkg"
    db = sqlite3.connect(str(g))
    w = 20037508.342789244
    db.executescript(f"""
        CREATE TABLE gpkg_tile_matrix_set (table_name TEXT, srs_id INT, min_x REAL, min_y REAL, max_x REAL, max_y REAL);
        CREATE TABLE gpkg_tile_matrix (table_name TEXT, zoom_level INT, matrix_width INT, matrix_height INT);
        CREATE TABLE area (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB);
        INSERT INTO gpkg_tile_matrix_set VALUES ('area', 3857, {-w}, {-w}, {w}, {w});
        INSERT INTO gpkg_tile_matrix VALUES ('area', 1, 2, 2);
    """)
    db.execute("INSERT INTO area VALUES (1, 1, 0, ?)", (tile_bytes(1, 1, 0),))
    db.commit()
    db.close()
    out = B.build_file(B.parse(["--source", f"file:{g}", "--name", "area"]), g, out=lambda m: None)
    row = sqlite3.connect(str(out)).execute("SELECT tile_row, tile_data FROM tiles").fetchone()
    assert row[0] == 1 and bytes(row[1]) == tile_bytes(1, 1, 0)


def test_builder_cli_refusals(tmp_path):
    env = dict(os.environ, ISR_MAPS_OUT=str(tmp_path))
    cli = [sys.executable, str(REPO / "laptop" / "build-map-pack.py")]
    p = subprocess.run(cli + ["--name", "x", "--center", "51,-1", "--radius-km", "20", "--zoom", "10-17", "--yes"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "REFUSED" in p.stderr and "5000" in p.stderr
    p = subprocess.run(cli + ["--source", "os", "--name", "x", "--center", "51,-1", "--radius-km", "0.2",
                              "--zoom", "12-13"], capture_output=True, text=True, env=dict(env, OS_API_KEY="k"))
    assert p.returncode == 2 and "confirm-terms" in p.stderr
    p = subprocess.run(cli + ["--name", "Bad Name", "--center", "51,-1", "--radius-km", "0.2"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "--name" in p.stderr


# ── launcher Maps card ───────────────────────────────────────────────────────

def test_launcher_maps_card(maps_server, monkeypatch):
    port, _, _ = maps_server
    import isr_launcher as L
    r = L.probe_maps(f"http://127.0.0.1:{port}")
    names = {p["name"]: p for p in r["packs"]}
    assert r["reachable"] and set(names) == {"venue", "photo"}
    assert names["venue"]["atak_xml"] == "http://192.168.4.1:8160/atak/venue.xml"
    assert names["venue"]["download"].endswith("/download/venue.mbtiles") and names["venue"]["bounds"] == [-1.1, 50.9, -0.9, 51.1]
    assert L.probe_maps("http://127.0.0.1:1")["reachable"] is False
    js = (REPO / "launcher" / "static" / "app.js").read_text()
    assert "Add to ATAK" in js and "Download" in js


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS push path")
def test_push_maps_only_stages_and_needs_sudo(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    make_pack(maps / "venue.mbtiles", [(1, 0, 0)])
    pi_home, fakebin = tmp_path / "pihome", tmp_path / "fakebin"
    pi_home.mkdir(); fakebin.mkdir()
    (fakebin / "sudo").write_text("#!/bin/sh\nexit 1\n")          # never real sudo in a test
    (fakebin / "sudo").chmod(0o755)
    stub = tmp_path / "ssh"
    stub.write_text(f"""#!/bin/bash
while [ "${{1#-}}" != "$1" ]; do shift 2; done
shift
cd "{pi_home}" && HOME="{pi_home}" PATH="{fakebin}:$PATH" exec bash -c "$*"
""")
    stub.chmod(0o755)
    env = dict(os.environ, ISR_SSH=str(stub), ISR_MAPS_OUT=str(maps), ISR_COPY_BACKOFF="0")
    p = subprocess.run(["bash", str(REPO / "laptop" / "push-to-pi.sh"), "--host", "pi@test", "--maps-only"],
                       capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout + p.stderr
    assert (pi_home / "isr-maps.staging" / "venue.mbtiles").read_bytes() == (maps / "venue.mbtiles").read_bytes()
    assert "sudo needs a password" in out and "venue.mbtiles" in out
    assert "Mirrors" not in out                                    # --maps-only skips the git part
