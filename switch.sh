#!/usr/bin/env bash
# Pi Profile Switcher: start/stop systemd service groups, hot-swap safe.
#
#   sudo pi-profiles <profile> [--force]   switch profile
#   sudo pi-profiles --boot                start the active profile (boot service)
#   pi-profiles --list | --status | --units <profile> | --plan <profile> | --check <profile>
#
# Switching:
#   1. resolves the target profile (following `include <profile>` lines)
#   2. refuses a profile with more than one camera-class unit (services.conf) unless --force
#   3. stops units that are running but not in the target (old profile + stray catalogue
#      units), in reverse order, and waits for each unit's processes to exit so cameras,
#      serial ports and SDRs are released
#   4. starts the target's units in order. Units shared with the old profile are left
#      running (OTS and the mesh bridge are never bounced between demo profiles)
#   5. units that are not installed are skipped with a note, not treated as errors
#
# Every path can be overridden with PI_PROFILES_ROOT (used by tests).
# Must run under bash 3.2 too (macOS test runs): no associative arrays or mapfile.
set -euo pipefail

ROOT="${PI_PROFILES_ROOT:-/opt/pi-profiles}"
PROFILES_DIR="${PI_PROFILES_DIR:-$ROOT/profiles.d}"
ACTIVE_FILE="${PI_PROFILES_ACTIVE:-$ROOT/active}"
SERVICES_CONF="${PI_PROFILES_SERVICES:-$ROOT/services.conf}"
STATE_FILE="${PI_PROFILES_STATE:-$ROOT/last-switch}"
LOCK_DIR="${PI_PROFILES_LOCK:-$ROOT/.switch.lock}"
STOP_TIMEOUT="${PI_PROFILES_STOP_TIMEOUT:-20}"     # seconds to wait for a unit to release devices
SETTLE_SECS="${PI_PROFILES_SETTLE:-1}"             # pause after stops (USB re-enumeration)
HOTSPOT_CON="${PI_PROFILES_HOTSPOT:-bft-hotspot}"  # NetworkManager connection kept up
CGROUP_ROOT="${PI_PROFILES_CGROUP_ROOT:-/sys/fs/cgroup}"
MAX_INCLUDE_DEPTH=5
# Per-profile env overrides (# @env <unit> KEY=VALUE ...): written to ENV_DIR/<unit>.env and
# loaded by a systemd drop-in AFTER the unit's own EnvironmentFile=, so they win.
ENV_DIR="${PI_PROFILES_ENV_DIR:-$ROOT/env}"
SYSTEMD_DIR="${PI_PROFILES_SYSTEMD_DIR:-/etc/systemd/system}"
DROPIN_NAME="pi-profiles-env.conf"

usage() {
    cat <<EOF
Usage: sudo pi-profiles <profile> [--force]
       sudo pi-profiles --boot
       pi-profiles --list
       pi-profiles --status
       pi-profiles --units <profile>    resolved unit list (follows includes)
       pi-profiles --plan <profile>     what a switch would stop/start (no changes)
       pi-profiles --check <profile>    validate (exists, camera limit, installed units)
       pi-profiles --env <profile>      per-tool env overrides the profile sets (# @env lines)
       sudo pi-profiles --restart       restart the active profile's units, the mesh bridge included
                                        (after an env or code change); OTS and the always-on
                                        launcher/maps are left up

Switches the active systemd service profile on this Pi.
EOF
}

log() { echo "$@"; }
warn() { echo "WARNING: $*" >&2; }

valid_name() {
    case "$1" in
        ''|-*|*[!a-z0-9-]*) return 1 ;;
    esac
    [ "${#1}" -le 48 ]
}

profile_conf() { echo "$PROFILES_DIR/$1.conf"; }

# Resolve a profile to its unit list (one per line, de-duplicated, include order kept).
resolve_units() {
    _resolve "$1" 0 | awk '!seen[$0]++'
}

