#!/usr/bin/env python3
"""Score a rehearsal run against contract v4 (C4.2 positions, C4.3 labels, C4.4 rates) from
<logs>/cot.jsonl and <logs>/mqtt.jsonl.
Usage: tools/rehearsal/measure.py [--logs DIR] [--since UNIX_TS]   (DIR defaults to ./logs)"""
import collections
import json
import re
import sys

since = float(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else 0.0
# Every rehearsal tool runs in simulate mode (fpv-vision's FPV_SOURCE=simulate counts as
# simulated, not recorded), so all expect SIMULATED:/[SIM]. Put a tool in REC when it runs a
# real recorded file via --replay / replay:PATH.
SIMLIKE = {"drone-detector", "acoustic-detector", "rf-spectrum", "op-sentry", "change-detect", "fpv-vision"}
REC = set()
PREFIX_TOOL = {"DRONE": "drone-detector", "ACOUSTIC": "acoustic-detector", "RFSPEC": "rf-spectrum",
               "OPSENTRY": "op-sentry", "CHANGE": "change-detect", "fpv": "fpv-vision", "FPV": "fpv-vision"}

LOGS = sys.argv[sys.argv.index("--logs") + 1] if "--logs" in sys.argv else "logs"
cot = [json.loads(l) for l in open(f"{LOGS}/cot.jsonl")]
cot = [e for e in cot if e["t"] >= since]
mq = [json.loads(l) for l in open(f"{LOGS}/mqtt.jsonl")]
mq = [m for m in mq if m["t"] >= since]
rows = []
fail = 0


def attr_of(xml, name):
    m = re.search(rf'\b{name}="([^"]*)"', xml)
    return m.group(1) if m else ""


def tool_of(uid):
    return PREFIX_TOOL.get(uid.split("-")[0])


def report(check, tool, ok, detail):
    global fail
    rows.append((check, tool, "PASS" if ok else "FAIL", detail))
    fail += not ok


# C4.3 CoT labels and C4.2 no 0,0
by_tool = collections.defaultdict(list)
for e in cot:
    t = tool_of(e["uid"])
    if t:
        by_tool[t].append(e)
for t, evs in sorted(by_tool.items()):
    want = "RECORDED: " if t in REC else "SIMULATED: "
    good = sum(e["remarks"].startswith(want) for e in evs)
    report("C4.3 CoT label", t, good == len(evs), f"{good}/{len(evs)} remarks start {want!r}")
    zero = sum(bool(re.search(r'<point lat="0(\.0+)?" lon="0(\.0+)?"', e["raw"])) for e in evs)
    report("C4.2 no 0,0", t, zero == 0, f"{zero} events at 0,0")
    # C4.4 CoT <= 1 per 5 s per uid
    per_uid = collections.defaultdict(list)
    for e in evs:
        per_uid[e["uid"]].append(e["t"])
    worst = 0
    for ts in per_uid.values():
        ts.sort()
        gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a < 4.5)
        worst = max(worst, gaps)
    report("C4.4 CoT rate", t, worst == 0, f"{len(evs)} events, {len(per_uid)} uids, worst uid has {worst} updates <5 s apart")

# C4.3 mesh labels and C4.4 mesh rate (alerts only; replies to direct queries have a 'to')
mesh = collections.defaultdict(list)
for m in mq:
    if m["topic"] == "meshbus/tx":
        p = json.loads(m["payload"])
        mesh[p.get("source", "?")].append((m["t"], p))
for t, msgs in sorted(mesh.items()):
    alerts = [(ts, p) for ts, p in msgs if not p.get("to")]
    if t in SIMLIKE | REC and alerts:
        want = "[REC] " if t in REC else "[SIM] "
        good = sum(p["text"].startswith(want) for _, p in alerts)
        report("C4.3 mesh label", t, good == len(alerts), f"{good}/{len(alerts)} texts start {want!r}")
    ts = sorted(x for x, _ in alerts)
    close = sum(1 for a, b in zip(ts, ts[1:]) if b - a < 29)
    if ts:
        report("C4.4 mesh rate", t, close == 0, f"{len(ts)} broadcast alerts, {close} less than 30 s after the previous one")

