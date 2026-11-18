#!/usr/bin/env bash
# Start/stop this Mac's ISTAR kit tools (as set up by laptop/setup-mac.sh).
#
#   laptop/start-mac.sh start        start every repo in ISR_REPOS via its scripts/run-laptop.sh
#   laptop/start-mac.sh stop
#   laptop/start-mac.sh status
#   laptop/start-mac.sh logs <repo>  follow a tool's log
#
# Each tool gets station.env plus ~/.config/isr-kit/<repo>.env (optional overrides) in its
# environment. The Mac is kept awake (caffeinate) while tools run.
# op-sentry source: OPSENTRY_SOURCE from station.env or op-sentry.env wins; otherwise, when the Pi's
# active profile runs op-sentry-stream (split mode), the Pi's stream http://<hub>:8120/raw.mjpg.
# Wi-Fi keep-awake: on the BFT-TAK hotspot a Mac's Wi-Fi power save plus the Pi's (brcmfmac) AP
# buffering loses most Pi->Mac unicast (60-100% ping loss from the Pi, TCP stalls after a minute or
# two), while Mac->Pi works. A steady `ping -i 0.2` to the hub keeps the Mac's radio awake and the loss
# goes to 0%. start runs it in the background when the hub is on the hotspot (192.168.4.x) or the
# station isn't tunnelled; stop kills it; status shows it. ISR_WIFI_KEEPALIVE=0 turns it off.
# Demo style (contract v6): when the Pi's active profile is step by step (demo-step-mac), each tool gets
# DEMO_MODE=on_demand, so nothing simulated runs until the presenter fires it. DEMO_MODE in station.env
# or <repo>.env wins. Checked at start, then every ISR_DEMO_FOLLOW_EVERY seconds (15) by a background
# follower: when the Pi switches profile, the demo tools here (ISR_DEMO_REPOS) whose mode no longer
# matches are restarted in the new mode (log: demo-follow.log). A tool with DEMO_MODE pinned is left alone.
# Tunnelled HQ (ISR_TUNNEL=1, setup-mac.sh --tunnel): also starts/stops laptop/hq-tunnel.sh, and with
# STATION_PUBLIC_BASE set, gives each tool ISR_PUBLIC_URL=<base>:<its port> (contract: the URL its
# heartbeat advertises), so launcher links point at the Pi rather than this Mac's .local name.
set -uo pipefail
CONF_DIR="${ISR_CONF_DIR:-$HOME/.config/isr-kit}"
STATION_ENV="$CONF_DIR/station.env"
RUN="${ISR_RUN_DIR:-$HOME/isr-kit/run}"
LOGS="${ISR_LOG_DIR:-$HOME/isr-kit/logs}"
[ -f "$STATION_ENV" ] || { echo "No $STATION_ENV: run laptop/setup-mac.sh --role hq|op first"; exit 1; }
set -a
# shellcheck disable=SC1090
. "$STATION_ENV"
set +a
mkdir -p "$RUN" "$LOGS"
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICES_CONF="${ISR_SERVICES_CONF:-$HERE/../services.conf}"
# Tell the Pi's launcher which role this Mac plays (retained isrkit/station/<host>), so tools on a
# Mac whose hostname doesn't say it (a managed Mac) still show as "HQ Mac" / "OP Mac". On the hotspot it
# also says this Mac's 192.168.4.x address, so the Pi can relay its tool pages (phones may not resolve
# .local names). Re-sent every 60 s by a background announcer: RabbitMQ doesn't replay retained messages
# to a wildcard subscriber, so a launcher restarted later would otherwise never hear it.
announce_station() {
    [ -n "${MQTT_PASS:-}" ] && [ -f "$HERE/../launcher/mqtt_mini.py" ] || return 0
    ISR_LAUNCHER_DIR="$HERE/../launcher" /usr/bin/env python3 - <<'PY' 2>/dev/null || echo "  (couldn't announce the station role to the Pi; the board uses the profile's plan)"
import json, os, sys
sys.path.insert(0, os.environ["ISR_LAUNCHER_DIR"])
import socket
from mqtt_mini import MiniMQTT
host, role = os.environ.get("ISR_HOST", ""), os.environ.get("ISR_STATION", "")
if host and role:
    msg = {"host": host, "station": role}
    if os.environ.get("ISR_TUNNEL", "0") != "1":
        try:   # the address this Mac uses towards the hub (no packet is sent)
            hub = socket.gethostbyname(os.environ.get("ISR_HUB") or "isr-pi.local")
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((hub, 9)); ip = s.getsockname()[0]; s.close()
            if hub.startswith("192.168.4.") and ip.startswith("192.168.4.") and ip != hub:
                msg["ip"] = ip
        except OSError:
            pass
    c = MiniMQTT(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")), os.environ.get("MQTT_USER", ""),
                 os.environ["MQTT_PASS"], client_id="isrkit-station-" + host)
    c.connect()
    c.publish("isrkit/station/" + host, json.dumps(msg).encode(), retain=True)
    c.close()
PY
}
start_announcer() {
    [ -n "${MQTT_PASS:-}" ] || return 0
    alive station-announce && return 0
    # sleep in the background and wait: a TERM interrupts wait at once (a foreground sleep holds it off)
    ( trap 'kill "$z" 2>/dev/null; exit 0' TERM
      while :; do sleep "${ISR_ANNOUNCE_EVERY:-60}" & z=$!; wait "$z"; announce_station; done ) >/dev/null 2>&1 &
    echo $! > "$RUN/station-announce.pid"
}
# The tools here with a demo API (contract v6): the follower restarts these, and only these, on a mode change.
DEMO_REPOS="${ISR_DEMO_REPOS:-op-sentry fpv-vision drone-detector acoustic-detector rf-spectrum blufor-tracker}"
follow_demo_mode() {   # one pass: restart the demo tools whose mode differs from the Pi's
    local m r have
    m=$(pi_demo_mode)
    [ -n "$m" ] || return 0
    for r in $ISR_REPOS; do
        case " $DEMO_REPOS " in *" $r "*) ;; *) continue ;; esac
        alive "$r" || continue
        have=$(cat "$RUN/$r.mode" 2>/dev/null)
        [ "$have" = pinned ] || [ "$have" = "$m" ] && continue
        echo "$(date '+%H:%M:%S') $r: the Pi's profile is now $m (was ${have:-unknown}): restarting it"
        stop_one "$r"
        PI_DEMO_MODE="$m" start_one "$r"
    done
}
start_follower() {
    [ -z "${DEMO_MODE:-}" ] || return 0          # pinned in station.env: nothing to follow
    alive demo-follow && return 0
    ( trap 'kill "$w" 2>/dev/null; exit 0' TERM
      while :; do sleep "${ISR_DEMO_FOLLOW_EVERY:-15}" & w=$!; wait "$w"; follow_demo_mode; done ) >> "$LOGS/demo-follow.log" 2>&1 &
    echo $! > "$RUN/demo-follow.pid"
}
hub_ip() {   # the hub's IPv4 address, or nothing
    case "${ISR_HUB:-}" in
        *[!0-9.]*|'') /usr/bin/env python3 -c 'import socket,sys
