#!/usr/bin/env python3
"""Push an offline map pack to every ATAK device connected to OTS, as a TAK data package.

    isr-push-maps                       list packs
    isr-push-maps <pack>                build, upload to OTS, send to all connected EUDs
    isr-push-maps <pack> --with-mbtiles also put the .mbtiles in the package (for use away from the Pi)
    isr-push-maps <pack> --dry-run      build and validate the zip only; print what would be sent
    isr-push-maps <pack> --auto         stay connected; send once to each EUD that connects later

What happens (checked against OpenTAKServer 1.8.3 and ATAK-CIV's importer):
1. A data package zip: MANIFEST/manifest.xml (MissionPackageManifest version 2, uid, name) plus
   <folder>/<pack>.xml, the ATAK customMapSource that isr-maps serves (tiles from
   http://192.168.4.1:8160/tiles/<pack>/{$z}/{$x}/{$y}.png). ATAK unzips each file and offers it to
   its import sorters; ImportLayersSort takes anything an imagery SPI supports, which includes a
   MOBAC customMapSource XML and MBTiles. So no contentType is set. The zip is deterministic, so the
   same pack always gives the same sha256.
2. Upload: POST multipart /Marti/sync/missionupload?hash=<sha256>&filename=<name>.zip&creatorUid=<uid>
   on OTS's plain-HTTP Marti port (8080). OTS 1.8.3 has no auth on the data-package Marti routes.
   Skipped when HEAD /Marti/sync/content?hash=<sha256> already answers 200.
3. Send: OTS has no server-side "send to all", so we send a fileshare CoT (type b-f-t-r) over a
   contract-C5 TAK connection (uid isrkit-maps-<host>, callsign "MAPS <host>"). A CoT with no
   <marti><dest> goes to the __ANON__ group, i.e. every EUD on the plain TCP port. ATAK then fetches
   senderUrl = http://<hub>:8080/Marti/sync/content?hash=<sha256> and imports it.
Stdlib only. Never needs root.
"""
import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from xml.sax.saxutils import quoteattr

VERSION = "1.0"
PACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
NS = uuid.UUID("6f1d3c2a-4b7e-5a9c-8e21-1f0a6c3d9b54")      # fixed namespace: package uid from content
ZIP_TIME = (2020, 1, 1, 0, 0, 0)
REG_WAIT_S = 3.0          # C5.2: OTS binds a client's queues only if it registers after its channel opens


def _env_file(path: str) -> Dict[str, str]:
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def settings() -> Dict[str, str]:
    conf = os.environ.get("ISR_KIT_CONF", "/etc/isr-kit")
    s = {}
    s.update(_env_file(f"{conf}/hub.env"))
    s.update(_env_file(f"{conf}/site.env"))
    s.update({k: v for k, v in os.environ.items() if k.startswith(("ISR_", "TAK_", "SENSOR_"))})
    hub_ip = s.get("ISR_HUB_IP", "192.168.4.1")
    s.setdefault("ISR_HOST", socket.gethostname().split(".")[0].lower())
    s.setdefault("ISR_MAPS_DIR", "/var/lib/isr-maps")
    s.setdefault("ISR_MAPS_URL", "http://127.0.0.1:8160")
    s.setdefault("ISR_OTS_URL", "http://127.0.0.1:8080")
    s.setdefault("ISR_OTS_PUBLIC_URL", f"http://{hub_ip}:8080")
    s.setdefault("TAK_HOST", "127.0.0.1")
    s.setdefault("TAK_PORT", "8087")
    return s


class PushError(Exception):
    pass


# ── 1. the data package ─────────────────────────────────────────────────────

