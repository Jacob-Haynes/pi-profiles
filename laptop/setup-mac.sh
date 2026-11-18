#!/usr/bin/env bash
# Set up an Apple Silicon Mac as an ISTAR kit station on the Pi's hotspot.
#
# Order (the hotspot has no internet, so everything is downloaded first):
#   1. On normal internet: gh auth login (op-sentry and photoAnalysis are private), then
#        laptop/setup-mac.sh --role op                (or --role hq [--extra fpv-vision])
#      It installs brew packages (git gh python@3.12 ffmpeg uv), clones the repos, installs Ollama
#      and pulls its models, then runs each repo's scripts/run-laptop.sh --setup-only: pip
#      packages, model weights (YOLO / YOLOE / CLIP), the fpv sample clips, and any Ollama model it needs.
#   2. Join the BFT-TAK hotspot and run it again to fetch the MQTT login and check the hub:
#        laptop/setup-mac.sh --role op --from-pi <user>@isr-pi.local --skip-prefetch --skip-clone --skip-brew
#      add --hub 192.168.4.1 if isr-pi.local doesn't resolve (Bonjour can be unreliable).
#   3. From Terminal.app (not an IDE or agent shell, or macOS silently blocks the camera):
#        laptop/start-mac.sh start
#
# Roles (the demo layout: HQ Mac on the cable, tunnelled, with --extra fpv-vision; OP Mac on the hotspot):
#   hq        change-detect + geo-locate (photoAnalysis) + Ollama (instruct LLM for tac-advisor,
#             VLM for the vision tools), plus the ATAK/WinTAK/browser HQ display.
#             --extra fpv-vision: FPV replay from the HQ table too ("as if receiving the drone feed");
#             its port 8110 joins the tunnel ports. --fpv-footage DIR sets FPV_FOOTAGE_DIRS.
#   op        OP Mac, the remote sensor post: op-sentry only (360 camera, C920 / iPhone), with its own
#             Ollama on 127.0.0.1 with only the VLM (qwen3-vl:8b-instruct), and the Wi-Fi keep-awake
#             (start-mac.sh). Fits a 24 GB Mac; fpv-vision as well needs more (it swapped at 24 GB).
#   droneops  older layout: fpv-vision + op-sentry on one Mac with its own VLM (32 GB or more).
#   --skip-ollama on op/droneops: use the HQ Mac's VLM instead (not with a tunnelled HQ).
#
# Options:
#   --role hq|op|droneops   required
#   --from-pi USER@HOST     copy the MQTT login from the Pi (ssh + sudo cat /etc/isr-kit/mqtt.env)
#   --mqtt-pass-file PATH   or read MQTT_PASS from a file (otherwise you are prompted, hidden)
#   --hub NAME              hub address (default isr-pi.local)
#   --hq-host NAME          HQ Mac address for LLM_URL/VLM_URL (default isr-hq.local)
#   --set-hostname          set this Mac's Bonjour name to isr-hq / isr-op / isr-droneops (sudo scutil)
#   --branch repo=branch    override repos.conf (repeatable)
#   --extra repo            also install another repo on this Mac (e.g. fpv-vision on HQ); its port
#                           joins the tunnel ports
#   --fpv-footage DIR       FPV_FOOTAGE_DIRS for fpv-vision (kept from the old station.env if not given)
#   --llm-model M --vlm-model M   override the Ollama models (HQ)
#   --lite                  smaller models (for a Mac with less than 48 GB)
#   --gemini-key-file PATH  also write GEMINI_API_KEY (the Mac VLM is preferred; Gemini is fallback)
#   --sensor LAT,LON        OP / sensor position (contract v4); --from-pi copies it from the Pi
#   --tunnel                tunnelled HQ (recommended for a managed Mac, or any Mac whose settings
#                           you can't change): no Bonjour name needed. laptop/hq-tunnel.sh (started by
#                           start-mac.sh) forwards Ollama and the tool ports to the Pi over one ssh
#                           connection; Ollama stays on 127.0.0.1; the tools' links use the Pi address.
#   --op-split              observation post in split mode (the Pi runs op-sentry-stream): op-sentry here
#                           detects on the Pi's camera stream, OPSENTRY_SOURCE=http://<hub>:8120/raw.mjpg.
#                           Without it start-mac.sh still picks the stream when the Pi's active profile
#                           runs op-sentry-stream; otherwise op-sentry uses the newest Insta360 file or sim.
#   --tunnel-pi USER@HOST   ssh login for the tunnel (default: --from-pi, else $USER@<hub>)
#   --skip-ollama --skip-clone --skip-brew --skip-camera-probe --skip-prefetch
#                           (--skip-ollama leaves Ollama exactly as it is; --skip-prefetch skips the
#                           repos' --setup-only downloads, e.g. on the hotspot)
#
# Camera/mic permission: macOS only shows the Camera/Microphone prompt to the app that starts
# the tools. Run this script and start-mac.sh from Terminal.app or iTerm, not from an IDE or
# agent shell, where capture is silently denied with no prompt. On the op/droneops roles this
# script grabs one camera frame (ffmpeg) to trigger the Terminal prompt now, not on demo day.
#
# Writes ~/.config/isr-kit/station.env (mode 600). Start the tools with laptop/start-mac.sh.
# Run it again any time: it is idempotent.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="$(dirname "$HERE")"
ROLE=""
FROM_PI=""
PASS_FILE=""
HUB="isr-pi.local"
HQ_HOST="isr-hq.local"
SET_HOSTNAME=0
OVERRIDES=""
EXTRA=""
# Checked against ollama.com/library on 2026-11-16: qwen3.5:27b 17 GB, qwen3-vl:8b-instruct 6.2 GB.
# Use the -instruct VLM tag: stock qwen3-vl:8b keeps "thinking" even with think:false (about 2 min
# per image in geo-locate's benchmark); the instruct tag answers in 1-5 s.
# HQ is an M4 Max with 48 GB. macOS lets the GPU wire about 75% of unified memory (about 36 GB),
# so 23 GB of weights plus KV cache at an 8k context (about 3-5 GB) fits, leaving about 10 GB of
# GPU headroom and about 12 GB of RAM for change-detect, ATAK/browser and the OS.
# Lite (for less than 48 GB): qwen3.5:9b 6.6 GB + qwen3-vl:4b-instruct 3.3 GB.
# Override with --llm-model/--vlm-model or ISR_LLM_MODEL/ISR_VLM_MODEL.
LLM_MODEL="${ISR_LLM_MODEL:-qwen3.5:27b}"
VLM_MODEL="${ISR_VLM_MODEL:-qwen3-vl:8b-instruct}"
CTX="${ISR_OLLAMA_CONTEXT:-8192}"
LITE=0
GEMINI_FILE=""
SENSOR=""
SKIP_OLLAMA=0; SKIP_CLONE=0; SKIP_BREW=0; SKIP_CAM=0; SKIP_PREFETCH=0
TUNNEL=0; TUNNEL_PI=""; OP_SPLIT=0; FPV_FOOTAGE=""
HUB_IP="${ISR_HUB_IP:-192.168.4.1}"
GH_OWNER="${ISR_GH_OWNER:-Jacob-Haynes}"
SRC="${ISR_MAC_SRC:-$HOME/isr-kit}"
CONF_DIR="$HOME/.config/isr-kit"
STATION_ENV="$CONF_DIR/station.env"