try: print(socket.gethostbyname(sys.argv[1]))
except OSError: pass' "${ISR_HUB:-isr-pi.local}" 2>/dev/null ;;
        *) echo "$ISR_HUB" ;;
    esac
}
keepalive_target() {
    [ "${ISR_WIFI_KEEPALIVE:-1}" = "1" ] || return 0
    local ip; ip=$(hub_ip)
    case "$ip" in
        192.168.4.*) echo "$ip" ;;
        *) [ "${ISR_TUNNEL:-0}" = "1" ] || echo "${ISR_HUB_IP:-192.168.4.1}" ;;
    esac
}
start_keepalive() {
    local t; t=$(keepalive_target)
    [ -n "$t" ] || return 0
    if alive wifi-keepalive; then echo "  wifi keep-awake: already running (ping $t)"; return; fi
    nohup ping -i 0.2 -q "$t" >/dev/null 2>&1 &
    echo $! > "$RUN/wifi-keepalive.pid"
    echo "  wifi keep-awake: ping -i 0.2 $t (stops Wi-Fi power save losing the Pi's packets)"
}
# Split mode (the Pi's profile runs op-sentry-stream; no shipped profile does since the 2026-11 prune):
# the Pi streams its camera and op-sentry here detects on it.
# True when the launcher says the active profile runs op-sentry-stream. setup-mac.sh --op-split pins it.
split_mode() {
    curl -fsS --max-time 3 "http://${ISR_HUB:-isr-pi.local}:${ISR_LAUNCHER_PORT:-8050}/api/status" 2>/dev/null \
        | /usr/bin/env python3 -c 'import json,sys
try: st = json.load(sys.stdin)
except ValueError: sys.exit(1)
sys.exit(0 if any(s.get("unit") == "op-sentry-stream" and s.get("in_profile") for s in st.get("services", [])) else 1)'
}
# The Pi's demo style: on_demand / continuous from the launcher's status, or nothing if it doesn't answer.
pi_demo_mode() {
    curl -fsS --max-time 3 "http://${ISR_HUB:-isr-pi.local}:${ISR_LAUNCHER_PORT:-8050}/api/status" 2>/dev/null \
        | /usr/bin/env python3 -c 'import json,sys
try: m = json.load(sys.stdin).get("demo_mode")
except ValueError: sys.exit(1)
print(m if m in ("continuous", "on_demand") else "")'
}
tool_port() { awk -v r="$1" '!/^[[:space:]]*#/ && $4 == r && $2 ~ /^[0-9]+$/ {print $2; exit}' "$SERVICES_CONF" 2>/dev/null; }

alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

start_one() {
    local r="$1" d="$ISR_SRC/$1"
    if alive "$r"; then echo "  $r: already running (pid $(cat "$RUN/$r.pid"))"; return; fi
    if [ ! -f "$d/scripts/run-laptop.sh" ]; then echo "  $r: MISSING $d/scripts/run-laptop.sh"; return; fi
    (
        cd "$d" || exit 1
        port=$(tool_port "$r")
        if [ -n "${STATION_PUBLIC_BASE:-}" ] && [ -n "$port" ]; then
            export ISR_PUBLIC_URL="${STATION_PUBLIC_BASE%/}:$port/"
            # change-detect and geo-locate read PUBLIC_URL today; ISR_PUBLIC_URL is the contract name.
            export PUBLIC_URL="${PUBLIC_URL:-$ISR_PUBLIC_URL}"
        fi
        set -a
        # shellcheck disable=SC1090
        [ -f "$CONF_DIR/$r.env" ] && . "$CONF_DIR/$r.env"
        set +a
        if [ -n "${DEMO_MODE:-}" ]; then echo pinned > "$RUN/$r.mode"
        elif [ "${PI_DEMO_MODE:-}" = on_demand ]; then
            # shellcheck disable=SC2030  # this tool's own subshell, on purpose
            export DEMO_MODE=on_demand
            echo on_demand > "$RUN/$r.mode"
            echo "  $r: the Pi's profile is step by step: DEMO_MODE=on_demand"
        else echo continuous > "$RUN/$r.mode"; fi
        if [ "$r" = op-sentry ] && [ -z "${OPSENTRY_SOURCE:-}" ] && split_mode; then
            export OPSENTRY_SOURCE="http://${ISR_HUB:-isr-pi.local}:8120/raw.mjpg"
            echo "  op-sentry: the Pi's profile runs op-sentry-stream (split mode): source $OPSENTRY_SOURCE"
        fi
        exec nohup bash scripts/run-laptop.sh >> "$LOGS/$r.log" 2>&1
    ) &
    echo $! > "$RUN/$r.pid"
    echo "  $r: started (pid $!, log $LOGS/$r.log)"
}

stop_one() {
    local r="$1" pid
    if ! alive "$r"; then rm -f "$RUN/$r.pid"; echo "  $r: not running"; return; fi
    pid=$(cat "$RUN/$r.pid")
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null; echo "  $r: killed"; else echo "  $r: stopped"; fi
    rm -f "$RUN/$r.pid"
}

