#!/usr/bin/env bash
# Install the pi-profiles platform to /opt/pi-profiles: switch.sh, profiles, service
# catalogue, the launcher (:8050), its privileged helper and sudoers rule, and the boot
# service. Idempotent and non-interactive. bootstrap.sh calls this; you can also run it
# on its own:  sudo bash install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${PI_PROFILES_ROOT:-/opt/pi-profiles}"
LAUNCHER_USER=isr-launcher

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash install.sh"; exit 1; }

echo "=== Pi Profiles platform: install ==="

install -d -m 755 -o root -g root "$INSTALL_DIR" "$INSTALL_DIR/profiles.d" "$INSTALL_DIR/bin" \
    "$INSTALL_DIR/launcher" "$INSTALL_DIR/launcher/static" "$INSTALL_DIR/launcher/static/vendor"
install -m 755 -o root -g root "$SRC/switch.sh" "$INSTALL_DIR/switch.sh"
install -m 644 -o root -g root "$SRC/services.conf" "$INSTALL_DIR/services.conf"
install -m 644 -o root -g root "$SRC/repos.conf" "$INSTALL_DIR/repos.conf"
install -m 755 -o root -g root "$SRC/bin/isr-switch-helper" "$INSTALL_DIR/bin/isr-switch-helper"
install -m 755 -o root -g root "$SRC/bin/isr-set-gemini-key" "$INSTALL_DIR/bin/isr-set-gemini-key"
install -m 755 -o root -g root "$SRC/bin/isr-netcheck" "$INSTALL_DIR/bin/isr-netcheck"
install -m 755 -o root -g root "$SRC/bin/isr-mesh-channel" "$INSTALL_DIR/bin/isr-mesh-channel"
install -m 755 -o root -g root "$SRC/bin/isr-set-site" "$INSTALL_DIR/bin/isr-set-site"
install -m 755 -o root -g root "$SRC/bin/isr-wired-setup" "$INSTALL_DIR/bin/isr-wired-setup"
install -m 755 -o root -g root "$SRC/bin/isr-pip-offline" "$INSTALL_DIR/bin/isr-pip-offline"
install -m 755 -o root -g root "$SRC/deploy/setup-mqtt-user.sh" "$INSTALL_DIR/bin/isr-setup-mqtt-user"
install -m 644 -o root -g root "$SRC"/launcher/*.py "$INSTALL_DIR/launcher/"
install -d -m 755 -o root -g root "$INSTALL_DIR/maps"
install -m 644 -o root -g root "$SRC/maps/isr_maps.py" "$INSTALL_DIR/maps/isr_maps.py"
install -m 644 -o root -g root "$SRC/maps/isr_push_maps.py" "$INSTALL_DIR/maps/isr_push_maps.py"
install -m 755 -o root -g root "$SRC/bin/isr-push-maps" "$INSTALL_DIR/bin/isr-push-maps"
install -m 755 -o root -g root "$SRC/bin/isr-hq-mode" "$INSTALL_DIR/bin/isr-hq-mode"
install -m 755 -o root -g root "$SRC/bin/isr-set-clock" "$INSTALL_DIR/bin/isr-set-clock"
install -m 755 -o root -g root "$SRC/bin/isr-hotspot-band" "$INSTALL_DIR/bin/isr-hotspot-band"
find "$SRC/launcher/static" -maxdepth 1 -type f -exec install -m 644 -o root -g root {} "$INSTALL_DIR/launcher/static/" \;
# /map's third-party libraries (Leaflet, milsymbol) and their licences: vendored, never a CDN
install -m 644 -o root -g root "$SRC"/launcher/static/vendor/* "$INSTALL_DIR/launcher/static/vendor/"
# Shared UI kit (source of truth for every tool's look), served by the launcher and isr-maps at /ui/.
install -d -m 755 -o root -g root "$INSTALL_DIR/ui"
install -m 644 -o root -g root "$SRC"/ui/isr-ui.css "$SRC"/ui/isr-ui.js "$SRC"/ui/UI-SPEC.md "$INSTALL_DIR/ui/"
install -d -m 755 -o root -g root "$INSTALL_DIR/ui/fonts"
install -m 644 -o root -g root "$SRC"/ui/fonts/* "$INSTALL_DIR/ui/fonts/"
for conf in "$SRC"/profiles.d/*.conf; do
    install -m 644 -o root -g root "$conf" "$INSTALL_DIR/profiles.d/"
done
# Profiles removed from the repo go from the Pi too (profiles.d is ours: root-owned, only written here),
# so the launcher's switcher shows exactly the shipped ones.
for conf in "$INSTALL_DIR"/profiles.d/*.conf; do
    [ -f "$conf" ] || continue
    if [ ! -f "$SRC/profiles.d/$(basename "$conf")" ]; then
        rm -f "$conf"
        echo "  removed profile $(basename "$conf" .conf) (no longer shipped)"
    fi
done

ln -sf "$INSTALL_DIR/switch.sh" /usr/local/bin/pi-profiles
ln -sf "$INSTALL_DIR/bin/isr-set-gemini-key" /usr/local/bin/isr-set-gemini-key
ln -sf "$INSTALL_DIR/bin/isr-netcheck" /usr/local/bin/isr-netcheck
ln -sf "$INSTALL_DIR/bin/isr-mesh-channel" /usr/local/bin/isr-mesh-channel
ln -sf "$INSTALL_DIR/bin/isr-set-site" /usr/local/bin/isr-set-site
ln -sf "$INSTALL_DIR/bin/isr-pip-offline" /usr/local/bin/isr-pip-offline
ln -sf "$INSTALL_DIR/bin/isr-push-maps" /usr/local/bin/isr-push-maps
ln -sf "$INSTALL_DIR/bin/isr-hq-mode" /usr/local/bin/isr-hq-mode
ln -sf "$INSTALL_DIR/bin/isr-set-clock" /usr/local/bin/isr-set-clock
ln -sf "$INSTALL_DIR/bin/isr-hotspot-band" /usr/local/bin/isr-hotspot-band

# Launcher: dedicated unprivileged system user
# The isr-launcher group may already exist: blufor-tracker's installer creates it for the
# channel-url file (/var/lib/blufor-tracker, 2750 setgid). Reuse it as the primary group.
if ! id "$LAUNCHER_USER" >/dev/null 2>&1; then
    if getent group "$LAUNCHER_USER" >/dev/null; then
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin -g "$LAUNCHER_USER" "$LAUNCHER_USER"
    else
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --user-group "$LAUNCHER_USER"
    fi
fi
getent group "$LAUNCHER_USER" >/dev/null || groupadd --system "$LAUNCHER_USER"
usermod -aG "$LAUNCHER_USER" "$LAUNCHER_USER"
getent group video >/dev/null && usermod -aG video "$LAUNCHER_USER"   # vcgencmd get_throttled

# Sudoers: exactly one helper. Validate before installing so a typo cannot lock out sudo.
tmp=$(mktemp)
install -m 440 "$SRC/deploy/isr-launcher.sudoers" "$tmp"
if visudo -cf "$tmp" >/dev/null; then
    install -m 440 -o root -g root "$tmp" /etc/sudoers.d/isr-launcher
else
    echo "ERROR: sudoers rule failed validation; launcher will be read-only" >&2
fi
rm -f "$tmp"

if [ ! -f /etc/isr-launcher.env ]; then
    install -m 600 -o root -g root "$SRC/deploy/isr-launcher.env.example" /etc/isr-launcher.env
else
    # Add settings introduced since the file was created; keep existing values, except an exact
    # old default this installer wrote itself (deploy/isr-launcher.env.stale).
    bash "$SRC/deploy/merge-env.sh" "$SRC/deploy/isr-launcher.env.example" "$SRC/deploy/isr-launcher.env.stale" /etc/isr-launcher.env
fi
touch /var/log/pi-profiles-switch.log && chmod 644 /var/log/pi-profiles-switch.log
# Hotspot band: 5 GHz ch36 by default (or the saved choice); idempotent. bootstrap runs it again after
# blufor-tracker's installer, which resets the hotspot to 2.4 GHz.
"$INSTALL_DIR/bin/isr-hotspot-band" || echo "WARNING: isr-hotspot-band failed"

# Hub address: isr-pi.local -> hotspot IP via an avahi alias (keeps the Pi's own hostname).
install -d -m 755 /etc/isr-kit
[ -f /etc/isr-kit/hub.env ] || install -m 644 "$SRC/deploy/hub.env.example" /etc/isr-kit/hub.env
install -m 644 "$SRC/deploy/isr-mdns-alias.service" /etc/systemd/system/isr-mdns-alias.service
HUB_NAME=$(sed -n 's/^ISR_HUB_NAME=//p' /etc/isr-kit/hub.env | head -1)

install -m 644 "$SRC/pi-profiles.service" /etc/systemd/system/pi-profiles.service
install -m 644 "$SRC/deploy/isr-launcher.service" /etc/systemd/system/isr-launcher.service
# Offline map server (always on, like the launcher). Packs live in /var/lib/isr-maps (root-owned,
# world-readable), filled by laptop/push-to-pi.sh --maps or by hand.
id isr-maps >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --user-group isr-maps
install -d -m 755 -o root -g root /var/lib/isr-maps
install -m 644 "$SRC/deploy/isr-maps.service" /etc/systemd/system/isr-maps.service
# Optional auto-push to newly connected ATAK devices: installed, never enabled here.
install -m 644 "$SRC/deploy/isr-push-maps-auto.service" /etc/systemd/system/isr-push-maps-auto.service
systemctl daemon-reload
systemctl enable pi-profiles.service >/dev/null
# The launcher is the one tool enabled at boot: it is how the operator picks a profile.
systemctl enable isr-launcher.service >/dev/null
systemctl enable isr-maps.service >/dev/null
systemctl restart isr-maps.service || echo "WARNING: isr-maps failed to start (journalctl -u isr-maps)"
systemctl restart isr-launcher.service || echo "WARNING: isr-launcher failed to start (journalctl -u isr-launcher)"
if [ "$(hostname -s)" = "${HUB_NAME:-isr-pi}" ]; then
    # avahi already publishes <hostname>.local; a second record for the same name would collide.
    systemctl disable --now isr-mdns-alias.service >/dev/null 2>&1 || true
    echo "Hostname is already ${HUB_NAME:-isr-pi}; avahi publishes ${HUB_NAME:-isr-pi}.local itself."
elif command -v avahi-publish >/dev/null 2>&1; then
    systemctl enable avahi-daemon.service >/dev/null 2>&1 || true
    systemctl enable isr-mdns-alias.service >/dev/null
    systemctl restart isr-mdns-alias.service || echo "WARNING: isr-mdns-alias failed (journalctl -u isr-mdns-alias)"
else
    echo "WARNING: avahi-utils not installed; isr-pi.local will not resolve (apt-get install avahi-daemon avahi-utils)"
fi

# Default profile: the core demo. An active profile that was removed falls back to it, and the Pi
# switches now (so the removed profile's units don't keep running until the next boot).
DEFAULT_PROFILE=demo-full-mac
if [ ! -f "$INSTALL_DIR/active" ]; then
    echo "$DEFAULT_PROFILE" > "$INSTALL_DIR/active"
else
    was=$(head -1 "$INSTALL_DIR/active" | tr -d '[:space:]')
    if [ -n "$was" ] && [ ! -f "$INSTALL_DIR/profiles.d/$was.conf" ]; then
        echo "  active profile '$was' was removed: switching to $DEFAULT_PROFILE"
        if [ -d /run/systemd/system ]; then
            "$INSTALL_DIR/switch.sh" "$DEFAULT_PROFILE" || echo "WARNING: switch to $DEFAULT_PROFILE failed (sudo pi-profiles $DEFAULT_PROFILE)"
        else
            echo "$DEFAULT_PROFILE" > "$INSTALL_DIR/active"
        fi
    fi
fi

echo ""
echo "=== Install complete ==="
echo "  sudo pi-profiles <profile>   switch profile      (pi-profiles --list)"
echo "  Launcher: http://isr-pi.local:8050 or http://192.168.4.1:8050  (PIN: LAUNCHER_PIN in /etc/isr-launcher.env)"
echo "  Network:  isr-netcheck   (hotspot, mDNS, listeners, firewall)"
echo "  Profiles: $INSTALL_DIR/profiles.d/   Catalogue: $INSTALL_DIR/services.conf"