def fetch_map_source(maps_url: str, pack: str) -> Tuple[bytes, dict]:
    """The pack's ATAK XML and its catalogue entry, from isr-maps (the one place that knows the public URL)."""
    try:
        with urllib.request.urlopen(f"{maps_url}/api/packs", timeout=10) as r:
            packs = {p["name"]: p for p in json.load(r).get("packs", [])}
    except (OSError, ValueError) as e:
        raise PushError(f"isr-maps not reachable at {maps_url} ({e})")
    if pack not in packs:
        raise PushError(f"no pack '{pack}' on isr-maps (have: {', '.join(sorted(packs)) or 'none'})")
    with urllib.request.urlopen(f"{maps_url}/atak/{pack}.xml", timeout=10) as r:
        xml = r.read()
    if b"<customMapSource>" not in xml:
        raise PushError("isr-maps returned something that isn't a customMapSource")
    return xml, packs[pack]


def manifest_xml(uid: str, name: str, entries) -> bytes:
    contents = "".join(f'    <Content ignore="false" zipEntry={quoteattr(e)}/>\n' for e in entries)
    return ('<MissionPackageManifest version="2">\n  <Configuration>\n'
            f'    <Parameter name="uid" value={quoteattr(uid)}/>\n'
            f'    <Parameter name="name" value={quoteattr(name)}/>\n'
            '    <Parameter name="onReceiveDelete" value="false"/>\n'
            f'  </Configuration>\n  <Contents>\n{contents}  </Contents>\n</MissionPackageManifest>\n').encode()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_package(pack: str, xml: bytes, out_dir: Path, mbtiles: Optional[Path] = None) -> dict:
    """Deterministic zip: same inputs, same bytes, same sha256 (so re-pushing never duplicates in OTS)."""
    content_id = hashlib.sha256(xml + (_sha_file(mbtiles).encode() if mbtiles else b"")).hexdigest()
    uid = str(uuid.uuid5(NS, f"{pack}:{content_id}"))
    folder = content_id[:32]
    name = f"{pack}-map" + ("-with-tiles" if mbtiles else "")
    entries = [f"{folder}/{pack}.xml"] + ([f"{folder}/{pack}.mbtiles"] if mbtiles else [])
    path = out_dir / f"{name}.zip"

    def info(n, compress):
        zi = zipfile.ZipInfo(n, ZIP_TIME)
        zi.compress_type = compress
        zi.external_attr = 0o644 << 16
        return zi
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(info("MANIFEST/manifest.xml", zipfile.ZIP_DEFLATED), manifest_xml(uid, name, entries))
        z.writestr(info(entries[0], zipfile.ZIP_DEFLATED), xml)
        if mbtiles:     # PNG/JPEG tiles don't compress: store, streamed, zip64 for big packs
            with open(mbtiles, "rb") as src, z.open(info(entries[1], zipfile.ZIP_STORED), "w", force_zip64=True) as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
    validate_package(path)
    return {"path": path, "sha256": _sha_file(path), "size": path.stat().st_size, "uid": uid,
            "name": name, "filename": path.name, "entries": entries}