_resolve() {
    local name="$1" depth="$2" conf line
    conf=$(profile_conf "$name")
    if [ "$depth" -gt "$MAX_INCLUDE_DEPTH" ]; then
        warn "include depth exceeded at '$name' (include loop?)"
        return 0
    fi
    if [ ! -f "$conf" ]; then
        [ "$depth" -gt 0 ] && warn "included profile '$name' not found"
        return 0
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        # trim whitespace
        line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [ -z "$line" ] && continue
        case "$line" in
            include\ *)
                local inc="${line#include }"
                inc="$(echo "$inc" | sed -e 's/^[[:space:]]*//')"
                if valid_name "$inc" && [ "$inc" != "$name" ]; then
                    _resolve "$inc" $((depth + 1))
                else
                    warn "bad include '$inc' in $conf"
                fi
                ;;
            *[!A-Za-z0-9@._:-]*)
                warn "ignoring invalid unit name '$line' in $conf"
                ;;
            *)
                echo "$line"
                ;;
        esac
    done < "$conf"
}

# Profile files in include order (includes first, the profile itself last).
profile_chain() {
    _chain "$1" 0 | awk '!seen[$0]++'
}
_chain() {
    local name="$1" depth="$2" conf line inc
    conf=$(profile_conf "$name")
    [ "$depth" -le "$MAX_INCLUDE_DEPTH" ] && [ -f "$conf" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        case "$line" in
            *include\ *)
                inc="$(echo "${line#*include }" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
                valid_name "$inc" && [ "$inc" != "$name" ] && _chain "$inc" $((depth + 1))
                ;;
        esac
    done < "$conf"
    echo "$conf"
}

# "unit KEY=VALUE" lines for a profile; later files and later lines win per unit+key.
profile_env() {
    local files
    files=$(profile_chain "$1")
    [ -n "$files" ] || return 0
    # shellcheck disable=SC2086
    awk '
        /^#[[:space:]]*@env[[:space:]]/ {
            unit = $3
            if (unit !~ /^[A-Za-z0-9@._:-]+$/) { print "WARNING: bad @env unit " unit > "/dev/stderr"; next }
            for (i = 4; i <= NF; i++) {
                kv = $i; k = kv; sub(/=.*/, "", k); v = kv; sub(/^[^=]*=/, "", v)
                if (kv !~ /=/ || k !~ /^[A-Z_][A-Z0-9_]*$/ || v !~ /^[A-Za-z0-9._:\/,@+-]*$/) {
                    print "WARNING: ignoring @env " unit " " kv " (KEY=VALUE, safe characters only)" > "/dev/stderr"; continue
                }
                key = unit SUBSEP k
                if (!(key in val)) order[++n] = key
                val[key] = v
            }
        }
        END { for (i = 1; i <= n; i++) { split(order[i], p, SUBSEP); print p[1], p[2] "=" val[order[i]] } }
    ' $files
}

# Make each unit's override file + drop-in match the target profile. Prints units whose
# effective environment changed (they need a restart if already running).
apply_env() {
    local target="$1" units="$2" envs u want have dropin changed=""
    envs=$(profile_env "$target")
    mkdir -p "$ENV_DIR" 2>/dev/null || true
    for u in $units $(cd "$ENV_DIR" 2>/dev/null && ls -- *.env 2>/dev/null | sed 's/\.env$//'); do
        want=$(printf '%s\n' "$envs" | awk -v u="$u" '$1 == u {print $2}')
        dropin="$SYSTEMD_DIR/$u.service.d/$DROPIN_NAME"
        have=""
        [ -f "$ENV_DIR/$u.env" ] && have=$(grep -v '^#' "$ENV_DIR/$u.env")
        if [ -n "$want" ]; then
            if [ "$want" != "$have" ] || [ ! -f "$dropin" ]; then
                { echo "# managed by pi-profiles: @env overrides from profile '$target'"; printf '%s\n' "$want"; } > "$ENV_DIR/$u.env"
                mkdir -p "$SYSTEMD_DIR/$u.service.d"
                printf '# managed by pi-profiles (switch.sh): profile @env overrides, read after the unit'"'"'s own env\n[Service]\nEnvironmentFile=-%s\n' \
                    "$ENV_DIR/$u.env" > "$dropin"
                changed="$changed $u"
            fi
        elif [ -f "$ENV_DIR/$u.env" ] || [ -f "$dropin" ]; then
            rm -f "$ENV_DIR/$u.env" "$dropin"
            rmdir "$SYSTEMD_DIR/$u.service.d" 2>/dev/null || true
            changed="$changed $u"
        fi
    done
    printf '%s\n' "$changed" | tr ' ' '\n' | awk 'NF && !seen[$0]++'
}