die() { echo "ERROR: $*" >&2; exit 1; }
WARN=""
warn() { echo "WARNING: $*"; WARN="$WARN\n  - $*"; }
step() { echo ""; echo "── $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="${2:?}"; shift 2 ;;
        --from-pi) FROM_PI="${2:?}"; shift 2 ;;
        --mqtt-pass-file) PASS_FILE="${2:?}"; shift 2 ;;
        --hub) HUB="${2:?}"; shift 2 ;;
        --hq-host) HQ_HOST="${2:?}"; shift 2 ;;
        --set-hostname) SET_HOSTNAME=1; shift ;;
        --branch) OVERRIDES="$OVERRIDES ${2:?}"; shift 2 ;;
        --extra) EXTRA="$EXTRA ${2:?}"; shift 2 ;;
        --llm-model) LLM_MODEL="${2:?}"; shift 2 ;;
        --vlm-model) VLM_MODEL="${2:?}"; shift 2 ;;
        --lite) LITE=1; shift ;;
        --gemini-key-file) GEMINI_FILE="${2:?}"; shift 2 ;;
        --sensor) SENSOR="${2:?}"; shift 2 ;;
        --skip-ollama) SKIP_OLLAMA=1; shift ;;
        --tunnel) TUNNEL=1; shift ;;
        --op-split) OP_SPLIT=1; shift ;;
        --fpv-footage) FPV_FOOTAGE="${2:?}"; shift 2 ;;
        --tunnel-pi) TUNNEL=1; TUNNEL_PI="${2:?}"; shift 2 ;;
        --skip-camera-probe) SKIP_CAM=1; shift ;;
        --skip-clone) SKIP_CLONE=1; shift ;;
        --skip-brew) SKIP_BREW=1; shift ;;
        --skip-prefetch) SKIP_PREFETCH=1; shift ;;
        -h|--help) sed -n '2,61p' "$0"; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

case "$ROLE" in
    hq) REPOS="change-detect photoAnalysis" ; HOSTNAME_WANT="isr-hq" ;;
    op) REPOS="op-sentry" ; HOSTNAME_WANT="isr-op" ;;
    droneops) REPOS="fpv-vision op-sentry" ; HOSTNAME_WANT="isr-droneops" ;;
    *) die "--role hq|op|droneops is required" ;;
