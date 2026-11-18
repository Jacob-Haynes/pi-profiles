#!/usr/bin/env bash
# Integration rehearsal on one Mac: every ISTAR kit tool in simulate/replay mode against the
# rehearsal broker (127.0.0.1:18830) and the OTS-emulating CoT relay (127.0.0.1:18087).
# Work dir: $ISR_REHEARSAL_DIR (default ~/Repos/isr-kit/rehearsal) with src/<repo> clones.
#   tools/rehearsal/run_all.sh clone      clone/update every repo in repos.conf into src/
#   tools/rehearsal/run_all.sh setup      create venvs (uv)
#   tools/rehearsal/run_all.sh infra      start broker + CoT relay (logs/mqtt.jsonl, logs/cot.jsonl)
#   tools/rehearsal/run_all.sh start [t]  start every tool, or one
#   tools/rehearsal/run_all.sh stop [t]   SIGTERM every tool (and infra), or one
# Then score it:  tools/rehearsal/measure.py --logs "$ISR_REHEARSAL_DIR/logs"
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="$(cd "$HERE/../.." && pwd)"
W="${ISR_REHEARSAL_DIR:-$HOME/Repos/isr-kit/rehearsal}"
mkdir -p "$W"
SRC="$W/src"; VENVS="$W/venvs"; LOGS="$W/logs"; DATA="$W/data"
REAL_HOME="$HOME"   # clone() needs the user's gh login; tools get an isolated HOME
export HOME="$W/home"; mkdir -p "$HOME" "$LOGS" "$DATA" "$VENVS"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$W/uv-cache}"
COMMON=(MQTT_HOST=127.0.0.1 MQTT_PORT=18830 MQTT_USER=isrkit MQTT_PASS=rehearsal
        TAK_HOST=127.0.0.1 TAK_PORT=18087 GEMINI_API_KEY= PYTHONUNBUFFERED=1 VLM_BACKEND=off
        SENSOR_LAT=51.0000 SENSOR_LON=-1.0000 ISR_HOST=isr-pi)   # contract v4: shared OP position, Pi station name
# REAL_AI=1: use the local Ollama (LLM + VLM) instead of AI off, e.g. on an HQ-class Mac.
if [ "${REAL_AI:-0}" = "1" ]; then
    OLLAMA="${OLLAMA_URL:-http://127.0.0.1:11434}"
    COMMON+=(VLM_BACKEND=auto VLM_URL="$OLLAMA" VLM_MODEL="${VLM_MODEL:-qwen3-vl:8b-instruct}")
fi

req_for() {
    case "$1" in
        fpv-vision|op-sentry|change-detect) echo "requirements-desktop.txt" ;;
        *) echo "requirements.txt" ;;
    esac
}

setup() {
    for r in blufor-tracker drone-detector tac-advisor mesh-deadrop acoustic-detector fpv-vision op-sentry change-detect rf-spectrum photoAnalysis; do
        v="$VENVS/$r"
        [ -x "$v/bin/python" ] || uv venv -q -p 3.11 "$v"
        req="$SRC/$r/$(req_for "$r")"
        [ -f "$req" ] || req="$SRC/$r/requirements.txt"
        # llama-cpp-python compiles from source on macOS; the rehearsal uses a stub
        # OpenAI-compatible LLM instead, so skip it.
        echo "== $r: installing $(basename "$req")"
        if grep -qiE '^llama[-_]cpp' "$req"; then
            grep -viE '^llama[-_]cpp' "$req" > "$v/req.txt"
            VIRTUAL_ENV="$v" uv pip install -q -r "$v/req.txt" || echo "!! $r install failed"
        else
            (cd "$(dirname "$req")" && VIRTUAL_ENV="$v" uv pip install -q -r "$(basename "$req")") || echo "!! $r install failed"
        fi
    done
}

start_one() {   # start_one <name> <repo> <env...> -- <args...>
    local name="$1" repo="$2"; shift 2
    local envs=() args=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
    [ "${1:-}" = "--" ] && shift
    args=("$@")
    mkdir -p "$DATA/$name"
    ( cd "$SRC/$repo" && exec env "${COMMON[@]}" ${envs[@]+"${envs[@]}"} "$VENVS/$repo/bin/python" "${args[@]}" ) \
        >> "$LOGS/$name.log" 2>&1 &
    echo $! > "$LOGS/$name.pid"
    echo "started $name (pid $!)"
}

