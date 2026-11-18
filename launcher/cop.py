"""Common Operating Picture for the launcher's /map page: a TAK viewer connection to OpenTAKServer and an
in-memory picture of what's on the map. Standard library only.

  CopClient    one long-lived TCP connection to OTS (contract v5, C5.1-C5.4): connect, wait 3 s, register
               as a viewer (uid isrkit-launcher-map-<host>, callsign "COP-VIEW <host>"), re-send the
               registration every 60 s, read every event. It never sends a marker: the registration SA
               uses the contract's "unknown position" (0,0, stale = start), so no TAK client draws it.
  Picture      the map keyed by uid, from parse_event(). Items stay until stale, then fade for FADE_S
               seconds, then go. Every change gets a sequence number, for /api/cop/stream deltas.

Nothing here forwards anything to OTS or anywhere else.
"""
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

REG_WAIT_S = 3.0            # C5.2: OTS binds a client's queues only if it registers after its channel opens
REG_EVERY_S = 60.0          # C5.3: re-send the SA to stay "Connected"
FADE_S = 120.0              # after stale: shown faded this long, then removed
MAX_EVENT = 64 * 1024       # one CoT event; anything bigger is skipped
MAX_BUFFER = 1024 * 1024
MAX_ITEMS = 2000
UNKNOWN_HAE = 9_000_000     # 9999999 = "unknown" in CoT
EVENT_END = re.compile(rb"</event\s*>|<event\b[^>]*/>", re.S)
SKIP_TYPES = ("t-x-", "b-f-t-", "b-t-f", "y-")        # pings, file transfer, GeoChat, acks
# A contact the tools place at the sensor because they have no range (op-sentry, rf-spectrum, acoustic,
# drone-detector "probable", fpv air watch): the map draws its bearing line and a badge at the OP instead.
NO_RANGE_RE = re.compile(r"range unknown|no range|placed at the (?:OP|sensor)|marker is (?:at )?the sensor|"
                         r"at the sensor, not|is the sensor position|somewhere along the bearing", re.I)
COUNT_RE = re.compile(r"\b(\d{1,3})\s*x\s*([A-Za-z][A-Za-z-]{1,20})")
BEARING_RE = re.compile(r"\bBRG\s*(\d{1,3})\b|\bbearing\s+(\d{1,3})\b", re.I)
AFF = {"f": "F", "a": "A", "h": "H", "s": "S", "j": "J", "k": "K", "u": "U", "p": "P", "o": "U", "n": "N",
       "x": "U", "g": "U"}
DIM = {"P": "P", "A": "A", "G": "G", "S": "S", "U": "U", "F": "F"}


def _ts(s: Optional[str]) -> Optional[float]:
    """CoT time (ISO-8601 UTC, …Z, optional fraction) to epoch seconds."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s if "." not in s else re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _f(v: Optional[str]) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def affiliation(cot_type: str) -> str:
    """CoT atom affiliation: f friend, h hostile, u unknown, n neutral (a/s/j/k/p folded in)."""
    parts = cot_type.split("-")
    if len(parts) < 2 or parts[0] != "a":
        return ""
    a = parts[1]
    return {"f": "f", "a": "f", "h": "h", "s": "h", "j": "h", "k": "h", "n": "n"}.get(a, "u")


def sidc_for(cot_type: str) -> Optional[str]:
    """MIL-STD-2525C / APP-6 letter SIDC (15 characters) for a CoT atom type, the same mapping ATAK uses:
    a-<aff>-<dim>-<function letters> -> S <AFF> <DIM> P <function, 6> -----.
    a-h-A-M-F-Q -> SHAPMFQ--------, a-f-G-E-S -> SFGPES---------. None for non-atoms."""
    parts = cot_type.split("-")
    if len(parts) < 3 or parts[0] != "a":
        return None
    aff = AFF.get(parts[1], "U")
    dim = DIM.get(parts[2], "G")
    fn = parts[3:]
    if any(len(p) != 1 or not p.isalpha() for p in fn) or len(fn) > 6:
        fn = []
    return ("S" + aff + dim + "P" + "".join(fn).upper().ljust(6, "-") + "-----")[:15]


def mode_of(remarks: str, callsign: str, uid: str) -> str:
    """SIM / REC / LIVE from the contract's remarks prefixes (and SIM callsigns or uids)."""
    r = (remarks or "").lstrip().upper()
    if r.startswith("SIMULATED") or r.startswith("[SIM]") or callsign.upper().startswith(("SIM ", "SIM-")) \
            or "-SIM-" in uid.upper() or uid.upper().startswith("SIM-"):
        return "sim"
    if r.startswith("RECORDED") or r.startswith("[REC]"):
        return "rec"
    return "live"


