#!/usr/bin/env bash
# Shell tests for switch.sh with a stubbed systemctl. Runs on macOS (bash 3.2) and Linux.
#   bash tests/test_switch.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
SWITCH="$REPO/switch.sh"
PASS=0; FAIL=0

setup() {
    T=$(mktemp -d)
    export STUB_DIR="$T/stub" PI_PROFILES_ROOT="$T/root"
    export PI_PROFILES_SETTLE=0 PI_PROFILES_STOP_TIMEOUT=2
    mkdir -p "$STUB_DIR/state" "$PI_PROFILES_ROOT/profiles.d"
    cp "$REPO/services.conf" "$PI_PROFILES_ROOT/"
    cp "$REPO"/profiles.d/*.conf "$PI_PROFILES_ROOT/profiles.d/"
    # Test-only profiles (the shipped set is just three, plus bft): a drone segment, an FPV one, an OP one.
    printf '# @env rf-spectrum ALERT_COT=1\ninclude bft\ndrone-detector\nacoustic-detector\nrf-spectrum\n' > "$PI_PROFILES_ROOT/profiles.d/t-threat.conf"
    printf 'include bft\nfpv-vision\nchange-detect\n' > "$PI_PROFILES_ROOT/profiles.d/t-isr.conf"
    printf 'include bft\nop-sentry\ntac-advisor\n' > "$PI_PROFILES_ROOT/profiles.d/t-op.conf"
    : > "$STUB_DIR/calls"
    # Full kit installed by default; tests remove units to simulate a partial kit.
    awk '!/^[[:space:]]*(#|$)/ {print $1}' "$REPO/services.conf" > "$STUB_DIR/installed"
    export PATH="$HERE/stubs:$PATH"
}
teardown() { rm -rf "$T"; }
st() { cat "$STUB_DIR/state/$1" 2>/dev/null || echo inactive; }
sw() { bash "$SWITCH" "$@" > "$T/out" 2>&1; RC=$?; }

check() {   # check <name> <condition...>
    local name="$1"; shift
    if "$@"; then PASS=$((PASS+1)); echo "ok   - $name"
    else FAIL=$((FAIL+1)); echo "FAIL - $name"; sed 's/^/       | /' "$T/out" 2>/dev/null | head -30; fi
}
eq() { [ "$1" = "$2" ] || { echo "       expected '$2' got '$1'"; return 1; }; }
has() { grep -q -- "$2" "$1" || { echo "       '$2' not in $1"; return 1; }; }
hasnt() { ! grep -q -- "$2" "$1" || { echo "       unexpected '$2' in $1"; return 1; }; }
line_no() { grep -n -- "$2" "$1" | head -1 | cut -d: -f1; }

# ── tests ────────────────────────────────────────────────────────────────────

t_includes_resolve() {
    setup
    sw --units t-threat
    check "includes: t-threat resolves bft first" eq "$(tr '\n' ' ' < "$T/out")" \
        "opentakserver mesh-bridge drone-detector acoustic-detector rf-spectrum "
    teardown
}

t_include_loop_guard() {
    setup
    printf 'include b\nunit-a\n' > "$PI_PROFILES_ROOT/profiles.d/a.conf"
    printf 'include a\nunit-b\n' > "$PI_PROFILES_ROOT/profiles.d/b.conf"
    sw --units a
    check "includes: a loop terminates and de-duplicates" eq "$(grep -c '^unit-' "$T/out")" "2"
    teardown
}

t_basic_switch() {
    setup
    sw bft
    check "switch bft: exit 0" eq "$RC" 0
    check "switch bft: OTS active" eq "$(st opentakserver)" active
    check "switch bft: launcher (always) active" eq "$(st isr-launcher)" active
    check "switch bft: active file written" eq "$(cat "$PI_PROFILES_ROOT/active")" bft
    check "switch bft: state ok" has "$PI_PROFILES_ROOT/last-switch" "result=ok"
    teardown
}

t_hot_swap_keeps_core() {
    setup
    sw t-threat
    : > "$STUB_DIR/calls"
    sw t-isr
    check "hot-swap: exit 0" eq "$RC" 0
    check "hot-swap: OTS never stopped" hasnt "$STUB_DIR/calls" "stop opentakserver"
    check "hot-swap: mesh-bridge never stopped" hasnt "$STUB_DIR/calls" "stop mesh-bridge"
    check "hot-swap: launcher never stopped" hasnt "$STUB_DIR/calls" "stop isr-launcher"
    check "hot-swap: drone-detector stopped" eq "$(st drone-detector)" inactive
    check "hot-swap: rf-spectrum stopped" eq "$(st rf-spectrum)" inactive
    check "hot-swap: fpv-vision started" eq "$(st fpv-vision)" active
    check "hot-swap: change-detect started" eq "$(st change-detect)" active
    local last_stop first_start
    last_stop=$(grep -n '^stop ' "$STUB_DIR/calls" | tail -1 | cut -d: -f1)
    first_start=$(grep -n '^start ' "$STUB_DIR/calls" | head -1 | cut -d: -f1)
    check "hot-swap: every stop happens before any start" [ "${last_stop:-0}" -lt "${first_start:-0}" ]
    check "hot-swap: stops in reverse profile order (rf before drone)" \
        [ "$(line_no "$STUB_DIR/calls" 'stop rf-spectrum')" -lt "$(line_no "$STUB_DIR/calls" 'stop drone-detector')" ]
    teardown
}

t_strays_stopped() {
    setup
    echo active > "$STUB_DIR/state/op-sentry"     # started by hand / enabled at boot
    sw t-isr
    check "strays: catalogue unit not in profile is stopped" eq "$(st op-sentry)" inactive
    teardown
}

t_missing_units_graceful() {
    setup
    grep -v -E '^(rf-spectrum|acoustic-detector)$' "$STUB_DIR/installed" > "$T/i" && mv "$T/i" "$STUB_DIR/installed"
    sw t-threat
    check "partial kit: exit 0" eq "$RC" 0
    check "partial kit: skip message" has "$T/out" "skip     rf-spectrum (not installed)"
    check "partial kit: installed units still started" eq "$(st drone-detector)" active
    check "partial kit: never tries to start missing unit" hasnt "$STUB_DIR/calls" "start rf-spectrum"
    check "partial kit: state partial" has "$PI_PROFILES_ROOT/last-switch" "result=partial"
    check "partial kit: missing list" has "$PI_PROFILES_ROOT/last-switch" "missing=acoustic-detector rf-spectrum"
    teardown
}

t_failed_start() {
    setup
    echo drone-detector > "$STUB_DIR/fail-start"
    sw t-threat
    check "failed start: exit 0 (switch continues)" eq "$RC" 0
    check "failed start: later units still started" eq "$(st rf-spectrum)" active
    check "failed start: state degraded" has "$PI_PROFILES_ROOT/last-switch" "result=degraded"
    teardown
}

t_camera_limit() {
    setup
    printf 'include bft\nfpv-vision\nop-sentry\n' > "$PI_PROFILES_ROOT/profiles.d/two-cams.conf"
    sw two-cams
    check "camera limit: refused with exit 2" eq "$RC" 2
    check "camera limit: nothing started" hasnt "$STUB_DIR/calls" "^start "
    sw --check two-cams
    check "camera limit: --check fails" eq "$RC" 2
    sw two-cams --force
    check "camera limit: --force overrides" eq "$RC" 0
    teardown
}

t_shipped_profiles_valid() {
    setup
    local p ok=0
    for p in "$REPO"/profiles.d/*.conf; do
        bash "$SWITCH" --check "$(basename "$p" .conf)" >/dev/null 2>&1 || { ok=1; echo "       $(basename "$p") fails --check"; }
    done
    check "every shipped profile passes --check (<=1 camera unit)" eq "$ok" 0
    sw --units demo-full
    check "demo-full has no fpv-vision" hasnt "$T/out" "fpv-vision"
    teardown
}

t_unknown_and_bad_names() {
    setup
    sw nope
    check "unknown profile: exit 1" eq "$RC" 1
    sw "../../etc/passwd"
    check "path traversal name rejected" eq "$RC" 1
    sw 'bft;reboot'
    check "shell metachar name rejected" eq "$RC" 1
    check "bad names never reach systemctl start" hasnt "$STUB_DIR/calls" "^start "
    teardown
}

t_invalid_unit_lines_ignored() {
    setup
    printf 'opentakserver\nrm -rf /\n$(reboot)\n' > "$PI_PROFILES_ROOT/profiles.d/evil.conf"
    sw --units evil
    check "invalid unit lines warned about" has "$T/out" "ignoring invalid unit name"
    check "only the valid unit survives" eq "$(bash "$SWITCH" --units evil 2>/dev/null | tr '\n' ' ')" "opentakserver "
    teardown
}

t_boot() {
    setup
    sw --boot
    check "boot with no active profile: no-op" has "$T/out" "No active profile"
    echo t-op > "$PI_PROFILES_ROOT/active"
    sw --boot
    check "boot starts active profile" eq "$(st op-sentry)" active
    echo gone > "$PI_PROFILES_ROOT/active"
    sw --boot
    check "boot with a removed profile falls back to demo-full-mac" eq "$(cat "$PI_PROFILES_ROOT/active")" demo-full-mac
    rm "$PI_PROFILES_ROOT/profiles.d/demo-full-mac.conf"
    echo gone > "$PI_PROFILES_ROOT/active"
    sw --boot
    check "… or to bft when that's gone too" eq "$(cat "$PI_PROFILES_ROOT/active")" bft
    teardown
}

t_lock() {
    setup
    mkdir "$PI_PROFILES_ROOT/.switch.lock"; echo $$ > "$PI_PROFILES_ROOT/.switch.lock/pid"
    sw bft
    check "lock held by live pid: exit 3" eq "$RC" 3
    echo 999999 > "$PI_PROFILES_ROOT/.switch.lock/pid"
    sw bft
    check "stale lock is cleared" eq "$RC" 0
    check "lock removed after switch" [ ! -d "$PI_PROFILES_ROOT/.switch.lock" ]
    teardown
}

t_plan_no_changes() {
    setup
    sw bft
    : > "$STUB_DIR/calls"
    sw --plan t-op
    check "plan: shows start op-sentry" has "$T/out" "start op-sentry"
    check "plan: shows keep opentakserver" has "$T/out" "keep  opentakserver"
    check "plan: makes no start/stop calls" hasnt "$STUB_DIR/calls" "^st[oa]"
    teardown
}

t_restart() {
    setup
    sw t-op
    : > "$STUB_DIR/calls"
    sw --restart
    check "restart: tool units restarted" has "$STUB_DIR/calls" "restart op-sentry"
    check "restart: OTS left alone" hasnt "$STUB_DIR/calls" "restart opentakserver"
    check "restart: mesh bridge restarted too" has "$STUB_DIR/calls" "restart mesh-bridge"
    check "restart: launcher left alone" hasnt "$STUB_DIR/calls" "restart isr-launcher"
    teardown
}

t_device_release_wait() {
    setup
    export PI_PROFILES_CGROUP_ROOT="$T/cg"
    mkdir -p "$T/cg/system.slice/fpv-vision.service"
    echo 4242 > "$T/cg/system.slice/fpv-vision.service/cgroup.procs"   # process still holding /dev/video0
    echo fpv-vision > "$STUB_DIR/cgroup-units"
    sw t-isr
    : > "$STUB_DIR/calls"
    sw t-op
    check "release: waits then SIGKILLs a unit whose processes linger" has "$STUB_DIR/calls" "kill --signal=SIGKILL fpv-vision"
    check "release: warns about it" has "$T/out" "still holding processes"
    check "release: new camera unit started after the wait" \
        [ "$(line_no "$STUB_DIR/calls" 'kill --signal')" -lt "$(line_no "$STUB_DIR/calls" 'start op-sentry')" ]
    : > "$T/cg/system.slice/fpv-vision.service/cgroup.procs"            # released cleanly
    sw t-isr
    : > "$STUB_DIR/calls"
    sw t-op
    check "release: no kill when the cgroup empties" hasnt "$STUB_DIR/calls" "kill --signal"
    unset PI_PROFILES_CGROUP_ROOT
    teardown
}

t_mac_profiles_keep_vision_off_pi() {
    setup
    local p bad=0
    for p in demo-full-mac demo-step-mac; do
        for u in fpv-vision op-sentry change-detect; do
            bash "$SWITCH" --units "$p" 2>/dev/null | grep -Fxq "$u" && { bad=1; echo "       $p runs $u on the Pi"; }
        done
    done
    check "mac profiles run no vision tools on the Pi" eq "$bad" 0
    echo active > "$STUB_DIR/state/op-sentry"
    sw demo-full-mac
    check "switching to a mac profile stops a Pi op-sentry" eq "$(st op-sentry)" inactive
    teardown
}

t_env_overrides() {
    setup
    export PI_PROFILES_SYSTEMD_DIR="$T/systemd"
    D="$PI_PROFILES_ROOT/profiles.d"
    printf '# @env rf-spectrum ALERT_COT=0 ALERT_MESH=1\n# @env rf-spectrum BAD-KEY=1\n# @env rf-spectrum EVIL=$(reboot)\ninclude bft\nrf-spectrum\n' > "$D/envbase.conf"
    printf '# @env rf-spectrum ALERT_COT=1\ninclude envbase\n' > "$D/envchild.conf"
    printf '# @env rf-spectrum ALERT_COT=1 SIM_SCENARIO=manual\ninclude envbase\n' > "$D/envchild2.conf"
    sw --env envchild
    check "env: child overrides include, keys merged" eq "$(grep -v WARNING "$T/out" | tr '\n' ' ')" "rf-spectrum ALERT_COT=1 rf-spectrum ALERT_MESH=1 "
    check "env: unsafe KEY/VALUE rejected" has "$T/out" "ignoring @env rf-spectrum BAD-KEY=1"
    grep -v '^WARNING' "$T/out" > "$T/accepted"
    check "env: \$( ) never accepted" hasnt "$T/accepted" "EVIL"
    : > "$STUB_DIR/calls"
    sw envchild
    check "env: switch exit 0" eq "$RC" 0
    check "env: override file written" eq "$(grep -v '^#' "$PI_PROFILES_ROOT/env/rf-spectrum.env" | tr '\n' ' ')" "ALERT_COT=1 ALERT_MESH=1 "
    check "env: drop-in loads it after the unit's own env" has "$T/systemd/rf-spectrum.service.d/pi-profiles-env.conf" "EnvironmentFile=-$PI_PROFILES_ROOT/env/rf-spectrum.env"
    check "env: daemon-reload before starting" [ "$(line_no "$STUB_DIR/calls" '^daemon-reload')" -lt "$(line_no "$STUB_DIR/calls" 'start rf-spectrum')" ]
    : > "$STUB_DIR/calls"
    sw envchild
    check "env: unchanged -> no reload, no restart" hasnt "$STUB_DIR/calls" "daemon-reload"
    : > "$STUB_DIR/calls"
    sw envchild2
    check "env: changed while running -> restart" has "$STUB_DIR/calls" "restart rf-spectrum"
    check "env: new value applied" has "$PI_PROFILES_ROOT/env/rf-spectrum.env" "SIM_SCENARIO=manual"
    : > "$STUB_DIR/calls"
    sw bft
    check "env: unit left the profile -> overrides removed" [ ! -f "$PI_PROFILES_ROOT/env/rf-spectrum.env" ] && [ ! -f "$T/systemd/rf-spectrum.service.d/pi-profiles-env.conf" ]
    check "env: removal reloads systemd" has "$STUB_DIR/calls" "daemon-reload"
    unset PI_PROFILES_SYSTEMD_DIR
    teardown
}

t_shipped_drone_profiles_enable_rf_cot() {
    setup
    local p
    for p in demo-full demo-full-mac demo-step-mac; do
        sw --env "$p"
        check "$p sets rf-spectrum ALERT_COT=1" has "$T/out" "rf-spectrum ALERT_COT=1"
    done
    teardown
}

t_shipped_profiles_are_three() {
    local names
    names=$(cd "$REPO/profiles.d" && LC_ALL=C ls *.conf | sed 's/\.conf$//' | tr '\n' ' ')
    T=$(mktemp -d); echo "$names" > "$T/out"
    check "shipped: bft + the three demo profiles only" eq "$names" "bft demo-full-mac demo-full demo-step-mac "
    check "shipped: bft is hidden" has "$REPO/profiles.d/bft.conf" "@hidden"
    check "shipped: demo-full is the Pi-only fallback" has "$REPO/profiles.d/demo-full.conf" "@title Pi only (fallback: no Macs)"
    rm -rf "$T"
}

t_list_status() {
    setup
    sw t-isr
    sw --list
    check "list marks active" has "$T/out" "\* t-isr"
    check "list marks bft as the hidden include base" has "$T/out" "include base, hidden in the launcher"
    sw --status
    check "status shows unit states" has "$T/out" "fpv-vision *active"
    teardown
}

for t in $(declare -F | awk '{print $3}' | grep '^t_'); do "$t"; done
echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
