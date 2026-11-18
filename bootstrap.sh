#!/usr/bin/env bash
# ISTAR demo kit: one-shot, idempotent installer for a fresh Raspberry Pi OS Bookworm
# (64-bit) on a Pi 5 booting from SSD.
#
#   git clone https://github.com/Jacob-Haynes/pi-profiles.git && cd pi-profiles
#   sudo ./bootstrap.sh                       # everything
#   sudo ./bootstrap.sh --only fpv-vision,op-sentry
#   sudo ./bootstrap.sh --branch fpv-vision=main --branch op-sentry=main
#   sudo ./bootstrap.sh --token-file /root/.isr-gh-token     # private repos, no gh login
#
# Options:
#   --only a,b,c        install only these repos (the platform itself is always installed)
#   --branch repo=br    override the branch from repos.conf (repeatable)
#   --token-file PATH   GitHub token (fine-grained, contents:read) for the private repos
#   --skip-apt          don't apt-get install base packages
#   --skip-platform     don't (re)install pi-profiles itself
#   --tune-ssd          2 GB swap on the SSD (journald is always set persistent + capped)
#   --src-dir DIR       where source checkouts live (default /opt/isr-kit/src)
#   --keep-network      don't touch NetworkManager wired profiles (see bin/isr-wired-setup)
#   --wired-dhcp        wired Ethernet uses DHCP (router cable) instead of the default pure
#                       link-local (direct cable to the HQ Mac)
#   --offline           force offline pip/uv (no index, no retries); auto when pypi is unreachable
#   --online            never switch to offline pip/uv, even if pypi looks unreachable
#   --sensor LAT,LON    OP / sensor position for every tool (contract v4; also: sudo isr-set-site)
#   --with-mediamtx     check/enable the mediamtx video relay (RTMP 1935 in, RTSP 8554 out) for
#                       op-sentry's best-effort live 360 path. It does not download anything:
#                       OpenTAKServer's installer provides mediamtx. Off by default.
#   --hq-tunnel         tunnelled HQ (managed Mac): point the launcher and tools at Ollama on this
#                       Pi's 127.0.0.1:11434, carried by laptop/hq-tunnel.sh (bin/isr-hq-mode tunnel)
#   --time-server       also serve this Pi's time to hotspot clients with chrony (local stratum 10),
#                       for laptops and Linux devices; phones keep their own (cellular/NITZ) time.
#                       Installs chrony if apt can (it replaces systemd-timesyncd). Off by default.
#   --rtc-trickle       enable trickle charging of the Pi 5 RTC battery (dtparam=rtc_bbat_vchg=3000000
#                       in config.txt). ONLY with the rechargeable ML-2020 cell; never a CR2032.
#   --hotspot-2ghz      keep the BFT-TAK hotspot on 2.4 GHz ch7 (phones or radios without 5 GHz).
#                       Default: 5 GHz ch36 (Wi-Fi country GB unless one is set). Saved for re-runs;
#                       --hotspot-5ghz switches back. Applies at the next reboot (isr-hotspot-band --apply).
#   --ssh               clone over SSH (git@github.com:...) using the Pi user's SSH or deploy keys
#   --dry-run           show what would be done
#
# Private repo access (pick ONE, never commit a token):
#   a) gh login as the Pi user:  gh auth login && gh auth setup-git   then  sudo ./bootstrap.sh
#      (git runs as $SUDO_USER and uses that user's gh credential helper)
#   b) token file: put a fine-grained token with read-only "Contents" on the private repos
#      in a root-owned 600 file and pass --token-file. It is fed to git via GIT_ASKPASS
#      from the environment, never in a URL, .git/config, argv or the log.
#
# Log: /var/log/isr-kit-install.log
set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
GH_OWNER="${ISR_GH_OWNER:-Jacob-Haynes}"
SRC_DIR="/opt/isr-kit/src"
LOG="${ISR_INSTALL_LOG:-/var/log/isr-kit-install.log}"
REPOS_CONF="$SELF_DIR/repos.conf"
ONLY=""
OVERRIDES=""
TOKEN_FILE=""
SKIP_APT=0
SKIP_PLATFORM=0
TUNE_SSD=0
DRY=0
URL_BASE="https://github.com/"
WITH_MEDIAMTX=0
SENSOR=""
KEEP_NET=0
WIRED_MODE=""
OFFLINE=auto