esac
for r in $EXTRA; do case " $REPOS " in *" $r "*) ;; *) REPOS="$REPOS $r" ;; esac; done
# op and droneops run their own VLM on 127.0.0.1 (camera stations); hq runs the LLM and VLM.
case "$ROLE" in op|droneops) OWN_VLM=1 ;; *) OWN_VLM=0 ;; esac
# FPV footage folders: --fpv-footage, else whatever the old station.env had (a re-run keeps it).
[ -n "$FPV_FOOTAGE" ] || FPV_FOOTAGE=$(sed -n 's/^FPV_FOOTAGE_DIRS=//p' "$STATION_ENV" 2>/dev/null | head -1 | tr -d "\"'")
[ -n "$TUNNEL_PI" ] || TUNNEL_PI="${FROM_PI:-$(id -un)@$HUB}"
for v in "$HUB" "$HQ_HOST" "$LLM_MODEL" "$VLM_MODEL" "$TUNNEL_PI" "$HUB_IP"; do
    case "$v" in *[!A-Za-z0-9._:@-]*) die "invalid value '$v'" ;; esac
done
case "$FPV_FOOTAGE" in *[!A-Za-z0-9._/~:-]*) die "invalid --fpv-footage '$FPV_FOOTAGE' (no spaces or quotes; folders separated by :)" ;; esac

# ── preflight ────────────────────────────────────────────────────────────────
step "Preflight"
[ "$(uname -s)" = "Darwin" ] || die "this script is for macOS (Windows/Linux: see laptop/README.md)"
[ "$(uname -m)" = "arm64" ] || warn "not Apple Silicon ($(uname -m)): MPS acceleration won't be available"
MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
echo "  macOS $(sw_vers -productVersion), $(sysctl -n machdep.cpu.brand_string 2>/dev/null), ${MEM_GB} GB RAM"
if [ "$ROLE" = "hq" ] && [ "$LITE" -eq 0 ] && [ "$MEM_GB" -lt 48 ]; then
    warn "${MEM_GB} GB RAM: switching to --lite models"
    LITE=1
fi
if [ "$LITE" -eq 1 ]; then
    [ "$LLM_MODEL" = "qwen3.5:27b" ] && LLM_MODEL="qwen3.5:9b"
    [ "$VLM_MODEL" = "qwen3-vl:8b-instruct" ] && VLM_MODEL="qwen3-vl:4b-instruct"
fi

# ── packages ─────────────────────────────────────────────────────────────────
step "Homebrew packages"
if [ "$SKIP_BREW" -eq 1 ]; then
    echo "  skipped"
elif ! command -v brew >/dev/null 2>&1; then
    die "Homebrew not found. Install it from https://brew.sh (one command), then re-run."
else
    for pkg in git gh python@3.12 ffmpeg uv; do
        brew list --formula "$pkg" >/dev/null 2>&1 && echo "  $pkg: installed" \
            || { echo "  installing $pkg"; brew install --quiet "$pkg" || warn "brew install $pkg failed"; }
    done
fi

# ── hostname (Bonjour) ───────────────────────────────────────────────────────
step "Bonjour name"
CUR_HOST=$(scutil --get LocalHostName 2>/dev/null || hostname -s)
# A managed profile (MDM) can stop this Mac advertising its Bonjour name at all; then <name>.local
# never resolves (not even on this Mac) and renaming doesn't help.
NO_MCAST=$(defaults read "/Library/Managed Preferences/com.apple.mDNSResponder" NoMulticastAdvertisements 2>/dev/null || true)
if [ "$NO_MCAST" = "1" ]; then
    if [ "$TUNNEL" -eq 1 ]; then
        echo "  note: a managed profile sets NoMulticastAdvertisements=1 ($CUR_HOST.local won't resolve);"
        echo "        fine: --tunnel doesn't need it"
    else
        warn "a managed profile sets NoMulticastAdvertisements=1: this Mac's Bonjour name ($CUR_HOST.local) never resolves, so the Pi and phones can't reach it by name and --set-hostname won't help. Re-run with --tunnel (tunnelled HQ: direct cable + one ssh tunnel to the Pi; no Mac settings changed)"
    fi
    SET_HOSTNAME=0
fi
if [ "$SET_HOSTNAME" -eq 1 ] && [ "$CUR_HOST" != "$HOSTNAME_WANT" ]; then
    echo "  setting LocalHostName to $HOSTNAME_WANT (asks for your password)"
    sudo scutil --set LocalHostName "$HOSTNAME_WANT" && CUR_HOST="$HOSTNAME_WANT"
fi
echo "  this Mac is $CUR_HOST.local"
[ "$CUR_HOST" = "$HOSTNAME_WANT" ] || warn "Bonjour name is $CUR_HOST, not $HOSTNAME_WANT. The launcher labels stations by name; use --set-hostname, or other stations need --hq-host $CUR_HOST.local"

# ── repos ────────────────────────────────────────────────────────────────────
repo_branch() {
    local r="$1" o
    for o in $OVERRIDES; do [ "${o%%=*}" = "$r" ] && { echo "${o#*=}"; return; }; done
    awk -v r="$r" '!/^[[:space:]]*(#|$)/ && $1==r {print $2}' "$KIT/repos.conf"
}
step "Repos -> $SRC"
mkdir -p "$SRC"
ROWS=""
if [ "$SKIP_CLONE" -eq 0 ]; then
    if ! gh auth status >/dev/null 2>&1; then
        warn "gh is not logged in: private repos will fail. Run: gh auth login && gh auth setup-git"
    else
        gh auth setup-git >/dev/null 2>&1 || true
    fi