profile_meta() {   # profile_meta <profile> <key>   e.g. hotspot, devices
    local conf
    conf=$(profile_conf "$1")
    [ -f "$conf" ] || return 0
    sed -n "s/^#[[:space:]]*@$2[[:space:]]\{1,\}//p" "$conf" | head -1
}

# services.conf lookups
catalog_units() {
    [ -f "$SERVICES_CONF" ] || return 0
    awk '!/^[[:space:]]*(#|$)/ {print $1}' "$SERVICES_CONF"
}
unit_class() {
    [ -f "$SERVICES_CONF" ] || { echo "unknown"; return 0; }
    awk -v u="$1" '!/^[[:space:]]*(#|$)/ && $1==u {print $3; found=1; exit} END {if(!found) print "unknown"}' "$SERVICES_CONF"
}
units_of_class() {
    [ -f "$SERVICES_CONF" ] || return 0
    awk -v c="$1" '!/^[[:space:]]*(#|$)/ && $3==c {print $1}' "$SERVICES_CONF"
}

get_active() {
    if [ -f "$ACTIVE_FILE" ]; then
        head -1 "$ACTIVE_FILE" | tr -d '[:space:]'
    fi
}

unit_installed() {
    systemctl cat "$1" >/dev/null 2>&1
}

unit_state() {
    systemctl is-active "$1" 2>/dev/null || true
}

in_list() {   # in_list <needle> <newline-separated list>
    printf '%s\n' "$2" | grep -Fxq -- "$1"
}

camera_units_in() {
    local units="$1" u out=""
    for u in $units; do
        [ "$(unit_class "$u")" = "camera" ] && out="$out $u"
    done
    echo "${out# }"
}

check_profile() {   # returns 0 ok, 1 not found, 2 camera conflict
    local name="$1" units cams n
    if ! valid_name "$name" || [ ! -f "$(profile_conf "$name")" ]; then
        echo "Error: profile '$name' not found ($(profile_conf "$name"))"
        return 1
    fi
    units=$(resolve_units "$name")
    cams=$(camera_units_in "$units")
    n=$(echo "$cams" | wc -w | tr -d ' ')
    if [ "$n" -gt 1 ]; then
        echo "Error: profile '$name' runs $n camera-heavy units ($cams); the Pi 5 CPU can only sustain one."
        return 2
    fi
    return 0
}

# Wait until a stopped unit has no processes left (devices released) or time out.
wait_released() {
    local unit="$1" waited=0 state cg procs
    while :; do
        state=$(unit_state "$unit")
        procs=""
        if [ "$state" != "active" ] && [ "$state" != "deactivating" ] && [ "$state" != "activating" ]; then
            cg=$(systemctl show -p ControlGroup --value "$unit" 2>/dev/null || true)
            if [ -n "$cg" ] && [ -r "$CGROUP_ROOT$cg/cgroup.procs" ]; then
                procs=$(tr -d '[:space:]' < "$CGROUP_ROOT$cg/cgroup.procs")
            fi
            [ -z "$procs" ] && return 0
        fi
        if [ "$waited" -ge "$STOP_TIMEOUT" ]; then
            warn "$unit still holding processes after ${STOP_TIMEOUT}s (state=$state); killing"
            systemctl kill --signal=SIGKILL "$unit" 2>/dev/null || true
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo $$ > "$LOCK_DIR/pid"
        trap 'rm -rf "$LOCK_DIR"' EXIT
        return 0
    fi
    local pid
    pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "Error: another switch is in progress (pid $pid)"
        exit 3
    fi
    warn "removing stale lock (pid ${pid:-?})"
    rm -rf "$LOCK_DIR"
    acquire_lock
}

