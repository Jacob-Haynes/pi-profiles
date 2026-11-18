#!/usr/bin/env bash
# Copy the shared UI kit (pi-profiles/ui/isr-ui.css, isr-ui.js and fonts/, the source of truth) into every
# sibling repo's static folder, or check that the copies there haven't drifted.
#
#   tools/sync-ui-kit.sh --check [--root ~/Repos/isr-kit]   report every copy: ok | DRIFT | missing (exit 1 on drift)
#   tools/sync-ui-kit.sh [--root DIR] [repo ...]           copy into each repo (default: all known repos)
#
# Copies go to the static folder each tool serves (TARGETS below). Copying also sets the cache-busting
# version on every link to the kit (href/src "…isr-ui.css?v=N" / "…isr-ui.js?v=N", N from line 1 of
# ui/isr-ui.css); --check reports links whose version is missing or stale. After copying, commit in
# each repo. Never edit a copy: change ui/ here and run this again.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$HERE")"
CHECK=0
ONLY=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1; shift ;;
        --root) ROOT="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 64 ;;
        *) ONLY+=("$1"); shift ;;
    esac
done
FILES="isr-ui.css isr-ui.js"     # plus fonts/ beside the CSS (kit v2)
# repo:static folder (relative to the repo).
TARGETS="change-detect:changedetect/web/static rf-spectrum:rfspectrum/web/static drone-detector:detector/web/static
op-sentry:opsentry/web/static acoustic-detector:acoustic/web/static fpv-vision:fpv_vision/web/static
mesh-deadrop:deadrop/web/static tac-advisor:advisor/web/static photoAnalysis:geolocate/static
blufor-tracker:bridge/web/static"

KIT_V=$(head -1 "$HERE/ui/isr-ui.css" | sed -n 's/.*isr-ui\.css v\([0-9][0-9]*\).*/\1/p')
[ -n "$KIT_V" ] || { echo "can't read the kit version from line 1 of ui/isr-ui.css" >&2; exit 1; }
# Only links in href/src attributes, never a bare file name (a Python route serving "isr-ui.css" stays as is).
LINK_RE='((?:href|src)\s*=\s*["'"'"'][^"'"'"'<>]*?isr-ui\.(?:css|js))(?:\?v=[0-9]+)?(?=["'"'"'])'
link_files() {   # text files in a repo that link the kit
    grep -rlE --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git --exclude=isr-ui.css --exclude=isr-ui.js \
        --include='*.html' --include='*.htm' --include='*.j2' --include='*.jinja' --include='*.py' --include='*.js' \
        'isr-ui\.(css|js)' "$1" 2>/dev/null
}
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }
want() { [ "${#ONLY[@]}" -eq 0 ] && return 0; local r; for r in "${ONLY[@]}"; do [ "$r" = "$1" ] && return 0; done; return 1; }

drift=0
for t in $TARGETS; do
    repo="${t%%:*}" dir="${t#*:}"
    want "$repo" || continue
    [ -d "$ROOT/$repo" ] || { [ "$CHECK" -eq 1 ] && printf "  %-8s %s (repo not checked out)\n" skip "$repo"; continue; }
    # A repo may have vendored the kit somewhere else: check every copy it has.
    copies=$(find "$ROOT/$repo" -path '*/.venv' -prune -o -path '*/node_modules' -prune -o -path '*/.git' -prune \
             -o \( -name isr-ui.css -o -name isr-ui.js \) -type f -print 2>/dev/null)
    fontcopies=""
    while IFS= read -r c; do
        [ -n "$c" ] && [ "$(basename "$c")" = isr-ui.css ] && fontcopies="$fontcopies $(dirname "$c")/fonts"
    done <<< "$copies"
    if [ "$CHECK" -eq 1 ]; then
        if [ -z "$copies" ]; then printf "  %-8s %s/%s\n" missing "$repo" "$dir"; continue; fi
        while IFS= read -r c; do
            f=$(basename "$c")
            if [ "$(sha "$c")" = "$(sha "$HERE/ui/$f")" ]; then s=ok; else s=DRIFT; drift=1; fi
            printf "  %-8s %s\n" "$s" "${c#"$ROOT"/}"
        done <<< "$copies"
        while IFS= read -r lf; do
            [ -n "$lf" ] || continue
            stale=$(perl -ne 'while (/'"$LINK_RE"'/g) { my $m = $&; print "$m\n" unless $m =~ /\?v='"$KIT_V"'$/ }' "$lf" 2>/dev/null \
                    | sed 's/.*=[[:space:]]*["'"'"']//' | head -3 | tr '\n' ' ')
            [ -n "$stale" ] && { printf "  %-8s %s: %s(want ?v=%s)\n" LINK "${lf#"$ROOT"/}" "$stale" "$KIT_V"; drift=1; }
        done <<< "$(link_files "$ROOT/$repo")"
        for fd in $fontcopies; do
            for f in "$HERE"/ui/fonts/*; do
                c="$fd/$(basename "$f")"
                if [ ! -f "$c" ]; then s=missing; drift=1
                elif [ "$(sha "$c")" = "$(sha "$f")" ]; then s=ok; else s=DRIFT; drift=1; fi
                [ "$s" = ok ] || printf "  %-8s %s\n" "$s" "${c#"$ROOT"/}"
            done
        done
    else
        dest="$ROOT/$repo/$dir"
        mkdir -p "$dest"
        for f in $FILES; do cp "$HERE/ui/$f" "$dest/$f"; done
        mkdir -p "$dest/fonts" && cp "$HERE"/ui/fonts/* "$dest/fonts/"
        # Also refresh copies vendored elsewhere in the repo.
        while IFS= read -r c; do
            [ -n "$c" ] || continue
            cp "$HERE/ui/$(basename "$c")" "$c"
            [ "$(basename "$c")" = isr-ui.css ] && mkdir -p "$(dirname "$c")/fonts" && cp "$HERE"/ui/fonts/* "$(dirname "$c")/fonts/"
        done <<< "$copies"
        while IFS= read -r lf; do
            [ -n "$lf" ] || continue
            perl -pi -e 's/'"$LINK_RE"'/$1?v='"$KIT_V"'/g' "$lf"
        done <<< "$(link_files "$ROOT/$repo")"
        echo "  copied into $repo/$dir (kit links set to ?v=$KIT_V)"
    fi
done
[ "$CHECK" -eq 1 ] && [ "$drift" -eq 1 ] && { echo "UI kit drift: run tools/sync-ui-kit.sh, then commit in those repos"; exit 1; }
exit 0