fi
for r in $REPOS; do
    br=$(repo_branch "$r"); br="${br:-main}"
    d="$SRC/$r"
    note=""
    if [ "$SKIP_CLONE" -eq 1 ]; then
        note="skipped"
    else
        url="https://github.com/$GH_OWNER/$r.git"
        if ! git ls-remote --exit-code --heads "$url" "$br" >/dev/null 2>&1; then
            def=$(git ls-remote --symref "$url" HEAD 2>/dev/null | awk '/^ref:/ {sub("refs/heads/","",$2); print $2}')
            if [ -z "$def" ]; then
                warn "$r: no access to $url"; ROWS="$ROWS$r|$br|-|no access\n"; continue
            fi
            warn "$r: branch $br not on GitHub, using $def"; note="fallback from $br"; br="$def"
        fi
        if [ -d "$d/.git" ]; then
            if [ -n "$(git -C "$d" status --porcelain --untracked-files=no)" ]; then
                note="${note:+$note; }local changes kept"
            else
                git -C "$d" fetch --quiet origin "+refs/heads/$br:refs/remotes/origin/$br" \
                    && git -C "$d" checkout --quiet -B "$br" "origin/$br" || note="update failed"
            fi
        else
            git clone --quiet --branch "$br" "$url" "$d" || { ROWS="$ROWS$r|$br|-|clone failed\n"; continue; }
        fi
    fi
    [ -f "$d/scripts/run-laptop.sh" ] || { warn "$r: no scripts/run-laptop.sh yet"; note="${note:+$note; }no run-laptop.sh"; }
    ROWS="$ROWS$r|$br|$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo -)|${note:-ok}\n"
done

# ── MQTT login ───────────────────────────────────────────────────────────────
step "MQTT login for $HUB"
MQTT_USER=isrkit
MQTT_PASS=""
if [ -f "$STATION_ENV" ]; then
    MQTT_PASS=$(sed -n 's/^MQTT_PASS=//p' "$STATION_ENV" | head -1)
fi
if [ -n "$FROM_PI" ]; then
    echo "  fetching from $FROM_PI (ssh; sudo may ask for the Pi user's password)"
    got=$(ssh -o ConnectTimeout=8 -t "$FROM_PI" 'sudo cat /etc/isr-kit/mqtt.env' 2>/dev/null | tr -d '\r' | sed -n 's/^MQTT_PASS=//p' | head -1)
    if [ -z "$SENSOR" ]; then   # site.env is 0644, no sudo needed
        site=$(ssh -o ConnectTimeout=8 "$FROM_PI" 'cat /etc/isr-kit/site.env' 2>/dev/null | tr -d '\r')
        slat=$(printf '%s\n' "$site" | sed -n 's/^SENSOR_LAT=//p' | head -1)
        slon=$(printf '%s\n' "$site" | sed -n 's/^SENSOR_LON=//p' | head -1)
        [ -n "$slat" ] && [ -n "$slon" ] && SENSOR="$slat,$slon" && echo "  sensor position from the Pi: $SENSOR"
    fi
    [ -n "$got" ] && MQTT_PASS="$got" || warn "could not read /etc/isr-kit/mqtt.env from $FROM_PI"
    # The Pi has no time source offline: set it from this Mac if it's off by > 2 s (key login needed).
    "$HERE/pi-clock.py" "$FROM_PI" --source "setup-mac $ROLE" | sed 's/^/  /' \
        || warn "couldn't check the Pi's clock (needs key ssh: ssh-copy-id $FROM_PI); start-mac/hq-tunnel/push-to-pi also set it"
elif [ -n "$PASS_FILE" ]; then
    MQTT_PASS=$(sed -n 's/^MQTT_PASS=//p' "$PASS_FILE" | head -1)
    [ -n "$MQTT_PASS" ] || MQTT_PASS=$(head -1 "$PASS_FILE" | tr -d '[:space:]')
fi
if [ -z "$MQTT_PASS" ] && [ -t 0 ]; then
    printf "  MQTT password for isrkit (on the Pi: sudo cat /etc/isr-kit/mqtt.env), hidden: "
    IFS= read -rs MQTT_PASS; echo
fi
[ -n "$MQTT_PASS" ] || warn "no MQTT password: tools will not reach the meshbus/heartbeats"
case "$MQTT_PASS" in *[!A-Za-z0-9_-]*) die "MQTT password has unexpected characters" ;; esac

GEMINI_KEY=""
if [ -n "$GEMINI_FILE" ]; then
    GEMINI_KEY=$(head -1 "$GEMINI_FILE" | tr -d '[:space:]')
    case "$GEMINI_KEY" in *[!A-Za-z0-9_-]*) die "Gemini key has unexpected characters" ;; esac
elif [ -f "$STATION_ENV" ]; then
    GEMINI_KEY=$(sed -n 's/^GEMINI_API_KEY=//p' "$STATION_ENV" | head -1)
fi