def validate_package(path: Path) -> None:
    """What ATAK's MissionPackageExtractor needs: a v2 manifest whose every zipEntry exists."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise PushError("zip CRC check failed")
        m = ET.fromstring(z.read("MANIFEST/manifest.xml"))
        if m.tag != "MissionPackageManifest" or m.get("version") != "2":
            raise PushError("manifest is not MissionPackageManifest version 2")
        params = {p.get("name"): p.get("value") for p in m.iter("Parameter")}
        if not params.get("uid") or not params.get("name"):
            raise PushError("manifest needs uid and name")
        names = set(z.namelist())
        entries = [c.get("zipEntry") for c in m.iter("Content")]
        if not entries or any(e not in names for e in entries):
            raise PushError("manifest lists a zipEntry that isn't in the zip")
        xmls = [e for e in entries if e.endswith(".xml")]
        if not xmls or ET.fromstring(z.read(xmls[0])).tag != "customMapSource":
            raise PushError("no customMapSource XML in the package")


# ── 2. upload to OTS ────────────────────────────────────────────────────────

def _http(base: str, method: str, path: str, body=None, headers=None, timeout=120):
    u = urllib.parse.urlsplit(base)
    c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(4096)
    finally:
        c.close()


def upload(ots_url: str, pkg: dict, creator_uid: str, out: Callable[[str], None] = print) -> bool:
    """Returns True if uploaded, False if OTS already had it."""
    sha = pkg["sha256"]
    try:
        status, _ = _http(ots_url, "HEAD", f"/Marti/sync/content?hash={sha}", timeout=10)
    except OSError as e:
        raise PushError(f"OTS not reachable at {ots_url} ({e})")
    if status == 200:
        out(f"OTS already has {pkg['filename']} ({sha[:12]}…)")
        return False
    boundary = "isrkit" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"assetfile\"; filename=\"{pkg['filename']}\"\r\n"
            "Content-Type: application/x-zip-compressed\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        with open(pkg["path"], "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                yield chunk
        yield tail
    q = urllib.parse.urlencode({"hash": sha, "filename": pkg["filename"], "creatorUid": creator_uid})
    status, resp = _http(ots_url, "POST", f"/Marti/sync/missionupload?{q}", body=body(),
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                  "Content-Length": str(len(head) + pkg["size"] + len(tail))}, timeout=300)
    if status == 413:
        raise PushError(f"OTS/nginx refused {pkg['size'] // 1_000_000} MB (413): raise client_max_body_size "
                        "on the :8080 server block, or push without --with-mbtiles")
    if status != 200:
        raise PushError(f"OTS upload failed: HTTP {status} {resp[:200].decode('utf-8', 'replace')}")
    out(f"uploaded {pkg['filename']} to OTS ({pkg['size'] / 1e6:.1f} MB, {sha[:12]}…)")
    return True


# ── 3. send over a C5-registered TAK connection ─────────────────────────────

def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _position(s: Dict[str, str]) -> Optional[Tuple[float, float]]:
    try:
        lat, lon = float(s["SENSOR_LAT"]), float(s["SENSOR_LON"])
    except (KeyError, ValueError):
        return None
    return None if (lat == 0 and lon == 0) else (lat, lon)


def identity(s: Dict[str, str]) -> Tuple[str, str]:
    host = re.sub(r"[^a-z0-9-]", "-", s["ISR_HOST"].lower())
    return f"isrkit-maps-{host}", f"MAPS {host}"


def registration_sa(s: Dict[str, str], now: Optional[float] = None) -> bytes:
    """Contract C5.2. At the site position if set (stale +120 s); else the 'unknown position' 0,0
    with stale = start, so ATAK never draws it."""
    now = now or time.time()
    uid, callsign = identity(s)
    pos = _position(s)
    lat, lon = pos if pos else (0, 0)
    stale = _iso(now + 120) if pos else _iso(now)
    return (f'<event version="2.0" uid="{uid}" type="a-f-G-E-S" how="m-g" time="{_iso(now)}" '
            f'start="{_iso(now)}" stale="{stale}"><point lat="{lat}" lon="{lon}" hae="9999999.0" '
            f'ce="9999999.0" le="9999999.0"/><detail><contact callsign="{callsign}" endpoint="*:-1:stcp"/>'
            '<__group name="Cyan" role="Team Member"/>'
            f'<takv platform="isr-kit" device="isr-push-maps" os="linux" version="{VERSION}"/></detail></event>').encode()


def fileshare_cot(s: Dict[str, str], pkg: dict, dest_uid: Optional[str] = None, now: Optional[float] = None) -> bytes:
    """ATAK's own data-package send: type b-f-t-r with <fileshare>. No <marti><dest> = everyone."""
    now = now or time.time()
    uid, callsign = identity(s)
    url = f"{s['ISR_OTS_PUBLIC_URL']}/Marti/sync/content?hash={pkg['sha256']}"
    ev = str(uuid.uuid4())
    pos = _position(s)
    lat, lon = pos if pos else (0, 0)      # b-f-t-r is never drawn on the map
    dest = f'<marti><dest uid={quoteattr(dest_uid)}/></marti>' if dest_uid else ""
    return (f'<event version="2.0" uid="{ev}" type="b-f-t-r" how="h-e" time="{_iso(now)}" start="{_iso(now)}" '
            f'stale="{_iso(now + 600)}"><point lat="{lat}" lon="{lon}" hae="9999999.0" ce="9999999.0" '
            f'le="9999999.0"/><detail><fileshare filename={quoteattr(pkg["filename"])} senderUrl={quoteattr(url)} '
            f'sizeInBytes="{pkg["size"]}" sha256="{pkg["sha256"]}" senderUid="{uid}" '
            f'senderCallsign={quoteattr(callsign)} name={quoteattr(pkg["name"])}/>'
            f'<ackrequest uid="{ev}" ackrequested="false" tag={quoteattr(pkg["name"])}/>{dest}</detail></event>').encode()