write_state() {   # write_state <profile> <result> <missing> <failed>
    {
        echo "profile=$1"
        echo "result=$2"
        echo "missing=$3"
        echo "failed=$4"
        echo "time=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

ensure_hotspot() {
    local profile="$1"
    [ "$(profile_meta "$profile" hotspot)" = "off" ] && return 0
    command -v nmcli >/dev/null 2>&1 || return 0
    nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$HOTSPOT_CON" || return 0
    if ! nmcli -t -f NAME connection show --active 2>/dev/null | grep -Fxq "$HOTSPOT_CON"; then
        log "  bringing hotspot '$HOTSPOT_CON' up"
        nmcli connection up "$HOTSPOT_CON" >/dev/null 2>&1 || warn "could not bring up hotspot $HOTSPOT_CON"
    fi
}

# Compute the stop list: running units not wanted by the target.
# Candidates = old profile units + catalogue units (catches strays started by hand or at boot).
plan_stops() {
    local target_units="$1" old="$2" candidates u always
    always=$(units_of_class always)
    candidates=$( { [ -n "$old" ] && resolve_units "$old"; catalog_units; } | awk '!seen[$0]++')
    # reverse order so dependants stop before the core
    for u in $(printf '%s\n' "$candidates" | awk '{a[NR]=$0} END {for(i=NR;i>0;i--) print a[i]}'); do
        in_list "$u" "$target_units" && continue
        in_list "$u" "$always" && continue
        case "$(unit_state "$u")" in
            active|activating|reloading|deactivating) echo "$u" ;;
        esac
    done
}

cmd_list() {
    echo "Available profiles:"
    local active conf name title
    active=$(get_active)
    for conf in "$PROFILES_DIR"/*.conf; do
        [ -f "$conf" ] || continue
        name=$(basename "$conf" .conf)
        title=$(profile_meta "$name" title)
        grep -q '^#[[:space:]]*@hidden' "$conf" && title="$title (include base, hidden in the launcher)"
        if [ "$name" = "$active" ]; then
            printf "  * %-20s %s (active)\n" "$name" "$title"
        else
            printf "    %-20s %s\n" "$name" "$title"
        fi
    done
}

cmd_status() {
    local active u state
    active=$(get_active)
    if [ -z "$active" ]; then
        echo "No active profile"
        return 0
    fi
    if [ ! -f "$(profile_conf "$active")" ]; then
        echo "Active profile '$active' but config missing: $(profile_conf "$active")"
        return 1
    fi
    echo "Active profile: $active"
    echo ""
    for u in $(resolve_units "$active") $(units_of_class always); do
        if unit_installed "$u"; then state=$(unit_state "$u"); else state="not-installed"; fi
        printf "  %-24s %s\n" "$u" "$state"
    done | awk '!seen[$0]++'
    if [ -f "$STATE_FILE" ]; then
        echo ""
        echo "Last switch: $(tr '\n' ' ' < "$STATE_FILE")"
    fi
}

cmd_plan() {
    local target="$1" units old
    check_profile "$target" || return $?
    units=$(resolve_units "$target")
    old=$(get_active)
    echo "Target: $target (active: ${old:-none})"
    for u in $(plan_stops "$units" "$old"); do echo "  stop  $u"; done
    for u in $units $(units_of_class always); do
        if ! unit_installed "$u"; then echo "  skip  $u (not installed)"
        elif [ "$(unit_state "$u")" = "active" ]; then echo "  keep  $u"
        else echo "  start $u"; fi
    done | awk '!seen[$0]++'
}

do_switch() {
    local target="$1" force="$2" units old stops u missing="" failed="" rc
    if check_profile "$target"; then :; else
        rc=$?
        if [ "$rc" -eq 2 ] && [ "$force" = "1" ]; then
            warn "--force given; continuing with more than one camera unit"
        else
            [ "$rc" -eq 1 ] && { echo ""; cmd_list; }
            exit "$rc"
        fi
    fi
    acquire_lock
    units=$(resolve_units "$target")
    old=$(get_active)

    stops=$(plan_stops "$units" "$old")
    if [ -n "$stops" ]; then
        log "Stopping units not in '$target':"
        for u in $stops; do
            log "  stopping $u"
            systemctl stop "$u" 2>/dev/null || warn "stop $u returned non-zero"
        done
        for u in $stops; do
            wait_released "$u" || true
        done
        command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=5 2>/dev/null || true
        [ "$SETTLE_SECS" -gt 0 ] 2>/dev/null && sleep "$SETTLE_SECS"
    fi

    # Record the target first so a reboot mid-switch comes back to it.
    echo "$target" > "$ACTIVE_FILE"

    # Per-profile env overrides (@env). Restart running units whose environment changed.
    local envchanged
    envchanged=$(apply_env "$target" "$units")
    if [ -n "$envchanged" ]; then
        systemctl daemon-reload 2>/dev/null || warn "daemon-reload failed"
        for u in $envchanged; do
            if [ -f "$ENV_DIR/$u.env" ]; then log "  env      $u: $(grep -v '^#' "$ENV_DIR/$u.env" | tr '\n' ' ')"
            else log "  env      $u: overrides cleared"; fi
            if in_list "$u" "$units" && [ "$(unit_state "$u")" = "active" ]; then
                log "  restart  $u (environment changed)"
                systemctl restart "$u" 2>/dev/null || { warn "$u failed to restart"; }
            fi
        done
    fi

    log "Starting profile: $target"
    for u in $units $(units_of_class always); do
        if ! unit_installed "$u"; then
            log "  skip     $u (not installed)"
            in_list "$u" "$(units_of_class always)" || missing="$missing $u"
            continue
        fi
        if [ "$(unit_state "$u")" = "active" ]; then
            log "  running  $u"
            continue
        fi
        log "  starting $u"
        systemctl start "$u" 2>/dev/null || { warn "$u failed to start (journalctl -u $u)"; failed="$failed $u"; }
    done
    ensure_hotspot "$target"

    missing="${missing# }"; failed="${failed# }"
    local result="ok"
    [ -n "$missing" ] && result="partial"
    [ -n "$failed" ] && result="degraded"
    write_state "$target" "$result" "$missing" "$failed"
    echo ""
    echo "Profile '$target' is now active ($result)."
    [ -n "$missing" ] && echo "  not installed: $missing"
    [ -n "$failed" ] && echo "  failed:        $failed"
    return 0
}

do_restart() {
    local active u
    active=$(get_active)
    [ -n "$active" ] || { echo "No active profile"; return 1; }
    acquire_lock
    # Core units too (mesh-bridge), except OpenTAKServer: restarting it drops every phone's
    # connection and takes a minute. Always-on units (launcher, maps) are install.sh's job.
    # resolve_units lists the core first, so the bridge is back before the tools.
    for u in $(resolve_units "$active"); do
        [ "$u" = opentakserver ] && continue
        case "$(unit_class "$u")" in always) continue ;; esac
        unit_installed "$u" || continue
        log "  restarting $u"
        systemctl restart "$u" 2>/dev/null || warn "$u failed to restart"
    done
}

do_boot() {
    local active
    active=$(get_active)
    if [ -z "$active" ]; then
        echo "No active profile set, skipping boot start"
        return 0
    fi
    if ! valid_name "$active" || [ ! -f "$(profile_conf "$active")" ]; then
        # a profile that was removed (the 2026-11 prune): the core demo, else the BFT base
        local fb
        for fb in demo-full-mac bft; do [ -f "$(profile_conf "$fb")" ] && break; done
        [ -f "$(profile_conf "$fb")" ] || return 0
        warn "active profile '$active' missing; falling back to $fb"
        active=$fb
    fi
    # At boot nothing from the old profile is running; reuse the switch path
    # (camera limit enforced, missing units skipped). --force keeps boot from failing.
    do_switch "$active" 1
}

# ── Main ──────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

case "$1" in
    --list|-l)    cmd_list ;;
    --status|-s)  cmd_status ;;
    --units)      [ $# -ge 2 ] || { usage; exit 1; }
                  check_profile "$2" >/dev/null || [ $? -eq 2 ] || { echo "Error: profile '$2' not found"; exit 1; }
                  resolve_units "$2" ;;
    --plan)       [ $# -ge 2 ] || { usage; exit 1; }; cmd_plan "$2" ;;
    --check)      [ $# -ge 2 ] || { usage; exit 1; }; check_profile "$2" && echo "ok" ;;
    --env)        [ $# -ge 2 ] || { usage; exit 1; }; check_profile "$2" >/dev/null || [ $? -eq 2 ] || exit 1; profile_env "$2" ;;
    --boot)       do_boot ;;
    --restart)    do_restart ;;
    --help|-h)    usage ;;
    -*)           echo "Unknown option: $1"; usage; exit 1 ;;
    *)
        force=0
        [ "${2:-}" = "--force" ] && force=1
        do_switch "$1" "$force"
        ;;
esac
