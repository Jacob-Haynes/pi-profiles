#!/usr/bin/env python3
"""Integration gate: check every kit repo's branch against docs/CONTRACT.md.

    tools/contract_gate.py                 # clone each repo's branch from repos.conf into a temp dir
    tools/contract_gate.py --src DIR       # check existing checkouts in DIR/<repo> instead

Heuristic and read-only: it greps code and config, so treat a finding as "go and look", not
proof. Exits 1 if any repo has findings. Needs git (and gh auth for the private repos).
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = os.environ.get("ISR_GH_OWNER", "Jacob-Haynes")
PORTS = {"photoAnalysis": 8150, "mesh-deadrop": 8070, "drone-detector": 8060, "tac-advisor": 8090, "acoustic-detector": 8100,
         "fpv-vision": 8110, "op-sentry": 8120, "change-detect": 8130, "rf-spectrum": 8140, "blufor-tracker": 8170}
UNIT = {"blufor-tracker": "mesh-bridge"}
VISION = {"fpv-vision", "op-sentry", "change-detect"}


def read(p):
    try:
        return open(p, errors="replace").read()
    except OSError:
        return ""


def code(G, repo):
    out = []
    for p in glob.glob(f"{G}/{repo}/**/*", recursive=True):
        if os.path.isfile(p) and "/.git/" not in p and "/tests/" not in p and \
                re.search(r"\.(py|sh|ps1|example|service|conf|toml|md|txt|cfg|env)$|/[^/.]+$", p):
            out.append((p[len(G) + 1 + len(repo) + 1:], read(p)))
    return out


def check(G, repo):
    d = f"{G}/{repo}"; unit = UNIT.get(repo, repo); files = code(G, repo); allc = "\n".join(c for _, c in files)
    nondoc = "\n".join(c for n, c in files if not n.endswith(".md"))
    issues, ok = [], []
    inst = read(f"{d}/deploy/install.sh")
    if not inst: issues.append("no deploy/install.sh")
    else:
        if re.search(r"^\s*systemctl\s+enable(?!\s+--now\s+avahi)", inst, re.M): issues.append("install.sh runs `systemctl enable` (contract: not enabled at boot)")
        if re.search(r"\bread\s+-[a-z]*p|\bread\s+-r?\s+\w+\s*$", inst, re.M) and "</dev/tty" not in inst: issues.append("install.sh may prompt (read)")
        inst_x = re.sub(r"\$\{?APP\}?", repo, inst) if re.search(rf"^APP=['\"]?{re.escape(repo)}['\"]?\s*$", inst, re.M) else inst
        if f"/opt/{repo}" not in inst_x: issues.append(f"install.sh doesn't mention /opt/{repo}")
        if f"/var/lib/{repo}" not in inst_x and repo not in ("tac-advisor",): issues.append(f"install.sh doesn't create /var/lib/{repo}")
    svc = read(f"{d}/deploy/{unit}.service")
    if not svc: issues.append(f"no deploy/{unit}.service")
    else:
        if f"EnvironmentFile=-/etc/{repo}.env" not in svc: issues.append(f"{unit}.service lacks EnvironmentFile=-/etc/{repo}.env")
        if re.search(r"^\[Install\]", svc, re.M) is None: pass
        m = re.search(r"TimeoutStopSec=(\d+)", svc)
        if m and int(m.group(1)) > 15: issues.append(f"TimeoutStopSec={m.group(1)} > 15")
    envs = [n for n in (f"deploy/{repo}.env.example", "deploy/env.example", f"{repo}.env.example", ".env.example") if os.path.exists(f"{d}/{n}")]
    if not envs: issues.append("no env example (bootstrap creates an empty /etc/<repo>.env)")
    envtxt = read(f"{d}/{envs[0]}") if envs else ""
    if not os.path.exists(f"{d}/profiles.d/{repo}.conf"): issues.append(f"no profiles.d/{repo}.conf (has: {', '.join(os.path.basename(p) for p in glob.glob(d+'/profiles.d/*.conf')) or 'none'})")
    for p in glob.glob(d + "/profiles.d/*.conf"):
        t = read(p)
        if re.search(r"^\s*(hostapd|dnsmasq)\s*$", t, re.M) and "@hotspot off" not in t:
            issues.append(f"{os.path.basename(p)} runs hostapd/dnsmasq without `# @hotspot off` (fights BFT-TAK)")
        if re.search(r"^\s*freetakserver", t, re.M): issues.append(f"{os.path.basename(p)} references FTS")
    if repo in PORTS:
        port = str(PORTS[repo])
        if port not in nondoc: issues.append(f"contract port {port} not found in code/config")
    for v in ("TAK_HOST", "TAK_PORT", "MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS"):
        if repo in ("blufor-tracker", "mesh-deadrop") and v.startswith("TAK"): continue   # mesh-only tools
        if v not in nondoc: issues.append(f"env {v} not used")
    if "isrkit/heartbeat" not in nondoc: issues.append("no heartbeat (isrkit/heartbeat/<tool>)")
    else:
        issues += heartbeat_name_issues(nondoc, repo)
        hb = re.findall(r"isrkit/heartbeat/[^\"'\s)]*", nondoc)
        if not any(repo in h or "{" in h or h.endswith("/") for h in hb): issues.append(f"heartbeat topic(s) {sorted(set(hb))[:3]} don't use the repo name")
        for f in ("\"mode\"", "\"status\"", "\"url\"", "\"host\"", "\"ts\""):
            if f not in nondoc and f.replace('"', "'") not in nondoc: issues.append(f"heartbeat field {f} not found")
        if not re.search(r"retain\s*=\s*True|retain=1|-r\b|retained", nondoc): issues.append("heartbeat may not be retained")
    if not re.search(r"SIMULAT|REPLAY|--simulate|simulate", nondoc): issues.append("no simulate/replay mode found")
    if repo in VISION:   # acoustic-detector runs on the Pi in v3, so a laptop script is optional there
        if not os.path.exists(f"{d}/scripts/run-laptop.sh"): issues.append("no scripts/run-laptop.sh")
        else:
            rl = read(f"{d}/scripts/run-laptop.sh")
            if "0.0.0.0" not in rl and "0.0.0.0" not in nondoc: issues.append("run-laptop.sh: no 0.0.0.0 bind seen")
    if repo in VISION:
        for v in ("VLM_URL", "VLM_MODEL"):
            if v not in nondoc: issues.append(f"env {v} not used")
    if repo == "blufor-tracker" and not os.path.exists(f"{d}/bridge/check.py"):
        issues.append("no bridge/check.py (demo-day self-check used by the launcher)")
    hb_code = "\n".join(c for n, c in files if "heartbeat" in n.lower() and not n.endswith(".md"))
    modes = set(re.findall(r"[\"']mode[\"']\s*[:=]\s*[\"']([a-z]+)[\"']", hb_code))
    bad_modes = modes - {"cpu", "cuda", "mps", "gemini", "vlm", "llm", "simulate", "replay"}
    if bad_modes:
        issues.append(f"heartbeat mode(s) outside the contract enum: {sorted(bad_modes)}")
    if repo == "op-sentry":   # split mode: second unit + heartbeat (contract exception to tool = repo)
        stream = read(f"{d}/deploy/op-sentry-stream.service")
        if not stream:
            issues.append("no deploy/op-sentry-stream.service")
        elif "EnvironmentFile=-/etc/op-sentry.env" not in stream:
            issues.append("op-sentry-stream.service lacks EnvironmentFile=-/etc/op-sentry.env")
        if not os.path.exists(f"{d}/profiles.d/op-sentry-stream.conf"):
            issues.append("no profiles.d/op-sentry-stream.conf (standalone split-mode profile)")
        combined = [os.path.basename(p) for p in glob.glob(d + "/profiles.d/bft-op-sentry*.conf")]
        if combined:
            issues.append(f"combined profiles still shipped (coordinator asked to drop them): {', '.join(combined)}")
    # ── contract v4 (static, heuristic) ──
    SENSORS = {"drone-detector", "acoustic-detector", "fpv-vision", "op-sentry", "change-detect", "rf-spectrum"}
    # Tools that replay recorded footage/audio as a live feed. change-detect analyses still photos
    # (operator input, live) and labels its synthetic samples SIMULATED, so it isn't in this set.
    REPLAY = {"fpv-vision", "op-sentry", "acoustic-detector"}
    if "isrkit/heartbeat" in nondoc and "ISR_HOST" not in nondoc:
        issues.append("C4.1: heartbeat host doesn't read ISR_HOST")
    if repo in SENSORS:
        for v in ("SENSOR_LAT", "SENSOR_LON"):
            if v not in envtxt:
                issues.append(f"C4.2: {v} missing from {envs[0] if envs else 'env example'}")
        if "SIMULATED: " not in nondoc:
            issues.append('C4.3: no "SIMULATED: " CoT remarks prefix in code')
        if "[SIM] " not in nondoc:
            issues.append('C4.3: no "[SIM] " mesh text prefix in code')
    if repo in REPLAY:
        if "RECORDED: " not in nondoc:
            issues.append('C4.3: no "RECORDED: " CoT remarks prefix in code (replay/file input)')
        if "[REC] " not in nondoc:
            issues.append('C4.3: no "[REC] " mesh text prefix in code (replay/file input)')
    # ── contract v5: OTS registration (static, heuristic) ──
    if "TAK_HOST" in nondoc and repo != "mesh-deadrop":
        issues += c5_issues(nondoc)
    if repo == "tac-advisor":
        for v in ("LLM_URL", "LLM_MODEL", "openai_compat"):
            if v not in nondoc: issues.append(f"{v} not used")
    if re.search(r"gemini-2\.[05]", nondoc): issues.append("deprecated gemini-2.x model default")
    issues += model_issues(nondoc)
    if re.search(r"Co-Authored-By: Claude|Generated with Claude", allc, re.I): issues.append("AI attribution found")
    return issues


HB_EXTRA = {"op-sentry": {"op-sentry-stream"}}   # contract-listed second units


def heartbeat_name_issues(nondoc, repo):
    """The heartbeat tool name must be the repo name (C: Heartbeat). Catch literal topics and
    tool-name constants that say something else (e.g. geo-locate for photoAnalysis)."""
    ok = {repo} | HB_EXTRA.get(repo, set())
    names = set(re.findall(r"isrkit/heartbeat/([A-Za-z][\w-]*)", nondoc))
    names |= set(re.findall(r"""["']tool["']\s*[:=]\s*["']([A-Za-z][\w-]*)["']""", nondoc))
    names |= set(re.findall(r"""\b(?:TOOL|TOOL_NAME|HEARTBEAT_TOOL)\s*=\s*["']([A-Za-z][\w-]*)["']""", nondoc))
    bad = sorted(n for n in names if n not in ok and n not in ("isr-launcher",))
    return [f"heartbeat tool name {bad} is not the repo name {repo!r}"] if bad else []