BASE_PACKAGES="git curl ca-certificates jq python3 python3-venv python3-pip usbutils psmisc \
iw rfkill wireless-tools alsa-utils v4l-utils network-manager rsync avahi-daemon avahi-utils \
iproute2 bluez"

die() { echo "ERROR: $*" >&2; exit 1; }
HQ_TUNNEL=0; TIME_SERVER=0; RTC_TRICKLE=0; HOTSPOT_BAND=""

while [ $# -gt 0 ]; do
    case "$1" in
        --only) ONLY="${2:?}"; shift 2 ;;
        --only=*) ONLY="${1#*=}"; shift ;;
        --branch) OVERRIDES="$OVERRIDES ${2:?}"; shift 2 ;;
        --branch=*) OVERRIDES="$OVERRIDES ${1#*=}"; shift ;;
        --token-file) TOKEN_FILE="${2:?}"; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --skip-platform) SKIP_PLATFORM=1; shift ;;
        --tune-ssd) TUNE_SSD=1; shift ;;
        --src-dir) SRC_DIR="${2:?}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --ssh) URL_BASE="git@github.com:"; shift ;;
        --with-mediamtx) WITH_MEDIAMTX=1; shift ;;
        --sensor) SENSOR="${2:?}"; shift 2 ;;
        --keep-network) KEEP_NET=1; shift ;;
        --wired-dhcp) WIRED_MODE="--dhcp"; shift ;;
        --offline) OFFLINE=1; shift ;;
        --online) OFFLINE=0; shift ;;
        --hq-tunnel) HQ_TUNNEL=1; shift ;;
        --time-server) TIME_SERVER=1; shift ;;
        --rtc-trickle) RTC_TRICKLE=1; shift ;;
        --hotspot-2ghz) HOTSPOT_BAND=2.4; shift ;;
        --hotspot-5ghz) HOTSPOT_BAND=5; shift ;;
        -h|--help) sed -n '2,48p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || [ "$DRY" -eq 1 ] || die "run as root: sudo ./bootstrap.sh"
[ -f "$REPOS_CONF" ] || die "repos.conf not found next to bootstrap.sh"

RUN_USER="${SUDO_USER:-root}"

if [ "$DRY" -eq 0 ]; then
    touch "$LOG" && chmod 640 "$LOG"
    exec > >(tee -a "$LOG") 2>&1
fi
echo ""
echo "=================================================================="
echo " ISTAR kit bootstrap  $(date -u +%Y-%m-%dT%H:%M:%SZ)  user=$RUN_USER  dry-run=$DRY"
echo "=================================================================="

run() {   # run <cmd...>  (honours --dry-run)
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; return 0; fi
    "$@"
}

# ── 1. preflight (warn, never fail) ─────────────────────────────────────────
WARNINGS=""
warn() { echo "WARNING: $*"; WARNINGS="$WARNINGS\n  - $*"; }