# ── sensor position (contract v4 C4.2) ───────────────────────────────────────
SLAT=""; SLON=""
if [ -z "$SENSOR" ] && [ -f "$STATION_ENV" ]; then
    SLAT=$(sed -n 's/^SENSOR_LAT=//p' "$STATION_ENV" | head -1); SLON=$(sed -n 's/^SENSOR_LON=//p' "$STATION_ENV" | head -1)
elif [ -n "$SENSOR" ]; then
    SLAT="${SENSOR%%,*}"; SLON="${SENSOR#*,}"
    printf '%s %s' "$SLAT" "$SLON" | grep -Eq '^-?[0-9]{1,3}(\.[0-9]{1,8})? -?[0-9]{1,3}(\.[0-9]{1,8})?$' \
        || die "--sensor must be LAT,LON in decimal degrees"
    [ "$SLAT" = "0" ] && [ "$SLON" = "0" ] && die "--sensor 0,0 is not allowed"
fi
[ -n "$SLAT" ] || warn "no sensor position: tools won't place position-bearing CoT. Use --sensor LAT,LON or --from-pi after sudo isr-set-site on the Pi"

# ── station.env ──────────────────────────────────────────────────────────────
step "Writing $STATION_ENV"
if [ "$ROLE" = "hq" ]; then OLLAMA_BASE="http://127.0.0.1:11434"; else OLLAMA_BASE="http://$HQ_HOST:11434"; fi
# The OP (or drone-ops) Mac runs its own VLM (op-sentry, fpv-vision use it); the LLM stays on HQ (tac-advisor).
VLM_BASE="$OLLAMA_BASE"
if [ "$OWN_VLM" -eq 1 ] && [ "$SKIP_OLLAMA" -eq 0 ]; then VLM_BASE="http://127.0.0.1:11434"; fi
# Tunnel ports: this station's tools, from services.conf (repo column -> port)
TUNNEL_PORTS=""
for r in $REPOS; do
    p=$(awk -v r="$r" '!/^[[:space:]]*#/ && $4 == r && $2 ~ /^[0-9]+$/ {print $2; exit}' "$KIT/services.conf" 2>/dev/null)
    [ -n "$p" ] && TUNNEL_PORTS="$TUNNEL_PORTS $p"
done
TUNNEL_PORTS="${TUNNEL_PORTS# }"
# fpv-vision's footage folders (only when this Mac runs fpv-vision)
FPV_LINE=""
case " $REPOS " in
    *" fpv-vision "*) if [ -n "$FPV_FOOTAGE" ]; then FPV_LINE="FPV_FOOTAGE_DIRS=$FPV_FOOTAGE"
                      else FPV_LINE="# FPV_FOOTAGE_DIRS=~/Desktop/ISR-demo/fpv   (fpv-vision's clips; default ~/Movies/FPV)"; fi ;;
esac
mkdir -p "$CONF_DIR"; chmod 700 "$CONF_DIR"
umask 077
tmp=$(mktemp "$CONF_DIR/.station.env.XXXXXX")
cat > "$tmp" <<EOF
# ISTAR kit station config, written by laptop/setup-mac.sh. Sourced by laptop/start-mac.sh.
ISR_STATION=$ROLE
ISR_HOST=$(printf '%s' "$CUR_HOST" | tr '[:upper:]' '[:lower:]')
SENSOR_LAT=$SLAT
SENSOR_LON=$SLON
ISR_REPOS="$REPOS"
ISR_SRC=$SRC
ISR_HUB=$HUB
TAK_HOST=$HUB
TAK_PORT=8087
MQTT_HOST=$HUB
MQTT_PORT=1883
MQTT_USER=$MQTT_USER
MQTT_PASS=$MQTT_PASS
# Ollama: bare base URL; tools add /v1/... or /api/... (the LLM is on HQ; the OP Mac has its own VLM)
LLM_URL=$OLLAMA_BASE
LLM_MODEL=$LLM_MODEL
VLM_URL=$VLM_BASE
VLM_MODEL=$VLM_MODEL
LLM_BACKEND=ollama
# Tunnelled HQ (laptop/hq-tunnel.sh). STATION_PUBLIC_BASE: start-mac.sh sets each tool's
# ISR_PUBLIC_URL=<base>:<port> so its heartbeat link uses the Pi's address, not this Mac's name.
ISR_TUNNEL=$TUNNEL
ISR_TUNNEL_PI=$TUNNEL_PI
ISR_TUNNEL_PORTS="$TUNNEL_PORTS"
STATION_PUBLIC_BASE=$([ "$TUNNEL" -eq 1 ] && echo "http://$HUB_IP")
# op-sentry source. Split mode (the Pi runs op-sentry-stream): the Pi's camera stream. Unset: start-mac.sh uses the stream
# when the Pi's profile runs op-sentry-stream, else op-sentry's own default (newest Insta360 file, or sim).
$([ "$OP_SPLIT" -eq 1 ] && echo "OPSENTRY_SOURCE=http://$HUB:8120/raw.mjpg" || echo "# OPSENTRY_SOURCE=http://$HUB:8120/raw.mjpg")
$FPV_LINE
# Demo style (contract v6): unset = follow the Pi's profile (start-mac.sh asks the launcher); on_demand
# or continuous here pins it for every tool on this Mac.
# DEMO_MODE=on_demand
EOF
[ -n "$GEMINI_KEY" ] && echo "GEMINI_API_KEY=$GEMINI_KEY" >> "$tmp"
chmod 600 "$tmp"; mv -f "$tmp" "$STATION_ENV"
umask 022
unset MQTT_PASS GEMINI_KEY
echo "  ok (mode 600). Per-tool overrides go in $CONF_DIR/<repo>.env"

