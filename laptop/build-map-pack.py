#!/usr/bin/env python3
"""Build an offline basemap pack (MBTiles) on a Mac with internet, for the Pi's isr-maps server.

    # DEFAULT: OpenTopoMap around the Pi's site position (from /etc/isr-kit/site.env over ssh),
    # 3 km radius, zooms 10-17 (dropping to 16 if that would exceed the cap). Prints the count first.
    laptop/build-map-pack.py --name op --from-site <user>@isr-pi.local            # estimate only
    laptop/build-map-pack.py --name op --from-site <user>@isr-pi.local --yes      # download

    # centre from this Mac's station.env (SENSOR_LAT/LON), or explicit
    laptop/build-map-pack.py --name op --from-station --yes
    laptop/build-map-pack.py --name area --center <lat>,<lon> --radius-km 2 --zoom 12-16 --yes

    # Ordnance Survey OS Maps API (larger areas; your own key; OpenData zooms 7-16 only)
    OS_API_KEY=... laptop/build-map-pack.py --source os --name op --from-site <host> --confirm-terms --yes

    # a pack you already have: MBTiles checked and copied; GeoPackage (EPSG:3857 tiles) converted
    laptop/build-map-pack.py --source file:/path/area.mbtiles --name area

Coordinates taken from the site or station file are never printed. Output goes to
~/isr-kit/maps/<name>.mbtiles (written as .partial; a re-run resumes and never re-fetches a tile).
Push it with laptop/push-to-pi.sh --maps-only.

Provider rules enforced here (see laptop/README.md > Offline maps):
  opentopomap (default, alias otm)  Use is allowed "provided the server isn't overly burdened, e.g. by
       mass downloads", under CC-BY-SA. Max 5000 tiles per pack (refused above that unless
       --i-asked-the-operator), at most 2 requests/s on one connection, backoff on 429/5xx, a full
       stop after repeated errors, an honest User-Agent, a/b/c subdomains, max zoom 17.
  os   OS Maps API ZXY Outdoor_3857: zooms 7-16 are OpenData (17+ Premium, refused). Key from
       OS_API_KEY, never printed or stored. 0.8 requests/s. --confirm-terms is required, because
       local-network serving of API tiles couldn't be confirmed in OS's published terms.
  file MBTiles validated; GeoPackage must be the standard EPSG:3857 pyramid.
"""
import argparse
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT_DIR = Path(os.environ.get("ISR_MAPS_OUT", str(Path.home() / "isr-kit" / "maps")))
VERSION = "1.1"
DEFAULT_MAX_TILES = 20000
AVG_TILE_KB = {"os": 25, "opentopomap": 30}
USER_AGENT = f"isr-kit-map-pack/{VERSION} (one-off small offline pack; contact via project repo)"
DEFAULT_RADIUS_KM = 3.0
DEFAULT_ZOOM = (10, 17)
MAX_CONSECUTIVE_FAILURES = 5
ALIASES = {"otm": "opentopomap"}
SOURCES = {
    "os": {
        "url": "https://api.os.uk/maps/raster/v1/zxy/Outdoor_3857/{z}/{x}/{y}.png?key={key}",
        "minzoom": 7, "maxzoom": 16, "rate": 0.8, "cap": None, "format": "png",
        "attribution": "Contains OS data © Crown copyright and database rights {year}",
        "short_credit": "© Crown copyright and database rights (OS)",
        "terms": ["https://docs.os.uk/os-apis/accessing-os-apis/os-maps-api/layers-and-styles",
                  "https://docs.os.uk/os-apis/core-concepts/rate-limiting-policy",
                  "https://docs.os.uk/os-apis/core-concepts/os-api-branding",
                  "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"],
    },
    "opentopomap": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "minzoom": 0, "maxzoom": 17, "rate": 2.0, "cap": 5000, "format": "png",
        "attribution": "Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)",
        "attribution_de": "Kartendaten: © OpenStreetMap-Mitwirkende, SRTM | Kartendarstellung: © OpenTopoMap (CC-BY-SA)",
        "short_credit": "© OpenStreetMap contributors, © OpenTopoMap (CC-BY-SA)",
        "terms": ["https://opentopomap.org/about"],
    },
}