preflight() {
    echo "[1/7] Preflight"
    local model rootsrc
    model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
    echo "  model:  $model"
    case "$model" in *"Raspberry Pi 5"*) ;; *) warn "not a Raspberry Pi 5 ($model). CPU budgets in profiles.d assume a Pi 5." ;; esac
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        echo "  os:     ${PRETTY_NAME:-?}"
        [ "${VERSION_CODENAME:-}" = "bookworm" ] || warn "expected Bookworm, found ${VERSION_CODENAME:-unknown}"
    fi
    [ "$(uname -m)" = "aarch64" ] || warn "expected 64-bit (aarch64), found $(uname -m)"
    rootsrc=$(findmnt -n -o SOURCE / 2>/dev/null || echo "?")
    echo "  root:   $rootsrc"
    case "$rootsrc" in
        /dev/nvme*|/dev/sd*) echo "  boot:   SSD" ;;
        /dev/mmcblk*) warn "root filesystem is on the SD card ($rootsrc). See docs/SSD-TIPS.md for SSD boot." ;;
        *) warn "could not tell what the root device is ($rootsrc)" ;;
    esac
    if command -v rpi-eeprom-config >/dev/null 2>&1; then
        echo "  eeprom: $(rpi-eeprom-config 2>/dev/null | grep -E '^BOOT_ORDER' || echo 'BOOT_ORDER default')"
    fi
    # Power: Pi 5 caps the USB ports at 600 mA unless the PSU negotiated 5 A.
    local pdt=/proc/device-tree/chosen/power max_ma thr
    if [ -r "$pdt/max_current" ]; then
        max_ma=$(od -An -tu4 --endian=big -N4 "$pdt/max_current" 2>/dev/null | tr -d ' ')
        echo "  power:  PSU negotiated ${max_ma:-?} mA"
        if [ -n "$max_ma" ] && [ "$max_ma" -lt 5000 ]; then
            warn "USB CURRENT-LIMITED: PSU gives ${max_ma} mA, not 5000. USB ports are capped at 600 mA total, so the mic, capture card or radio may drop out. Use the official 27 W PSU (a 65 W laptop charger or Mac port usually only offers 3 A at 5 V)."
        fi
    fi
    if command -v vcgencmd >/dev/null 2>&1; then
        thr=$(vcgencmd get_throttled 2>/dev/null | sed 's/.*=//')
        echo "  throttled: ${thr:-?}"
        case "$thr" in 0x0|"") ;; *) warn "vcgencmd get_throttled=$thr (under-voltage or throttling seen since boot; check PSU and cooling)" ;; esac
    fi
    # Offline Pi (no uplink): git can come from laptop/push-to-pi.sh mirrors, but pip can't fetch
    # dependencies that aren't already installed in /opt/<repo>/.venv.
    if [ "$OFFLINE" = "auto" ]; then
        if curl -s -m 6 -o /dev/null https://pypi.org/simple/ 2>/dev/null; then OFFLINE=0; else OFFLINE=1; fi
    fi
    if [ "$OFFLINE" -eq 1 ]; then
        warn "OFFLINE: repos must come from local mirrors (laptop/push-to-pi.sh sets git insteadOf). pip/uv run with no index and no retries, so installed requirements pass instantly and NEW Python dependencies fail fast (push-to-pi.sh lists them). Use --skip-apt."
    fi
    local free_gb
    free_gb=$(df -Pk / | awk 'NR==2 {printf "%d", $4/1048576}')
    echo "  disk:   ${free_gb} GB free on /"
    [ "${free_gb:-0}" -ge 8 ] || warn "less than 8 GB free on / (YOLO models and OTS need room)"
}

# ── 2. base packages ─────────────────────────────────────────────────────────
base_packages() {
    echo "[2/7] Base packages"
    if [ "$SKIP_APT" -eq 1 ]; then echo "  skipped (--skip-apt)"; return 0; fi
    if [ "$OFFLINE" -eq 1 ]; then echo "  skipped (offline: apt can't reach its mirrors)"; return 0; fi
    export DEBIAN_FRONTEND=noninteractive
    run apt-get update -qq || warn "apt-get update failed (offline?)"
    # shellcheck disable=SC2086
    run apt-get install -y -qq $BASE_PACKAGES || warn "some base packages failed to install"
    if ! command -v gh >/dev/null 2>&1; then
        run apt-get install -y -qq gh >/dev/null 2>&1 || echo "  gh not in apt; private repos will need --token-file"
    fi
}

# ── repos.conf handling ──────────────────────────────────────────────────────
all_repos() { awk '!/^[[:space:]]*(#|$)/ && $4 != "mac" {print $1}' "$REPOS_CONF"; }   # Mac-only repos: setup-mac.sh
repo_branch() {
    local repo="$1" o
    for o in $OVERRIDES; do
        [ "${o%%=*}" = "$repo" ] && { echo "${o#*=}"; return; }
    done
    awk -v r="$repo" '!/^[[:space:]]*(#|$)/ && $1==r {print $2}' "$REPOS_CONF"
}
selected() {
    [ -z "$ONLY" ] && return 0
    case ",$ONLY," in *",$1,"*) return 0 ;; esac
    return 1
}
valid_ref() { case "$1" in ''|-*|*[!A-Za-z0-9._/-]*) return 1 ;; esac; return 0; }

