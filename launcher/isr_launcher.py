#!/usr/bin/env python3
"""ISTAR kit launcher: one phone-friendly page on :8050.

Shows the active pi-profiles profile, each service's state with a link to its UI, Pi
health (CPU temperature, load, throttling, disk), detected USB devices and internet
reachability. Switches profile with one tap via a root-owned helper that accepts only a
whitelisted profile name (see bin/isr-switch-helper and deploy/isr-launcher.sudoers).

Standard library only, so it runs offline without a venv (Python 3.9+).
"""
import hmac
import json
import os
import re
import shutil
import socket
import sys
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:  # works both as `python3 isr_launcher.py` and when imported by tests
    from mqtt_mini import MiniMQTT, MQTTError, topic_matches
    from qr import qr_svg
    import cop
    import demo
except ImportError:  # pragma: no cover
    from .mqtt_mini import MiniMQTT, MQTTError, topic_matches
    from .qr import qr_svg
    from . import cop, demo

ROOT = Path(os.environ.get("PI_PROFILES_ROOT", "/opt/pi-profiles"))
PROFILES_DIR = Path(os.environ.get("PI_PROFILES_DIR", str(ROOT / "profiles.d")))
SERVICES_CONF = Path(os.environ.get("PI_PROFILES_SERVICES", str(ROOT / "services.conf")))
ACTIVE_FILE = Path(os.environ.get("PI_PROFILES_ACTIVE", str(ROOT / "active")))
STATE_FILE = Path(os.environ.get("PI_PROFILES_STATE", str(ROOT / "last-switch")))
SWITCH_SH = os.environ.get("PI_PROFILES_SWITCH", str(ROOT / "switch.sh"))
HELPER = os.environ.get("LAUNCHER_HELPER", str(ROOT / "bin" / "isr-switch-helper"))
CLOCK_HELPER = os.environ.get("LAUNCHER_CLOCK_HELPER", str(ROOT / "bin" / "isr-set-clock"))
CLOCK_STATE = Path(os.environ.get("ISR_CLOCK_STATE", "/var/lib/isr-kit/clock.json"))
CLOCK_TRUST_S = 24 * 3600          # a station-set clock counts as good for a day (the Pi drifts slowly)
CLOCK_BROWSER_MIN_SKEW = 5
SWITCH_LOG = Path(os.environ.get("LAUNCHER_SWITCH_LOG", "/var/log/pi-profiles-switch.log"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The shared UI kit (pi-profiles/ui/, the source of truth for every tool's look): /ui/isr-ui.css|js
UI_DIR = Path(os.environ.get("ISR_UI_DIR", str(Path(__file__).resolve().parent.parent / "ui")))
UI_FILES = {"isr-ui.css": "text/css; charset=utf-8", "isr-ui.js": "application/javascript; charset=utf-8"}
UI_FONT_RE = re.compile(r"^(?:[a-z0-9-]+\.woff2|OFL-[A-Za-z]+\.txt)$")      # /ui/fonts/: the kit's fonts and licences
STATIC_FILES = {"app.js": "application/javascript; charset=utf-8", "board.js": "application/javascript; charset=utf-8",
                "common.js": "application/javascript; charset=utf-8", "launcher.css": "text/css; charset=utf-8",
                "map.js": "application/javascript; charset=utf-8", "mgrs.js": "application/javascript; charset=utf-8",
                "map.css": "text/css; charset=utf-8", "presenter.js": "application/javascript; charset=utf-8"}
# Third-party libraries for /map, vendored (no CDN: the kit is offline). Licences beside them.
VENDOR_DIR = STATIC_DIR / "vendor"
VENDOR_FILES = {"leaflet.js": "application/javascript; charset=utf-8", "leaflet.css": "text/css; charset=utf-8",
                "milsymbol.js": "application/javascript; charset=utf-8",
                "LICENSE-leaflet.txt": "text/plain; charset=utf-8", "LICENSE-milsymbol.txt": "text/plain; charset=utf-8"}
# Hotspot Wi-Fi QR on the mission board (PIN-gated; the password is a credential). Optional.
HOTSPOT_SSID = os.environ.get("LAUNCHER_HOTSPOT_SSID", "BFT-TAK").strip()
HOTSPOT_PSK = os.environ.get("LAUNCHER_HOTSPOT_PSK", "").strip()

PIN = os.environ.get("LAUNCHER_PIN", "").strip()
PIN_FOR_VIEW = os.environ.get("LAUNCHER_PIN_FOR_VIEW", "0") == "1"
PORT = int(os.environ.get("LAUNCHER_PORT", "8050"))
BIND = os.environ.get("LAUNCHER_BIND", "::")          # dual-stack; see bind_server()

# Heartbeats (contract: retained JSON on isrkit/heartbeat/<tool> every 10 s)
MQTT_HOST = os.environ.get("LAUNCHER_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("LAUNCHER_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("LAUNCHER_MQTT_USER", "guest")
MQTT_PASS = os.environ.get("LAUNCHER_MQTT_PASS", "guest")
HEARTBEAT_TOPIC = "isrkit/heartbeat/+"
# Station announces (retained): laptop/start-mac.sh publishes {"host": ISR_HOST, "station": ISR_STATION}
# so a Mac whose hostname doesn't say its role (a managed Mac) is still shown as "HQ Mac".
STATION_TOPIC = "isrkit/station/+"
HEARTBEAT_EVERY = 10
STALE_AFTER = 30
OFFLINE_AFTER = 120
HUB_NAME = os.environ.get("ISR_HUB_NAME", "isr-pi")
HUB_IP = os.environ.get("ISR_HUB_IP", "192.168.4.1")
# Ollama on the HQ Mac (bare base URL, e.g. http://isr-hq.local:11434)
LLM_URL = os.environ.get("LLM_URL", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
VLM_URL = os.environ.get("VLM_URL", "").strip()
VLM_MODEL = os.environ.get("VLM_MODEL", "").strip()
PROBE_EVERY = 15
# Tunnelled HQ (laptop/hq-tunnel.sh): the Mac's ssh -R forwards land on the Pi's 127.0.0.1:<port+10000>.
# While one is up, the launcher listens on <port> on all addresses and relays to it, so phones on the
# hotspot and a Mac on the cable can open the Mac tool's UI. Only for ports of class "mac", or of tools the active profile runs on a
# Mac (@hq/@op/@droneops/@mac), so it never takes a port a Pi tool needs.
TUNNEL_FORWARD = os.environ.get("LAUNCHER_TUNNEL_FORWARD", "1") == "1"
TUNNEL_BIND = os.environ.get("LAUNCHER_TUNNEL_BIND", "").strip() or None     # default: all addresses
TUNNEL_OFFSET = int(os.environ.get("ISR_TUNNEL_PORT_OFFSET", "10000"))          # must match hq-tunnel.sh
TUNNEL_TARGET = os.environ.get("LAUNCHER_TUNNEL_TARGET", "127.0.0.1")
TUNNEL_EVERY = 5
TUNNEL_MAX_CONNS = 64
MAPS_URL = os.environ.get("LAUNCHER_MAPS_URL", "http://127.0.0.1:8160").rstrip("/")
# Common Operating Picture (/map): a TAK viewer connection to OTS (cop.py). It only reads.
COP_ENABLED = os.environ.get("LAUNCHER_COP", "1") == "1"
COP_TAK_HOST = os.environ.get("LAUNCHER_TAK_HOST", "127.0.0.1")
COP_TAK_PORT = int(os.environ.get("LAUNCHER_TAK_PORT", "8087"))
COP_MAX_STREAMS = int(os.environ.get("LAUNCHER_COP_MAX_STREAMS", "12"))
SITE_ENV = Path(os.environ.get("ISR_SITE_ENV", "/etc/isr-kit/site.env"))     # SENSOR_LAT/LON (0644)
TILE_RE = re.compile(r"^/tiles/([a-z0-9][a-z0-9_-]{0,63})/(\d{1,2})/(\d{1,8})/(\d{1,8})\.(png|jpg|jpeg)$")
GEMINI_FLAG = Path(os.environ.get("LAUNCHER_GEMINI_FLAG", "/etc/isr-kit/gemini.configured"))
# blufor-tracker's demo-day self-check (TAK send+receive via OTS, MQTT login, heartbeats).
# It runs as the launcher user against localhost with the launcher's MQTT login, so no root.
CHECK_PY = os.environ.get("LAUNCHER_CHECK_PY", "/opt/blufor-tracker/.venv/bin/python")
CHECK_DIR = os.environ.get("LAUNCHER_CHECK_DIR", "/opt/blufor-tracker")
CHECK_MESH_TEXT = "radio check from HQ"
# Mesh channel URL for on-the-day node onboarding (contains the channel PSK: PIN-gated).
# First readable file wins: one the bridge may cache, then the one isr-mesh-channel writes.
CHANNEL_FILES = [Path(p) for p in os.environ.get(
    "LAUNCHER_CHANNEL_FILES", "/var/lib/blufor-tracker/channel-url:/etc/isr-kit/mesh-channel.url").split(":") if p]
CHANNEL_RE = re.compile(r"^https://meshtastic\.org/e/(\?add=true)?#[A-Za-z0-9_\-+/=]{4,1024}$")

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]+$")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "isr-launcher"

MAX_PIN_FAILS = 5
LOCKOUT_SECS = 60


# ── small helpers ────────────────────────────────────────────────────────────

def run(cmd: List[str], timeout: float = 5.0) -> Tuple[int, str]:
    """Run a command (list form, never a shell). Returns (rc, stdout). Missing binary -> 127."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


class TTLCache:
    def __init__(self):
        self._data: Dict[str, Tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key, ttl, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._data[key] = (now, val)
        return val

    def clear(self):
        with self._lock:
            self._data.clear()


cache = TTLCache()


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text()
    except OSError:
        return default


# ── catalogue and profiles ───────────────────────────────────────────────────

def load_catalog() -> List[dict]:
    out = []
    for line in read_text(SERVICES_CONF).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 4 or not UNIT_RE.match(parts[0]):
            continue
        port = int(parts[1]) if parts[1].isdigit() else None
        out.append({
            "unit": parts[0],
            "port": port,
            "class": parts[2],
            "repo": None if parts[3] == "-" else parts[3],
            "title": parts[4] if len(parts) > 4 else parts[0],
        })
    return out


def parse_profile(path: Path) -> dict:
    meta = {"name": path.stem, "title": "", "devices": [], "optional": [], "remote": [], "note": "",
            "hotspot": "on", "comment": "", "env": {}, "demo_mode": "continuous", "hidden": False}
    for line in read_text(path).splitlines():
        m = re.match(r"^#\s*@(\w+)(?:\s+(.*))?$", line.strip())
        if m:
            key, val = m.group(1), (m.group(2) or "").strip()
            if key in ("devices", "optional"):
                meta[key] = [d.strip() for d in val.split(",") if d.strip()]
            elif key == "remote":   # tool@station, e.g. fpv-vision@droneops
                for item in [d.strip() for d in val.split(",") if d.strip()]:
                    tool, _, station = item.partition("@")
                    if UNIT_RE.match(tool):
                        meta["remote"].append({"tool": tool, "station": station or "mac"})
            elif key in ("title", "note", "hotspot"):
                meta[key] = val
            elif key == "hidden":   # an include base (bft): never offered in the switcher
                meta["hidden"] = True
            elif key == "env":      # @env <unit> KEY=VALUE … (switch.sh applies them; shown here for the demo mode)
                unit, _, kvs = val.partition(" ")
                if UNIT_RE.match(unit):
                    for kv in kvs.split():
                        k, _, v = kv.partition("=")
                        if re.match(r"^[A-Z][A-Z0-9_]{0,63}$", k):
                            meta["env"].setdefault(unit, {})[k] = v[:120]
                            if k == "DEMO_MODE" and v == "on_demand":
                                meta["demo_mode"] = "on_demand"   # "Step by step": the presenter fires each event
        elif line.startswith("#") and not meta["comment"]:
            meta["comment"] = line.lstrip("# ").strip()
    if not meta["title"]:
        meta["title"] = meta["comment"] or meta["name"]
    return meta


def list_profiles() -> List[dict]:
    if not PROFILES_DIR.is_dir():
        return []
    return [parse_profile(p) for p in sorted(PROFILES_DIR.glob("*.conf")) if PROFILE_RE.match(p.stem)]


def profile_units(name: str) -> List[str]:
    """Ask switch.sh to resolve includes so the launcher never disagrees with it."""
    if not PROFILE_RE.match(name):
        return []
    rc, out = run([SWITCH_SH, "--units", name])
    if rc != 0:
        return []
    return [u for u in out.split() if UNIT_RE.match(u)]


def active_profile() -> str:
    name = read_text(ACTIVE_FILE).strip()
    return name if PROFILE_RE.match(name) else ""


def last_switch() -> dict:
    out = {}
    for line in read_text(STATE_FILE).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def switch_in_progress() -> bool:
    rc, out = run(["systemctl", "is-active", "pi-profiles-switch"])
    return out.strip() in ("active", "activating", "deactivating")


def switch_log_tail(lines: int = 25) -> List[str]:
    txt = read_text(SWITCH_LOG)
    return txt.splitlines()[-lines:] if txt else []


# ── systemd unit states ──────────────────────────────────────────────────────

def unit_states(units: List[str]) -> Dict[str, dict]:
    units = [u for u in units if UNIT_RE.match(u)]
    if not units:
        return {}
    rc, out = run(["systemctl", "show", "--property=Id,LoadState,ActiveState,SubState", "--"]
                  + [u if "." in u else u + ".service" for u in units])
    blocks = [b for b in out.strip().split("\n\n") if b.strip()] if out else []
    states = {}
    for unit, block in zip(units, blocks):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        load = kv.get("LoadState", "unknown")
        states[unit] = {
            "installed": load not in ("not-found", "unknown", ""),
            "active": kv.get("ActiveState", "unknown"),
            "sub": kv.get("SubState", ""),
        }
    for u in units:
        states.setdefault(u, {"installed": False, "active": "unknown", "sub": ""})
    return states


# ── Pi health ────────────────────────────────────────────────────────────────

THROTTLE_BITS = {
    0: "under-voltage now", 1: "ARM frequency capped now", 2: "throttled now",
    3: "soft temperature limit now", 16: "under-voltage has occurred",
    17: "frequency capping has occurred", 18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


def decode_throttled(value: Optional[int]) -> dict:
    if value is None:
        return {"raw": None, "flags": [], "ok": None}
    flags = [msg for bit, msg in THROTTLE_BITS.items() if value & (1 << bit)]
    return {"raw": hex(value), "flags": flags, "ok": (value & 0xF) == 0}


def cpu_temp_c() -> Optional[float]:
    raw = read_text(Path("/sys/class/thermal/thermal_zone0/temp")).strip()
    try:
        return round(int(raw) / 1000.0, 1)
    except ValueError:
        return None


def throttled() -> dict:
    rc, out = run(["vcgencmd", "get_throttled"], timeout=2)
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out or "")
    return decode_throttled(int(m.group(1), 16) if m else None)


POWER_DT = Path(os.environ.get("LAUNCHER_POWER_DT", "/proc/device-tree/chosen/power"))


def _dt_u32(path: Path) -> Optional[int]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return int.from_bytes(raw[:4], "big") if len(raw) >= 4 else None


def power_state() -> dict:
    """Pi 5 PSU negotiation. The firmware records the current it agreed with the supply in
    /proc/device-tree/chosen/power/max_current (mA). Below 5000 mA the Pi caps the USB ports at
    600 mA total, unless usb_max_current_enable=1 is set in config.txt."""
    max_ma = _dt_u32(POWER_DT / "max_current")
    override = _dt_u32(POWER_DT / "usb_max_current_enable")
    if override is None:
        rc, out = run(["vcgencmd", "get_config", "usb_max_current_enable"], timeout=2)
        m = re.search(r"usb_max_current_enable=(\d+)", out or "")
        override = int(m.group(1)) if m else None
    st = {"max_current_ma": max_ma, "usb_override": bool(override) if override is not None else None}
    if max_ma is None:
        st.update(level="unknown", usb_limited=None,
                  verdict="PSU current unknown (not a Pi 5, or old firmware: sudo rpi-eeprom-update -a)")
    elif max_ma >= 5000:
        st.update(level="ok", usb_limited=False, verdict="PSU negotiated 5 A: USB ports get full power (1.6 A)")
    elif override:
        st.update(level="warn", usb_limited=False,
                  verdict=f"PSU only {max_ma / 1000:.1f} A, but the USB limit is overridden in config.txt: brownout risk under load")
    else:
        st.update(level="bad", usb_limited=True,
                  verdict=f"USB CURRENT-LIMITED: PSU offers {max_ma / 1000:.1f} A (not 5 A), so the USB ports are capped at "
                          "600 mA total. The mic, capture card and radio may drop out. Use the official 27 W PSU.")
    return st


def disk_free(path: str = "/") -> dict:
    try:
        u = shutil.disk_usage(path)
        return {"path": path, "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
                "used_pct": round(100.0 * u.used / u.total, 1) if u.total else None}
    except OSError:
        return {"path": path, "free_gb": None, "total_gb": None, "used_pct": None}


def mem_info() -> dict:
    kv = {}
    for line in read_text(Path("/proc/meminfo")).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            kv[parts[0].rstrip(":")] = int(parts[1])
    if "MemTotal" not in kv:
        return {"total_mb": None, "available_mb": None}
    return {"total_mb": kv["MemTotal"] // 1024, "available_mb": kv.get("MemAvailable", 0) // 1024}


def health() -> dict:
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return {
        "cpu_temp_c": cpu_temp_c(),
        "load": load,
        "cpus": os.cpu_count(),
        "throttled": cache.get("throttled", 10, throttled),
        "power": cache.get("power", 30, power_state),
        "disk": disk_free("/"),
        "mem": mem_info(),
        "hostname": socket.gethostname(),
    }


# ── USB / device detection ───────────────────────────────────────────────────

# vid:pid (or vid:*) -> device key. Keys match the `# @devices` tokens in profiles.d.
USB_IDS = {
    "10c4:ea60": "heltec-v3",    # CP210x (Heltec V3)
    "1a86:55d4": "heltec-v3",    # CH9102
    "1a86:7523": "heltec-v3",    # CH340
    "303a:*": "heltec-v3",       # ESP32-S3 native USB
    "0bda:8812": "alfa-wifi",    # RTL8812AU (AWUS036ACH)
    "0bda:8813": "alfa-wifi",    # RTL8814AU (AWUS1900)
    "0e8d:7612": "alfa-wifi",    # MT7612U (AWUS036ACM)
    "0e8d:7961": "alfa-wifi",    # MT7921AU (AWUS036AXML)
    "148f:3070": "alfa-wifi",    # RT3070 (AWUS036NH)
    "148f:5370": "alfa-wifi",    # RT5370
    "0bda:2838": "rtl-sdr",      # RTL2838 dongle
    "0bda:2832": "rtl-sdr",
    "1d50:6089": "rtl-sdr",      # HackRF One
    "1d50:60a1": "rtl-sdr",      # Airspy
    "534d:2109": "capture-card", # MacroSilicon MS2109
    "345f:2130": "capture-card", # MS2130
    "2672:*": "gopro",           # GoPro (webcam / MTP)
    "2e1a:*": "360-camera",      # Insta360
    "05ca:*": "360-camera",      # Ricoh Theta
}

DEVICE_LABELS = {
    "heltec-v3": "Meshtastic radio (Heltec V3)",
    "alfa-wifi": "Alfa Wi-Fi adapter (monitor mode)",
    "usb-mic": "USB microphone",
    "rtl-sdr": "SDR receiver",
    "capture-card": "HDMI/AV capture card (FPV goggles)",
    "360-camera": "360 camera",
    "pi-camera": "Pi camera module",
    "gopro": "GoPro",
}


def parse_lsusb(text: str) -> List[dict]:
    out = []
    for line in text.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)", line.strip())
        if not m:
            continue
        vid, pid = m.group(3).lower(), m.group(4).lower()
        key = USB_IDS.get(f"{vid}:{pid}") or USB_IDS.get(f"{vid}:*")
        out.append({"id": f"{vid}:{pid}", "name": m.group(5).strip(), "kind": key})
    return out


def extra_wlan_ifaces() -> List[str]:
    try:
        return sorted(n for n in os.listdir("/sys/class/net") if n.startswith("wl") and n != "wlan0")
    except OSError:
        return []


def usb_audio_cards() -> List[str]:
    cards = []
    for line in read_text(Path("/proc/asound/cards")).splitlines():
        if "USB-Audio" in line or "USB Audio" in line:
            cards.append(line.split("-", 1)[-1].strip())
    return cards


def pi_cameras() -> int:
    rc, out = run(["rpicam-hello", "--list-cameras", "-n"], timeout=6)
    if rc == 127:
        rc, out = run(["libcamera-hello", "--list-cameras", "-n"], timeout=6)
    return len(re.findall(r"^\s*\d+\s*:", out or "", re.M))


def serial_ports() -> List[str]:
    dev = Path("/dev")
    try:
        return sorted(str(p) for p in dev.iterdir() if re.match(r"tty(USB|ACM)\d+$", p.name))
    except OSError:
        return []


def detect_devices() -> dict:
    rc, out = run(["lsusb"], timeout=3)
    usb = parse_lsusb(out) if rc == 0 else []
    present = {d["kind"] for d in usb if d["kind"]}
    wlans = extra_wlan_ifaces()
    if wlans:
        present.add("alfa-wifi")
    mics = usb_audio_cards()
    if mics:
        present.add("usb-mic")
    ncam = cache.get("picam", 60, pi_cameras)
    if ncam:
        present.add("pi-camera")
    return {
        "present": sorted(present),
        "labels": DEVICE_LABELS,
        "usb": usb,
        "serial": serial_ports(),
        "wlan_extra": wlans,
        "audio": mics,
        "pi_cameras": ncam,
        "lsusb_ok": rc == 0,
    }


# ── internet ─────────────────────────────────────────────────────────────────

def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def internet() -> dict:
    ip_ok = _tcp_ok("1.1.1.1", 443) or _tcp_ok("8.8.8.8", 53)
    gemini_ok = False
    if ip_ok:
        try:
            socket.setdefaulttimeout(2)
            socket.getaddrinfo("generativelanguage.googleapis.com", 443)
            gemini_ok = _tcp_ok("generativelanguage.googleapis.com", 443)
        except OSError:
            gemini_ok = False
        finally:
            socket.setdefaulttimeout(None)
    return {"online": ip_ok, "gemini_reachable": gemini_ok,
            "mode": "Gemini available" if gemini_ok else "Offline mode (local models / simulate)"}


# ── heartbeats (MQTT) ────────────────────────────────────────────────────────

# Station roles for hosts whose name doesn't say it (a managed Mac keeps its own hostname):
# LAUNCHER_STATIONS="corp-laptop-01=hq,another-mac=op". A heartbeat's own "station" field wins.
STATION_MAP = {k.strip().lower(): v.strip().lower() for k, _, v in
               (p.partition("=") for p in os.environ.get("LAUNCHER_STATIONS", "").split(",")) if k.strip() and v.strip()}
ROLES = ("pi", "hq", "op", "droneops")


ANNOUNCED: Dict[str, str] = {}        # host -> role, from isrkit/station/<host>
STATION_IPS: Dict[str, str] = {}      # host -> its hotspot address (192.168.4.x), from the same announce
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def hotspot_ip(ip: str) -> Optional[str]:
    """ip if it's a client address on the hub's /24 (the BFT-TAK hotspot), else None. The Pi only ever
    relays to these, never to an address a message names elsewhere."""
    try:
        parts = [int(x) for x in str(ip).split(".")]
        hub = [int(x) for x in HUB_IP.split(".")]
    except ValueError:
        return None
    if len(parts) != 4 or len(hub) != 4 or parts[:3] != hub[:3] or not 1 <= parts[3] <= 254 or parts == hub:
        return None
    return ".".join(map(str, parts))


def station_of(host: str, declared: str = "") -> str:
    """Label a heartbeat's host: Pi hub, HQ Mac, OP Mac, drone-ops Mac or the raw name. Order: the heartbeat's
    own station field, the Mac's station announce, LAUNCHER_STATIONS, then the host name."""
    d = (declared or "").strip().lower()
    if d in ROLES:
        return d
    h = (host or "").lower().split(".")[0]
    if ANNOUNCED.get(h) in ROLES:
        return ANNOUNCED[h]
    if STATION_MAP.get(h) in ROLES:
        return STATION_MAP[h]
    pi_names = {HUB_NAME.lower(), socket.gethostname().lower().split(".")[0], "localhost", "127", HUB_IP}
    if h in pi_names or (host or "") in (HUB_IP, "127.0.0.1"):
        return "pi"
    if "hq" in h:
        return "hq"
    if "drone" in h:
        return "droneops"
    if h in ("op", "isr-op") or h.endswith("-op") or "-op-" in h:
        return "op"
    if "ops" in h:
        return "droneops"
    return h or "?"


STATION_LABELS = {"pi": "Pi hub", "hq": "HQ Mac", "op": "OP Mac", "droneops": "Drone-ops Mac", "mac": "a Mac"}
MODES = {"cpu", "cuda", "mps", "gemini", "vlm", "llm", "simulate", "replay"}


def safe_url(url: str) -> str:
    try:
        u = urlparse(str(url))
    except ValueError:
        return ""
    return str(url) if u.scheme in ("http", "https") and u.netloc else ""


class HeartbeatStore:
    """Latest heartbeat per tool. `live_at` is set only for messages that arrive live (not the
    retained copy replayed on subscribe), so a retained heartbeat from a dead tool never looks
    fresh, and a laptop with a wrong clock can't fake freshness."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tools: Dict[str, dict] = {}
        self.connected = False
        self.error = ""
        self.station_subs: set = set()        # exact isrkit/station/<host> topics subscribed on this connection

    def station_topics(self) -> List[str]:
        """Exact isrkit/station/<host> topics to subscribe for heartbeat hosts we have no announce for.
        RabbitMQ's MQTT plugin replays retained messages only to non-wildcard subscriptions, so the
        isrkit/station/+ subscription alone misses every announce made before the launcher (re)started."""
        with self.lock:
            hosts = {e["host"].lower().split(".")[0] for e in self.tools.values() if e.get("host")}
            new = [h for h in sorted(hosts) if HOST_RE.match(h) and h not in ANNOUNCED
                   and f"isrkit/station/{h}" not in self.station_subs]
            topics = [f"isrkit/station/{h}" for h in new][:20]
            self.station_subs.update(topics)
            return topics

    def on_message(self, topic: str, payload: bytes, retained: bool, now: Optional[float] = None):
        if topic_matches(STATION_TOPIC, topic):
            try:
                d = json.loads(payload.decode("utf-8", "replace") or "{}") if payload else {}
            except ValueError:
                return
            host = str((d or {}).get("host") or topic.rsplit("/", 1)[-1]).lower().split(".")[0][:64]
            role = str((d or {}).get("station", "")).lower()
            ip = hotspot_ip((d or {}).get("ip", "")) if isinstance(d, dict) else None
            with self.lock:
                if role in ROLES and host:
                    ANNOUNCED[host] = role
                else:
                    ANNOUNCED.pop(host, None)
                if ip and host and role in ROLES:
                    STATION_IPS[host] = ip
                else:
                    STATION_IPS.pop(host, None)
            return
        if not topic_matches(HEARTBEAT_TOPIC, topic):
            return
        tool = topic.rsplit("/", 1)[-1]
        if not UNIT_RE.match(tool):
            return
        if not payload:   # retained message cleared: forget the tool
            with self.lock:
                self.tools.pop(tool, None)
            return
        try:
            data = json.loads(payload.decode("utf-8", "replace") or "{}")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        now = time.monotonic() if now is None else now
        with self.lock:
            prev = self.tools.get(tool, {})
            entry = {
                "tool": tool,
                "host": str(data.get("host", ""))[:64],
                "url": safe_url(data.get("url", "")),
                "demo_url": safe_url(data.get("demo_url", "")),      # contract v6: where its demo API is, if not url
                "mode": str(data.get("mode", ""))[:16],
                "status": str(data.get("status", ""))[:32],
                "detail": str(data.get("detail", ""))[:160],
                "ts": data.get("ts") if isinstance(data.get("ts"), (int, float)) else None,
                "live_at": prev.get("live_at") if retained else now,
            }
            entry["declared"] = str(data.get("station", ""))[:16]
            # contract v6: demo_mode / demo_active at the top level (op-sentry: its detail is a string),
            # else inside a detail object
            det = data.get("detail")
            inner = det if isinstance(det, dict) else {}
            if isinstance(det, dict):
                entry["detail"] = str(det.get("text") or det.get("message") or "")[:160]
            dm = data["demo_mode"] if "demo_mode" in data else inner.get("demo_mode")
            entry["demo_mode"] = dm if dm in demo.MODES else None
            da = data["demo_active"] if "demo_active" in data else inner.get("demo_active")
            entry["demo_active"] = [str(x)[:48] for x in da[:10] if demo.ID_RE.match(str(x))] if isinstance(da, list) else []
            self.tools[tool] = entry

    def snapshot(self, now: Optional[float] = None) -> Dict[str, dict]:
        now = time.monotonic() if now is None else now
        out = {}
        with self.lock:
            for tool, e in self.tools.items():
                e = dict(e)
                live = e.pop("live_at")
                e["station"] = station_of(e["host"], e.pop("declared", ""))    # announces may come later
                age = None if live is None else round(now - live, 1)
                if e["status"] == "stopped":
                    state = "stopped"
                elif age is None or age > OFFLINE_AFTER:
                    state = "offline"
                elif age > STALE_AFTER:
                    state = "stale"
                else:
                    state = "live"
                e.update(age_s=age, state=state, station_label=STATION_LABELS.get(e["station"], e["station"]))
                out[tool] = e
        return out


heartbeats = HeartbeatStore()


def own_heartbeat() -> bytes:
    return json.dumps({"tool": "isr-launcher", "host": socket.gethostname().split(".")[0],
                       "url": f"http://{HUB_NAME}.local:{PORT}/", "mode": "cpu", "status": "ok",
                       "ts": int(time.time())}).encode()


def heartbeat_thread(stop: threading.Event):
    while not stop.is_set():
        c = MiniMQTT(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, client_id=f"isr-launcher-{os.getpid()}")
        try:
            c.connect()
            c.subscribe(HEARTBEAT_TOPIC)
            c.subscribe(STATION_TOPIC)
            heartbeats.connected, heartbeats.error = True, ""
            heartbeats.station_subs = set()
            last = [0.0]

            def tick():
                if stop.is_set():
                    raise MQTTError("stopping")
                for t in heartbeats.station_topics():      # the retained announce of each Mac we hear from
                    c.subscribe(t)
                if time.monotonic() - last[0] >= HEARTBEAT_EVERY:
                    c.publish("isrkit/heartbeat/isr-launcher", own_heartbeat(), retain=True)
                    last[0] = time.monotonic()

            c.loop(heartbeats.on_message, tick)
        except (OSError, MQTTError) as e:
            heartbeats.connected, heartbeats.error = False, str(e)
        finally:
            c.close()
        stop.wait(5)


# ── background probes (internet, Ollama) ─────────────────────────────────────

def probe_ollama(base: str, want_model: str = "") -> dict:
    """Probe an Ollama base URL: reachable, model list, whether the configured model is there."""
    if not base:
        return {"configured": False}
    url = base.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    out = {"configured": True, "url": base, "reachable": False, "models": [], "model": want_model,
           "model_present": None}
    if not safe_url(url):
        out["error"] = "not an http(s) URL"
        return out
    try:
        with urlopen(Request(url + "/api/tags", headers={"Accept": "application/json"}), timeout=2.5) as r:
            data = json.loads(r.read(200000) or b"{}")
        out["reachable"] = True
        out["models"] = sorted(m.get("name", "") for m in data.get("models", []) if isinstance(m, dict))[:30]
    except Exception as e:  # noqa: BLE001 - any failure means "not reachable"
        try:  # maybe an OpenAI-compatible server that isn't Ollama
            with urlopen(url + "/v1/models", timeout=2.5) as r:
                data = json.loads(r.read(200000) or b"{}")
            out["reachable"] = True
            out["models"] = sorted(m.get("id", "") for m in data.get("data", []) if isinstance(m, dict))[:30]
        except Exception:  # noqa: BLE001
            out["error"] = type(e).__name__
            return out
    if want_model:
        names = set(out["models"]) | {m.split(":")[0] for m in out["models"]}
        out["model_present"] = want_model in names or want_model + ":latest" in names
    return out


def probe_maps(base: str = None) -> dict:
    """Map packs from the local isr-maps server (offline ATAK basemaps)."""
    base = base or MAPS_URL
    try:
        with urlopen(base + "/api/packs", timeout=2.5) as r:
            data = json.loads(r.read(500000) or b"{}")
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "packs": [], "error": type(e).__name__}
    packs = []
    for p in data.get("packs", [])[:50]:
        if not isinstance(p, dict) or not PROFILE_RE.match(str(p.get("name", "")).replace("_", "-")):
            continue
        packs.append({
            "name": str(p["name"])[:64], "title": str(p.get("title", p["name"]))[:120],
            "minzoom": p.get("minzoom"), "maxzoom": p.get("maxzoom"),
            "format": p.get("format") if p.get("format") in ("png", "jpg", "jpeg") else "png",
            "bounds": [round(float(v), 4) for v in (p.get("bounds") or [])][:4],
            "attribution": str(p.get("attribution", ""))[:300], "size_mb": p.get("size_mb"),
            "tiles": p.get("tiles"), "atak_xml": safe_url(p.get("atak_xml", "")),
            "download": safe_url(p.get("download", "")),
        })
    return {"reachable": True, "packs": packs}


class Probes:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"internet": {"online": None, "gemini_reachable": None, "mode": "checking…"},
                     "llm": {"configured": bool(LLM_URL)}, "vlm": {"configured": bool(VLM_URL)}}

    def refresh(self):
        res = {"internet": internet(), "llm": probe_ollama(LLM_URL, LLM_MODEL), "maps": probe_maps()}
        res["vlm"] = res["llm"] if VLM_URL and VLM_URL == LLM_URL and not VLM_MODEL else probe_ollama(VLM_URL, VLM_MODEL)
        with self.lock:
            self.data = res

    def get(self) -> dict:
        with self.lock:
            return dict(self.data)


probes = Probes()


def probe_thread(stop: threading.Event):
    while not stop.is_set():
        try:
            probes.refresh()
        except Exception as e:  # noqa: BLE001 - never let a probe kill the thread
            print(f"probe error: {e}", flush=True)
        stop.wait(PROBE_EVERY)


def ai_ladder(p: dict, gemini_key: Optional[bool] = None) -> str:
    """Which AI tier is usable now: Mac VLM -> Gemini -> offline/template.
    Gemini counts only if isr-set-gemini-key has run (its non-secret marker file) AND the API is
    reachable; the launcher can't read the keys themselves."""
    v = p.get("vlm") or {}
    if v.get("reachable") and v.get("model_present") is not False:
        return "Mac VLM"
    if gemini_key is None:
        gemini_key = GEMINI_FLAG.exists()
    if gemini_key and (p.get("internet") or {}).get("gemini_reachable"):
        return "Gemini"
    return "offline/template"


# ── tunnelled HQ: hub IP -> 127.0.0.1 relay for Mac tool ports ───────────────

def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tunnel_candidates(catalog: List[dict], active_meta: Optional[dict]) -> Dict[int, str]:
    by_unit = {c["unit"]: c for c in catalog}
    want: Dict[int, str] = {}
    for c in catalog:
        if c["class"] == "mac" and isinstance(c.get("port"), int):
            want[c["port"]] = c["unit"]
    for r in (active_meta or {}).get("remote", []):
        c = by_unit.get(r["tool"])
        if c and isinstance(c.get("port"), int) and c["class"] not in ("always", "core"):
            want.setdefault(c["port"], c["unit"])
    return want


def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class TunnelForwarder:
    """Relay a tunnelled Mac tool port to every address on the Pi.

    hq-tunnel.sh forwards tool port P to the Pi's 127.0.0.1:(P + TUNNEL_OFFSET), so this relay can
    listen on all addresses ([::] dual-stack, else 0.0.0.0) on P itself: phones on the hotspot use
    192.168.4.1:P and a Mac on the cable uses isr-pi.local:P. An older hq-tunnel.sh that still forwards
    to 127.0.0.1:P is relayed from the hub IP only (all-address binds would clash with its listener).
    """

    def __init__(self, bind: Optional[str] = None, target: str = TUNNEL_TARGET, offset: Optional[int] = None):
        self.bind, self.target = bind, target
        self.offset = TUNNEL_OFFSET if offset is None else offset
        # port -> (listener, unit, (target host, target port), bind address, "tunnel" | "relay"). A hotspot
        # Mac's port relays straight to it ("relay").
        self.listeners: Dict[int, Tuple[socket.socket, str, Tuple[str, int], str, str]] = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(TUNNEL_MAX_CONNS)
        self.error = ""

    def active(self) -> Dict[int, str]:
        with self.lock:
            return {p: v[1] for p, v in self.listeners.items()}

    def local_url(self, port: int) -> Optional[str]:
        """How the Pi itself reaches a relayed port: loopback for an all-address relay, else its address."""
        with self.lock:
            v = self.listeners.get(port)
        if not v:
            return None
        addr = v[3]
        host = "127.0.0.1" if addr in ("::", "0.0.0.0", "") else (f"[{addr}]" if ":" in addr else addr)
        return f"http://{host}:{port}"

    def via(self) -> Dict[int, str]:
        """port -> "tunnel" (the HQ's ssh forward) or "relay" (straight to a Mac on the hotspot)."""
        with self.lock:
            return {p: v[4] for p, v in self.listeners.items()}

    def _mode(self, port: int) -> Optional[Tuple[Tuple[str, int], str]]:
        """((target host, target port), bind address) for the HQ tunnel now up for this port, else None."""
        if self.offset and port_open(self.target, port + self.offset):
            return (self.target, port + self.offset), self.bind or "::"
        legacy_bind = self.bind or HUB_IP
        with self.lock:
            own = self.listeners.get(port)
        # An older hq-tunnel.sh listens on 127.0.0.1:<port> itself. Our own all-address listener answers
        # there too, so it never counts as that.
        if legacy_bind != self.target and not (own and own[3] != legacy_bind) and port_open(self.target, port):
            return (self.target, port), legacy_bind
        return None

    def sync(self, wanted: Dict[int, str], direct: Optional[Dict[int, Tuple[str, int]]] = None):
        """direct: ports run by a live hotspot station -> (its address, port). The station relay alone owns
        such a port: no tunnel probe, no switching modes, and while the target stays the same it isn't
        re-probed (one slow answer mustn't drop the page)."""
        direct = direct or {}
        for port, unit in wanted.items():
            with self.lock:
                have = self.listeners.get(port)
            d = direct.get(port)
            kind = "relay" if d else "tunnel"
            if d:
                if have and have[2] == d and have[4] == "relay":
                    continue
                mode = (d, self.bind or "::") if port_open(*d) else None
            else:
                mode = self._mode(port)
            if have and (not mode or have[2] != mode[0] or have[4] != kind):
                self._close(port)
                have = None
            if mode and not have:
                self._listen(port, unit, *mode, kind=kind)
        for port in [p for p in self.active() if p not in wanted]:
            self._close(port)

    def _socket(self, addr: str, port: int) -> socket.socket:
        fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
        ls = socket.socket(fam, socket.SOCK_STREAM)
        try:
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if fam == socket.AF_INET6 and addr == "::":
                ls.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)     # IPv4 too
            ls.bind((addr, port))
            ls.listen(16)
        except OSError:
            ls.close()
            raise
        ls.settimeout(1.0)
        return ls

    def _listen(self, port: int, unit: str, target: Tuple[str, int], addr: str, kind: str = "tunnel"):
        try:
            try:
                ls = self._socket(addr, port)
            except OSError:
                if addr != "::":
                    raise
                addr = "0.0.0.0"                  # IPv6 disabled on this Pi
                ls = self._socket(addr, port)
        except OSError as e:          # hotspot not up yet, or the port is taken: try again next round
            self.error = f"{addr}:{port}: {e}"
            return
        with self.lock:
            self.listeners[port] = (ls, unit, target, addr, kind)
        self.error = ""
        threading.Thread(target=self._accept, args=(port, ls, target), daemon=True, name=f"tunnel-{port}").start()
        print(f"{'station' if kind == 'relay' else 'tunnel'} relay [{addr}]:{port} -> {target[0]}:{target[1]} ({unit})", flush=True)

    def _close(self, port: int):
        with self.lock:
            ls, unit, _t, _a, kind = self.listeners.pop(port, (None, "", ("", 0), "", "tunnel"))
        if ls:
            ls.close()
            why = "tunnel down" if kind == "tunnel" else "station gone"
            print(f"tunnel relay for {unit} on :{port} closed ({why})", flush=True)

    def _accept(self, port: int, ls: socket.socket, target: Tuple[str, int]):
        while True:
            with self.lock:
                if self.listeners.get(port, (None,))[0] is not ls:
                    return
            try:
                c, _ = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                c.close()
                continue
            threading.Thread(target=self._relay, args=(c, target), daemon=True).start()

    def _relay(self, c: socket.socket, target: Tuple[str, int]):
        try:
            u = socket.create_connection(target, timeout=5)
        except OSError:
            c.close()
            self.slots.release()
            return
        c.settimeout(None); u.settimeout(None)
        t = threading.Thread(target=_pipe, args=(u, c), daemon=True)
        t.start()
        _pipe(c, u)
        t.join()
        c.close(); u.close()
        self.slots.release()

    def stop(self):
        for p in list(self.active()):
            self._close(p)


tunnels = TunnelForwarder(bind=TUNNEL_BIND)


def tunnel_thread(stop: threading.Event):
    while not stop.is_set():
        try:
            catalog = load_catalog()
            act = active_profile()
            meta = next((p for p in list_profiles() if p["name"] == act), None) if act else None
            wanted = relay_wanted(catalog, meta, profile_units(act) if act else [])
            tunnels.sync(wanted, direct_targets(catalog, wanted, heartbeats.snapshot()))
        except Exception as e:          # never take the launcher down over this
            tunnels.error = str(e)
        stop.wait(TUNNEL_EVERY)


def relay_wanted(catalog: List[dict], meta: Optional[dict], pi_units: List[str]) -> Dict[int, str]:
    """Ports the Pi may relay (tunnel_candidates), minus any port a unit the profile runs on the Pi
    itself uses (split OP: op-sentry-stream owns 8120 there)."""
    pi_ports = {c.get("port") for c in catalog if c["unit"] in pi_units}
    return {p: u for p, u in tunnel_candidates(catalog, meta).items() if p not in pi_ports}


def direct_targets(catalog: List[dict], wanted: Dict[int, str], hb: Dict[str, dict]) -> Dict[int, Tuple[str, int]]:
    """port -> (hotspot address, port) of the Mac whose live heartbeat runs that tool (its station announce
    gave the address; the heartbeat URL's port wins over the catalogue's)."""
    by_unit = {c["unit"]: c for c in catalog}
    out = {}
    for tool, e in hb.items():
        if e.get("state") not in ("live", "stale"):
            continue
        ip = STATION_IPS.get((e.get("host") or "").lower().split(".")[0])
        port = (by_unit.get(tool) or {}).get("port")
        try:
            mac_port = urlparse(e.get("url") or "").port or port
        except ValueError:
            mac_port = port
        if ip and isinstance(port, int) and port in wanted and isinstance(mac_port, int):
            out[port] = (ip, mac_port)
    return out


def direct_link(c: dict, beat: Optional[dict]) -> Optional[dict]:
    """The tool's page by its Mac's hotspot address, when the Pi can't relay it (phones may not resolve
    .local names)."""
    ip = beat and STATION_IPS.get((beat.get("host") or "").lower().split(".")[0])
    if not ip:
        return None
    port = c.get("port")
    try:
        port = urlparse(beat.get("url") or "").port or port
    except ValueError:
        pass
    return {"url": f"http://{ip}:{port}/"} if isinstance(port, int) else None


def tunnel_link(tool: str, c: dict, beat: Optional[dict], active: Dict[int, str]) -> Optional[dict]:
    """The hub URL for a Mac tool reachable through the tunnel relay: by unit, or by its heartbeat port."""
    port = None
    if isinstance(c.get("port"), int) and c["port"] in active:
        port = c["port"]
    elif beat and beat.get("url"):
        try:
            p = urlparse(beat["url"]).port
        except ValueError:
            p = None
        if p in active:
            port = p
    if port is None:
        return None
    return {"port": port, "url": f"http://{HUB_IP}:{port}/", "via": tunnels.via().get(port, "tunnel")}


# ── clock (offline Pi: no NTP, no RTC battery) ───────────────────────────────

def ntp_synced() -> Optional[bool]:
    # chrony serving its own clock to the hotspot (local stratum) isn't a real source (see isr-set-clock).
    rc, out = run(["chronyc", "-n", "tracking"], timeout=5)
    if rc == 0 and ("7F7F0101" in out or "(LOCL)" in out):
        return False
    rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5)
    return (out.strip() == "yes") if rc == 0 else None


def clock_status() -> dict:
    ntp = cache.get("ntp", 10, ntp_synced)
    try:
        last = json.loads(CLOCK_STATE.read_text())
        if not isinstance(last, dict):
            last = None
    except (OSError, ValueError):
        last = None
    now = time.time()
    recent = bool(last and isinstance(last.get("ts"), (int, float)) and 0 <= now - last["ts"] < CLOCK_TRUST_S)
    trusted = bool(ntp) or recent
    return {"now": round(now, 3), "ntp_synced": ntp, "last_set": last, "trusted": trusted,
            "warning": None if trusted else "CLOCK NOT SYNCED: no NTP, and not set or verified by a station in the last 24 h",
            "browser_min_skew": CLOCK_BROWSER_MIN_SKEW}


_clock_lock = threading.Lock()


def set_clock(epoch, ip: str) -> Tuple[int, dict]:
    try:
        e = float(epoch)
    except (TypeError, ValueError):
        return 400, {"error": "epoch must be unix seconds"}
    if not 1704067200 < e < 4102444800:
        return 400, {"error": "implausible time"}
    if abs(e - time.time()) <= CLOCK_BROWSER_MIN_SKEW:
        return 200, {"ok": True, "changed": False, "message": f"clock within {CLOCK_BROWSER_MIN_SKEW}s: not changed"}
    if not _clock_lock.acquire(blocking=False):
        return 409, {"error": "already setting the clock"}
    try:
        src = "browser " + re.sub(r"[^0-9a-fA-F.:]", "", ip)[:40]
        rc, out = run(["sudo", "-n", CLOCK_HELPER, "--source", src, "--min-skew", str(CLOCK_BROWSER_MIN_SKEW),
                       f"{e:.3f}"], timeout=15)
        msg = (out or "").strip().splitlines()[-1:] or [""]
        if rc != 0:
            return 500, {"error": msg[0] or f"isr-set-clock exited {rc}"}
        return 200, {"ok": True, "changed": "clock set" in msg[0], "message": msg[0]}
    finally:
        _clock_lock.release()


# ── TAK clients (for the mission board) ─────────────────────────────────────

def tak_clients(port: int = 8087) -> Optional[dict]:
    """Established TCP connections to OTS's CoT port from other machines (phones, Macs). None if
    `ss` isn't available. Loopback connections (the Pi's own tools) aren't counted."""
    rc, out = run(["ss", "-Htn", "state", "established", f"( sport = :{port} )"], timeout=5)
    if rc != 0:
        return None
    peers = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        peer = parts[-1].rsplit(":", 1)[0].strip("[]")
        if peer.startswith("::ffff:"):
            peer = peer[7:]
        if peer and not peer.startswith("127.") and peer != "::1":
            peers.add(peer)
    return {"clients": len(peers)}


def _wifi_escape(s: str) -> str:
    return re.sub(r'([\\;,:"])', r"\\\1", s)


def qr_for(what: str) -> Optional[dict]:
    """QR codes the board can show without a PIN: this launcher, or a map pack's ATAK XML."""
    if what == "launcher":
        url = f"http://{HUB_IP}:{PORT}/"
        return {"label": "Open the launcher", "text": url, "svg": qr_svg(url)}
    m = re.match(r"^map:([a-z0-9][a-z0-9_-]{0,63})$", what)
    if m:
        for p in probes.get().get("maps", {}).get("packs", []):
            if p.get("name") == m.group(1) and p.get("atak_xml"):
                return {"label": f"Load map {p.get('title') or p['name']} in ATAK", "text": p["atak_xml"],
                        "svg": qr_svg(p["atak_xml"])}
    return None


# ── status aggregate ─────────────────────────────────────────────────────────

# ── common operating picture (/map) ─────────────────────────────────────────

picture = cop.Picture()
cop_client = cop.CopClient(picture, COP_TAK_HOST, COP_TAK_PORT, me=HUB_NAME)
cop_streams = threading.BoundedSemaphore(COP_MAX_STREAMS)


def site_position() -> Optional[dict]:
    """The sensor site (isr-set-site): SENSOR_LAT/SENSOR_LON from site.env, or None."""
    kv = {}
    for line in read_text(SITE_ENV).splitlines():
        k, _, v = line.partition("=")
        kv[k.strip()] = v.strip().strip('"').strip("'")
    try:
        lat, lon = float(kv.get("SENSOR_LAT", "")), float(kv.get("SENSOR_LON", ""))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return {"lat": lat, "lon": lon}


def cop_meta() -> dict:
    """What the map page needs besides the picture: the TAK link, the site and the offline map packs."""
    maps = probes.get().get("maps") or {}
    packs = [{k: p.get(k) for k in ("name", "title", "minzoom", "maxzoom", "bounds", "attribution", "format")}
             for p in maps.get("packs", [])]
    return {"tak": dict(cop_client.status(), enabled=COP_ENABLED), "site": site_position(),
            "maps": {"reachable": bool(maps.get("reachable")), "packs": packs}, "hub": HUB_NAME}


def cop_snapshot() -> dict:
    return dict(picture.snapshot(), **cop_meta())


def proxy_tile(path: str) -> Tuple[int, bytes, str, dict]:
    """/tiles/<pack>/<z>/<x>/<y>.<ext> from isr-maps, so the map works from whatever host the page was
    opened with (a phone on 192.168.4.1, a Mac on isr-pi.local, the tunnel)."""
    try:
        with urlopen(Request(MAPS_URL + path, headers={"User-Agent": "isr-launcher"}), timeout=5) as r:
            data = r.read(4 * 1024 * 1024)
            ctype = r.headers.get("Content-Type", "image/png")
            return 200, data, ctype, {"Cache-Control": r.headers.get("Cache-Control", "public, max-age=86400")}
    except urllib.error.HTTPError as e:
        return e.code, b"", "image/png", {"Cache-Control": "public, max-age=600"}
    except Exception:  # noqa: BLE001 - isr-maps down: an empty 502, the map shows its background
        return 502, b"", "image/png", {"Cache-Control": "no-store"}


def cop_thread(stop: threading.Event):
    cop_client.run(stop)


# ── presenter panel (contract v6 demo triggers) ─────────────────────────────

def hub_hosts() -> set:
    """The names and address the Pi hub goes by (links naming one of these are rewritten for the viewer)."""
    return {HUB_IP, f"{HUB_NAME}.local".lower(), HUB_NAME.lower(), "isr-pi.local", "127.0.0.1", "localhost"}

def demo_tools() -> List[dict]:
    """The active profile's tools that have demo scenarios, with how the Pi reaches each one's API."""
    catalog = load_catalog()
    by_unit = {c["unit"]: c for c in catalog}
    act = active_profile()
    units = profile_units(act) if act else []
    meta = next((p for p in list_profiles() if p["name"] == act), None) if act else None
    hb = heartbeats.snapshot()
    known = {u for _, us in demo.GROUPS for u in us}
    out = []
    for u in units:
        if u not in known:
            continue
        c = by_unit.get(u, {})
        e = hb.get(u) or (hb.get(c["repo"]) if c.get("repo") else None)
        port = c.get("port")
        # The heartbeat's demo_url wins (blufor-tracker: its url is the OTS UI, its demo server is :8170),
        # when it names the Pi; else the url's port for a unit with none in services.conf.
        for key in ("demo_url",) + (("url",) if not port else ()):
            try:
                p = urlparse((e or {}).get(key) or "")
                if (p.hostname or "").lower() in hub_hosts() and p.port:
                    port = p.port
                    break
            except ValueError:
                pass
        out.append({"unit": u, "where": "Pi", "base": f"http://127.0.0.1:{port}" if port else None,
                    "why": "" if port else "no web port known for its demo API", "mode": (e or {}).get("demo_mode")})
    for r in (meta or {}).get("remote", []):
        u = r["tool"]
        if u not in known or u in units:
            continue
        c = by_unit.get(u, {})
        e = hb.get(u)
        port = c.get("port")
        base = tunnels.local_url(port) if port else None
        if not base and e and port:
            ip = STATION_IPS.get((e.get("host") or "").lower().split(".")[0])
            base = f"http://{ip}:{port}" if ip else None
        live = bool(e) and e.get("state") in ("live", "stale")
        why = "" if base else ("not running (no heartbeat)" if not live else "no route from the Pi (no relay or hotspot address)")
        out.append({"unit": u, "where": (e and STATION_LABELS.get(e.get("station"))) or STATION_LABELS.get(r["station"], r["station"]),
                    "base": base, "why": why, "mode": (e or {}).get("demo_mode")})
    return out


after_switch = demo.AfterSwitch()


def demo_mode_now() -> Tuple[str, str]:
    act = active_profile()
    meta = next((p for p in list_profiles() if p["name"] == act), None) if act else None
    return act, (meta or {}).get("demo_mode", "continuous")


def drop_sim_before(t: float) -> int:
    """The launcher's own picture: simulated items received before t go at once (ATAK keeps its copies
    until the tools expire them or they go stale)."""
    with picture.cond:
        uids = [u for u, it in picture.items.items() if it.get("mode") == "sim" and it.get("received", 0) < t]
    return picture.remove(uids)


def after_switch_tick(now: Optional[float] = None) -> List[dict]:
    """Every few seconds: notice a switch into step by step, then clear the tools as they come back."""
    now = time.time() if now is None else now
    if switch_in_progress():
        return []
    act, mode = demo_mode_now()
    if after_switch.observe(act, mode, now):
        n = drop_sim_before(now)
        print(f"demo: switched to {act} (step by step): dropped {n} simulated item(s) from the map;"
              f" clearing the tools' demo events as they restart", flush=True)
    res = after_switch.sweep(demo_tools(), now)
    for r in res:
        print(f"demo: {r['unit']}: " + (f"cleared {r.get('cleared') or 0}" if r["ok"] else f"clear failed ({r['error']})"), flush=True)
    return res


def demo_thread(stop: threading.Event):
    while not stop.is_set():
        try:
            after_switch_tick()
        except Exception as e:  # noqa: BLE001 - never let it kill the thread
            print(f"demo error: {e}", flush=True)
        stop.wait(5)


def demo_panel() -> dict:
    act = active_profile()
    meta = next((p for p in list_profiles() if p["name"] == act), None) if act else None
    return {"profile": act, "title": (meta or {}).get("title", act), "mode": (meta or {}).get("demo_mode", "continuous"),
            "tools": demo.scenarios(demo_tools())}


def build_status() -> dict:
    catalog = load_catalog()
    by_unit = {c["unit"]: c for c in catalog}
    profiles = list_profiles()
    active = active_profile()
    active_units = profile_units(active) if active else []
    always = [c["unit"] for c in catalog if c["class"] == "always"]
    # Mac-only tools (class "mac") never run on the Pi: they appear under Mac stations only.
    shown = list(dict.fromkeys(active_units + always + [c["unit"] for c in catalog if c["class"] != "mac"]))
    states = unit_states(shown)
    devices = cache.get("devices", 5, detect_devices)
    present = set(devices["present"])
    hb = heartbeats.snapshot()
    pr = probes.get()

    services = []
    matched = set()
    for u in shown:
        c = by_unit.get(u, {"unit": u, "port": None, "class": "unknown", "repo": None, "title": u})
        s = states.get(u, {})
        # A unit's heartbeat is published under its own name, or under the repo name when the
        # repo's unit is named differently (blufor-tracker -> mesh-bridge).
        beat = hb.get(u) or (hb.get(c["repo"]) if c.get("repo") and c["repo"] not in by_unit else None)
        if beat:
            matched.add(beat["tool"])
        services.append(dict(c, in_profile=u in active_units or u in always, heartbeat=beat, **s))

    for p in profiles:
        p["missing_devices"] = [d for d in p["devices"] if d not in present]
        p["missing_optional"] = [d for d in p["optional"] if d not in present]
        p["active"] = p["name"] == active

    # Tools the active profile expects on a Mac, plus any other off-Pi heartbeat.
    active_meta = next((p for p in profiles if p["active"]), None)
    relayed = tunnels.active()
    remote = []
    seen = set()
    for r in (active_meta or {}).get("remote", []):
        e = hb.get(r["tool"])
        c = by_unit.get(r["tool"], {})
        remote.append({"tool": r["tool"], "title": c.get("title", r["tool"]), "expected_station": r["station"],
                       "expected_label": STATION_LABELS.get(r["station"], r["station"]), "heartbeat": e,
                       "port": c.get("port"), "direct": direct_link(c, e),
                       "tunnel": tunnel_link(r["tool"], c, e, relayed)})
        seen.add(r["tool"])
    for tool, e in sorted(hb.items()):
        if tool not in seen and (e["station"] != "pi" or tool not in matched):
            c = by_unit.get(tool, {})
            remote.append({"tool": tool, "title": c.get("title", tool), "expected_station": None, "port": c.get("port"),
                           "expected_label": None, "heartbeat": e, "direct": direct_link(c, e),
                           "tunnel": tunnel_link(tool, c, e, relayed)})

    return {
        "active": active,
        "switching": switch_in_progress(),
        "last_switch": last_switch(),
        "switch_log": switch_log_tail(),
        "profiles": profiles,
        "services": services,
        "health": health(),
        "devices": devices,
        "internet": pr.get("internet", {}),
        "llm": pr.get("llm", {}),
        "vlm": pr.get("vlm", {}),
        "ai_tier": ai_ladder(pr),
        "maps": pr.get("maps", {"reachable": False, "packs": []}),
        "cop": dict(cop_client.status(), enabled=COP_ENABLED, items=len(picture.items)),
        "demo_mode": (active_meta or {}).get("demo_mode", "continuous"),
        "remote": remote,
        "mqtt": {"connected": heartbeats.connected, "error": heartbeats.error},
        "clock": clock_status(),
        "tak": cache.get("tak", 10, tak_clients),
        "hotspot_qr": bool(HOTSPOT_PSK),
        "hub_hosts": sorted(hub_hosts()),
        "tunnel": {"relayed": {str(p): u for p, u in relayed.items()}, "error": tunnels.error},
        "pin_required": bool(PIN),
        "channel_available": mesh_channel() is not None,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ── mesh channel (onboarding QR) ─────────────────────────────────────────────

def mesh_channel() -> Optional[dict]:
    for f in CHANNEL_FILES:
        try:
            txt = f.read_text()
            mtime = f.stat().st_mtime
        except OSError:
            continue
        url = next((l.strip() for l in txt.splitlines() if l.strip().startswith("https://")), "")
        if CHANNEL_RE.match(url):
            return {"url": url, "source": str(f), "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))}
    return None


# ── self-check (blufor-tracker bridge.check) ─────────────────────────────────

_check_lock = threading.Lock()
_push_lock = threading.Lock()


def _push_module():
    """maps/isr_push_maps.py sits beside launcher/ in the repo and in /opt/pi-profiles."""
    d = str(Path(__file__).resolve().parent.parent / "maps")
    if d not in sys.path:
        sys.path.insert(0, d)
    import isr_push_maps
    return isr_push_maps


def push_maps(pack: str, with_mbtiles: bool) -> Tuple[int, dict]:
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", pack):
        return 400, {"error": "bad pack name"}
    if not _push_lock.acquire(blocking=False):
        return 409, {"error": "a map push is already running"}
    try:
        M = _push_module()
        log: List[str] = []
        try:
            r = M.push(pack, with_mbtiles=with_mbtiles, out=lambda m: log.append(str(m)[:300]))
        except M.PushError as e:
            return 502, {"error": str(e), "log": log}
        return 200, dict(r, log=log)
    finally:
        _push_lock.release()


def run_self_check(mesh: bool) -> Tuple[int, dict]:
    if not os.path.exists(CHECK_PY):
        return 404, {"error": "blufor-tracker not installed (no bridge.check)"}
    if not _check_lock.acquire(blocking=False):
        return 409, {"error": "a check is already running"}
    try:
        env = {"PATH": "/usr/bin:/bin", "PYTHONUNBUFFERED": "1", "TAK_HOST": "127.0.0.1", "TAK_PORT": "8087",
               "MQTT_HOST": MQTT_HOST, "MQTT_PORT": str(MQTT_PORT), "MQTT_USER": MQTT_USER, "MQTT_PASS": MQTT_PASS}
        cmd = [CHECK_PY, "-m", "bridge.check"] + (["--mesh-text", CHECK_MESH_TEXT] if mesh else [])
        try:
            p = subprocess.run(cmd, cwd=CHECK_DIR, env=env, capture_output=True, text=True, timeout=60)
            rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = 124, "[FAIL] self-check timed out after 60 s"
        except OSError as e:
            rc, out = 127, f"[FAIL] could not run bridge.check: {e}"
        lines = []
        for line in out.splitlines()[-60:]:
            m = re.match(r"^\[(PASS|FAIL|WARN|INFO|OK)\s*\]\s*(.*)$", line)
            lines.append({"level": m.group(1) if m else "", "text": (m.group(2) if m else line)[:300]})
        return 200, {"ok": rc == 0, "rc": rc, "mesh": mesh, "lines": lines, "ran_at": time.strftime("%H:%M:%S")}
    finally:
        _check_lock.release()


# ── switching ────────────────────────────────────────────────────────────────

class PinGuard:
    def __init__(self):
        self.fails: Dict[str, Tuple[int, float]] = {}
        self.lock = threading.Lock()

    def locked_out(self, ip: str) -> int:
        with self.lock:
            n, since = self.fails.get(ip, (0, 0.0))
            if n >= MAX_PIN_FAILS:
                left = int(LOCKOUT_SECS - (time.monotonic() - since))
                if left > 0:
                    return left
                self.fails.pop(ip, None)
        return 0

    def check(self, ip: str, supplied: str) -> bool:
        if not PIN:
            return True
        ok = hmac.compare_digest(supplied.encode(), PIN.encode())
        with self.lock:
            if ok:
                self.fails.pop(ip, None)
            else:
                n, _ = self.fails.get(ip, (0, 0.0))
                self.fails[ip] = (n + 1, time.monotonic())
        return ok


pin_guard = PinGuard()


def request_switch(name: str) -> Tuple[int, dict]:
    if not PROFILE_RE.match(name or ""):
        return 400, {"error": "invalid profile name"}
    if name not in {p["name"] for p in list_profiles()}:
        return 404, {"error": "unknown profile"}
    if switch_in_progress():
        return 409, {"error": "a switch is already in progress"}
    rc, out = run(["sudo", "-n", HELPER, name], timeout=15)
    cache.clear()
    if rc == 0:
        return 202, {"ok": True, "profile": name}
    msgs = {64: "usage", 65: "invalid profile name", 66: "unknown profile",
            67: "profile rejected (more than one camera-heavy tool?)", 127: "helper not installed"}
    return 500, {"error": msgs.get(rc, f"helper failed (rc={rc})")}


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "isr-launcher"
    sys_version = ""

    def log_message(self, fmt, *args):  # quieter journal: only non-GET or errors
        if self.command != "GET" or (args and str(args[1])[:1] in "45"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        return ip[7:] if ip.startswith("::ffff:") else ip        # IPv4 via the dual-stack socket

    def _view_allowed(self) -> bool:
        if not (PIN and PIN_FOR_VIEW):
            return True
        return pin_guard.check(self._client_ip(), self.headers.get("X-PIN", ""))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):            # mission board (projector view)
            body = read_text(STATIC_DIR / "board.html", "<h1>launcher UI missing</h1>").encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if path in ("/engineer", "/engineer.html"):  # everything else: health, profiles, checks, maps …
            body = read_text(STATIC_DIR / "index.html", "<h1>launcher UI missing</h1>").encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if path in ("/presenter", "/presenter.html"):   # step-by-step demo: one button per scenario
            body = read_text(STATIC_DIR / "presenter.html", "<h1>presenter UI missing</h1>").encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/demo/scenarios":
            if not self._view_allowed():
                return self._json(401, {"error": "PIN required", "pin_required": True})
            return self._json(200, demo_panel())
        if path in ("/map", "/map.html"):            # common operating picture
            body = read_text(STATIC_DIR / "map.html", "<h1>map UI missing</h1>").encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if TILE_RE.match(path):
            code, data, ctype, extra = proxy_tile(path)
            return self._send(code, data, ctype, extra.get("Cache-Control", "no-store"))
        name = path.rsplit("/", 1)[-1]
        if path == f"/ui/fonts/{name}" and UI_FONT_RE.match(name):
            try:
                return self._send(200, (UI_DIR / "fonts" / name).read_bytes(),
                                  "font/woff2" if name.endswith(".woff2") else "text/plain; charset=utf-8")
            except OSError:
                return self._json(404, {"error": "not found"})
        if path == f"/ui/{name}" and name in UI_FILES:
            try:
                return self._send(200, (UI_DIR / name).read_bytes(), UI_FILES[name])
            except OSError:
                return self._json(404, {"error": "UI kit not installed"})
        if path in (f"/{name}", f"/static/{name}") and name in STATIC_FILES:
            return self._send(200, read_text(STATIC_DIR / name).encode(), STATIC_FILES[name])
        if path == f"/vendor/{name}" and name in VENDOR_FILES:
            try:   # versioned by ?v= in the page, so the browser may keep them
                return self._send(200, (VENDOR_DIR / name).read_bytes(), VENDOR_FILES[name], "public, max-age=86400")
            except OSError:
                return self._json(404, {"error": "not found"})
        if path in ("/api/cop", "/api/cop/stream"):
            if not self._view_allowed():
                return self._json(401, {"error": "PIN required", "pin_required": True})
            if path == "/api/cop":
                return self._json(200, cop_snapshot())
            return self._cop_stream()
        if path == "/api/qr":
            if not self._view_allowed():
                return self._json(401, {"error": "PIN required", "pin_required": True})
            what = urllib.parse.parse_qs(urlparse(self.path).query).get("for", [""])[0]
            q = qr_for(what)
            return self._json(200, q) if q else self._json(404, {"error": "no such QR"})
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/status":
            if not self._view_allowed():
                return self._json(401, {"error": "PIN required", "pin_required": True})
            return self._json(200, build_status())
        self._json(404, {"error": "not found"})

    def _cop_stream(self):
        """Server-sent events: one "snapshot", then "delta" {upsert, remove} on every change and a "status"
        every 5 s. Capped at COP_MAX_STREAMS; past that a 503 and the page polls /api/cop instead."""
        if not cop_streams.acquire(blocking=False):
            return self._json(503, {"error": "too many map streams; poll /api/cop"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            snap = cop_snapshot()
            self.wfile.write(b"retry: 3000\n" + cop.sse("snapshot", snap))
            self.wfile.flush()
            last, last_status = snap["seq"], time.time()
            while True:
                picture.wait(last, 5.0)
                d = picture.delta(last)
                if d is None:
                    snap = cop_snapshot()
                    self.wfile.write(cop.sse("snapshot", snap))
                    last = snap["seq"]
                elif d["upsert"] or d["remove"]:
                    self.wfile.write(cop.sse("delta", d))
                    last = d["seq"]
                if time.time() - last_status >= 5:
                    self.wfile.write(cop.sse("status", {"now": time.time(), "tak": cop_client.status()}))
                    last_status = time.time()
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            cop_streams.release()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/switch", "/api/channel", "/api/check", "/api/push-maps", "/api/clock", "/api/hotspot-qr",
                        "/api/demo/trigger", "/api/demo/clear"):
            return self._json(404, {"error": "not found"})
        # Custom header = browsers will not send this cross-site without CORS preflight.
        if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return self._json(403, {"error": "missing request header"})
        ip = self._client_ip()
        left = pin_guard.locked_out(ip)
        if left:
            return self._json(429, {"error": f"too many wrong PINs, wait {left}s"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1024:
            return self._json(400, {"error": "bad request body"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "bad JSON"})
        if path == "/api/channel" and not PIN:
            # The channel URL carries the PSK: never show it on an un-PINned launcher.
            return self._json(403, {"error": "set LAUNCHER_PIN in /etc/isr-launcher.env to show the channel QR"})
        if not pin_guard.check(ip, str(self.headers.get("X-PIN", ""))):
            return self._json(401, {"error": "wrong PIN", "pin_required": True})
        if path == "/api/check":
            code, obj = run_self_check(bool(body.get("mesh")))
            return self._json(code, obj)
        if path in ("/api/demo/trigger", "/api/demo/clear"):
            tools = {t["unit"]: t for t in demo_tools()}
            if path == "/api/demo/clear" and body.get("all"):        # only tools whose demo API answers
                return self._json(200, {"results": demo.clear_all(list(tools.values()))})
            t = tools.get(str(body.get("tool", "")))
            if not t:
                return self._json(404, {"error": "that tool isn't in the active profile"})
            if path == "/api/demo/clear":
                r = demo.clear([t])[0]
                return self._json(200 if r["ok"] else 502, {"results": [r]})
            code, obj = demo.trigger(t, body)
            if code == 200:
                after_switch.note_trigger(t["unit"], time.time())
            return self._json(code, obj)
        if path == "/api/hotspot-qr":
            if not HOTSPOT_PSK:
                return self._json(404, {"error": "set LAUNCHER_HOTSPOT_PSK in /etc/isr-launcher.env to show the Wi-Fi QR"})
            text = f"WIFI:T:WPA;S:{_wifi_escape(HOTSPOT_SSID)};P:{_wifi_escape(HOTSPOT_PSK)};;"
            return self._json(200, {"label": f"Join Wi-Fi {HOTSPOT_SSID}", "ssid": HOTSPOT_SSID, "svg": qr_svg(text)})
        if path == "/api/clock":
            code, obj = set_clock(body.get("epoch"), ip)
            return self._json(code, obj)
        if path == "/api/push-maps":
            code, obj = push_maps(str(body.get("pack", "")), bool(body.get("with_mbtiles")))
            return self._json(code, obj)
        if path == "/api/channel":
            ch = mesh_channel()
            if not ch:
                return self._json(404, {"error": "no channel cached: on the Pi run sudo isr-mesh-channel --pause-bridge"})
            return self._json(200, dict(ch, svg=qr_svg(ch["url"])))
        code, obj = request_switch(str(body.get("profile", "")))
        self._json(code, obj)


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


def make_server(bind: str = BIND, port: int = PORT) -> ThreadingHTTPServer:
    return bind_server(bind, port, Handler)


def main():
    stop = threading.Event()
    threading.Thread(target=heartbeat_thread, args=(stop,), daemon=True, name="heartbeats").start()
    threading.Thread(target=probe_thread, args=(stop,), daemon=True, name="probes").start()
    threading.Thread(target=demo_thread, args=(stop,), daemon=True, name="demo").start()
    if TUNNEL_FORWARD:
        threading.Thread(target=tunnel_thread, args=(stop,), daemon=True, name="tunnels").start()
    if COP_ENABLED:
        threading.Thread(target=cop_thread, args=(stop,), daemon=True, name="cop").start()
    srv = make_server()
    where = "[::] (IPv6 + IPv4)" if srv.address_family == socket.AF_INET6 and srv.server_address[0] == "::" else srv.server_address[0]
    print(f"isr-launcher listening on {where}:{PORT} (PIN {'on' if PIN else 'off'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