class Refused(Exception):
    pass


# ── tile maths (EPSG:3857 / XYZ) ─────────────────────────────────────────────

def lonlat_to_tile(lon, lat, z):
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def bbox_from_center(lat, lon, radius_km):
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 1e-6))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def tiles_for(bbox, zmin, zmax):
    lat1, lon1, lat2, lon2 = bbox
    s, n = min(lat1, lat2), max(lat1, lat2)
    w, e = min(lon1, lon2), max(lon1, lon2)
    for z in range(zmin, zmax + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def count_tiles(bbox, zmin, zmax):
    return sum(1 for _ in tiles_for(bbox, zmin, zmax))


# ── MBTiles ──────────────────────────────────────────────────────────────────

def mbtiles_open(path: Path):
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);
    """)
    return db


def put_tile(db, z, x, y, data):
    db.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", (z, x, (2 ** z - 1) - y, sqlite3.Binary(data)))


def has_tile(db, z, x, y):
    return db.execute("SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                      (z, x, (2 ** z - 1) - y)).fetchone() is not None


def write_metadata(db, **kv):
    for k, v in kv.items():
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, str(v)))


def validate_mbtiles(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if not {"metadata", "tiles"} <= names:
            raise Refused(f"{path.name} is not MBTiles (needs tables metadata and tiles)")
        n = db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        if n == 0:
            raise Refused(f"{path.name} has no tiles")
        meta = dict(db.execute("SELECT name, value FROM metadata").fetchall())
        z = db.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
        return {"tiles": n, "meta": meta, "minzoom": z[0], "maxzoom": z[1]}
    finally:
        db.close()


def gpkg_to_mbtiles(src: Path, dst: Path, name: str):
    g = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    sets = g.execute("SELECT table_name, srs_id, min_x, min_y, max_x, max_y FROM gpkg_tile_matrix_set").fetchall()
    sets = [s for s in sets if int(s[1]) == 3857]
    if not sets:
        raise Refused("GeoPackage has no EPSG:3857 tile matrix set (re-export as web-mercator tiles or MBTiles)")
    table, _, minx, miny, maxx, maxy = sets[0]
    world = 20037508.342789244
    if not all(abs(a - b) < 1.0 for a, b in ((minx, -world), (miny, -world), (maxx, world), (maxy, world))):
        raise Refused("GeoPackage 3857 matrix set isn't the standard web-mercator pyramid; export as MBTiles instead")
    if not re.match(r"^[A-Za-z0-9_]+$", table):
        raise Refused("unexpected tile table name")
    matrices = {r[0]: (r[1], r[2]) for r in g.execute(
        "SELECT zoom_level, matrix_width, matrix_height FROM gpkg_tile_matrix WHERE table_name=?", (table,))}
    for z, (w, h) in matrices.items():
        if w != 2 ** z or h != 2 ** z:
            raise Refused(f"zoom {z} isn't a 2^z × 2^z matrix (w={w}, h={h})")
    db = mbtiles_open(dst)
    n = 0
    for z, x, y, data in g.execute(f"SELECT zoom_level, tile_column, tile_row, tile_data FROM {table}"):
        put_tile(db, z, x, y, data)     # GeoPackage rows are top-origin (XYZ); MBTiles wants TMS
        n += 1
    fmt = "jpg" if n and bytes(g.execute(f"SELECT tile_data FROM {table} LIMIT 1").fetchone()[0])[:3] == b"\xff\xd8\xff" else "png"
    write_metadata(db, name=name, format=fmt, minzoom=min(matrices), maxzoom=max(matrices), type="baselayer",
                   bounds="-180,-85.0511,180,85.0511", source=f"file:{src.name}",
                   attribution="see the source GeoPackage's licence", description=f"converted from {src.name}")
    db.commit()
    db.close()
    return n


# ── download ─────────────────────────────────────────────────────────────────

def http_fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def safe(msg: str, key: str) -> str:
    return msg.replace(key, "***") if key else msg


def build_download(args, src_name, fetch=http_fetch, sleep=time.sleep, out=print):
    src = SOURCES[src_name]
    zmin, zmax = args.zmin, args.zmax
    if args.zoom_defaulted and zmax > src["maxzoom"]:
        out(f"{src_name}: default zooms capped at {src['maxzoom']}")
        zmax = args.zmax = src["maxzoom"]
    if zmin < src["minzoom"] or zmax > src["maxzoom"]:
        if src_name == "os":
            raise Refused(f"OS Maps API 3857: only zooms {src['minzoom']}–{src['maxzoom']} are OpenData; zoom 17+ is "
                          "Premium (needs a Premium plan and licence). Use --zoom within 7-16.")
        raise Refused(f"{src_name}: zooms {src['minzoom']}–{src['maxzoom']} only")
    key = ""
    if src_name == "os":
        key = os.environ.get("OS_API_KEY", "").strip()
        if not key:
            raise Refused("set OS_API_KEY (your own OS Data Hub project key); it's never written to the pack or logs")
    n = count_tiles(args.bbox, zmin, zmax)
    cap = min(c for c in (src["cap"], args.max_tiles) if c)
    if src_name == "opentopomap" and args.i_asked_the_operator:
        cap = args.max_tiles
    if n > cap and args.zoom_defaulted and zmax > zmin:
        while n > cap and zmax > zmin and zmax > 16:      # the default pack: drop the top zoom first
            zmax -= 1
            n = count_tiles(args.bbox, zmin, zmax)
        out(f"default zoom range reduced to z{zmin}-{zmax} to stay under the cap ({n} tiles)")
        args.zmax = zmax
    est_mb = n * AVG_TILE_KB[src_name] / 1024
    eta = n / src["rate"]
    where = ("around the " + args.center_from + " position (coordinates not shown)") if args.center_from \
        else "in bbox " + ",".join(f"{v:.5f}" for v in args.bbox)
    out(f"{src_name}: {n} tiles for z{zmin}-{zmax} {where}, "
        f"about {est_mb:.0f} MB, about {eta / 60:.0f} min at {src['rate']} req/s (cap {cap})")
    if n > cap:
        raise Refused(f"{n} tiles exceeds the cap of {cap}"
                      + (" (OpenTopoMap asks not to be loaded by mass downloads; --i-asked-the-operator if they agreed)"
                         if src_name == "opentopomap" else "")
                      + ": shrink --radius-km/--bbox or the zoom range")
    out("Terms to check: " + " · ".join(src["terms"]))
    if src_name == "os":
        out("NOTE: we could not confirm that OS permits storing API tiles offline and serving them on a local network.")
        out("      Check your OS Data Hub plan and the OGL before using or sharing the pack.")
    if src_name == "opentopomap":
        out("OpenTopoMap: free use as long as their server isn't overloaded; CC-BY-SA; they appreciate a short note")
        out("             about how it's used. For larger areas use OS (--source os, your OS_API_KEY).")
    if src_name == "os" and not args.confirm_terms:
        raise Refused("re-run with --confirm-terms once you've checked the provider's terms (links above)")
    if not args.yes:
        out("dry run: add --yes to download")
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    partial = OUT_DIR / f"{args.name}.mbtiles.partial"
    db = mbtiles_open(partial)
    got = skipped = missing = 0
    sub = "abc"
    delay = 1.0 / src["rate"]
    failures = 0
    for i, (z, x, y) in enumerate(tiles_for(args.bbox, zmin, zmax)):
        if has_tile(db, z, x, y):
            skipped += 1
            continue
        if failures >= MAX_CONSECUTIVE_FAILURES:
            db.commit()
            db.close()
            raise Refused(f"stopped after {failures} failed tiles in a row (server unhappy?); "
                          f"{got} tiles saved in {partial.name}, re-run later to resume")
        url = src["url"].format(z=z, x=x, y=y, key=key, s=sub[i % 3])
        for attempt in range(4):
            try:
                status, data = fetch(url)
                if status == 200 and data:
                    put_tile(db, z, x, y, data)
                    got += 1
                    failures = 0
                    break
                missing += 1
                break
            except urllib.error.HTTPError as e:
                if (e.code == 429 or 500 <= e.code < 600) and attempt < 3:
                    sleep(10 * (attempt + 1))
                    continue
                if e.code == 429 or 500 <= e.code < 600:
                    failures += 1
                    break
                if e.code in (401, 403):
                    db.commit()
                    db.close()
                    raise Refused(safe(f"{src_name} refused the request (HTTP {e.code}); check the key/plan", key))
                missing += 1
                break
            except (urllib.error.URLError, OSError) as e:
                if attempt < 3:
                    sleep(5)
                    continue
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    db.commit()
                    db.close()
                    raise Refused(safe(f"network error: {e}", key) + f"; {got} tiles saved, re-run to resume")
        if got % 50 == 0:
            db.commit()
        sleep(delay)
    year = time.strftime("%Y")
    lat1, lon1, lat2, lon2 = args.bbox
    write_metadata(db, name=args.title or args.name, format=src["format"], minzoom=zmin, maxzoom=zmax,
                   credit=src.get("short_credit", ""),
                   bounds=f"{min(lon1, lon2)},{min(lat1, lat2)},{max(lon1, lon2)},{max(lat1, lat2)}",
                   center=f"{(lon1 + lon2) / 2},{(lat1 + lat2) / 2},{zmin}", type="baselayer", version="1",
                   attribution=src["attribution"].format(year=year), source=src_name,
                   description=f"isr-kit offline pack ({src_name}), built {time.strftime('%Y-%m-%d')}")
    db.commit()
    db.close()
    final = OUT_DIR / f"{args.name}.mbtiles"
    partial.replace(final)
    out(f"wrote {final} ({got} downloaded, {skipped} already there, {missing} missing/empty)")
    return final


def build_file(args, path: Path, out=print):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final = OUT_DIR / f"{args.name}.mbtiles"
    if path.suffix.lower() == ".mbtiles":
        info = validate_mbtiles(path)
        shutil.copy2(path, final)
        out(f"copied {path.name}: {info['tiles']} tiles, z{info['minzoom']}-{info['maxzoom']}, "
            f"attribution: {info['meta'].get('attribution', '(none in metadata: add it)')} -> {final}")
    elif path.suffix.lower() == ".gpkg":
        tmp = OUT_DIR / f"{args.name}.mbtiles.partial"
        if tmp.exists():
            tmp.unlink()
        n = gpkg_to_mbtiles(path, tmp, args.title or args.name)
        tmp.replace(final)
        out(f"converted {path.name}: {n} tiles -> {final}")
    else:
        raise Refused("file source must be .mbtiles or .gpkg")
    return final


def _sensor(text: str):
    kv = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.lstrip().startswith("#"))
    try:
        lat, lon = float(kv["SENSOR_LAT"]), float(kv["SENSOR_LON"])
    except (KeyError, ValueError):
        raise Refused("no SENSOR_LAT/SENSOR_LON set there (on the Pi: sudo isr-set-site --sensor LAT,LON)")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise Refused("sensor position is invalid or 0,0")
    return lat, lon


def site_position(host: str):
    if not re.match(r"^[A-Za-z0-9._@:%-]+$", host):
        raise Refused("bad --from-site host")
    ssh = os.environ.get("ISR_SSH", "ssh").split()
    import subprocess
    p = subprocess.run(ssh + ["-o", "ConnectTimeout=10", host, "cat /etc/isr-kit/site.env"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise Refused(f"couldn't read /etc/isr-kit/site.env on {host} (ssh exit {p.returncode})")
    return _sensor(p.stdout)


def station_position():
    f = Path(os.environ.get("ISR_CONF_DIR", str(Path.home() / ".config" / "isr-kit"))) / "station.env"
    if not f.exists():
        raise Refused(f"{f} not found (run laptop/setup-mac.sh, or use --from-site / --center)")
    return _sensor(f.read_text())


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", default="opentopomap", help="opentopomap (default) | os | file:PATH")
    ap.add_argument("--name", required=True, help="pack name: [a-z0-9_-] (becomes the URL and file name)")
    ap.add_argument("--title", default="", help="human title shown in ATAK and the launcher")
    ap.add_argument("--bbox", help="lat1,lon1,lat2,lon2")
    ap.add_argument("--center", help="lat,lon")
    ap.add_argument("--from-site", metavar="SSH_HOST", help="centre = the Pi's SENSOR_LAT/LON (/etc/isr-kit/site.env)")
    ap.add_argument("--from-station", action="store_true", help="centre = SENSOR_LAT/LON in ~/.config/isr-kit/station.env")
    ap.add_argument("--i-asked-the-operator", action="store_true",
                    help="OpenTopoMap: allow more than 5000 tiles because the operators agreed")
    ap.add_argument("--radius-km", type=float)
    ap.add_argument("--zoom", default=None, help=f"min-max (default {DEFAULT_ZOOM[0]}-{DEFAULT_ZOOM[1]})")
    ap.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    ap.add_argument("--confirm-terms", action="store_true", help="you've checked the provider's terms (printed)")
    ap.add_argument("--yes", action="store_true", help="actually download (otherwise estimate only)")
    a = ap.parse_args(argv)
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", a.name):
        ap.error("--name must be lowercase [a-z0-9_-], up to 64 chars")
    a.source = ALIASES.get(a.source, a.source)
    a.zoom_defaulted = a.zoom is None
    a.zoom = a.zoom or f"{DEFAULT_ZOOM[0]}-{DEFAULT_ZOOM[1]}"
    a.center_from = ""
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", a.zoom)
    if not m or int(m.group(1)) > int(m.group(2)):
        ap.error("--zoom must be MIN-MAX, e.g. 10-16")
    a.zmin, a.zmax = int(m.group(1)), int(m.group(2))
    if not a.source.startswith("file:"):
        if a.from_site or a.from_station:
            try:
                lat, lon = site_position(a.from_site) if a.from_site else station_position()
            except Refused as e:
                ap.error(str(e))
            a.center_from = "Pi site" if a.from_site else "station"
            a.bbox = bbox_from_center(lat, lon, a.radius_km or DEFAULT_RADIUS_KM)
        elif a.bbox:
            v = [float(t) for t in a.bbox.split(",")]
            if len(v) != 4:
                ap.error("--bbox needs lat1,lon1,lat2,lon2")
            a.bbox = tuple(v)
        elif a.center and a.radius_km:
            lat, lon = (float(t) for t in a.center.split(","))
            if not 0 < a.radius_km <= 50:
                ap.error("--radius-km must be in (0, 50]")
            a.bbox = bbox_from_center(lat, lon, a.radius_km)
        else:
            ap.error("give --bbox or --center with --radius-km")
        lat1, lon1, lat2, lon2 = a.bbox
        if not all(-90 <= t <= 90 for t in (lat1, lat2)) or not all(-180 <= t <= 180 for t in (lon1, lon2)):
            ap.error("coordinates out of range")
    return a


def main(argv=None, fetch=http_fetch, sleep=time.sleep):
    a = parse(argv if argv is not None else sys.argv[1:])
    try:
        if a.source.startswith("file:"):
            build_file(a, Path(a.source[5:]).expanduser())
        elif a.source in SOURCES:
            build_download(a, a.source, fetch=fetch, sleep=sleep)
        else:
            raise Refused("--source must be opentopomap, os or file:PATH")
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