def layer_of(item: dict) -> str:
    t, uid = item["type"], item["uid"]
    if item.get("line") is not None:
        return "bearing" if (t.startswith("u-d-f") and ("-brg-" in uid.lower() or " BRG " in item["callsign"].upper())) \
            else "shape"
    if item.get("circle") is not None:
        return "contact"                      # an estimate's uncertainty (geo-locate): with the contacts
    if t.startswith("a-f-G-E-S") or t.startswith("a-a-G-E-S"):
        return "sensor"
    aff = affiliation(t)
    if aff == "f":
        return "friend"
    if aff in ("h", "u", "n"):
        return "contact"
    if t.startswith("b-"):
        return "friend"                       # mesh SOS alerts and waypoints come from our own people
    return "other"


def _safe_xml(ev: bytes) -> Optional[ET.Element]:
    if len(ev) > MAX_EVENT or b"<!DOCTYPE" in ev or b"<!ENTITY" in ev:
        return None
    try:
        return ET.fromstring(ev)
    except ET.ParseError:
        return None


def delete_uids(ev: bytes) -> List[str]:
    """The uids a CoT delete removes: type t-x-d-d (ATAK's delete, with <__forcedelete/> or not) names
    them in <link uid=…>. [] for anything else."""
    if b"t-x-d-d" not in ev and b"__forcedelete" not in ev:
        return []
    root = _safe_xml(ev)
    if root is None or root.tag != "event":
        return []
    det = root.find("detail")
    if not (root.get("type") or "").startswith("t-x-d-d") and (det is None or det.find("__forcedelete") is None):
        return []
    uids = [ln.get("uid")[:200] for ln in (det.findall("link") if det is not None else []) if ln.get("uid")]
    return uids[:50]


def parse_event(ev: bytes, now: Optional[float] = None) -> Optional[dict]:
    """One CoT <event> to a picture item, or None (not for the map: pings, chat, file shares, the
    'unknown position' 0,0 registration, unparseable XML)."""
    root = _safe_xml(ev)
    if root is None or root.tag != "event":
        return None
    uid = (root.get("uid") or "").strip()[:200]
    cot_type = (root.get("type") or "").strip()[:60]
    if not uid or not cot_type or cot_type.startswith(SKIP_TYPES):
        return None
    pt = root.find("point")
    lat, lon = _f(pt.get("lat")) if pt is not None else None, _f(pt.get("lon")) if pt is not None else None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    hae = _f(pt.get("hae"))
    ce = _f(pt.get("ce"))
    det = root.find("detail")
    det = det if det is not None else ET.Element("detail")
    contact = det.find("contact")
    callsign = (contact.get("callsign") if contact is not None else "") or ""
    rem_el = det.find("remarks")
    remarks = ("".join(rem_el.itertext()) if rem_el is not None else "").strip()
    track = det.find("track")
    course = _f(track.get("course")) if track is not None else None
    speed = _f(track.get("speed")) if track is not None else None
    links, line = [], []
    for ln in det.findall("link"):
        if ln.get("point"):
            p = [_f(v) for v in ln.get("point").split(",")[:2]]
            if len(p) == 2 and None not in p and -90 <= p[0] <= 90 and -180 <= p[1] <= 180:
                line.append([round(p[0], 7), round(p[1], 7)])
        elif ln.get("uid"):
            links.append({"uid": ln.get("uid")[:200], "relation": (ln.get("relation") or "")[:20],
                          "type": (ln.get("type") or "")[:60]})
    stale = _ts(root.get("stale"))
    t = _ts(root.get("time")) or now or time.time()
    unknown_pos = lat == 0 and lon == 0 and (hae is None or hae >= UNKNOWN_HAE)
    if unknown_pos and len(line) < 2:
        return None                           # C5.2's "unknown position" SA: never drawn
    item = {
        "uid": uid, "type": cot_type, "how": (root.get("how") or "")[:20],
        "lat": round(lat, 7), "lon": round(lon, 7),
        "hae": None if hae is None or hae >= UNKNOWN_HAE else round(hae, 1),
        "ce": None if ce is None or ce >= UNKNOWN_HAE else round(ce, 1),
        "course": None if course is None or speed is None else round(course % 360, 1),
        "speed": None if course is None or speed is None or speed < 0 else round(speed, 2),
        "callsign": callsign[:120], "remarks": remarks[:1000],
        "time": t, "stale": stale if stale is not None else t + 300,
        "links": links[:20], "line": line[:50] if len(line) >= 2 else None, "circle": None,
        "aff": affiliation(cot_type), "sidc": sidc_for(cot_type),
        "mode": mode_of(remarks, callsign, uid), "emergency": cot_type.startswith("b-a-o") or det.find("emergency") is not None,
    }
    if item["line"] is None and cot_type.startswith("u-d-") and len(line) == 1:
        return None
    ell = det.find("shape/ellipse")
    if ell is not None:
        r = _f(ell.get("major"))
        if r and r > 0:
            item["circle"] = round(min(r, 500_000.0), 1)
    if cot_type.startswith("u-d-") and item["line"] is None and item["circle"] is None:
        return None                           # a drawing we don't render
    parent = next((ln["uid"] for ln in links if ln["relation"] == "p-p"), "")
    item["parent"] = parent
    item["layer"] = layer_of(item)
    text = callsign + " " + remarks
    item["norange"] = bool(item["layer"] == "contact" and item["line"] is None and item["circle"] is None
                           and item["course"] is None and NO_RANGE_RE.search(remarks))
    m = COUNT_RE.search(text)
    item["count"], item["what"] = (int(m.group(1)), m.group(2).lower()) if m else (1, "")
    b = BEARING_RE.search(text)
    item["bearing"] = int(b.group(1) or b.group(2)) % 360 if b else None
    return item


