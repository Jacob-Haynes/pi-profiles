#!/usr/bin/env bash
# Offline update: refresh the kit's git mirrors on this Mac and push them to a Pi with no internet.
#
#   laptop/push-to-pi.sh --host pi@raspberrypi.local              # mirrors + insteadOf + pip check
#   laptop/push-to-pi.sh --host pi@raspberrypi.local --bootstrap  # … then sudo ./bootstrap.sh --skip-apt in tmux
#
# Steps:
#   1. Mirrors: `git clone --mirror` / `remote update` every Pi-side repo in repos.conf, plus
#      pi-profiles, into --mirror-dir on the Mac (private repos via your gh login).
#   2. Copy them to a persistent staging dir on the Pi with rsync over ssh (retried, resumable,
#      keep-alives; ._* and .DS_Store excluded). It falls back to a tar stream with COPYFILE_DISABLE=1
#      --no-xattrs --no-mac-metadata if rsync is missing. Each mirror is checked with git fsck, then
#      swapped in with hard links. A failed copy leaves the Pi's live mirrors untouched.
#   3. On the Pi, for the SSH user: git config --global url."<remote-dir>/".insteadOf
#      "https://github.com/<owner>/", so bootstrap.sh's normal https URLs resolve to the mirrors.
#      Then fast-forward the Pi's pi-profiles checkout (--pi-profiles-dir) from its mirror.
#   4. Offline pip check: with no internet on the Pi, pip can't fetch dependencies that aren't
#      already in /opt/<repo>/.venv. For each repo it dry-runs the new requirements.txt with
#      --no-index against the installed venv and lists what would be missing.
#   5. --bootstrap: run `sudo ./bootstrap.sh --skip-apt` in a tmux session on the Pi, wait, and
#      print its summary table (attach with: ssh -t <host> tmux attach -t isr-bootstrap).
#
# Options:
#   --host USER@HOST       SSH target (required, or $ISR_PI_SSH). Over a direct Ethernet cable use
#                          the Pi's <hostname>.local or its 169.254.x.y address. isr-pi.local is the
#                          hotspot alias (192.168.4.1) and only works on BFT-TAK.
#   --mirror-dir DIR       Mac-side mirrors (default ~/isr-kit/mirror)
#   --remote-dir DIR       Pi-side mirrors (default ~/isr-mirror, i.e. the SSH user's home)
#   --pi-profiles-dir DIR  pi-profiles checkout on the Pi (default ~/pi-profiles; cloned if missing)
#   --branch BR            pi-profiles branch to check out on the Pi (default feature/one-pi-platform)
#   --bootstrap            also run bootstrap.sh --skip-apt (extra args after --, e.g. -- --only fpv-vision)
#   --maps                 also copy ~/isr-kit/maps/*.mbtiles (laptop/build-map-pack.py) to the Pi's
#                          /var/lib/isr-maps (resumable rsync to ~/isr-maps.staging, then sudo install)
#   --maps-only            only the map packs (skip mirrors, git, pip and bootstrap)
#   --no-fetch             push the existing mirrors without refreshing them from GitHub
#   --dry-run              print what would happen
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="$(dirname "$HERE")"
OWNER="${ISR_GH_OWNER:-Jacob-Haynes}"
HOST="${ISR_PI_SSH:-}"
MIRROR="${ISR_MIRROR_DIR:-$HOME/isr-kit/mirror}"
# shellcheck disable=SC2088  # deliberately unexpanded: ~ expands on the Pi, in the remote shell
REMOTE_DIR="~/isr-mirror"
# shellcheck disable=SC2088
PIPROF="~/pi-profiles"
PIBRANCH="feature/one-pi-platform"
BOOT=0; FETCH=1; DRY=0; MAPS=0; MAPS_ONLY=0
MAPS_SRC="${ISR_MAPS_OUT:-$HOME/isr-kit/maps}"
BOOT_ARGS=()
SSH="${ISR_SSH:-ssh}"
# Keep-alives so a stalled cable is noticed in about 1 minute rather than hanging forever.
SSH_OPTS=(-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
COPY_RETRIES="${ISR_COPY_RETRIES:-5}"
BACKOFF="${ISR_COPY_BACKOFF:-5}"
PIP_CHECK_URL="${ISR_PIP_CHECK_URL:-https://pypi.org/simple/}"   # overridable for tests
OPT="${ISR_PI_OPT:-/opt}"

die() { echo "ERROR: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:?}"; shift 2 ;;
        --mirror-dir) MIRROR="${2:?}"; shift 2 ;;
        --remote-dir) REMOTE_DIR="${2:?}"; shift 2 ;;
        --pi-profiles-dir) PIPROF="${2:?}"; shift 2 ;;
        --branch) PIBRANCH="${2:?}"; shift 2 ;;
        --bootstrap) BOOT=1; shift ;;
        --maps) MAPS=1; shift ;;
        --maps-only) MAPS=1; MAPS_ONLY=1; shift ;;
        --no-fetch) FETCH=0; shift ;;
        --dry-run) DRY=1; shift ;;
        --) shift; BOOT_ARGS=("$@"); break ;;
        -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
