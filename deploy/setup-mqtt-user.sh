#!/usr/bin/env bash
# Create (or re-sync) the RabbitMQ MQTT user `isrkit` that the Macs use over the hotspot.
# `guest` only works from the Pi itself (RabbitMQ loopback_users), so off-Pi tools need this.
#
#   sudo bash deploy/setup-mqtt-user.sh            # idempotent
#   sudo bash deploy/setup-mqtt-user.sh --rotate   # new password
#
# Password: random, generated once, stored in /etc/isr-kit/mqtt.env (mode 600, root).
# It is shown on the terminal only when newly created, never written to the install log.
# Permissions: publish to meshbus/tx and isrkit/#, subscribe to meshbus/rx only (RabbitMQ
# topic permissions on amq.topic, where MQTT "a/b" is routing key "a.b").
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
ENV_DIR=/etc/isr-kit
ENV_FILE="$ENV_DIR/mqtt.env"
USER_NAME=isrkit
VHOST="${ISR_MQTT_VHOST:-/}"
ROTATE=0
[ "${1:-}" = "--rotate" ] && ROTATE=1

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v rabbitmqctl >/dev/null || { echo "rabbitmqctl not found (OpenTAKServer/RabbitMQ not installed yet); skipping" >&2; exit 2; }
systemctl is-active --quiet rabbitmq-server || systemctl start rabbitmq-server || true
rabbitmqctl -q await_startup >/dev/null 2>&1 || true

install -d -m 755 "$ENV_DIR"
NEW=0
PASS=""
if [ -f "$ENV_FILE" ] && [ "$ROTATE" -eq 0 ]; then
    PASS=$(sed -n 's/^MQTT_PASS=//p' "$ENV_FILE" | head -1)
fi
if [ -z "$PASS" ]; then
    PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
    NEW=1
fi

umask 077
tmp=$(mktemp "$ENV_DIR/.mqtt.env.XXXXXX")
{
    echo "# MQTT login for tools off the Pi (Macs). Copy to the Mac with laptop/setup-mac.sh --from-pi."
    echo "MQTT_HOST=isr-pi.local"
    echo "MQTT_PORT=1883"
    echo "MQTT_USER=$USER_NAME"
    echo "MQTT_PASS=$PASS"
} > "$tmp"
chmod 600 "$tmp"; chown root:root "$tmp"; mv -f "$tmp" "$ENV_FILE"

verify() {   # log in over MQTT with the stdlib client shipped with the launcher
    python3 - "$USER_NAME" <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, "/opt/pi-profiles/launcher")
from mqtt_mini import check_login
pw = [l.split("=", 1)[1].strip() for l in open("/etc/isr-kit/mqtt.env") if l.startswith("MQTT_PASS=")][0]
sys.exit(1 if check_login("127.0.0.1", 1883, sys.argv[1], pw) else 0)
PY
}

# Prefer giving rabbitmqctl the password on stdin (keeps it out of `ps`). Versions that
# don't read stdin get it as an argument instead (a brief local-only exposure).
if rabbitmqctl -q list_users 2>/dev/null | awk '{print $1}' | grep -Fxq "$USER_NAME"; then
    printf '%s\n' "$PASS" | rabbitmqctl -q change_password "$USER_NAME" >/dev/null 2>&1 || true
else
    printf '%s\n' "$PASS" | rabbitmqctl -q add_user "$USER_NAME" >/dev/null 2>&1 \
        || rabbitmqctl -q add_user "$USER_NAME" "$PASS" >/dev/null
fi
rabbitmqctl -q set_user_tags "$USER_NAME" >/dev/null   # no management/admin tags
# Queue/exchange permissions the MQTT plugin needs: its own subscription queues + amq.topic.
rabbitmqctl -q set_permissions -p "$VHOST" "$USER_NAME" \
    '^mqtt-subscription-.*' '^(amq\.topic|mqtt-subscription-.*)$' '^(amq\.topic|mqtt-subscription-.*)$' >/dev/null
# Topic-level limit (contract v3; MQTT "/" is "." in RabbitMQ routing keys):
#   write: meshbus/tx and isrkit/#        read: meshbus/rx only
rabbitmqctl -q set_topic_permissions -p "$VHOST" "$USER_NAME" amq.topic \
    '^(meshbus\.tx|isrkit(\..*)?)$' '^meshbus\.rx$' >/dev/null

if verify; then
    echo "MQTT user '$USER_NAME' verified on 127.0.0.1:1883."
else
    rabbitmqctl -q change_password "$USER_NAME" "$PASS" >/dev/null 2>&1 || true
    if verify; then
        echo "MQTT user '$USER_NAME' verified on 127.0.0.1:1883."
    else
        echo "WARNING: could not log in as '$USER_NAME' on 1883 (is the RabbitMQ MQTT plugin enabled? rabbitmq-plugins list)" >&2
    fi
fi

if [ "$NEW" -eq 1 ]; then
    msg="MQTT user $USER_NAME password (shown once; also in $ENV_FILE): $PASS"
    if [ -w /dev/tty ] && { : > /dev/tty; } 2>/dev/null; then
        echo "$msg" > /dev/tty      # terminal only, not the tee'd bootstrap log
    else
        echo "New MQTT password written to $ENV_FILE (read it with: sudo cat $ENV_FILE)"
    fi
fi
unset PASS