class Picture:
    """The COP, keyed by uid. Thread-safe; wait() blocks until something changes (for SSE)."""

    def __init__(self, clock: Callable[[], float] = time.time, fade_s: float = FADE_S):
        self.clock, self.fade_s = clock, fade_s
        self.cond = threading.Condition()
        self.items: Dict[str, dict] = {}
        self.seq = 0
        self.removed: deque = deque(maxlen=2000)       # (seq, uid)
        self.oldest_removal = 0                          # seq before which removals were forgotten
        self.events = 0

    def _bump(self) -> int:
        self.seq += 1
        return self.seq

    def ingest(self, item: Optional[dict]) -> bool:
        if not item:
            return False
        now = self.clock()
        if item["stale"] <= now:                         # arrives already stale: it ends now (no fade)
            self.remove([item["uid"]])
            return False
        with self.cond:
            self.events += 1
            old = self.items.get(item["uid"])
            if old and old.get("time", 0) > item["time"] + 1:
                return False                             # an older copy arriving late
            if old is None and len(self.items) >= MAX_ITEMS:
                self._prune_locked(now, force=True)
            item = dict(item, seq=self._bump(), received=now)
            self.items[item["uid"]] = item
            self.cond.notify_all()
        return True

    def _prune_locked(self, now: float, force: bool = False) -> int:
        gone = [u for u, it in self.items.items() if it["stale"] + self.fade_s < now]
        if force and not gone and self.items:
            gone = [min(self.items.values(), key=lambda it: it["stale"])["uid"]]
        for u in gone:
            del self.items[u]
            if len(self.removed) == self.removed.maxlen:
                self.oldest_removal = self.removed[0][0]
            self.removed.append((self._bump(), u))
        return len(gone)

    def remove(self, uids: List[str]) -> int:
        """A CoT delete (or an already-stale update): gone at once, no fade."""
        with self.cond:
            n = 0
            for u in uids:
                if self.items.pop(u, None) is not None:
                    if len(self.removed) == self.removed.maxlen:
                        self.oldest_removal = self.removed[0][0]
                    self.removed.append((self._bump(), u))
                    n += 1
            if n:
                self.cond.notify_all()
            return n

    def prune(self) -> int:
        with self.cond:
            n = self._prune_locked(self.clock())
            if n:
                self.cond.notify_all()
            return n

    def snapshot(self) -> dict:
        now = self.clock()
        with self.cond:
            self._prune_locked(now)
            items = sorted(self.items.values(), key=lambda it: (-it["received"], -it["seq"]))    # newest first
            return {"now": now, "seq": self.seq, "fade_s": self.fade_s, "items": [_public(it, now) for it in items]}

    def delta(self, since: int) -> Optional[dict]:
        """Changes after seq `since`: upserts and removals. None = too old, send a snapshot instead."""
        now = self.clock()
        with self.cond:
            self._prune_locked(now)
            if since < self.oldest_removal:
                return None
            up = [_public(it, now) for it in self.items.values() if it["seq"] > since]
            rm = [u for s, u in self.removed if s > since and u not in self.items]
            return {"now": now, "seq": self.seq, "upsert": up, "remove": rm}

    def wait(self, since: int, timeout: float) -> int:
        with self.cond:
            if self.seq <= since:
                self.cond.wait(timeout)
            return self.seq