class TakLink:
    """One registered TCP connection (C5.1-C5.3)."""

    def __init__(self, s: Dict[str, str], sleep=time.sleep, connect=socket.create_connection):
        self.s, self.sleep, self.connect = s, sleep, connect
        self.sock = None

    def open(self):
        try:
            self.sock = self.connect((self.s["TAK_HOST"], int(self.s["TAK_PORT"])), timeout=10)
        except OSError as e:
            raise PushError(f"TAK server not reachable at {self.s['TAK_HOST']}:{self.s['TAK_PORT']} ({e})")
        self.sleep(REG_WAIT_S)
        self.send(registration_sa(self.s))
        self.sleep(1.0)            # let OTS create the EUD row before anything else arrives from us
        return self

    def send(self, data: bytes):
        self.sock.sendall(data)

    def close(self):
        if self.sock:
            try:
                self.sleep(1.0)        # give OTS a moment to route before the disconnect
                self.sock.close()
            finally:
                self.sock = None


def prepare(pack: str, s: Dict[str, str], with_mbtiles: bool, work: Path, out=print) -> dict:
    if not PACK_RE.match(pack):
        raise PushError("bad pack name")
    xml, entry = fetch_map_source(s["ISR_MAPS_URL"], pack)
    mb = None
    if with_mbtiles:
        mb = Path(s["ISR_MAPS_DIR"]) / f"{pack}.mbtiles"
        if not mb.is_file():
            raise PushError(f"{mb} not found (needed for --with-mbtiles)")
    pkg = build_package(pack, xml, work, mb)
    out(f"package {pkg['filename']}: {pkg['size'] / 1e6:.1f} MB, sha256 {pkg['sha256'][:12]}…, "
        f"{len(pkg['entries'])} file(s); {entry.get('attribution', '')}")
    return pkg


def push(pack: str, with_mbtiles: bool = False, dry_run: bool = False, s: Optional[Dict[str, str]] = None,
         out: Callable[[str], None] = print, link_factory=TakLink) -> dict:
    s = s or settings()
    uid, callsign = identity(s)
    with tempfile.TemporaryDirectory(prefix="isr-push-maps-") as tmp:
        pkg = prepare(pack, s, with_mbtiles, Path(tmp), out)
        result = {"pack": pack, "filename": pkg["filename"], "sha256": pkg["sha256"], "size": pkg["size"],
                  "url": f"{s['ISR_OTS_PUBLIC_URL']}/Marti/sync/content?hash={pkg['sha256']}", "sent": False}
        if dry_run:
            out("dry run: not uploaded, not sent")
            return result
        result["uploaded"] = upload(s["ISR_OTS_URL"], pkg, uid, out)
    link = link_factory(s).open()
    try:
        link.send(fileshare_cot(s, pkg))
    finally:
        link.close()
    result["sent"] = True
    out(f"sent to all connected ATAK devices as '{callsign}'. Each phone shows a file-transfer "
        "notification and imports the map source (Maps & Favorites).")
    return result


# ── optional: once to each EUD that connects later ──────────────────────────

EVENT_RE = re.compile(rb"<event\b.*?</event>", re.S)


