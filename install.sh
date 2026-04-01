#!/usr/bin/env bash
# Install pi-profiles to /opt/pi-profiles
# Usage: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi-profiles"

echo "=== Pi Profiles — Install ==="

mkdir -p "$INSTALL_DIR/profiles.d"
cp switch.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/switch.sh"
cp -r profiles.d/* "$INSTALL_DIR/profiles.d/" 2>/dev/null || true

# Symlink for convenience
ln -sf "$INSTALL_DIR/switch.sh" /usr/local/bin/pi-profiles

# Systemd boot service
cp pi-profiles.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pi-profiles

echo ""
echo "=== Install complete ==="
echo ""
echo "Usage:"
echo "  sudo pi-profiles <profile>   # switch to a profile"
echo "  sudo pi-profiles --list      # list available profiles"
echo "  sudo pi-profiles --status    # show active profile and service states"
echo ""
echo "Profiles are defined in $INSTALL_DIR/profiles.d/"
echo "The active profile starts automatically on boot."