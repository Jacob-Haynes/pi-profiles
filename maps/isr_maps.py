#!/usr/bin/env python3
"""isr-maps: offline basemap server for ATAK phones on the hotspot (stdlib only; runs on an offline Pi).

Serves every MBTiles pack in ISR_MAPS_DIR (default /var/lib/isr-maps):

  GET /tiles/<pack>/<z>/<x>/<y>.<png|jpg>   XYZ tile (MBTiles stores TMS rows: flipped here)
  GET /atak/<pack>.xml                      ATAK customMapSource XML pointing at the hub's tile URL
  GET /download/<pack>.mbtiles              the pack itself, to import into ATAK for use away from the Pi
  GET /api/packs                            JSON: name, title, bounds, zooms, format, attribution, size
  GET /                                     a small index page

Outside a pack's coverage it returns a transparent 256x256 PNG with status 404, so ATAK shows nothing
there instead of an error tile. Tiles get long cache headers and ETags. The databases are opened
read-only (immutable). The pack list is re-scanned every 30 s, so new packs appear without a restart.
It publishes a heartbeat (tool "isr-maps") with mini MQTT from the launcher when that's available.
"""
import hashlib
import html
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

MAPS_DIR = Path(os.environ.get("ISR_MAPS_DIR", "/var/lib/isr-maps"))
# The shared UI kit (pi-profiles/ui/), served at /ui/isr-ui.css|js like the launcher does.
UI_DIR = Path(os.environ.get("ISR_UI_DIR", str(Path(__file__).resolve().parent.parent / "ui")))
UI_FILES = {"isr-ui.css": "text/css; charset=utf-8", "isr-ui.js": "application/javascript; charset=utf-8"}
UI_FONT_RE = re.compile(r"^(?:[a-z0-9-]+\.woff2|OFL-[A-Za-z]+\.txt)$")
PORT = int(os.environ.get("ISR_MAPS_PORT", "8160"))
BIND = os.environ.get("ISR_MAPS_BIND", "::")          # dual-stack; see bind_server()
# The tile URL put into ATAK's XML. Phones on the hotspot reach the hub by IP; Android often can't
# resolve .local names, so the hub IP is the safe default.
PUBLIC_BASE = os.environ.get("ISR_MAPS_PUBLIC_BASE", f"http://{os.environ.get('ISR_HUB_IP', '192.168.4.1')}:{PORT}")
PACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESCAN_S = 30


def _png_transparent(size=256) -> bytes:
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * size for _ in range(size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


EMPTY_PNG = _png_transparent()


class Pack:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.local = threading.local()
        meta = dict(self._db().execute("SELECT name, value FROM metadata").fetchall())
        self.meta = meta
        self.format = (meta.get("format") or "png").lower().replace("jpeg", "jpg")
        try:
            self.minzoom = int(meta.get("minzoom", 0))
            self.maxzoom = int(meta.get("maxzoom", 22))
        except ValueError:
            self.minzoom, self.maxzoom = 0, 22
        b = [float(v) for v in meta.get("bounds", "-180,-85.05,180,85.05").split(",")]
        self.bounds = b if len(b) == 4 else [-180, -85.05, 180, 85.05]
        self.size = path.stat().st_size
        self.mtime = path.stat().st_mtime

    def _db(self) -> sqlite3.Connection:
        db = getattr(self.local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            self.local.db = db
        return db

    def tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        if not (self.minzoom <= z <= self.maxzoom) or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return None
        tms_y = (2 ** z - 1) - y
        row = self._db().execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                 (z, x, tms_y)).fetchone()
        return bytes(row[0]) if row else None

    def info(self) -> dict:
        n = self._db().execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        return {"name": self.name, "title": self.meta.get("name", self.name), "format": self.format,
                "minzoom": self.minzoom, "maxzoom": self.maxzoom, "bounds": self.bounds,
                "attribution": self.meta.get("attribution", ""), "description": self.meta.get("description", ""),
                "source": self.meta.get("source", ""), "tiles": n, "size_mb": round(self.size / 1e6, 1),
                "atak_xml": f"{PUBLIC_BASE}/atak/{self.name}.xml",
                "download": f"{PUBLIC_BASE}/download/{self.name}.mbtiles",
                "tile_url": f"{PUBLIC_BASE}/tiles/{self.name}/{{z}}/{{x}}/{{y}}.{self.format}"}


class Catalog:
    def __init__(self, root: Path):
        self.root = root
        self.packs: Dict[str, Pack] = {}
        self.scanned = 0.0
        self.lock = threading.Lock()

    def get(self) -> Dict[str, Pack]:
        with self.lock:
            if time.monotonic() - self.scanned > RESCAN_S:
                self._scan()
            return dict(self.packs)

    def _scan(self):
        found = {}
        for p in sorted(self.root.glob("*.mbtiles")) if self.root.is_dir() else []:
            if not PACK_RE.match(p.stem):
                continue
            old = self.packs.get(p.stem)
            if old and old.mtime == p.stat().st_mtime:
                found[p.stem] = old
                continue
            try:
                found[p.stem] = Pack(p)
            except (sqlite3.Error, OSError, ValueError) as e:
                print(f"skipping {p.name}: {e}", flush=True)
        self.packs, self.scanned = found, time.monotonic()


catalog = Catalog(MAPS_DIR)


def atak_xml(pack: Pack) -> str:
    """ATAK customMapSource (import via the app's Import → Local SD, or drop into atak/imagery/mobile/mapsources)."""
    url = f"{PUBLIC_BASE}/tiles/{pack.name}/{{$z}}/{{$x}}/{{$y}}.{pack.format}"
    title = html.escape(pack.meta.get("name", pack.name), quote=False)
    credit = pack.meta.get("credit", "")
    attribution = pack.meta.get("attribution", "")
    # ATAK shows only <name> in its layer list, so the short credit goes there; the full attribution is
    # in a comment (ATAK's parser rejects unknown elements in some versions, so no <description> tag).
    name = f"{title} (offline, {credit})" if credit else f"{title} (offline)"
    safe_attr = html.escape(attribution, quote=False).replace("--", "-")
    note = f"  <!-- {safe_attr} -->\n" if attribution else ""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<customMapSource>\n" + note +
            f"  <name>{html.escape(name, quote=False)}</name>\n"
            f"  <minZoom>{pack.minzoom}</minZoom>\n  <maxZoom>{pack.maxzoom}</maxZoom>\n"
            f"  <tileType>{'jpeg' if pack.format == 'jpg' else 'png'}</tileType>\n"
            "  <tileUpdate>None</tileUpdate>\n"
            f"  <url>{html.escape(url, quote=False)}</url>\n"
            "  <backgroundColor>#000000</backgroundColor>\n</customMapSource>\n")


