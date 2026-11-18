#!/usr/bin/env python3
"""Simulated TAK traffic for rehearsing the launcher's /map (COP) without the tools running: a drone with
course and speed, OP sentry detections with bearing lines (a new alert uid every 10 s, as op-sentry used
to, and a track that ends with a CoT delete after 60 s), an RF hit, a BFT patrol, a geo-locate estimate with
its uncertainty circle, a civil aircraft, and an acoustic contact that stops reporting after 20 s. Everything is labelled SIMULATED
(contract C4: SIM- uids/callsigns, "SIMULATED: " remarks), and placed relative to --site.

    cop_traffic.py --site LAT,LON [--host 127.0.0.1] [--port 8087] [--every 2] [--duration 0]

Registers first like every kit sender (contract v5 C5.2: wait 3 s, then the SA, uid
isrkit-cop-traffic-<host>). Use a placeholder site for rehearsals and screenshots, never a real OP.
Stdlib only.
"""
import argparse
import math
import socket
import sys
import time
from datetime import datetime, timezone

R = 6371008.8


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def offset(lat, lon, brg, m):
    d, b, p1, l1 = m / R, math.radians(brg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def ev(uid, typ, lat, lon, now, stale_s, callsign="", remarks="", how="m-g", ce=15, extra="", stale_at=None):
    st = iso(stale_at if stale_at is not None else now + stale_s)
    det = (f'<contact callsign="{callsign}"/>' if callsign else "") + extra + (f"<remarks>{remarks}</remarks>" if remarks else "")
    return (f'<event version="2.0" uid="{uid}" type="{typ}" how="{how}" time="{iso(now)}" start="{iso(now)}" stale="{st}">'
            f'<point lat="{lat:.7f}" lon="{lon:.7f}" hae="9999999.0" ce="{ce}" le="9999999.0"/><detail>{det}</detail></event>').encode()


def registration(host, lat, lon, now):
    return (f'<event version="2.0" uid="isrkit-cop-traffic-{host}" type="a-f-G-E-S" how="m-g" time="{iso(now)}" '
            f'start="{iso(now)}" stale="{iso(now + 120)}"><point lat="{lat:.7f}" lon="{lon:.7f}" hae="9999999.0" '
            f'ce="9999999.0" le="9999999.0"/><detail><contact callsign="SIM-COP-TRAFFIC {host}" endpoint="*:-1:stcp"/>'
            f'<__group name="Cyan" role="Team Member"/><takv platform="isr-kit" device="cop-traffic" os="macos" '
            f'version="1"/></detail></event>').encode()


def picture(lat, lon, t, host):
    """Every event for time t (seconds since start)."""
    now = time.time()
    out = []
    par = f'<link uid="isrkit-cop-traffic-{host}" relation="p-p" type="a-f-G-E-S"/>'
    # sensors registered at the site (as the real tools do)
    for uid, cs in (("drone-detector", "DRONE-DET"), ("op-sentry", "OP-SENTRY"), ("rf-spectrum", "RF-SPEC"),
                    ("acoustic-detector", "ACOUSTIC")):
        out.append(ev(f"isrkit-{uid}-sim", "a-f-G-E-S", lat, lon, now, 120, f"SIM {cs} isr-pi"))
    # a hostile quad orbiting 600 m out, with course and speed
    a = (t * 2.0) % 360
    dlat, dlon = offset(lat, lon, a, 600)
    course = (a + 90) % 360
    out.append(ev("DRONE-SIM-60601A2B3C4D", "a-h-A-M-F-Q", dlat, dlon, now, 300, "SIM DJI Mavic 3",
                  f"SIMULATED: drone-detector: DRONE_CONFIRMED, Remote ID, RSSI -61 dBm, course {course:03.0f} 21.0 m/s",
                  ce=15, extra=f'<track course="{course:.1f}" speed="21.0"/>{par}'))
    # a probable drone at the sensor (no position of its own)
    out.append(ev("DRONE-SIM-OPERATOR-PROBABLE", "a-u-A-M-F-Q", lat, lon, now, 300, "SIM UAV probable",
                  "SIMULATED: drone-detector: DRONE_PROBABLE, Wi-Fi only, RSSI -78 dBm. Marker is at the sensor.", ce=400, extra=par))
    # OP sentry: two people on bearing 045 and a vehicle on 300, recorded 360 clip
    alerts = [(45, "a-u-G-U-C-I", "PERSON", 2)]
    if t < 60:
        alerts.append((300, "a-u-G-E-V", "VEHICLE", 1))
    else:   # the vehicle's track ended: op-sentry deletes its marker and line (ATAK's t-x-d-d)
        for u in ("OPSENTRY-SIM-OP1-det-1", "OPSENTRY-SIM-OP1-brg-1"):
            out.append((f'<event version="2.0" uid="{u}-del" type="t-x-d-d" how="h-g-i-g-o" time="{iso(now)}" start="{iso(now)}" '
                        f'stale="{iso(now + 20)}"><point lat="0" lon="0" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
                        f'<detail><link uid="{u}" relation="none" type="none"/><__forcedelete/></detail></event>').encode())
    for n, (brg, typ, cls, cnt) in enumerate(alerts):
        out.append(ev(f"OPSENTRY-SIM-OP1-det-{n}", typ, lat, lon, now, 900, f"SIM OP1 {cnt}x{cls} BRG {brg:03d}",
                      f"RECORDED: {cnt}x {cls} detected by OP1 at bearing {brg:03d} deg (true), est. range unknown. "
                      "Marker is placed at the OP; the contact is somewhere along the bearing line.", how="m-r", ce=9999999.0, extra=par))
        e_lat, e_lon = offset(lat, lon, brg, 400)
        out.append((f'<event version="2.0" uid="OPSENTRY-SIM-OP1-brg-{n}" type="u-d-f" how="h-e" time="{iso(now)}" '
                    f'start="{iso(now)}" stale="{iso(now + 900)}"><point lat="{lat:.7f}" lon="{lon:.7f}" hae="9999999.0" '
                    f'ce="9999999.0" le="9999999.0"/><detail><link point="{lat:.7f},{lon:.7f}"/><link point="{e_lat:.7f},{e_lon:.7f}"/>'
                    f'<strokeColor value="-65536"/><strokeWeight value="3.0"/><contact callsign="SIM OP1 BRG {brg:03d} {cls} (range unknown)"/>'
                    f'<remarks>RECORDED: Bearing only. Line length is a display setting, not a range estimate.</remarks>'
                    f'<labels_on value="true"/>{par}</detail></event>').encode())
    # a fresh alert uid every 10 s on a drifting bearing (op-sentry's old behaviour): the map keeps the newest 6, 3 min
    for k in range(max(0, int(t // 10) - 7), int(t // 10) + 1):
        brg = (80 + 9 * k) % 360
        e_lat, e_lon = offset(lat, lon, brg, 400)
        out.append(ev(f"OPSENTRY-SIM-OP1-det-a{k}", "a-u-G-U-C-I", lat, lon, now - (t - 10 * k), 900, f"SIM OP1 1xPERSON BRG {brg:03d}",
                      f"SIMULATED: 1x PERSON detected by OP1 at bearing {brg:03d} deg (true), est. range unknown. "
                      "Marker is placed at the OP; the contact is somewhere along the bearing line.", how="m-r", ce=9999999.0, extra=par))
        out.append((f'<event version="2.0" uid="OPSENTRY-SIM-OP1-brg-a{k}" type="u-d-f" how="h-e" time="{iso(now - (t - 10 * k))}" '
                    f'start="{iso(now)}" stale="{iso(now + 900)}"><point lat="{lat:.7f}" lon="{lon:.7f}" hae="9999999.0" '
                    f'ce="9999999.0" le="9999999.0"/><detail><link point="{lat:.7f},{lon:.7f}"/><link point="{e_lat:.7f},{e_lon:.7f}"/>'
                    f'<contact callsign="SIM OP1 BRG {brg:03d} PERSON (range unknown)"/><remarks>SIMULATED: Bearing only.</remarks>'
                    f'{par}</detail></event>').encode())
    # RF energy at the sensor
    out.append(ev("RFSPEC-DRONE", "a-u-A", lat, lon, now, 300, "SIM RF 2.4 GHz drone link",
                  "SIMULATED: RF: 2.4 GHz frequency-hopping burst, bearing unknown. Energy detection only; marker is at the sensor, not the drone.",
                  ce=2000, extra=par))
    # BFT patrol of three walking north-east
    for k in range(3):
        p_lat, p_lon = offset(lat, lon, 200 + 12 * k, 450 - 40 * k)
        p_lat, p_lon = offset(p_lat, p_lon, 40, (t * 1.3) % 300)
        out.append(ev(f"SIM-BFT-PATROL{k + 1}", "a-f-G-U-C", p_lat, p_lon, now, 120, f"SIM PATROL {k + 1}",
                      "SIMULATED: simulated BFT position", ce=10, extra='<track course="40.0" speed="1.3"/><__group name="Cyan" role="Team Member"/>'))
    # geo-locate estimate with its uncertainty circle
    g_lat, g_lon = offset(lat, lon, 130, 1100)
    out.append(ev("GEO-SIM-0a1b2c3d4e", "a-u-G", g_lat, g_lon, now, 21600, "SIM GEO estimate",
                  "SIMULATED: ESTIMATE (medium): farm buildings by a river bend | uncertainty circle ~0.3 km radius.", how="h-e", ce=300))
    out.append((f'<event version="2.0" uid="GEO-SIM-0a1b2c3d4e-C" type="u-d-c-c" how="h-e" time="{iso(now)}" start="{iso(now)}" '
                f'stale="{iso(now + 21600)}"><point lat="{g_lat:.7f}" lon="{g_lon:.7f}" hae="9999999.0" ce="300" le="9999999.0"/>'
                f'<detail><shape><ellipse major="300" minor="300" angle="360"/></shape><contact callsign="SIM GEO estimate (circle)"/>'
                f'<remarks>SIMULATED: uncertainty circle</remarks></detail></event>').encode())
    # a civil aircraft passing, and a contact that went stale 40 s ago
    c_lat, c_lon = offset(lat, lon, 250, 1400 - (t * 60) % 2800)
    out.append(ev("fpv-vision-sim-air-7", "a-n-A-C", c_lat, c_lon, now, 300, "SIM Civil aircraft",
                  "SIMULATED: Air watch: civil aircraft, no range or bearing.", ce=1000, extra='<track course="070.0" speed="60.0"/>'))
    if t < 20:     # then it stops reporting: stale after 30 s, fades, goes
        s_lat, s_lon = offset(lat, lon, 330, 800)
        out.append(ev("ACOUSTIC-SIM-UAV", "a-u-A-M-F-Q", s_lat, s_lon, now, 30, "SIM Acoustic UAV",
                      "SIMULATED: acoustic: rotor tones, within ~150 m, bearing unknown.", how="m-f", ce=150))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", required=True, help="LAT,LON of the (placeholder) sensor site")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=0, help="seconds, 0 = until Ctrl-C")
    ap.add_argument("--me", default=socket.gethostname().split(".")[0].lower())
    a = ap.parse_args(argv)
    lat, lon = (float(v) for v in a.site.split(","))
    s = socket.create_connection((a.host, a.port), timeout=10)
    time.sleep(3.0)                                    # C5.2: register after OTS's channel has opened
    s.sendall(registration(a.me, lat, lon, time.time()))
    time.sleep(1.0)
    t0 = last_reg = time.time()
    try:
        while True:
            t = time.time() - t0
            for e in picture(lat, lon, t, a.me):
                s.sendall(e)
            if time.time() - last_reg > 60:
                s.sendall(registration(a.me, lat, lon, time.time()))
                last_reg = time.time()
            if a.duration and t >= a.duration:
                break
            time.sleep(a.every)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