[ -n "$HOST" ] || die "--host USER@HOST is required (or set ISR_PI_SSH)"
case "$HOST$REMOTE_DIR$PIPROF$PIBRANCH" in *[\;\&\|\`\$\(\)\<\>\'\"]*) die "unsafe characters in --host/--remote-dir/--pi-profiles-dir/--branch" ;; esac
run() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }
rsh() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] ssh $HOST: ${1:0:200}"; else $SSH "${SSH_OPTS[@]}" "$HOST" "$1"; fi; }

push_maps() {
    echo "── maps: $MAPS_SRC/*.mbtiles -> $HOST:/var/lib/isr-maps"
    local packs=() f
    for f in "$MAPS_SRC"/*.mbtiles; do [ -f "$f" ] && packs+=("$f"); done
    [ "${#packs[@]}" -gt 0 ] || { echo "  no packs in $MAPS_SRC (build one with laptop/build-map-pack.py)"; return 0; }
    for f in "${packs[@]}"; do printf "  %-28s %s\n" "$(basename "$f")" "$(du -h "$f" | cut -f1)"; done
    if [ "$DRY" -eq 1 ]; then echo "  [dry-run] rsync -> ~/isr-maps.staging, then sudo install into /var/lib/isr-maps"; return 0; fi
    local ok=0 attempt
    $SSH "${SSH_OPTS[@]}" "$HOST" "mkdir -p isr-maps.staging" || true
    for attempt in $(seq 1 "$COPY_RETRIES"); do
        if command -v rsync >/dev/null 2>&1 && [ "${ISR_COPY_METHOD:-auto}" != "tar" ]; then
            COPYFILE_DISABLE=1 rsync -a --exclude '._*' -e "$SSH ${SSH_OPTS[*]}" "${packs[@]}" "$HOST:isr-maps.staging/" && { ok=1; break; }
        else
            COPYFILE_DISABLE=1 tar -C "$MAPS_SRC" -cf - "${packs[@]##*/}" \
                | $SSH "${SSH_OPTS[@]}" "$HOST" "tar -C isr-maps.staging -xf -" && { ok=1; break; }
        fi
        echo "  maps copy attempt $attempt/$COPY_RETRIES failed; retrying in ${BACKOFF}s"; sleep "$BACKOFF"
    done
    [ "$ok" -eq 1 ] || { echo "  maps copy FAILED: the Pi's /var/lib/isr-maps is unchanged; staging kept, re-run to resume"; return 1; }
    names=""; for f in "${packs[@]}"; do names="$names $(basename "$f")"; done
    rsh "cd isr-maps.staging || exit 1; \
         if ! sudo -n true 2>/dev/null; then echo '  sudo needs a password: run on the Pi: sudo install -m 644 ~/isr-maps.staging/*.mbtiles /var/lib/isr-maps/'; exit 1; fi; \
         for f in $names; do sqlite3_ok=1; python3 -c \"import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); c.execute('SELECT COUNT(*) FROM tiles').fetchone()\" \"\$f\" || sqlite3_ok=0; \
           if [ \$sqlite3_ok -eq 1 ]; then sudo install -m 644 -o root -g root \"\$f\" /var/lib/isr-maps/.\$f.tmp && sudo mv -f /var/lib/isr-maps/.\$f.tmp /var/lib/isr-maps/\$f && echo \"  installed \$f\"; \
           else echo \"  SKIPPED \$f: not a readable MBTiles file\"; fi; done; \
         echo '  isr-maps picks new packs up within 30 s: http://192.168.4.1:8160/'"
}

# Repos that run on the Pi (repos.conf where != mac), plus pi-profiles itself.
REPOS="pi-profiles $(awk '!/^[[:space:]]*(#|$)/ && $4 != "mac" {print $1}' "$KIT/repos.conf" | tr '\n' ' ')"

# The Pi has no time source offline (no NTP, no RTC battery): set it from this Mac if it's off by > 2 s.
if [ "$DRY" -eq 0 ] && [ "${ISR_CLOCK_SYNC:-1}" = "1" ]; then
    echo "── clock"
    /usr/bin/env python3 "$HERE/pi-clock.py" "$HOST" | sed 's/^/  /' || echo "  (couldn't check the Pi's clock; continuing)"
fi

if [ "$MAPS_ONLY" -eq 1 ]; then push_maps; exit $?; fi

# ── 1. mirrors on the Mac ────────────────────────────────────────────────────
echo "── 1. Mirrors in $MIRROR"
GITC=(git)
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    GITC=(git -c credential.helper= -c 'credential.helper=!gh auth git-credential')
fi
mkdir -p "$MIRROR"
for r in $REPOS; do
    m="$MIRROR/$r.git"
    if [ "$FETCH" -eq 0 ]; then
        [ -d "$m" ] && echo "  $r: (not refreshed)" || echo "  $r: MISSING mirror (run without --no-fetch)"
        continue
    fi
    if [ -d "$m" ]; then
        run "${GITC[@]}" -C "$m" remote update --prune >/dev/null 2>&1 && echo "  $r: updated" || echo "  $r: UPDATE FAILED (offline? no access?)"
    else
        run "${GITC[@]}" clone -q --mirror "https://github.com/$OWNER/$r.git" "$m" && echo "  $r: cloned" || echo "  $r: CLONE FAILED (private? gh auth login)"
    fi
done

# ── 2. stream to the Pi ──────────────────────────────────────────────────────
echo "── 2. Copying mirrors to $HOST:$REMOTE_DIR"
present=""
for r in $REPOS; do [ -d "$MIRROR/$r.git" ] && present="$present $r.git"; done
[ -n "$present" ] || die "no mirrors to push"
# Copy into a persistent STAGING dir on the Pi (resumable), then swap it into place on the Pi
# with hard links. The live mirror is only touched after a complete, checked copy, so a dropped
# link leaves the Pi exactly as it was. Re-running resumes from staging.
STAGE="$REMOTE_DIR.staging"
RSTAGE="${STAGE#\~/}"            # rsync paths are relative to the remote home
copy_ok=0
if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] rsync -a --delete (retry ${COPY_RETRIES}x) $MIRROR/{$present} $HOST:$STAGE/  then swap into $REMOTE_DIR"
    copy_ok=1
elif [ "${ISR_COPY_METHOD:-auto}" != "tar" ] && command -v rsync >/dev/null 2>&1 \
        && $SSH "${SSH_OPTS[@]}" "$HOST" "command -v rsync >/dev/null" 2>/dev/null; then
    ropts=(-a --delete --exclude '._*' --exclude '.DS_Store')
    rsync --help 2>&1 | grep -q -- '--partial' && ropts+=(--partial)
    rsync --help 2>&1 | grep -q -- '--timeout' && ropts+=(--timeout=60)
    $SSH "${SSH_OPTS[@]}" "$HOST" "mkdir -p $STAGE" || true
    srcs=(); for d in $present; do srcs+=("$MIRROR/$d"); done
    for attempt in $(seq 1 "$COPY_RETRIES"); do
        if COPYFILE_DISABLE=1 rsync "${ropts[@]}" -e "$SSH ${SSH_OPTS[*]}" "${srcs[@]}" "$HOST:$RSTAGE/"; then
            copy_ok=1; break
        fi
        echo "  rsync attempt $attempt/$COPY_RETRIES failed; retrying in ${BACKOFF}s (it resumes)"; sleep "$BACKOFF"
    done
else
    echo "  (rsync not available on both ends: falling back to one tar stream, which is not resumable)"
    topts=(--exclude '._*' --exclude '.DS_Store')
    tar --no-xattrs -cf /dev/null /dev/null 2>/dev/null && topts+=(--no-xattrs)          # bsdtar: no LIBARCHIVE.xattr.* headers
    tar --no-mac-metadata -cf /dev/null /dev/null 2>/dev/null && topts+=(--no-mac-metadata)
    for attempt in $(seq 1 "$COPY_RETRIES"); do
        # shellcheck disable=SC2086
        if COPYFILE_DISABLE=1 tar -C "$MIRROR" "${topts[@]}" -cf - $present \
            | $SSH "${SSH_OPTS[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE || exit 1; tar -C $STAGE -xf - 2>/tmp/isr-tar.err; rc=\$?; grep -v 'Ignoring unknown extended header' /tmp/isr-tar.err >&2; exit \$rc"; then
            copy_ok=1; break
        fi
        echo "  tar attempt $attempt/$COPY_RETRIES failed; retrying in ${BACKOFF}s"; sleep "$BACKOFF"
    done
fi
if [ "$copy_ok" -ne 1 ]; then
    die "copy failed after $COPY_RETRIES attempts: the Pi is UNCHANGED (its $REMOTE_DIR was not touched). Staging is kept on the Pi; re-run to resume."
fi
if [ "$DRY" -eq 0 ]; then
    # shellcheck disable=SC2016
    $SSH "${SSH_OPTS[@]}" "$HOST" "set -e; find $STAGE -name '._*' -delete; bad=0; \
        for r in $present; do git --git-dir $STAGE/\$r fsck --connectivity-only --no-progress 2>/dev/null || { echo \"  fsck FAILED: \$r\"; bad=1; }; done; \
        [ \$bad -eq 0 ] || { echo '  staging copy is damaged: the Pi is UNCHANGED; re-run to resume'; exit 3; }; \
        rm -rf $REMOTE_DIR.new; cp -al $STAGE $REMOTE_DIR.new 2>/dev/null || cp -a $STAGE $REMOTE_DIR.new; \
        rm -rf $REMOTE_DIR.old; [ -d $REMOTE_DIR ] && mv $REMOTE_DIR $REMOTE_DIR.old; mv $REMOTE_DIR.new $REMOTE_DIR; rm -rf $REMOTE_DIR.old; \
        echo \"  \$(ls -d $REMOTE_DIR/*.git | wc -l) mirrors in place (checked with git fsck)\"" \
        || die "swap on the Pi failed: its previous $REMOTE_DIR is still in place"
fi

# ── 3. insteadOf + pi-profiles checkout ──────────────────────────────────────
echo "── 3. git insteadOf on the Pi, pi-profiles checkout ($PIPROF @ $PIBRANCH)"
rsh "set -e; abs=\$(cd $REMOTE_DIR && pwd); git config --global url.\"\$abs/\".insteadOf \"https://github.com/$OWNER/\"; \
     echo \"  insteadOf: https://github.com/$OWNER/ -> \$abs/\"; \
     if [ -d $PIPROF/.git ]; then git -C $PIPROF fetch -q origin $PIBRANCH && git -C $PIPROF checkout -q $PIBRANCH && git -C $PIPROF merge -q --ff-only origin/$PIBRANCH && echo \"  pi-profiles: \$(git -C $PIPROF log -1 --format='%h %s' | cut -c1-70)\"; \
     else git clone -q -b $PIBRANCH https://github.com/$OWNER/pi-profiles.git $PIPROF && echo \"  pi-profiles: cloned \$(git -C $PIPROF log -1 --format=%h)\"; fi" \
    || echo "  WARNING: pi-profiles checkout could not be updated (local changes? see git -C $PIPROF status)"

# ── 4. offline pip check ─────────────────────────────────────────────────────
echo "── 4. Python dependencies on the Pi"
pairs=$(awk '!/^[[:space:]]*(#|$)/ && $4 != "mac" {print $1 ":" $2}' "$KIT/repos.conf" | tr '\n' ' ')
rsh "online=0; curl -s -m 6 -o /dev/null $PIP_CHECK_URL && online=1; \
     if [ \$online -eq 1 ]; then echo '  Pi has internet: pip can fetch new dependencies.'; fi; \
     for p in $pairs; do repo=\${p%%:*}; br=\${p#*:}; venv=$OPT/\$repo/.venv; \
       req=\$(git --git-dir $REMOTE_DIR/\$repo.git show \"\$br:requirements.txt\" 2>/dev/null) || { echo \"  \$repo: no requirements.txt on \$br\"; continue; }; \
       if [ ! -x \$venv/bin/python ]; then [ \$online -eq 1 ] || echo \"  \$repo: NOT INSTALLED YET (first install needs internet for pip)\"; continue; fi; \
       [ \$online -eq 1 ] && continue; \
       printf '%s\n' \"\$req\" | grep -viE '^\s*(#|$)|^\s*-' > /tmp/isr-req-\$repo.txt; \
       out=\$(\$venv/bin/python -m pip install --dry-run --no-index --quiet -r /tmp/isr-req-\$repo.txt 2>&1); rc=\$?; \
       if [ \$rc -eq 0 ] && ! printf '%s' \"\$out\" | grep -q 'Would install'; then echo \"  \$repo: ok (all requirements already installed)\"; \
       else echo \"  \$repo: MISSING offline: \$(printf '%s' \"\$out\" | grep -oE 'for [A-Za-z0-9_.-]+|Would install .*|requirement [^ ]+' | sort -u | tr '\n' ' ' | cut -c1-200)\"; fi; \
     done"
echo "  Missing dependencies can't be installed offline: give the Pi internet once (phone tether on eth0 or wlan1),"
echo "  or skip that repo with bootstrap --only. Everything else updates from the mirrors."

# ── 5. bootstrap ─────────────────────────────────────────────────────────────
if [ "$BOOT" -eq 1 ]; then
    echo "── 5. sudo ./bootstrap.sh --skip-apt ${BOOT_ARGS[*]:-} (tmux session isr-bootstrap on the Pi)"
    args="--skip-apt"
    for a in "${BOOT_ARGS[@]:-}"; do
        [ -n "$a" ] || continue
        case "$a" in *[!A-Za-z0-9._,=/-]*) die "unsafe bootstrap argument: $a" ;; esac
        args="$args $a"
    done
    rsh "command -v tmux >/dev/null || { echo 'tmux not installed on the Pi'; exit 1; }; \
         sudo -n true 2>/dev/null || { echo 'sudo needs a password: run it yourself: ssh -t $HOST \"cd $PIPROF && sudo ./bootstrap.sh $args\"'; exit 1; }; \
         tmux kill-session -t isr-bootstrap 2>/dev/null; rm -f /tmp/isr-bootstrap.done; \
         tmux new-session -d -s isr-bootstrap \"cd $PIPROF && sudo ./bootstrap.sh $args 2>&1 | tee /tmp/isr-bootstrap.out; echo \\\$? > /tmp/isr-bootstrap.done\"; \
         echo '  started; attach with: ssh -t $HOST tmux attach -t isr-bootstrap'" || exit 1
    if [ "$DRY" -eq 0 ]; then
        printf "  waiting"
        for _ in $(seq 1 360); do
            $SSH "${SSH_OPTS[@]}" "$HOST" 'test -f /tmp/isr-bootstrap.done' 2>/dev/null && break
            printf "."; sleep 5
        done
        echo
        $SSH "${SSH_OPTS[@]}" "$HOST" "sed -n '/\\[7\\/7\\] Summary/,\$p' /tmp/isr-bootstrap.out; echo \"  bootstrap exit: \$(cat /tmp/isr-bootstrap.done 2>/dev/null || echo still running)\""
    fi
fi
[ "$MAPS" -eq 1 ] && { push_maps || true; }
echo "Done."
