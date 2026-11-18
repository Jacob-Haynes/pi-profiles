"""Presenter panel support (contract v6, demo triggers): collect every tool's demo scenarios, fire one,
clear one tool or all of them. Each tool serves GET /api/demo/scenarios, POST /api/demo/trigger and
POST /api/demo/clear on its own port; the launcher reaches Mac tools through the Pi's relays. Stdlib only.
"""
import json
import re
import threading
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Run-of-show order on the Presenter panel: (group, units).
GROUPS = [
    ("BFT", ["mesh-bridge"]),
    ("OP", ["op-sentry"]),
    ("Counter-drone", ["drone-detector", "acoustic-detector", "rf-spectrum"]),
    ("FPV", ["fpv-vision"]),
    ("Analysis", ["change-detect", "photoAnalysis", "tac-advisor"]),
]
TITLES = {"mesh-bridge": "Blue force tracker", "op-sentry": "OP sentry", "drone-detector": "Drone detector",
          "acoustic-detector": "Acoustic", "rf-spectrum": "RF spectrum", "fpv-vision": "FPV vision",
          "change-detect": "Change detect", "photoAnalysis": "Geo-locate", "tac-advisor": "AI advisor"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
MODES = ("continuous", "on_demand")
TIMEOUT = 2.5
MAX_BODY = 64 * 1024


def _call(url: str, body: Optional[dict] = None, timeout: float = TIMEOUT) -> Tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method="GET" if body is None else "POST",
                  headers={"Content-Type": "application/json", "User-Agent": "isr-launcher"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(MAX_BODY) or b"{}")
    except HTTPError as e:
        try:
            msg = json.loads(e.read(4096) or b"{}")
        except ValueError:
            msg = {}
        detail = msg.get("detail") or msg.get("error") if isinstance(msg, dict) else ""
        return e.code, {"error": str(detail or f"HTTP {e.code}")[:200]}
    except (OSError, ValueError) as e:      # unreachable, timeout, not JSON
        return 0, {"error": f"{type(e).__name__}: {e}"[:200]}


OPT_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def clean_options(opts) -> dict:
    """A scenario's options, e.g. op-sentry's {"bearing": {"type": "degrees", "default": 45},
    "clip": {"choices": [{"clip": "08", "label": "V-Bat"}], "default": "08"}} -> {name: {type, default,
    choices: [{value, label}]}}. Types: degrees, number, text, choice (when there are choices)."""
    out = {}
    if not isinstance(opts, dict):
        return out
    for name, o in list(opts.items())[:6]:
        if not OPT_RE.match(str(name)) or not isinstance(o, dict):
            continue
        choices = []
        for c in (o.get("choices") or [])[:20]:
            if isinstance(c, dict):
                v = c.get("value", c.get(name, c.get("id")))
                lbl = c.get("label", v)
            else:
                v, lbl = c, c
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and len(str(v)) <= 80:
                choices.append({"value": v, "label": str(lbl)[:60]})
        t = "choice" if choices else (o.get("type") if o.get("type") in ("degrees", "number", "text") else "text")
        dflt = o.get("default")
        if not isinstance(dflt, (str, int, float)) or isinstance(dflt, bool) or len(str(dflt)) > 80:
            dflt = choices[0]["value"] if choices else None
        out[str(name)] = {"type": t, "default": dflt, "label": str(o.get("label") or name.replace("_", " "))[:30],
                          "choices": choices}
    return out


def clean_scenarios(d: dict) -> dict:
    """A tool's /api/demo/scenarios answer, checked and trimmed (it comes from another process)."""
    out = []
    for s in (d.get("scenarios") or [])[:20] if isinstance(d, dict) else []:
        if not isinstance(s, dict) or not ID_RE.match(str(s.get("id", ""))):
            continue
        dur = s.get("duration_s")
        out.append({"id": str(s["id"]), "label": str(s.get("label") or s["id"])[:60], "hint": str(s.get("hint") or "")[:200],
                    "duration_s": dur if isinstance(dur, (int, float)) and 0 < dur < 86400 else None,
                    "options": clean_options(s.get("options"))})
    active = [str(a) for a in (d.get("active") or [])[:20] if ID_RE.match(str(a))] if isinstance(d, dict) else []
    mode = d.get("mode") if isinstance(d, dict) and d.get("mode") in MODES else None
    return {"scenarios": out, "active": active, "mode": mode}


def options_of(body: dict) -> dict:
    """The trigger options to pass on: id plus simple scalar options (bearing, clip …), nothing else."""
    out = {}
    for k, v in body.items():
        if k == "tool" or not re.match(r"^[a-z][a-z0-9_]{0,23}$", str(k)):
            continue
        if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 80):
            out[k] = v
    return out