start() {
    [ -n "${1:-}" ] && { start_named "$1"; return; }
    start_one blufor-shim blufor-tracker -- -m bridge.mqtt_shim --simulate
    start_one blufor-cotsim blufor-tracker HEARTBEAT=0 -- -m bridge --simulate
    start_one drone-detector drone-detector WEB_PORT=8060 PUBLIC_URL=http://127.0.0.1:8060/ -- -m detector --simulate --no-ble
    local llm=(LLM_BACKEND=auto LLM_URL= LLM_MODEL=)
    [ "${REAL_AI:-0}" = "1" ] && llm=(LLM_BACKEND=ollama LLM_URL="${OLLAMA:-http://127.0.0.1:11434}" LLM_MODEL="${LLM_MODEL:-qwen3.5:27b}")
    start_one tac-advisor tac-advisor WEB_PORT=8090 "${llm[@]}" \
        ADVISOR_MODELS_DIR="$DATA/tac-advisor" PUBLIC_URL=http://127.0.0.1:8090/ -- -m advisor
    start_one mesh-deadrop mesh-deadrop WEB_PORT=8070 PUBLIC_URL=http://127.0.0.1:8070/ -- -m deadrop --db-path "$DATA/mesh-deadrop/messages.db"
    start_one acoustic-detector acoustic-detector WEB_PORT=8100 DATA_DIR="$DATA/acoustic-detector" PUBLIC_URL=http://127.0.0.1:8100/ \
        -- -m acoustic --simulate --no-yamnet
    start_one rf-spectrum rf-spectrum RFSPEC_PORT=8140 RFSPEC_DATA_DIR="$DATA/rf-spectrum" ALERT_MESH=1 ALERT_COT=1 \
        RFSPEC_PUBLIC_URL=http://127.0.0.1:8140/ -- -m rfspectrum --simulate
    start_one op-sentry op-sentry ISR_HOST=isr-droneops OPSENTRY_WEB_PORT=8120 OPSENTRY_DATA_DIR="$DATA/op-sentry" OPSENTRY_SOURCE=sim360 \
        OPSENTRY_PUBLIC_URL=http://127.0.0.1:8120/ -- -m opsentry --simulate --data-dir "$DATA/op-sentry"
    start_one fpv-vision fpv-vision ISR_HOST=isr-droneops FPV_PORT=8110 FPV_DATA_DIR="$DATA/fpv-vision" FPV_SOURCE=simulate \
        FPV_PUBLIC_URL=http://127.0.0.1:8110/ -- -m fpv_vision --data-dir "$DATA/fpv-vision"
    local geo=(--port 8150)
    [ "${REAL_AI:-0}" = "1" ] || geo+=(--simulate)
    start_one photoAnalysis photoAnalysis ISR_HOST=isr-hq WEB_PORT=8150 GEO_VLM_MODEL="${VLM_MODEL:-qwen3-vl:8b-instruct}" \
        PUBLIC_URL=http://127.0.0.1:8150/ -- -m geolocate "${geo[@]}"
    start_one change-detect change-detect ISR_HOST=isr-hq WEB_PORT=8130 CHANGE_DETECT_DATA_DIR="$DATA/change-detect" PUBLIC_URL=http://127.0.0.1:8130/ \
        -- -m changedetect --simulate --data-dir "$DATA/change-detect" --port 8130
}

start_named() {   # restart a single tool by re-running only its start_one line
    eval "$(declare -f start | grep -E "start_one $1 " | sed 's/^ *//; s/\\$//')"
}

clone() {
    mkdir -p "$SRC"
    # Private repos (op-sentry, photoAnalysis): use gh's credential helper when gh is logged in.
    local GITC=(env HOME="$REAL_HOME" git)
    if command -v gh >/dev/null 2>&1 && HOME="$REAL_HOME" gh auth status >/dev/null 2>&1; then
        GITC=(env HOME="$REAL_HOME" git -c credential.helper= -c 'credential.helper=!gh auth git-credential')
    fi
    awk '!/^[[:space:]]*(#|$)/ {print $1, $2}' "$KIT/repos.conf" | while read -r repo branch; do
        if [ -d "$SRC/$repo/.git" ]; then
            "${GITC[@]}" -C "$SRC/$repo" fetch -q origin "$branch" && "${GITC[@]}" -C "$SRC/$repo" checkout -q -B "$branch" "origin/$branch" \
                && echo "$repo $(git -C "$SRC/$repo" log -1 --format=%h)" || echo "!! $repo update failed"
        else
            "${GITC[@]}" clone -q -b "$branch" "https://github.com/${ISR_GH_OWNER:-Jacob-Haynes}/$repo.git" "$SRC/$repo" \
                && echo "$repo $(git -C "$SRC/$repo" log -1 --format=%h)" || echo "!! $repo clone failed (private? gh auth)"
        fi
    done
}

infra() {
    date +%s > "$LOGS/run.start"
    nohup python3 "$HERE/broker.py" 18830 "$LOGS/mqtt.jsonl" > "$LOGS/broker.out" 2>&1 & echo $! > "$LOGS/broker.pid"
    nohup python3 "$HERE/cot_relay.py" 18087 "$LOGS/cot.jsonl" > "$LOGS/cot.out" 2>&1 & echo $! > "$LOGS/cot.pid"
    sleep 1; cat "$LOGS/broker.out" "$LOGS/cot.out"
}

stop() {
    local names pids=""
    if [ -n "${1:-}" ]; then names="$1"; else
        names="$(cd "$LOGS" && ls *.pid 2>/dev/null | sed 's/\.pid$//' | grep -vE '^(broker|cot|launcher|stubllm)$')"
    fi
    # Tools first, and wait for them, so their retained "stopped" heartbeats reach the broker.
    for n in $names; do [ -f "$LOGS/$n.pid" ] && pids="$pids $(cat "$LOGS/$n.pid")"; done
    stop_pids "$names"
    for _ in $(seq 1 20); do
        alive=0; for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
        [ "$alive" -eq 0 ] && break; sleep 1
    done
    [ -n "${1:-}" ] || { sleep 1; stop_pids "broker cot"; }
}

stop_pids() {
    local names="$1"
    for n in $names; do
        [ -f "$LOGS/$n.pid" ] || continue
        pid=$(cat "$LOGS/$n.pid")
        kill -TERM "$pid" 2>/dev/null && echo "SIGTERM $n ($pid)"
        rm -f "$LOGS/$n.pid"
    done
}

case "${1:-}" in
    clone) clone ;;
    infra) infra ;;
    setup) setup ;;
    start) shift; start "${1:-}" ;;
    stop) shift; stop "${1:-}" ;;
    *) sed -n '2,11p' "$0"; exit 1 ;;
esac