# ── git with optional token (never written anywhere) ─────────────────────────
ASKPASS=""
setup_token() {
    [ -n "$TOKEN_FILE" ] || return 0
    [ -r "$TOKEN_FILE" ] || die "token file $TOKEN_FILE not readable"
    local mode
    mode=$(stat -c %a "$TOKEN_FILE" 2>/dev/null || echo "?")
    case "$mode" in 600|400) ;; *) warn "token file mode is $mode; use chmod 600" ;; esac
    ISR_GH_TOKEN=$(head -1 "$TOKEN_FILE" | tr -d '[:space:]')
    export ISR_GH_TOKEN
    ASKPASS=$(mktemp -d)/askpass
    cat > "$ASKPASS" <<'EOF'
#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  *) printf '%s\n' "$ISR_GH_TOKEN" ;;
esac
EOF
    chmod 700 "$ASKPASS"
    CLEANUP_ASKPASS=1
}

git_as() {   # git_as <args...>: as root with askpass (token mode) or as the invoking user
    if [ -n "$ASKPASS" ]; then
        GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git -c credential.helper= "$@"
    elif [ "$RUN_USER" != "root" ]; then
        sudo -u "$RUN_USER" -H env GIT_TERMINAL_PROMPT=0 git "$@"
    else
        GIT_TERMINAL_PROMPT=0 git "$@"
    fi
}