# ── Ollama (HQ) ──────────────────────────────────────────────────────────────
# Tunnelled HQ: Ollama stays on this Mac's loopback; the tunnel carries it to the Pi.
# Drone-ops: its own VLM, for its own tools only, so loopback too.
if [ "$TUNNEL" -eq 1 ] || [ "$OWN_VLM" -eq 1 ]; then OLLAMA_LISTEN="127.0.0.1:11434"; else OLLAMA_LISTEN="0.0.0.0:11434"; fi
if [ "$OWN_VLM" -eq 1 ]; then OLLAMA_MODELS="$VLM_MODEL"; MAX_LOADED=1; else OLLAMA_MODELS="$LLM_MODEL $VLM_MODEL"; MAX_LOADED=2; fi
# True when $HOME is this account's real home (a test or dry run sets a fake HOME). brew services,
# launchctl and Ollama act on the real login session whatever HOME says, so never touch them then.
real_home() { [ "${ISR_ALLOW_SERVICE_CHANGES:-0}" = "1" ] && return 0
    local h; h=$(eval echo "~$(id -un)" 2>/dev/null); [ -n "$h" ] && [ "$(cd "$HOME" 2>/dev/null && pwd -P)" = "$(cd "$h" 2>/dev/null && pwd -P)" ]; }

setup_ollama() {
    if ! real_home; then
        warn "HOME ($HOME) isn't this account's home: leaving Ollama, brew services and launchctl alone"
        return
    fi
    local why=" for the Pi and the drone-ops Mac"
    [ "$TUNNEL" -eq 1 ] && why=", reaches the Pi through the tunnel"
    [ "$OWN_VLM" -eq 1 ] && why=", VLM only, for this Mac's tools"
    step "Ollama (listening on $OLLAMA_LISTEN$why)"
    local plist="$HOME/Library/LaunchAgents/com.isrkit.ollama.plist" app=0 bin
    [ -d /Applications/Ollama.app ] && app=1
    if [ "$app" -eq 0 ] && ! command -v ollama >/dev/null 2>&1; then
        echo "  installing ollama (brew)"
        brew install --quiet ollama || { warn "brew install ollama failed"; return; }
    fi
    if [ "$app" -eq 1 ]; then
        # The menu-bar app reads launchctl env. Set it now and on every login.
        launchctl setenv OLLAMA_HOST $OLLAMA_LISTEN
        launchctl setenv OLLAMA_KEEP_ALIVE 24h
        launchctl setenv OLLAMA_MAX_LOADED_MODELS "$MAX_LOADED"
        launchctl setenv OLLAMA_CONTEXT_LENGTH "$CTX"
        cat > "$HOME/Library/LaunchAgents/com.isrkit.ollama-env.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.isrkit.ollama-env</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>-c</string>
    <string>launchctl setenv OLLAMA_HOST $OLLAMA_LISTEN; launchctl setenv OLLAMA_KEEP_ALIVE 24h; launchctl setenv OLLAMA_MAX_LOADED_MODELS $MAX_LOADED; launchctl setenv OLLAMA_CONTEXT_LENGTH $CTX</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
        launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.isrkit.ollama-env.plist" 2>/dev/null || true
        echo "  restarting Ollama.app so it picks up OLLAMA_HOST"
        osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
        sleep 2
        open -a Ollama
    else
        bin=$(command -v ollama)
        brew services stop ollama >/dev/null 2>&1 || true   # we run our own agent with the right env
        cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.isrkit.ollama</string>
  <key>ProgramArguments</key><array><string>$bin</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>OLLAMA_HOST</key><string>$OLLAMA_LISTEN</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>24h</string>
    <key>OLLAMA_MAX_LOADED_MODELS</key><string>$MAX_LOADED</string>
    <key>OLLAMA_CONTEXT_LENGTH</key><string>$CTX</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/isrkit-ollama.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/isrkit-ollama.log</string>
</dict></plist>
EOF
        launchctl bootout "gui/$(id -u)/com.isrkit.ollama" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$plist" || warn "could not load $plist"
    fi

    for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
    curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 || { warn "Ollama not answering on 11434"; return; }
    if lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null | grep -Eq '\*:11434|0\.0\.0\.0:11434'; then
        echo "  listening on all interfaces"
        [ "${OLLAMA_LISTEN%%:*}" = "127.0.0.1" ] && warn "Ollama listens on all interfaces, but this setup keeps it on 127.0.0.1 (quit Ollama.app and re-run)"
    elif [ "${OLLAMA_LISTEN%%:*}" = "127.0.0.1" ]; then
        echo "  on 127.0.0.1 only (as intended)"
    else
        warn "Ollama is only on localhost: the Pi and drone-ops Mac can't reach it (quit Ollama.app, re-run)"
    fi
    local m
    for m in $OLLAMA_MODELS; do
        if curl -fsS http://127.0.0.1:11434/api/tags | grep -q "\"name\":\"$m\""; then
            echo "  model $m: present"
        else
            echo "  pulling $m (large download; do this on good internet, not on demo day)"
            ollama pull "$m" || warn "ollama pull $m failed (check the tag on ollama.com/library)"
        fi
    done
    echo "  warming models (keep_alive 24h)"
    for m in $OLLAMA_MODELS; do
        curl -fsS http://127.0.0.1:11434/api/generate -d "{\"model\":\"$m\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":\"24h\",\"options\":{\"num_predict\":1}}" \
            >/dev/null 2>&1 || warn "warm-up of $m failed"
    done
    # Verify both stay loaded and fully on the GPU (a CPU split means memory is too tight).
    if command -v ollama >/dev/null 2>&1; then
        local ps
        ps=$(ollama ps 2>/dev/null)
        printf '%s\n' "$ps" | sed 's/^/  /'
        for m in $OLLAMA_MODELS; do
            printf '%s\n' "$ps" | grep -q "^$m " || warn "$m is not loaded after warm-up (OLLAMA_MAX_LOADED_MODELS?)"
        done
        if printf '%s\n' "$ps" | grep -q 'CPU'; then
            warn "a model is partly on the CPU (see ollama ps): memory is tight. Use --llm-model qwen3.5:9b, or close other apps."
        fi
    fi
}
if [ "$SKIP_OLLAMA" -eq 0 ]; then setup_ollama; fi