def strip_comments(text):
    """Drop comment lines (#, //, leading * in block comments) and trailing ' # …' comments."""
    out = []
    for line in text.splitlines():
        t = line.strip()
        if t.startswith(("#", "//", "*", "/*")):
            continue
        out.append(re.sub(r"\s#\s.*$", "", line))
    return "\n".join(out)


def model_issues(nondoc):
    nondoc = strip_comments(nondoc)
    out = []
    if re.search(r"qwen3-vl:(8|4)b(?![-\w])", nondoc):
        out.append("VLM default: use qwen3-vl:8b-instruct (stock qwen3-vl:8b keeps thinking; ~2 min/image)")
    if re.search(r"""["']format["']\s*:\s*["']json["']""", nondoc):
        out.append('uses Ollama format:"json" (HTTP 501 on MLX-runner models like qwen3.5:27b): ask for JSON in the prompt instead')
    return out


def c5_issues(nondoc):
    out = []
    if "isrkit-" not in nondoc:
        out.append('C5.2: no stable "isrkit-<repo>-<ISR_HOST>" registration uid in code')
    if not ("takv" in nondoc and re.search(r"platform[^\n]{0,40}isr-kit", nondoc)):
        out.append('C5.2: no <takv platform="isr-kit"> in the registration SA')
    if "a-f-G-E-S" not in nondoc:
        out.append("C5.2: registration SA type a-f-G-E-S not found")
    if not re.search(r"(sleep|wait|delay|DELAY|REGISTER)[^\n]{0,80}\b3(\.0)?\b|\b3(\.0)?\b[^\n]{0,40}(register|registration|channel|bind)",
                     nondoc, re.I):
        out.append("C5.2: no 3 s wait before the registration SA found")
    return out