# Results table rows: repo|branch|commit|code|install|env|note
ROWS=""
add_row() { ROWS="$ROWS$1|$2|$3|$4|$5|$6|$7
"; }

sync_repo() {   # sets SYNC_BRANCH SYNC_COMMIT SYNC_NOTE; returns non-zero on failure
    local repo="$1" want="$2" dir="$SRC_DIR/$1" url="$URL_BASE$GH_OWNER/$1.git" br
    SYNC_NOTE=""; SYNC_BRANCH="$want"; SYNC_COMMIT="-"
    valid_ref "$want" || { SYNC_NOTE="invalid branch '$want'"; return 1; }
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] clone/update $url ($want) -> $dir"; SYNC_COMMIT="dry-run"; return 0; fi

    if ! git_as ls-remote --exit-code --heads "$url" "$want" >/dev/null 2>&1; then
        if ! git_as ls-remote "$url" >/dev/null 2>&1; then
            SYNC_NOTE="no access (private? gh auth / --token-file)"
            return 1
        fi
        br=$(git_as ls-remote --symref "$url" HEAD 2>/dev/null | awk '/^ref:/ {sub("refs/heads/","",$2); print $2}')
        warn "$repo: branch '$want' not on GitHub, using default '${br:-main}'"
        SYNC_BRANCH="${br:-main}"; SYNC_NOTE="fallback from $want"
    fi

    if [ -d "$dir/.git" ]; then
        if [ -n "$(git_as -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
            warn "$repo: local changes in $dir, not updating"
            SYNC_NOTE="${SYNC_NOTE:+$SYNC_NOTE; }local changes kept"
        else
            git_as -C "$dir" fetch --quiet origin "+refs/heads/$SYNC_BRANCH:refs/remotes/origin/$SYNC_BRANCH" \
                && git_as -C "$dir" checkout --quiet -B "$SYNC_BRANCH" "origin/$SYNC_BRANCH" \
                || { SYNC_NOTE="fetch/checkout failed"; return 1; }
        fi
    else
        install -d -m 755 "$SRC_DIR"
        [ "$RUN_USER" != "root" ] && [ -z "$ASKPASS" ] && chown "$RUN_USER" "$SRC_DIR"
        git_as clone --quiet --branch "$SYNC_BRANCH" "$url" "$dir" || { SYNC_NOTE="clone failed"; return 1; }
    fi
    SYNC_COMMIT=$(git_as -C "$dir" rev-parse --short HEAD 2>/dev/null || echo "?")
    return 0
}

install_env() {   # create /etc/<repo>.env from the repo's example, only if missing
    local repo="$1" dir="$SRC_DIR/$1" ex
    if [ -f "/etc/$repo.env" ]; then echo "kept"; return; fi
    if [ "$DRY" -eq 1 ]; then echo "dry-run"; return; fi
    for ex in "deploy/$repo.env.example" "deploy/env.example" "$repo.env.example" ".env.example" "deploy/.env.example"; do
        if [ -f "$dir/$ex" ]; then
            run install -m 600 -o root -g root "$dir/$ex" "/etc/$repo.env"
            echo "created"
            return
        fi
    done
    run install -m 600 -o root -g root /dev/null "/etc/$repo.env"
    echo "empty"
}

install_repo() {
    local repo="$1" branch dir rc envst
    branch=$(repo_branch "$repo")
    dir="$SRC_DIR/$repo"
    echo ""
    echo "── $repo ($branch)"
    if ! sync_repo "$repo" "$branch"; then
        echo "  FAILED: $SYNC_NOTE"
        add_row "$repo" "$SYNC_BRANCH" "-" "FAIL" "-" "-" "$SYNC_NOTE"
        return
    fi
    local inst="-"
    if [ "$DRY" -eq 1 ]; then
        inst="dry-run"
    elif [ -f "$dir/deploy/install.sh" ]; then
        echo "  running deploy/install.sh"
        ( cd "$dir" && DEBIAN_FRONTEND=noninteractive NONINTERACTIVE=1 SUDO_USER="$RUN_USER" \
            bash deploy/install.sh </dev/null )
        rc=$?
        if [ "$rc" -eq 0 ]; then inst="ok"; else inst="FAIL($rc)"; warn "$repo: install.sh exited $rc (see $LOG)"; fi
    else
        inst="no-installer"; warn "$repo: no deploy/install.sh"
    fi
    if compgen -G "$dir/profiles.d/*.conf" >/dev/null; then
        run install -m 644 -o root -g root "$dir"/profiles.d/*.conf /opt/pi-profiles/profiles.d/
    fi
    envst=$(install_env "$repo")
    [ "$inst" = "ok" ] && restart_if_changed "$repo" "$dir"
    add_row "$repo" "$SYNC_BRANCH" "$SYNC_COMMIT" "ok" "$inst" "$envst" "$SYNC_NOTE"
}

# A running unit keeps its old code until restarted. After a successful install, restart the
# repo's units (services.conf repo column) that are active, if the installed commit differs from
# the one recorded last time (or none is recorded yet). The record: $INSTALLED_DIR/<repo>.
INSTALLED_DIR="${ISR_INSTALLED_DIR:-/var/lib/isr-kit/installed}"
RESTARTED=""
restart_if_changed() {
    local repo="$1" dir="$2" now prev u
    now=$(git_as -C "$dir" rev-parse HEAD 2>/dev/null) || return 0
    prev=$(cat "$INSTALLED_DIR/$repo" 2>/dev/null || true)
    install -d -m 755 "$INSTALLED_DIR"
    echo "$now" > "$INSTALLED_DIR/$repo"
    [ "$now" = "$prev" ] && return 0
    for u in $(awk -v r="$repo" '!/^[[:space:]]*#/ && $4 == r && $1 != "opentakserver" {print $1}' "$SELF_DIR/services.conf"); do
        [ "$(systemctl is-active "$u" 2>/dev/null)" = active ] || continue
        if systemctl restart "$u" 2>/dev/null; then
            echo "  restarted $u (code ${prev:0:7}${prev:+ -> }${now:0:7}; it was running the old code)"
            RESTARTED="$RESTARTED $u"
        else
            warn "$u failed to restart after the update (journalctl -u $u)"
        fi
    done
}

# Tool installers may `systemctl enable` their unit; pi-profiles owns boot instead.
disable_tool_units_at_boot() {
    echo ""
    echo "[6/7] Boot ownership: pi-profiles starts tools, not systemd"
    local u
    for u in $(awk '!/^[[:space:]]*(#|$)/ && $3!="always" && $1!="opentakserver" {print $1}' "$SELF_DIR/services.conf"); do
        if systemctl is-enabled "$u" >/dev/null 2>&1; then
            run systemctl disable "$u" >/dev/null 2>&1 && echo "  disabled $u at boot (pi-profiles starts it)"
        fi
    done
}

journald_persistent() {
    # Pi OS sets Storage=volatile in /etc/systemd/journald.conf, so a freeze or power cut leaves no
    # logs. Persist them (capped), on every run: logs matter more than the trivial SSD wear.
    echo ""
    echo "[logs] journald: persistent, capped at 200M / 7 days"
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] write /etc/systemd/journald.conf.d/isr-kit.conf"; return 0; fi
    install -d /etc/systemd/journald.conf.d /var/log/journal
    printf '%s\n' '# isr-kit (bootstrap.sh): keep logs across reboots, capped.' '[Journal]' 'Storage=persistent' \
        'SystemMaxUse=200M' 'RuntimeMaxUse=50M' 'MaxRetentionSec=7day' > /etc/systemd/journald.conf.d/isr-kit.conf
    systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
    systemctl restart systemd-journald || true
    journalctl --flush >/dev/null 2>&1 || true
}

tune_ssd() {
    [ "$TUNE_SSD" -eq 1 ] || return 0
    echo ""
    echo "[ssd] Applying SSD tweaks (docs/SSD-TIPS.md)"
    if [ -f /etc/dphys-swapfile ]; then
        run sed -i 's/^#\?CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        run sed -i 's/^#\?CONF_MAXSWAP=.*/CONF_MAXSWAP=4096/' /etc/dphys-swapfile
        run systemctl restart dphys-swapfile || true
    fi
    echo "  swap 2 GB on SSD. EEPROM boot order left alone (see docs)."
}

summary() {
    echo ""
    echo "[7/7] Summary"
    {
        echo "REPO|BRANCH|COMMIT|CODE|INSTALL|ENV|NOTE"
        printf '%s' "$ROWS"
    } | awk -F'|' '{printf "  %-18s %-24s %-8s %-5s %-12s %-8s %s\n", $1, $2, $3, $4, $5, $6, $7}'
    echo ""
    echo "  Active profile: $(cat /opt/pi-profiles/active 2>/dev/null || echo none)"
    echo "  Launcher:       http://isr-pi.local:8050  (or http://192.168.4.1:8050 on the hotspot)"
    echo "  Macs:           MQTT login in /etc/isr-kit/mqtt.env; on each Mac run laptop/setup-mac.sh --role hq|droneops"
    echo "  Gemini key:     sudo isr-set-gemini-key   (optional; offline works without)"
    echo "  Log:            $LOG"
    if [ -n "$WARNINGS" ]; then
        echo ""
        echo "  Warnings:"
        printf '%b\n' "$WARNINGS"
    fi
    if printf '%s' "$ROWS" | grep -q '|FAIL\||FAIL('; then
        return 1
    fi
    return 0
}

# ── cleanup on exit (token askpass, offline pip/uv config) ───────────────────
CLEANUP_ASKPASS=0
PIP_OFFLINE_ON=0
cleanup() {
    [ "$CLEANUP_ASKPASS" -eq 1 ] && [ -n "$ASKPASS" ] && rm -rf "$(dirname "$ASKPASS")"
    unset ISR_GH_TOKEN
    if [ "$PIP_OFFLINE_ON" -eq 1 ] && [ -x /opt/pi-profiles/bin/isr-pip-offline ]; then
        /opt/pi-profiles/bin/isr-pip-offline off | sed 's/^/  /'
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

pip_offline() {   # switch pip/uv to offline for every installer (env + /etc config, see bin/isr-pip-offline)
    local helper=/opt/pi-profiles/bin/isr-pip-offline
    [ -x "$helper" ] || helper="$SELF_DIR/bin/isr-pip-offline"
    if [ "$OFFLINE" -eq 1 ]; then
        export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_RETRIES=0 PIP_TIMEOUT=3 UV_OFFLINE=1
        if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $helper on"; return 0; fi
        "$helper" on | sed 's/^/  /' && PIP_OFFLINE_ON=1
    elif [ "$DRY" -eq 0 ] && "$helper" status 2>/dev/null | grep -q OFFLINE; then
        echo "  restoring pip/uv config left offline by an earlier run"
        "$helper" off | sed 's/^/  /'
    fi
}

# ── main ─────────────────────────────────────────────────────────────────────
preflight
base_packages
setup_token

echo ""
echo "[3/7] Platform (pi-profiles, launcher, switch helper)"
if [ "$SKIP_PLATFORM" -eq 1 ]; then
    echo "  skipped (--skip-platform)"
elif [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] bash $SELF_DIR/install.sh"
else
    if bash "$SELF_DIR/install.sh"; then
        add_row "pi-profiles" "$(git -C "$SELF_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')" \
            "$(git -C "$SELF_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')" "local" "ok" "-" "platform"
    else
        add_row "pi-profiles" "-" "-" "local" "FAIL" "-" "platform install failed"
    fi
fi

pip_offline

echo ""
echo "[4/7] Repos -> $SRC_DIR (installers deploy to /opt/<repo>)"
if [ -n "$ONLY" ]; then
    for r in $(echo "$ONLY" | tr ',' ' '); do
        all_repos | grep -Fxq "$r" || warn "--only: '$r' is not in repos.conf"
    done
fi
for repo in $(all_repos); do
    selected "$repo" || continue
    install_repo "$repo"
done

echo ""
echo "[hotspot] band (after the repos: blufor-tracker's installer resets it to 2.4 GHz)"
if [ "$DRY" -eq 1 ]; then echo "  [dry-run] isr-hotspot-band $HOTSPOT_BAND"
elif [ -x /opt/pi-profiles/bin/isr-hotspot-band ]; then
    # shellcheck disable=SC2086
    /opt/pi-profiles/bin/isr-hotspot-band $HOTSPOT_BAND || warn "isr-hotspot-band failed"
fi

echo ""
echo "[5/7] Env files: /etc/<repo>.env (mode 600, created from examples if missing)"
for repo in $(all_repos); do
    selected "$repo" || continue
    [ -f "/etc/$repo.env" ] && printf "  %-20s %s\n" "/etc/$repo.env" "$(stat -c %a "/etc/$repo.env" 2>/dev/null)"
done

echo ""
if [ -n "$WIRED_MODE" ]; then wdesc="DHCP (router cable)"; else wdesc="pure link-local (direct cable)"; fi
echo "[5-/7] Wired Ethernet: $wdesc, wait-online 15 s"
if [ "$KEEP_NET" -eq 1 ]; then
    echo "  skipped (--keep-network)"
elif [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] isr-wired-setup"
elif [ -x /opt/pi-profiles/bin/isr-wired-setup ]; then
    /opt/pi-profiles/bin/isr-wired-setup ${WIRED_MODE:+"$WIRED_MODE"} 2>&1 | sed 's/^/  /' || warn "isr-wired-setup failed"
fi

echo ""
echo "[5a/7] Site settings: ISR_HOST + SENSOR_LAT/LON into every /etc/<repo>.env (contract v4)"
if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] isr-set-site ${SENSOR:+--sensor $SENSOR} --sync"
elif [ -x /opt/pi-profiles/bin/isr-set-site ]; then
    if /opt/pi-profiles/bin/isr-set-site ${SENSOR:+--sensor "$SENSOR"} --sync | sed 's/^/  /'; then
        grep -q '^SENSOR_LAT=.' /etc/isr-kit/site.env 2>/dev/null \
            || warn "no sensor position set: run sudo isr-set-site --sensor LAT,LON (tools won't place position-bearing CoT)"
    else
        warn "isr-set-site failed (bad --sensor value?)"
    fi