# ── downloads for offline use (each repo's --setup-only) ─────────────────────
# pip packages, model weights (YOLO/YOLOE, CLIP), sample clips, Ollama models a tool pulls itself.
# Needs normal internet: the hotspot has none. Runs with station.env like start-mac.sh does.
prefetch() {
    step "Downloads for offline use (scripts/run-laptop.sh --setup-only)"
    if [ "$SKIP_PREFETCH" -eq 1 ]; then echo "  skipped (--skip-prefetch)"; return; fi
    if ! curl -fsS --max-time 5 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null; then
        warn "no internet (pypi.org unreachable): run this again on normal internet before the demo, or the tools will try to download on the hotspot"
        return
    fi
    local r d
    for r in $REPOS; do
        d="$SRC/$r"
        if [ ! -f "$d/scripts/run-laptop.sh" ]; then continue; fi
        if ! grep -q -- "--setup-only" "$d/scripts/run-laptop.sh"; then
            warn "$r: its run-laptop.sh has no --setup-only yet. Start it once on normal internet (laptop/start-mac.sh start, wait for its heartbeat, stop) so it downloads its weights"
            continue
        fi
        echo "  $r: downloading (first time: several minutes, PyTorch is large)"
        # shellcheck disable=SC1090
        if ( cd "$d" && set -a && . "$STATION_ENV" && { [ -f "$CONF_DIR/$r.env" ] && . "$CONF_DIR/$r.env" || true; } && set +a \
             && bash scripts/run-laptop.sh --setup-only ) > "$CONF_DIR/setup-$r.log" 2>&1; then
            echo "  $r: ready for offline use"
        else
            warn "$r: --setup-only failed (log: $CONF_DIR/setup-$r.log)"
        fi
    done
}
prefetch

# ── camera / microphone permission (TCC) ─────────────────────────────────────
TERM_APP="${TERM_PROGRAM:-unknown}"
case "$TERM_APP" in Apple_Terminal|iTerm.app) IN_TERMINAL=1 ;; *) IN_TERMINAL=0 ;; esac
if [ "$IN_TERMINAL" -eq 0 ]; then
    warn "not running in Terminal.app or iTerm (TERM_PROGRAM=$TERM_APP). macOS silently denies camera and mic access to tools started from an IDE or agent shell. Run laptop/start-mac.sh from Terminal.app."
fi
if [ "$OWN_VLM" -eq 1 ] && [ "$SKIP_CAM" -eq 0 ]; then
    step "Camera permission probe (triggers the macOS prompt for $TERM_APP now, not on demo day)"
    if [ "$IN_TERMINAL" -eq 0 ]; then
        echo "  skipped: re-run this script from Terminal.app to get the Camera prompt"
    elif ! command -v ffmpeg >/dev/null 2>&1; then
        warn "ffmpeg not found; skipping the camera probe"
    else
        probe="$(mktemp -d)/probe.jpg"
        echo "  grabbing one frame from camera 0. If macOS asks, click Allow for Terminal."
        if ffmpeg -hide_banner -loglevel error -f avfoundation -framerate 30 -video_size 1280x720 -i "0:none" \
                -frames:v 1 -y "$probe" </dev/null 2>"$probe.log" && [ -s "$probe" ]; then
            echo "  camera access OK"
        else
            warn "camera probe failed ($(tail -1 "$probe.log" 2>/dev/null)). Check System Settings > Privacy & Security > Camera > $TERM_APP is on, then quit and reopen Terminal fully (Cmd-Q)."
        fi
        rm -rf "$(dirname "$probe")"
    fi