TILE_RE = re.compile(r"^/tiles/([a-z0-9][a-z0-9_-]{0,63})/(\d{1,2})/(\d{1,8})/(\d{1,8})\.(png|jpg|jpeg)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "isr-maps"
    sys_version = ""

    def log_message(self, fmt, *args):
        if args and str(args[1])[:1] in "45" and "/tiles/" not in str(args[0]):
            super().log_message(fmt, *args)

    def _send(self, code, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        packs = catalog.get()
        m = TILE_RE.match(path)
        if m:
            pack = packs.get(m.group(1))
            if not pack:
                return self._send(404, EMPTY_PNG, "image/png")
            data = pack.tile(int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if data is None:
                return self._send(404, EMPTY_PNG, "image/png", {"Cache-Control": "public, max-age=3600"})
            etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            ctype = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
            return self._send(200, data, ctype, {"Cache-Control": "public, max-age=604800", "ETag": etag})
        m = re.match(r"^/atak/([a-z0-9][a-z0-9_-]{0,63})\.xml$", path)
        if m and m.group(1) in packs:
            return self._send(200, atak_xml(packs[m.group(1)]).encode(), "application/xml",
                              {"Content-Disposition": f'attachment; filename="{m.group(1)}.xml"'})
        m = re.match(r"^/download/([a-z0-9][a-z0-9_-]{0,63})\.mbtiles$", path)
        if m and m.group(1) in packs:
            return self._send_file(packs[m.group(1)].path)
        name = path.rsplit("/", 1)[-1]
        if path == f"/ui/fonts/{name}" and UI_FONT_RE.match(name):
            try:
                return self._send(200, (UI_DIR / "fonts" / name).read_bytes(),
                                  "font/woff2" if name.endswith(".woff2") else "text/plain; charset=utf-8",
                                  {"Cache-Control": "max-age=86400"})
            except OSError:
                return self._send(404, b"not found", "text/plain")
        if path == f"/ui/{name}" and name in UI_FILES:
            try:
                return self._send(200, (UI_DIR / name).read_bytes(), UI_FILES[name], {"Cache-Control": "max-age=300"})
            except OSError:
                return self._send(404, b"UI kit not installed", "text/plain")
        if path == "/api/packs":
            return self._send(200, json.dumps({"packs": [p.info() for p in packs.values()]}).encode(), "application/json",
                              {"Cache-Control": "no-store"})
        if path in ("/", "/index.html"):
            return self._send(200, index_html(packs).encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        if path == "/healthz":
            return self._send(200, b'{"ok":true}', "application/json")
        self._send(404, b'{"error":"not found"}', "application/json")

    def _send_file(self, p: Path):
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)


def index_html(packs: Dict[str, Pack]) -> str:
    """The kit look: header with one pill, a card per pack, help in a collapsed Details."""
    e = html.escape
    cards = "".join(
        f"<div class='isr-card'><h2>{e(i['name'])}</h2><div class='isr-big'>{e(i['title'])}</div>"
        f"<div class='isr-muted'>Zoom {i['minzoom']}–{i['maxzoom']} · {i['size_mb']} MB</div>"
        f"<p><a class='isr-btn primary' href='/atak/{e(i['name'])}.xml' download>Add to ATAK</a> "
        f"<a class='isr-btn' href='/download/{e(i['name'])}.mbtiles' download>Download</a></p>"
        f"<div class='isr-muted'>{e(i['attribution'])}</div></div>"
        for i in (p.info() for p in packs.values()))
    n = len(packs)
    state, word = ("ok", "Ready") if n else ("idle", "Off")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'><title>Offline maps</title>"
            "<link rel='stylesheet' href='/ui/isr-ui.css?v=4'><script src='/ui/isr-ui.js?v=4'></script></head><body>"
            "<header class='isr-bar'><h1>Offline maps</h1><span class='isr-sub'>ATAK basemaps</span>"
            f"<div class='isr-right'><button class='isr-pill {state}' id='isr-status' type='button'>{word}</button>"
            "<a class='isr-kit-link' id='isr-kit-link' href='#'>Kit</a></div>"
            f"<div class='isr-status-list' id='isr-status-list'><div><span class='k'>Packs</span>"
            f"<span class='isr-pill {state}'>{n} pack{'s' if n != 1 else ''}</span></div></div></header>"
            "<main><div class='isr-grid'>"
            + (cards or "<div class='isr-card'><h2>No packs</h2><div class='isr-muted'>Build one on a Mac, then "
               "push it to the Pi.</div></div>")
            + "</div><details class='isr-more'><summary>Details</summary>Add to ATAK downloads the map source; in ATAK: "
            "☰ › Import › Local SD › Downloads › the .xml, then Maps &amp; Favorites. Tiles stream from this Pi. Download "
            "gets the .mbtiles for use away from the Pi. Packs: laptop/build-map-pack.py, then push-to-pi.sh --maps-only, "
            f"into {e(str(MAPS_DIR))}.</details></main></body></html>")


def heartbeat_loop(stop: threading.Event):
    """Contract heartbeat (tool isr-maps), via the launcher's stdlib MQTT client if installed."""
    sys.path.insert(0, os.environ.get("ISR_LAUNCHER_DIR", "/opt/pi-profiles/launcher"))
    try:
        from mqtt_mini import MiniMQTT, MQTTError
    except ImportError:
        return
    import socket
    host = os.environ.get("ISR_HOST") or socket.gethostname().split(".")[0].lower()
    while not stop.is_set():
        c = MiniMQTT(os.environ.get("MQTT_HOST", "127.0.0.1"), int(os.environ.get("MQTT_PORT", "1883")),
                     os.environ.get("MQTT_USER", "guest"), os.environ.get("MQTT_PASS", "guest"),
                     client_id=f"isr-maps-{os.getpid()}")
        try:
            c.connect()
            while not stop.is_set():
                n = len(catalog.get())
                c.publish("isrkit/heartbeat/isr-maps", json.dumps({
                    "tool": "isr-maps", "host": host, "url": f"{PUBLIC_BASE}/", "mode": "cpu",
                    "status": "ok" if n else "degraded", "detail": f"{n} map pack(s)", "ts": int(time.time())}).encode(),
                    retain=True)
                stop.wait(10)
            c.publish("isrkit/heartbeat/isr-maps", json.dumps({"tool": "isr-maps", "host": host, "url": f"{PUBLIC_BASE}/",
                      "mode": "cpu", "status": "stopped", "ts": int(time.time())}).encode(), retain=True)
        except (OSError, MQTTError):
            pass
        finally:
            c.close()
        stop.wait(5)


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once ("::" with IPV6_V6ONLY off). A Mac on the cable that is also on
    Wi-Fi routes 169.254/16 out of Wi-Fi, so it can only reach the Pi over IPv6 link-local (fe80::%en8);
    a 0.0.0.0-only listener is unreachable from it."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (OSError, AttributeError):
            pass
        super().server_bind()


def bind_server(bind: str, port: int, handler) -> ThreadingHTTPServer:
    """"::" (the default) = dual-stack, falling back to 0.0.0.0 when IPv6 is off; anything else as given."""
    ThreadingHTTPServer.allow_reuse_address = True
    DualStackServer.allow_reuse_address = True
    if bind in ("::", "[::]"):
        try:
            srv = DualStackServer(("::", port), handler)
        except OSError:
            srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    elif ":" in bind:
        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        srv = V6((bind.strip("[]"), port), handler)
    else:
        srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    return srv


def make_server(bind=BIND, port=PORT) -> ThreadingHTTPServer:
    return bind_server(bind, port, Handler)


def main():
    import signal
    stop = threading.Event()
    hb = threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True)
    hb.start()
    srv = make_server()
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=srv.shutdown, daemon=True).start())
    where = "[::] (IPv6 + IPv4)" if srv.address_family == socket.AF_INET6 and srv.server_address[0] == "::" else srv.server_address[0]
    print(f"isr-maps on {where}:{PORT}, packs in {MAPS_DIR}: {', '.join(catalog.get()) or 'none'}", flush=True)
    try:
        srv.serve_forever()
    finally:
        stop.set()
        hb.join(timeout=3)


if __name__ == "__main__":
    main()