def check_mac(G, repo):
    """Mac-only tools (repos.conf where=mac): no Pi install, but the Mac-station contract."""
    d = f"{G}/{repo}"
    files = code(G, repo)
    nondoc = "\n".join(c for n, c in files if not n.endswith(".md"))
    issues = []
    if not os.path.exists(f"{d}/scripts/run-laptop.sh"):
        issues.append("no scripts/run-laptop.sh (Mac launch path for start-mac.sh)")
    else:
        rl = read(f"{d}/scripts/run-laptop.sh")
        if "0.0.0.0" not in rl and "0.0.0.0" not in nondoc:
            issues.append("run-laptop.sh: no 0.0.0.0 bind seen")
    port = str(PORTS.get(repo, ""))
    if port and port not in nondoc:
        issues.append(f"contract port {port} not found in code/config")
    for v in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS", "VLM_URL", "VLM_MODEL", "ISR_HOST"):
        if v not in nondoc:
            issues.append(f"env {v} not used")
    if f"isrkit/heartbeat/{repo}" not in nondoc and not ("isrkit/heartbeat" in nondoc and repo in nondoc):
        issues.append(f"no heartbeat on isrkit/heartbeat/{repo}")
    issues += heartbeat_name_issues(nondoc, repo)
    if "TAK_HOST" in nondoc:
        issues += c5_issues(nondoc)
    if "ESTIMATE" not in nondoc:
        issues.append("geolocation results not labelled ESTIMATE")
    if re.search(r"gemini-2\.[05]", nondoc):
        issues.append("deprecated gemini-2.x model default")
    issues += model_issues(nondoc)
    if re.search(r"Co-Authored-By: Claude|Generated with Claude", "\n".join(c for _, c in files), re.I):
        issues.append("AI attribution found")
    return issues


def repos_conf():
    rows = []
    for line in read(os.path.join(KIT, "repos.conf")).splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            parts = line.split()
            rows.append((parts[0], parts[1], parts[3] if len(parts) > 3 else "pi"))
    return rows


def main():
    args = sys.argv[1:]
    src = args[args.index("--src") + 1] if "--src" in args else None
    G = src or tempfile.mkdtemp(prefix="isr-gate-")
    bad = 0
    for repo, branch, where in repos_conf():
        d = os.path.join(G, repo)
        head = ""
        if not src:
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", branch,
                                f"https://github.com/{OWNER}/{repo}.git", d], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"## {repo} ({branch})\n  - branch not found or no access")
                bad += 1
                continue
        if os.path.isdir(os.path.join(d, ".git")):
            head = subprocess.run(["git", "-C", d, "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M"],
                                  capture_output=True, text=True).stdout.strip()
        issues = check_mac(G, repo) if where == "mac" else check(G, repo)
        print(f"## {repo} ({branch} {head})")
        for i in issues:
            print(f"  - {i}")
        if not issues:
            print("  - conforms")
        bad += bool(issues)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
