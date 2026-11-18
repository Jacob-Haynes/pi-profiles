#!/usr/bin/env bash
# Tunnelled HQ: reach the Pi over one SSH connection instead of Bonjour (for managed Macs that
# can't advertise their .local name, or any Mac whose settings you can't change).
#
#   laptop/hq-tunnel.sh start      run the reconnect loop in the background (start-mac.sh does this
#                                  when station.env has ISR_TUNNEL=1)
#   laptop/hq-tunnel.sh stop | status
#   laptop/hq-tunnel.sh run        the loop in the foreground (Ctrl-C to stop)
#   laptop/hq-tunnel.sh --print    show the ssh command it would run
#
# Forwards, all binding the Pi's 127.0.0.1 only (no sshd change on the Pi):
#   11434  Ollama (stays 127.0.0.1-only on this Mac; the Pi's tools use http://127.0.0.1:11434)
#   ISR_TUNNEL_PORTS (default "8130 8150": change-detect, geo-locate), each to the Pi's
#          127.0.0.1:<port + 10000>. The Pi's launcher relays <port> on every address to it while the
#          tunnel is up (ports of class "mac", or of tools the profile runs @hq/@mac), so phones open
#          http://192.168.4.1:<port> and this Mac, on the cable, http://isr-pi.local:<port>.
# Settings (station.env or the environment): ISR_TUNNEL_PI=user@isr-pi.local (default: <you>@$ISR_HUB),
#   ISR_TUNNEL_PORTS, ISR_TUNNEL_OLLAMA=0 to not forward Ollama. Needs key-based ssh to the Pi
#   (ssh-copy-id user@isr-pi.local once). On every connect it also sets the Pi's clock from this Mac
#   when they differ by more than 2 s (laptop/pi-clock.py; ISR_CLOCK_SYNC=0 to skip). Plain ssh, no extra tools: ServerAliveInterval notices a dead
#   link in about 45 s, ExitOnForwardFailure makes a stale forward fail loudly, and the loop retries.
set -uo pipefail
CONF_DIR="${ISR_CONF_DIR:-$HOME/.config/isr-kit}"
RUN="${ISR_RUN_DIR:-$HOME/isr-kit/run}"
LOGS="${ISR_LOG_DIR:-$HOME/isr-kit/logs}"
if [ -f "$CONF_DIR/station.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$CONF_DIR/station.env"
    set +a
fi
PI="${ISR_TUNNEL_PI:-$(id -un)@${ISR_HUB:-isr-pi.local}}"
PORTS="${ISR_TUNNEL_PORTS:-8130 8150}"
SSH="${ISR_SSH:-ssh}"
PIDFILE="$RUN/hq-tunnel.pid"
HERE="$(cd "$(dirname "$0")" && pwd)"
MAX_BACKOFF="${ISR_TUNNEL_MAX_BACKOFF:-30}"
OFFSET="${ISR_TUNNEL_PORT_OFFSET:-10000}"
case "$OFFSET" in *[!0-9]*|'') echo "bad ISR_TUNNEL_PORT_OFFSET" >&2; exit 64 ;; esac

case "$PI" in *[!A-Za-z0-9._@:%-]*|'') echo "bad ISR_TUNNEL_PI '$PI'" >&2; exit 64 ;; esac
args=(-N -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes
      -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
[ "${ISR_TUNNEL_OLLAMA:-1}" = "1" ] && args+=(-R 127.0.0.1:11434:127.0.0.1:11434)
for p in $PORTS; do
    case "$p" in *[!0-9]*|'') echo "bad port '$p' in ISR_TUNNEL_PORTS" >&2; exit 64 ;; esac
    # Tool UIs land on the Pi's loopback at port + OFFSET; the Pi's launcher relays <port> on every
    # address to it (a 127.0.0.1:<port> listener here would block that all-address bind).
    args+=(-R "127.0.0.1:$((p + OFFSET)):127.0.0.1:$p")
done

FWD_LABEL="$([ "${ISR_TUNNEL_OLLAMA:-1}" = "1" ] && echo "11434 (Ollama) ")$PORTS"
alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

loop() {
    local backoff=2 started rc child=""
    trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM INT
    while :; do
        started=$(date +%s)
        # Every (re)connect: the Pi has no time source offline, so set it from this Mac if it's off by > 2 s.
        [ "${ISR_CLOCK_SYNC:-1}" = "1" ] && /usr/bin/env python3 "$HERE/pi-clock.py" "$PI"
        echo "$(date '+%F %T') connecting to $PI (forwards to the Pi's 127.0.0.1: $FWD_LABEL)"
        "$SSH" "${args[@]}" "$PI" &
        child=$!
        wait "$child"; rc=$?; child=""
        # A connection that held for a minute was fine: start the backoff again from 2 s.
        [ $(( $(date +%s) - started )) -ge 60 ] && backoff=2
        echo "$(date '+%F %T') ssh exited ($rc); retrying in ${backoff}s"
        [ "$rc" -eq 255 ] && echo "  (255 = connect/auth failure, or a forward port already taken on the Pi: an old session may still hold it until ServerAlive times it out)"
        sleep "$backoff"
        backoff=$(( backoff * 2 )); [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff=$MAX_BACKOFF
    done
}

case "${1:-status}" in
    --print) printf '%q ' "$SSH" "${args[@]}" "$PI"; echo ;;
    run) loop ;;
    start)
        if alive; then echo "  hq-tunnel: already running (pid $(cat "$PIDFILE"))"; exit 0; fi
        mkdir -p "$RUN" "$LOGS"
        nohup "$0" run >> "$LOGS/hq-tunnel.log" 2>&1 &
        echo $! > "$PIDFILE"
        echo "  hq-tunnel: started to $PI (pid $!, log $LOGS/hq-tunnel.log)"
        ;;
    stop)
        if alive; then kill -TERM "$(cat "$PIDFILE")" 2>/dev/null; echo "  hq-tunnel: stopped"; else echo "  hq-tunnel: not running"; fi
        rm -f "$PIDFILE"
        ;;
    status)
        if alive; then
            echo "  hq-tunnel: running to $PI (pid $(cat "$PIDFILE"))"
            tail -n 2 "$LOGS/hq-tunnel.log" 2>/dev/null | sed 's/^/    /'
        else
            echo "  hq-tunnel: not running"
        fi
        ;;
    *) sed -n '2,22p' "$0"; exit 64 ;;
esac