fi

echo ""
echo "[5b/7] Hub networking: MQTT user for the Macs, mDNS, reachability"
if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] /opt/pi-profiles/bin/isr-setup-mqtt-user; isr-netcheck"
elif command -v rabbitmqctl >/dev/null 2>&1; then
    if /opt/pi-profiles/bin/isr-setup-mqtt-user; then
        add_row "mqtt-user" "-" "-" "-" "ok" "600" "isrkit in /etc/isr-kit/mqtt.env"
    else
        add_row "mqtt-user" "-" "-" "-" "FAIL" "-" "see log"; warn "MQTT user setup failed"
    fi
else
    warn "rabbitmqctl not found (blufor-tracker/OTS not installed?): Macs cannot log in to MQTT yet"
fi
if [ "$DRY" -eq 0 ] && id isr-launcher >/dev/null 2>&1; then
    # blufor-tracker writes /var/lib/blufor-tracker/channel-url (0640, group isr-launcher).
    getent group isr-launcher >/dev/null || groupadd --system isr-launcher
    usermod -aG isr-launcher isr-launcher
    id -nG isr-launcher | tr ' ' '\n' | grep -qx isr-launcher && echo "  launcher user is in group isr-launcher (channel QR readable)"
    systemctl try-restart isr-launcher.service || true   # pick up group membership