def _public(it: dict, now: float) -> dict:
    out = {k: v for k, v in it.items() if k not in ("received",)}
    out["state"] = "live" if it["stale"] >= now else "fading"
    return out


# ── the TAK viewer connection ────────────────────────────────────────────────

def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def identity(host: str) -> Tuple[str, str]:
    h = re.sub(r"[^a-z0-9-]", "-", (host or "isr-pi").lower())[:40] or "isr-pi"
    return f"isrkit-launcher-map-{h}", f"COP-VIEW {h}"


def registration_sa(host: str, now: Optional[float] = None, version: str = "1") -> bytes:
    """C5.2 registration as a pure viewer: the 'unknown position' (0,0, hae/ce/le 9999999, stale = start),
    so OTS creates the EUD row and binds the queues, and no TAK client ever draws it."""
    now = now or time.time()
    uid, callsign = identity(host)
    t = _iso(now)
    return (f'<event version="2.0" uid="{uid}" type="a-f-G-E-S" how="m-g" time="{t}" start="{t}" stale="{t}">'
            '<point lat="0" lon="0" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
            f'<detail><contact callsign="{callsign}" endpoint="*:-1:stcp"/><__group name="Cyan" role="Team Member"/>'
            f'<takv platform="isr-kit" device="isr-launcher-map" os="linux" version="{version}"/></detail></event>').encode()


def split_events(buf: bytes) -> Tuple[List[bytes], bytes]:
    """Complete <event>…</event> chunks from a stream buffer, and the rest."""
    out = []
    while True:
        start = buf.find(b"<event")
        if start < 0:
            return out, (buf[-16:] if len(buf) > 16 else buf)       # keep a possible partial "<even"
        m = EVENT_END.search(buf, start)
        if not m:
            rest = buf[start:]
            return out, (rest if len(rest) <= MAX_BUFFER else b"")
        out.append(buf[start:m.end()])
        buf = buf[m.end():]


class CopClient:
    """C5.1: one long-lived connection, reconnect with backoff, register on every (re)connect."""

    def __init__(self, picture: Picture, host: str = "127.0.0.1", port: int = 8087, me: str = "isr-pi",
                 reg_wait: float = REG_WAIT_S, reg_every: float = REG_EVERY_S, connect=socket.create_connection):
        self.picture, self.host, self.port, self.me = picture, host, port, me
        self.reg_wait, self.reg_every, self.connect = reg_wait, reg_every, connect
        self.lock = threading.Lock()
        self.state = {"connected": False, "since": None, "error": "", "registered_at": None,
                      "uid": identity(me)[0], "callsign": identity(me)[1], "server": f"{host}:{port}"}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state, events=self.picture.events)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def run(self, stop: threading.Event):
        backoff = 2.0
        while not stop.is_set():
            try:
                self._session(stop)
                backoff = 2.0
            except OSError as e:
                self._set(connected=False, error=f"{type(e).__name__}: {e}"[:200])
            if not stop.is_set():
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _session(self, stop: threading.Event):
        sock = self.connect((self.host, self.port), timeout=10)
        try:
            sock.settimeout(1.0)
            self._set(connected=True, since=time.time(), error="")
            if stop.wait(self.reg_wait):                      # C5.2: wait, then register first
                return
            sock.sendall(registration_sa(self.me))
            last_reg = time.time()
            self._set(registered_at=last_reg)
            buf = b""
            while not stop.is_set():
                if time.time() - last_reg >= self.reg_every:
                    sock.sendall(registration_sa(self.me))
                    last_reg = time.time()
                    self._set(registered_at=last_reg)
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    self.picture.prune()
                    continue
                if not data:
                    raise ConnectionResetError("OTS closed the connection")
                evs, buf = split_events(buf + data)
                for ev in evs:
                    gone = delete_uids(ev)
                    if gone:
                        self.picture.remove(gone)
                    else:
                        self.picture.ingest(parse_event(ev))
        finally:
            self._set(connected=False)
            try:
                sock.close()
            except OSError:
                pass


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