def _parallel(fn: Callable, args: List) -> List:
    res: List = [None] * len(args)

    def run(i, a):
        res[i] = fn(a)
    ts = [threading.Thread(target=run, args=(i, a), daemon=True) for i, a in enumerate(args)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(TIMEOUT + 1.5)
    return res


def scenarios(tools: List[dict], call=_call) -> List[dict]:
    """tools: [{unit, where, base (or None), mode (heartbeat), why}] -> panel rows, in run-of-show order."""
    def one(t):
        row = {"unit": t["unit"], "title": TITLES.get(t["unit"], t["unit"]), "where": t.get("where", ""),
               "reachable": False, "has_demo": None, "error": t.get("why") or "", "scenarios": [], "active": [],
               "mode": t.get("mode") if t.get("mode") in MODES else None}
        if not t.get("base"):
            return row
        code, d = call(t["base"] + "/api/demo/scenarios")
        if code == 200:
            c = clean_scenarios(d)
            row.update(reachable=True, has_demo=True, error="", scenarios=c["scenarios"], active=c["active"],
                       mode=c["mode"] or row["mode"])
        elif code == 404:            # no demo API (tac-advisor, change-detect, geo-locate): not an error, no buttons
            row.update(reachable=True, has_demo=False, error="")
        else:
            row["error"] = d.get("error") or f"HTTP {code}"
        return row
    rows = _parallel(one, tools)
    order = {u: (gi, ui) for gi, (_, units) in enumerate(GROUPS) for ui, u in enumerate(units)}
    group = {u: g for g, units in GROUPS for u in units}
    for r in rows:
        r["group"] = group.get(r["unit"], "Other")
    return sorted(rows, key=lambda r: order.get(r["unit"], (len(GROUPS), r["unit"])))


def trigger(tool: dict, body: dict, call=_call) -> Tuple[int, dict]:
    if not ID_RE.match(str(body.get("id", ""))):
        return 400, {"error": "bad scenario id"}
    if not tool.get("base"):
        return 502, {"error": tool.get("why") or f"{tool['unit']} isn't reachable from the Pi"}
    code, d = call(tool["base"] + "/api/demo/trigger", options_of(body))
    if code == 0:
        return 502, {"error": d.get("error", "unreachable")}
    return code, d if isinstance(d, dict) else {"ok": code == 200}


def clear(tools: List[dict], call=_call, body: Optional[dict] = None) -> List[dict]:
    """POST /api/demo/clear on every tool given; one result each (never stops at the first failure).
    body {"background": true} (after a switch into step by step) also asks it to expire the CoT its
    continuous simulator sent before; a tool that doesn't know the field clears its demo events as usual."""
    def one(t):
        if not t.get("base"):
            return {"unit": t["unit"], "ok": False, "error": t.get("why") or "not reachable"}
        code, d = call(t["base"] + "/api/demo/clear", dict(body or {}))
        ok = code == 200 and isinstance(d, dict) and d.get("ok", True) is not False
        return {"unit": t["unit"], "ok": ok, "cleared": d.get("cleared") if ok else None,
                "error": "" if ok else (d.get("error") or f"HTTP {code}")}
    return _parallel(one, tools)


def clear_all(tools: List[dict], call=_call, body: Optional[dict] = None) -> List[dict]:
    """Reset picture: ask every tool for its scenarios first, then clear only the ones with a demo API.
    A tool that answers 404 (no demo API) is left out; one that doesn't answer is reported as unreachable."""
    rows = {r["unit"]: r for r in scenarios(tools, call=call)}
    with_api = [t for t in tools if rows.get(t["unit"], {}).get("has_demo")]
    res = clear(with_api, call=call, body=body) if with_api else []
    for t in tools:
        r = rows.get(t["unit"], {})
        if r.get("has_demo") is None:            # unreachable: say so, but don't call it
            res.append({"unit": t["unit"], "ok": False, "cleared": None, "error": r.get("error") or "not reachable"})
    return res


class AfterSwitch:
    """After the Pi switches into a step-by-step profile, the continuous simulators' CoT would sit on the
    map (and ATAK) until it went stale, up to 5 minutes. So: drop simulated items received before the
    switch from the launcher's picture at once, then POST /api/demo/clear {"background": true} to each demo
    tool as soon as it answers again (it restarts with DEMO_MODE=on_demand), for up to SETTLE_S. A tool the
    presenter has already fired since the switch is left alone, so a fresh event is never wiped."""
    SETTLE_S = 120.0

    def __init__(self):
        self.last: Optional[Tuple[str, str]] = None
        self.since = 0.0
        self.until = 0.0
        self.done: set = set()
        self.fired: Dict[str, float] = {}
        self.lock = threading.Lock()

    def note_trigger(self, unit: str, now: float):
        with self.lock:
            self.fired[unit] = now

    def observe(self, profile: str, mode: str, now: float) -> bool:
        """True when this is a change into on_demand (not the first look, e.g. a launcher restart)."""
        with self.lock:
            key = (profile, mode)
            first, changed = self.last is None, key != self.last
            self.last = key
            if first or not changed:
                return False
            if mode != "on_demand":
                self.until = 0.0
                return False
            self.since, self.until, self.done = now, now + self.SETTLE_S, set()
            return True

    def pending(self, tools: List[dict], now: float) -> List[dict]:
        with self.lock:
            if not self.until or now > self.until:
                self.until = 0.0
                return []
            return [t for t in tools if t["unit"] not in self.done and self.fired.get(t["unit"], 0) < self.since]

    def sweep(self, tools: List[dict], now: float, call=_call) -> List[dict]:
        """One pass: clear every pending tool that answers with a demo API; 404 tools count as done."""
        todo = self.pending(tools, now)
        if not todo:
            return []
        rows = {r["unit"]: r for r in scenarios(todo, call=call)}
        # ready: it has a demo API and has restarted in step-by-step mode (a Mac tool restarts a few
        # seconds after the Pi switches; clearing the old continuous process would let it send more).
        # A tool that reports no mode at all is cleared as soon as it answers.
        ready = [t for t in todo if rows.get(t["unit"], {}).get("has_demo") and rows[t["unit"]].get("mode") != "continuous"]
        with self.lock:
            self.done |= {t["unit"] for t in todo if rows.get(t["unit"], {}).get("has_demo") is False}
        res = clear(ready, call=call, body={"background": True}) if ready else []
        with self.lock:
            self.done |= {r["unit"] for r in res if r["ok"]}
        return res