def eud_uid(event: bytes) -> Optional[str]:
    """uid of an ATAK/WinTAK/iTAK self-SA (has <contact> and <takv>, isn't one of ours)."""
    if b"<takv" not in event or b"<contact" not in event:
        return None
    m = re.search(rb'<event\b[^>]*\buid="([^"]+)"', event)
    if not m:
        return None
    u = m.group(1).decode("utf-8", "replace")
    if u.startswith("isrkit-") or b'platform="isr-kit"' in event or u.endswith("-ping"):
        return None
    return u


def auto(pack: str, with_mbtiles: bool, s: Optional[Dict[str, str]] = None, out=print,
         state_file: Optional[Path] = None, stop: Optional[threading.Event] = None, link_factory=TakLink):
    """Stay registered; send the package (directed) once to each EUD uid we haven't sent this sha to."""
    s = s or settings()
    stop = stop or threading.Event()
    state_file = state_file or Path(os.environ.get("STATE_DIRECTORY", str(Path.home() / ".cache"))) / "isr-push-maps.json"
    with tempfile.TemporaryDirectory(prefix="isr-push-maps-") as tmp:
        pkg = prepare(pack, s, with_mbtiles, Path(tmp), out)
        upload(s["ISR_OTS_URL"], pkg, identity(s)[0], out)
    try:
        done = json.loads(state_file.read_text())
    except (OSError, ValueError):
        done = {}
    sent = set(done.get(pkg["sha256"], []))
    while not stop.is_set():
        link = None
        try:
            link = link_factory(s).open()
            link.sock.settimeout(5)
            buf, last_sa = b"", time.monotonic()
            out(f"auto: watching for new EUDs ({len(sent)} already have {pkg['filename']})")
            while not stop.is_set():
                if time.monotonic() - last_sa > 60:           # C5.3 keep-alive SA
                    link.send(registration_sa(s))
                    last_sa = time.monotonic()
                try:
                    data = link.sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise OSError("connection closed")
                buf = (buf + data)[-262144:]
                for m in list(EVENT_RE.finditer(buf)):
                    u = eud_uid(m.group(0))
                    if u and u not in sent:
                        link.send(fileshare_cot(s, pkg, dest_uid=u))
                        sent.add(u)
                        done[pkg["sha256"]] = sorted(sent)
                        try:
                            state_file.parent.mkdir(parents=True, exist_ok=True)
                            state_file.write_text(json.dumps(done))
                        except OSError:
                            pass
                        out(f"auto: sent {pkg['filename']} to {u}")
                last = buf.rfind(b"</event>")
                if last >= 0:
                    buf = buf[last + 8:]
        except (OSError, PushError) as e:
            out(f"auto: {e}; reconnecting in 10 s")
            stop.wait(10)
        finally:
            if link and link.sock:
                link.sock.close()
    return sent


def list_packs(s) -> int:
    try:
        with urllib.request.urlopen(f"{s['ISR_MAPS_URL']}/api/packs", timeout=10) as r:
            packs = json.load(r).get("packs", [])
    except (OSError, ValueError) as e:
        print(f"isr-maps not reachable at {s['ISR_MAPS_URL']} ({e})", file=sys.stderr)
        return 1
    for p in packs:
        print(f"{p['name']:20s} z{p.get('minzoom')}-{p.get('maxzoom')}  {p.get('size_mb', '?')} MB  {p.get('title', '')}")
    if not packs:
        print("no packs (build on a Mac: laptop/build-map-pack.py, then push-to-pi.sh --maps-only)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="isr-push-maps", description=__doc__.split("\n\n")[0])
    ap.add_argument("pack", nargs="?")
    ap.add_argument("--with-mbtiles", action="store_true", help="include the .mbtiles (big; for use away from the Pi)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto", action="store_true", help="keep running: send once to each EUD that connects")
    a = ap.parse_args(argv)
    s = settings()
    if not a.pack:
        return list_packs(s)
    try:
        if a.auto:
            auto(a.pack, a.with_mbtiles, s)
        else:
            r = push(a.pack, a.with_mbtiles, a.dry_run, s)
            print(f"sha256 {r['sha256']}  {r['url']}")
    except PushError as e:
        print(f"isr-push-maps: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