# ── contract v5 (needs the OTS-emulating relay's log fields) ──
REPOS = ["drone-detector", "acoustic-detector", "rf-spectrum", "op-sentry", "change-detect", "fpv-vision",
         "tac-advisor", "blufor-tracker", "mesh-deadrop", "photoAnalysis"]
if cot and "accepted" in cot[0]:
    def repo_of_uid(u):
        if not u or not u.startswith("isrkit-"):
            return None
        return next((r for r in sorted(REPOS, key=len, reverse=True) if u.startswith(f"isrkit-{r}-")), None)

    regs = [e for e in cot if e.get("registration")]
    by_repo = collections.defaultdict(list)
    for e in regs:
        by_repo[repo_of_uid(e["conn_uid"]) or e["conn_uid"]].append(e)
    for t in sorted(by_tool):          # every tool that sent markers must be registered
        if not any(repo_of_uid(e["conn_uid"]) == t for e in regs):
            report("C5.2 registered", t, False, "markers sent but no isrkit-<repo>-<host> registration SA")
    for t, rs in sorted(by_repo.items(), key=lambda kv: str(kv[0])):
        ok = [r for r in rs if r["registration"].startswith("registered")]
        good_uid = repo_of_uid(rs[0]["conn_uid"]) is not None
        plat = all(r.get("takv_platform") == "isr-kit" for r in rs)
        waited = all((r.get("reg_delay_s") or 0) >= 2.5 for r in rs)
        report("C5.2 registered", str(t), bool(ok) and good_uid and plat,
               f"{len(ok)}/{len(rs)} registrations ok, uid {rs[0]['conn_uid']!r}, platform {rs[0].get('takv_platform')!r}"
               + ("" if ok else f" ({rs[0]['registration']})"))
        report("C5.2 waited 3 s", str(t), waited, "delays " + ", ".join(f"{r.get('reg_delay_s')}s" for r in rs[:4]))
    # C5.2 / C4.2 exception: an SA at 0,0 is allowed only as the stale-on-arrival "unknown position" form
    pt = re.compile(r'<point lat="([^"]+)" lon="([^"]+)"[^>]*?ce="([^"]+)"')
    for e in cot:
        if not (e["uid"].startswith("isrkit-") and e["uid"] == e["conn_uid"]):
            continue
        m = pt.search(e["raw"])
        if not m or float(m.group(1)) != 0 or float(m.group(2)) != 0:
            continue
        start, stale = attr_of(e["raw"], "start"), attr_of(e["raw"], "stale")
        ok = start and start == stale and float(m.group(3)) >= 9999999
        if not ok:
            report("C5.2 unknown-pos SA", repo_of_uid(e["uid"]) or e["uid"], False,
                   f"SA at 0,0 must have stale==start and ce=9999999 (start={start}, stale={stale}, ce={m.group(3)})")
    # C5.3 nothing dropped; SA re-sent every ~60 s on a long-lived connection
    drops = collections.Counter(repo_of_uid(e["sender"]) or tool_of(e["uid"]) or e["sender"] for e in cot if not e["accepted"])
    for who, n in sorted(drops.items(), key=lambda kv: str(kv[0])):
        report("C5.3 not dropped", str(who), False, f"{n} events DROPPED (unregistered sender)")
    sa = collections.defaultdict(list)
    for e in cot:
        if e["uid"].startswith("isrkit-") and e["uid"] == e["conn_uid"]:
            sa[e["uid"]].append(e["t"])
    for u, ts in sorted(sa.items()):
        ts.sort()
        if ts[-1] - ts[0] >= 90:
            gap = max(b - a for a, b in zip(ts, ts[1:])) if len(ts) > 1 else 1e9
            report("C5.3 SA every 60 s", repo_of_uid(u) or u, gap <= 75, f"{len(ts)} SAs, max gap {gap:.0f}s")
    # C5.4 receivers are bound
    for r in ("tac-advisor",):
        rs = by_repo.get(r, [])
        if rs:
            report("C5.4 receiver bound", r, any(x["bound"] for x in rs), rs[-1].get("unbound_reason") or "bound")

w = max(len(r[1]) for r in rows) if rows else 10
for c, t, s, d in rows:
    print(f"[{s}] {c:16} {t:{w}}  {d}")
print(f"\n{len(rows) - fail} pass, {fail} fail")
sys.exit(1 if fail else 0)