fi

# ── reachability ─────────────────────────────────────────────────────────────
step "Checking the hub ($HUB)"
SSID=$(ipconfig getsummary en0 2>/dev/null | awk -F' : ' '/ SSID/ {print $2; exit}')
echo "  Wi-Fi: ${SSID:-unknown} (should be BFT-TAK)"
if ping -c1 -t3 "$HUB" >/dev/null 2>&1; then echo "  $HUB answers ping"; else warn "$HUB not reachable: join the BFT-TAK hotspot"; fi
for p in 8087 1883 8050; do
    if nc -z -G 3 "$HUB" "$p" >/dev/null 2>&1; then echo "  port $p open"; else warn "$HUB:$p not reachable"; fi
done
if [ -f "$STATION_ENV" ]; then
    MQTT_PASS=$(sed -n 's/^MQTT_PASS=//p' "$STATION_ENV")
    if [ -n "$MQTT_PASS" ]; then
        res=$(MQTT_PASS="$MQTT_PASS" /usr/bin/python3 -c '
import os, socket, sys
# MQTT 3.1.1 CONNECT with user/pass; CONNACK return code 0 = ok. Stdlib only (no kit modules).
def s(b): return len(b).to_bytes(2, "big") + b
u, p = b"isrkit", os.environ["MQTT_PASS"].encode()
var = s(b"MQTT") + bytes([4, 0xC2]) + (30).to_bytes(2, "big")
pay = s(b"isrkit-setup-check") + s(u) + s(p)
body = var + pay
try:
    c = socket.create_connection((sys.argv[1], 1883), timeout=5)
    n, rl = len(body), b""
    while True:
        n, d = divmod(n, 128)
        rl += bytes([d | (128 if n else 0)])
        if not n: break
    c.sendall(b"\x10" + rl + body)
    r = c.recv(4)
    c.close()
except OSError as e:
    print(f"cannot connect: {e}"); sys.exit()
codes = {1: "bad protocol", 2: "client id rejected", 3: "broker unavailable", 4: "bad user/password", 5: "not authorized"}
print("ok" if len(r) == 4 and r[0] == 0x20 and r[3] == 0 else codes.get(r[3] if len(r) == 4 else -1, f"unexpected reply {r!r}"))
' "$HUB" 2>&1)
        [ "$res" = "ok" ] && echo "  MQTT login as isrkit: ok" || warn "MQTT login failed: $res"
    fi
    unset MQTT_PASS
fi
if [ "$OWN_VLM" -eq 1 ] && [ "$SKIP_OLLAMA" -eq 1 ]; then
    curl -fsS --max-time 3 "http://$HQ_HOST:11434/api/version" >/dev/null 2>&1 \
        && echo "  HQ Ollama at $HQ_HOST: ok" || warn "HQ Ollama at $HQ_HOST:11434 not reachable (a tunnelled HQ keeps it on 127.0.0.1: drop --skip-ollama to run a VLM here)"
fi

# ── summary ──────────────────────────────────────────────────────────────────
step "Summary ($ROLE station, $CUR_HOST.local)"
printf "  %-18s %-24s %-9s %s\n" REPO BRANCH COMMIT NOTE
printf '%b' "$ROWS" | awk -F'|' 'NF {printf "  %-18s %-24s %-9s %s\n", $1, $2, $3, $4}'
[ "$SKIP_OLLAMA" -eq 0 ] && echo "  Ollama: $(echo "$OLLAMA_MODELS" | sed 's/ /, /') on $OLLAMA_LISTEN"
if [ "$TUNNEL" -eq 1 ]; then
    echo "  Tunnel: $TUNNEL_PI, forwards 11434 $TUNNEL_PORTS (start-mac.sh start runs laptop/hq-tunnel.sh)"
    echo "          needs key login: ssh-copy-id $TUNNEL_PI   On the Pi: sudo ./bootstrap.sh --hq-tunnel (or set"
    echo "          LLM_URL/VLM_URL=http://127.0.0.1:11434 in /etc/isr-launcher.env and /etc/tac-advisor.env)"
fi
echo "  Config: $STATION_ENV"
echo "  Next:   laptop/start-mac.sh start      (stop | status | logs <repo>)"
echo "  macOS firewall: allow incoming connections for python3 and ollama when asked."
echo "  Launch start-mac.sh from Terminal.app (or iTerm), not an IDE/agent shell. On first run allow"
echo "  Camera (OP Mac: op-sentry; drone-ops: fpv-vision, op-sentry) and Microphone (if acoustic runs on a Mac) for Terminal."
echo "  'not authorized' camera errors: System Settings > Privacy & Security > Camera > Terminal on, then Cmd-Q Terminal and reopen."
[ -n "$WARN" ] && { echo ""; echo "  Warnings:"; printf '%b\n' "$WARN"; }
exit 0
