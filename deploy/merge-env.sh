#!/usr/bin/env bash
# merge-env.sh EXAMPLE STALE LIVE
# Bring a live KEY=value env file up to date with its example, without overriding the operator:
#   - a key missing from LIVE is appended from EXAMPLE;
#   - a LIVE line that is exactly a line in STALE (an old default we wrote ourselves) is replaced
#     by EXAMPLE's current line for that key;
#   - every other existing value is kept as it is.
# LIVE keeps its mode (600 if it can't be read back).
set -uo pipefail
ex="$1" stale="$2" live="$3"
[ -f "$ex" ] && [ -f "$live" ] || { echo "merge-env: missing $ex or $live" >&2; exit 1; }
while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    cur=$(grep -m1 "^$key=" "$live")
    if [ -z "$cur" ]; then
        echo "$line" >> "$live"
    elif [ "$cur" != "$line" ] && [ -f "$stale" ] && grep -Fxq -- "$cur" "$stale"; then
        tmp=$(mktemp "$live.XXXXXX")
        awk -v k="$key=" -v new="$line" 'index($0, k) == 1 && !done {print new; done = 1; next} {print}' "$live" > "$tmp"
        chmod 600 "$tmp"
        mv -f "$tmp" "$live"
        echo "  $(basename "$live"): old default $cur -> ${line#*=}"
    fi
done < "$ex"
