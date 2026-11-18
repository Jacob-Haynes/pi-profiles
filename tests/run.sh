#!/usr/bin/env bash
# Run all tests (macOS or Linux). Needs bash and python3; pytest via `python3 -m pytest`,
# or `uv run --with pytest` if pytest isn't installed.
set -euo pipefail
cd "$(dirname "$0")/.."
bash tests/test_switch.sh
if python3 -m pytest --version >/dev/null 2>&1; then
    python3 -m pytest -q tests/
elif command -v uv >/dev/null 2>&1; then
    uv run --quiet --no-project --with pytest python -m pytest -q tests/
else
    echo "pytest not found: pip install pytest (or install uv)"; exit 1
fi
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -S warning switch.sh install.sh bootstrap.sh bin/isr-switch-helper bin/isr-set-gemini-key bin/isr-netcheck bin/isr-mesh-channel bin/isr-set-site bin/isr-wired-setup bin/isr-pip-offline bin/isr-hq-mode bin/isr-push-maps bin/isr-set-clock bin/isr-hotspot-band \
        deploy/setup-mqtt-user.sh deploy/merge-env.sh laptop/hq-tunnel.sh laptop/setup-mac.sh laptop/start-mac.sh laptop/push-to-pi.sh tools/rehearsal/run_all.sh tools/sync-ui-kit.sh tests/test_switch.sh
    echo "shellcheck clean"
fi
# UI kit drift in the sibling checkouts next to this one (information only: their repos, their commits).
if ls ../*/ >/dev/null 2>&1; then
    echo "UI kit copies in sibling repos (tools/sync-ui-kit.sh --check):"
    tools/sync-ui-kit.sh --check || echo "  (drift reported above: run tools/sync-ui-kit.sh, then commit in those repos)"
fi