fi
if [ "$DRY" -eq 0 ] && [ -x /opt/pi-profiles/bin/isr-mesh-channel ]; then
    # Best effort: works when the bridge radio is plugged in and mesh-bridge isn't running yet.
    /opt/pi-profiles/bin/isr-mesh-channel 2>&1 | sed 's/^/  /' \
        || echo "  mesh channel not cached yet: later run sudo isr-mesh-channel --pause-bridge"
fi
[ "$DRY" -eq 1 ] || /opt/pi-profiles/bin/isr-netcheck || warn "isr-netcheck found problems (see above; sudo isr-netcheck --fix opens ufw ports)"

mediamtx_check() {
    [ "$WITH_MEDIAMTX" -eq 1 ] || return 0
    echo ""
    echo "[mediamtx] optional live-video relay (op-sentry live 360: rtmp://192.168.4.1:1935/op360 -> rtsp://isr-pi.local:8554/op360)"
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] check mediamtx.service"; return 0; fi
    if systemctl cat mediamtx.service >/dev/null 2>&1; then
        systemctl enable --now mediamtx.service >/dev/null 2>&1 || warn "could not start mediamtx.service"
        if systemctl is-active --quiet mediamtx.service; then
            echo "  mediamtx running (installed by OpenTAKServer)."
            add_row "mediamtx" "-" "-" "-" "ok" "-" "1935 RTMP / 8554 RTSP"
        fi
        echo "  NOTE: OTS's mediamtx may require OTS credentials to publish or read a path. Check that the"
        echo "        op360 path works in rehearsal. Live RTMP from the Insta360 ONE is UNCONFIRMED; the"
        echo "        recorded .insv replay is the guaranteed path."
    else
        warn "--with-mediamtx: no mediamtx.service (normally installed by OpenTAKServer). Not installing one automatically; see docs/CONTRACT.md."
        add_row "mediamtx" "-" "-" "-" "missing" "-" "optional"
    fi
}
mediamtx_check

