#!/usr/bin/env bash
# Pi Profile Switcher — start/stop systemd service groups
set -euo pipefail

PROFILES_DIR="/opt/pi-profiles/profiles.d"
ACTIVE_FILE="/opt/pi-profiles/active"

usage() {
    echo "Usage: sudo pi-profiles <profile-name>"
    echo "       sudo pi-profiles --status"
    echo "       sudo pi-profiles --list"
    echo "       sudo pi-profiles --boot"
    echo ""
    echo "Switches the active systemd service profile on this Pi."
}

get_services() {
    grep -v '^\s*#' "$1" | grep -v '^\s*$'
}

get_active() {
    if [ -f "$ACTIVE_FILE" ]; then
        cat "$ACTIVE_FILE"
    fi
}

cmd_list() {
    echo "Available profiles:"
    local active
    active=$(get_active)
    for conf in "$PROFILES_DIR"/*.conf; do
        [ -f "$conf" ] || continue
        local name
        name=$(basename "$conf" .conf)
        if [ "$name" = "$active" ]; then
            echo "  * $name (active)"
        else
            echo "    $name"
        fi
    done
}

cmd_status() {
    local active
    active=$(get_active)
    if [ -z "$active" ]; then
        echo "No active profile"
        return
    fi

    local conf="$PROFILES_DIR/$active.conf"
    if [ ! -f "$conf" ]; then
        echo "Active profile '$active' but config missing: $conf"
        return 1
    fi

    echo "Active profile: $active"
    echo ""
    while IFS= read -r svc; do
        local state
        state=$(systemctl is-active "$svc" 2>/dev/null || true)
        printf "  %-24s %s\n" "$svc" "$state"
    done < <(get_services "$conf")
}

stop_profile() {
    local name="$1"
    local conf="$PROFILES_DIR/$name.conf"
    [ -f "$conf" ] || return 0

    echo "Stopping profile: $name"
    while IFS= read -r svc; do
        echo "  stopping $svc"
        systemctl stop "$svc" 2>/dev/null || true
    done < <(get_services "$conf")
}

start_profile() {
    local name="$1"
    local conf="$PROFILES_DIR/$name.conf"

    if [ ! -f "$conf" ]; then
        echo "Error: profile '$name' not found ($conf)"
        echo ""
        cmd_list
        exit 1
    fi

    echo "Starting profile: $name"
    while IFS= read -r svc; do
        echo "  starting $svc"
        systemctl start "$svc" || echo "  WARNING: $svc failed to start"
    done < <(get_services "$conf")

    echo "$name" > "$ACTIVE_FILE"
    echo ""
    echo "Profile '$name' is now active."
}

# ── Main ──────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

case "$1" in
    --list|-l)
        cmd_list
        ;;
    --status|-s)
        cmd_status
        ;;
    --boot)
        active=$(get_active)
        if [ -n "$active" ]; then
            start_profile "$active"
        else
            echo "No active profile set, skipping boot start"
        fi
        ;;
    --help|-h)
        usage
        ;;
    -*)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
    *)
        active=$(get_active)
        target="$1"

        if [ -n "$active" ] && [ "$active" != "$target" ]; then
            stop_profile "$active"
            echo ""
        fi

        start_profile "$target"
        ;;
esac