case "${1:-status}" in
    start)
        case "${TERM_PROGRAM:-}" in
            Apple_Terminal|iTerm.app) ;;
            *) echo "WARNING: not started from Terminal.app or iTerm (TERM_PROGRAM=${TERM_PROGRAM:-unset})."
               echo "         macOS silently denies camera/mic to tools launched from an IDE or agent shell."
               echo "         Quit and run 'laptop/start-mac.sh start' from Terminal.app." ;;
        esac
        # This Mac's own Ollama (OP/drone-ops VLM, or HQ): check it's up with its model before the tools need it.
        for pair in "${VLM_URL:-}|${VLM_MODEL:-}" "${LLM_URL:-}|${LLM_MODEL:-}"; do
            u="${pair%%|*}"; m="${pair#*|}"
            case "$u" in http://127.0.0.1:*|http://localhost:*) ;; *) continue ;; esac
            if ! tags=$(curl -fsS --max-time 3 "${u%/}/api/tags" 2>/dev/null); then
                echo "WARNING: Ollama at $u is not answering: the tools will run without AI descriptions."
                echo "         Start it (open -a Ollama, or launchctl kickstart gui/$(id -u)/com.isrkit.ollama), or re-run setup-mac.sh."
            elif [ -n "$m" ] && ! printf '%s' "$tags" | grep -Eq "\"name\": *\"$m\""; then
                echo "WARNING: Ollama at $u is up but model $m is missing (ollama pull $m on normal internet)."
            else
                echo "Ollama at $u: up${m:+, $m present}"
            fi
        done
        echo "Starting $ISR_STATION tools: $ISR_REPOS"
        PI_DEMO_MODE=""
        # shellcheck disable=SC2031  # station.env's DEMO_MODE, not the per-tool one
        if [ -n "${DEMO_MODE:-}" ]; then echo "  demo style: DEMO_MODE=$DEMO_MODE (station.env)"
        else PI_DEMO_MODE=$(pi_demo_mode); [ -n "$PI_DEMO_MODE" ] && echo "  demo style: the Pi's profile is $PI_DEMO_MODE"; fi
        for r in $ISR_REPOS; do start_one "$r"; done
        if ! alive caffeinate; then
            nohup caffeinate -dimsu >/dev/null 2>&1 &
            echo $! > "$RUN/caffeinate.pid"
        fi
        [ "${ISR_TUNNEL:-0}" = "1" ] && "$HERE/hq-tunnel.sh" start
        start_keepalive
        announce_station
        start_announcer
        start_follower
        echo "Heartbeats appear on the launcher: http://$ISR_HUB:8050"
        ;;
    stop)
        alive demo-follow && stop_one demo-follow >/dev/null     # first, so it can't restart a tool mid-stop
        for r in $ISR_REPOS; do stop_one "$r"; rm -f "$RUN/$r.mode"; done
        stop_one caffeinate >/dev/null
        alive wifi-keepalive && { stop_one wifi-keepalive >/dev/null; echo "  wifi keep-awake: stopped"; }
        alive station-announce && stop_one station-announce >/dev/null
        [ "${ISR_TUNNEL:-0}" = "1" ] && "$HERE/hq-tunnel.sh" stop
        ;;
    status)
        echo "$ISR_STATION station ($ISR_HOST.local), hub $ISR_HUB"
        for r in $ISR_REPOS; do
            if alive "$r"; then echo "  $r: running (pid $(cat "$RUN/$r.pid"))"; else echo "  $r: stopped"; fi
        done
        [ "${ISR_TUNNEL:-0}" = "1" ] && "$HERE/hq-tunnel.sh" status
        alive demo-follow && echo "  demo style: follows the Pi's profile (pid $(cat "$RUN/demo-follow.pid"))"
        if alive wifi-keepalive; then echo "  wifi keep-awake: running (pid $(cat "$RUN/wifi-keepalive.pid"))"
        elif [ -n "$(keepalive_target)" ]; then echo "  wifi keep-awake: not running (start runs it)"; fi
        for u in $(printf '%s\n' "${LLM_URL:-}" "${VLM_URL:-}" | awk 'NF && !seen[$0]++'); do
            if curl -fsS --max-time 2 "${u%/}/api/version" >/dev/null 2>&1; then echo "  Ollama $u: up"
            else echo "  Ollama $u: unreachable"; fi
        done
        ;;
    logs)
        [ -n "${2:-}" ] || { echo "usage: $0 logs <repo>"; exit 1; }
        tail -n 50 -f "$LOGS/$2.log"
        ;;
    *) sed -n '2,20p' "$0"; exit 1 ;;
esac