if [ "$HQ_TUNNEL" -eq 1 ]; then
    echo ""
    echo "[hq-tunnel] Ollama via the HQ Mac's ssh tunnel (127.0.0.1:11434)"
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] isr-hq-mode tunnel"
    else /opt/pi-profiles/bin/isr-hq-mode tunnel || warn "isr-hq-mode tunnel failed"; fi
fi

time_setup() {
    echo ""
    echo "[clock] no NTP offline and no RTC battery by default: Macs set the clock on connect (isr-set-clock)"
    local cfg=/boot/firmware/config.txt
    [ -f "$cfg" ] || cfg=/boot/config.txt
    if [ "$RTC_TRICKLE" -eq 1 ]; then
        if [ "$DRY" -eq 1 ]; then echo "  [dry-run] add dtparam=rtc_bbat_vchg=3000000 to $cfg"
        elif grep -q '^dtparam=rtc_bbat_vchg=' "$cfg" 2>/dev/null; then echo "  RTC trickle charging already set in $cfg"
        elif [ -f "$cfg" ]; then
            printf '\n# isr-kit: trickle-charge the rechargeable RTC battery (ML-2020 on J5). Never with a CR2032.\ndtparam=rtc_bbat_vchg=3000000\n' >> "$cfg"
            echo "  RTC trickle charging enabled in $cfg (takes effect after a reboot)"
        else warn "--rtc-trickle: no config.txt found"; fi
    fi
    [ "$TIME_SERVER" -eq 1 ] || return 0
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] chrony: allow 192.168.4.0/24, local stratum 10"; return 0; fi
    if ! command -v chronyd >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony >/dev/null 2>&1 \
            || { warn "--time-server: couldn't install chrony (offline? install it once online: sudo apt-get install chrony)"; return 0; }
    fi
    install -d -m 755 /etc/chrony/conf.d
    cat > /etc/chrony/conf.d/isr-kit.conf <<'CHRONY'
# isr-kit: serve this Pi's clock to the hotspot even with no upstream server. The time comes from the
# Macs (isr-set-clock on connect) or upstream NTP when online. Phones keep their own time.
allow 192.168.4.0/24
local stratum 10 orphan
CHRONY
    systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || warn "chrony did not restart"
    echo "  chrony serves time on udp/123 to 192.168.4.0/24 (clients: sudo sntp -sS 192.168.4.1 on a Mac)"
}
time_setup

disable_tool_units_at_boot
journald_persistent
tune_ssd
[ "$DRY" -eq 1 ] || systemctl daemon-reload
if [ -n "$RESTARTED" ]; then echo ""; echo "Restarted after code updates:$RESTARTED"; fi
summary
