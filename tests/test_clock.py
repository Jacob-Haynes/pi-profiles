"""Pi clock (offline: no NTP, no RTC battery): isr-set-clock, the Mac's pi-clock.py, the launcher's clock
card and set-from-browser, the sudoers rule, and bootstrap's restart-after-code-change."""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "launcher"))
import isr_launcher as L        # noqa: E402

HELPER = str(REPO / "bin" / "isr-set-clock")


def helper(tmp_path, *args, ntp="no"):
    env = dict(os.environ, ISR_CLOCK_DRY="1", ISR_SKIP_ROOT_CHECK="1", ISR_CLOCK_NTP=ntp,
               ISR_CLOCK_STATE=str(tmp_path / "clock.json"))
    return subprocess.run([HELPER, *args], capture_output=True, text=True, env=env)


def test_set_clock_helper(tmp_path):
    p = helper(tmp_path, "--source", "hq mac", f"{time.time() + 30:.3f}")
    assert p.returncode == 0 and "[dry] date -u -s @" in p.stdout
    assert re.search(r"clock set from hq mac: corrected (29|30|31)(\.\d)?s", p.stdout)     # macOS date has no %N
    st = json.loads((tmp_path / "clock.json").read_text())
    assert st["source"] == "hq mac" and 29 < st["skew_s"] < 31 and abs(st["ts"] - time.time()) < 5
    assert st["action"] == "set"
    (tmp_path / "clock.json").unlink()
    p = helper(tmp_path, "--source", "hq mac", f"{time.time() + 1:.3f}")
    assert "clock ok" in p.stdout and "[dry]" not in p.stdout                      # within 2 s: not changed...
    st = json.loads((tmp_path / "clock.json").read_text())                       # ...but recorded as verified
    assert st["action"] == "verified" and st["source"] == "hq mac" and abs(st["skew_s"]) <= 2
    (tmp_path / "clock.json").unlink()
    p = helper(tmp_path, "--source", "x", f"{time.time() - 600:.0f}", ntp="yes")
    assert "NTP-synced: not changing" in p.stdout and not (tmp_path / "clock.json").exists()
    p = helper(tmp_path, "--source", "x", "--force", f"{time.time() - 600:.0f}", ntp="yes")
    assert re.search(r"corrected -(599|600|601)", p.stdout)
    assert helper(tmp_path, "--source", "x", "0").returncode == 65                 # 1970 browser never wins
    assert helper(tmp_path, "--source", "x", "12;reboot").returncode == 64
    p = helper(tmp_path, "--source", 'evil"name$(x)', f"{time.time() + 30:.0f}")
    assert json.loads((tmp_path / "clock.json").read_text())["source"] == "evilnamex"
    st = json.loads(helper(tmp_path, "--status").stdout)
    assert st["ntp_synced"] == "no" and st["last_set"]["source"] == "evilnamex"


def test_pi_clock_from_mac_fallback_path(tmp_path):
    """A Pi without isr-set-clock yet: date + fake-hwclock via sudo -n. Stubs: ssh runs the remote script
    locally; the 'Pi' date is 30 s slow; sudo only records what it would run."""
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "sudo.calls"
    (fake / "sudo").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 0\n')
    (fake / "date").write_text('#!/bin/sh\nif [ "$1" = "+%s" ]; then echo $(( $(/bin/date +%s) - 30 )); else /bin/date "$@"; fi\n')
    ssh = tmp_path / "ssh"
    ssh.write_text(f'#!/bin/bash\nwhile [ "${{1#-}}" != "$1" ]; do shift 2; done\nshift\nPATH="{fake}:$PATH" exec bash -c "$*"\n')
    for f in (fake / "sudo", fake / "date", ssh):
        f.chmod(0o755)
    env = dict(os.environ, ISR_SSH=str(ssh), ISR_HOST="hq-test", ISR_STATION="hq")
    p = subprocess.run([sys.executable, str(REPO / "laptop" / "pi-clock.py"), "pi@isr-pi.local"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "clock set from hq hq-test: corrected 30s" in p.stdout or "corrected 29s" in p.stdout or "corrected 31s" in p.stdout
    c = calls.read_text()
    assert "-n date -u -s @" in c and "-n fake-hwclock save" in c
    # in sync -> nothing is set
    (fake / "date").write_text('#!/bin/sh\nexec /bin/date "$@"\n')
    calls.unlink()
    p = subprocess.run([sys.executable, str(REPO / "laptop" / "pi-clock.py"), "pi@isr-pi.local"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and "clock ok" in p.stdout and not calls.exists()
    # unreachable Pi
    ssh.write_text("#!/bin/sh\necho 'ssh: connect to host isr-pi.local port 22: No route to host' >&2\nexit 255\n")
    p = subprocess.run([sys.executable, str(REPO / "laptop" / "pi-clock.py"), "pi@isr-pi.local"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 1 and "No route to host" in p.stderr
    assert subprocess.run([sys.executable, str(REPO / "laptop" / "pi-clock.py"), "bad;host"],
                          capture_output=True, text=True).returncode == 64


def test_callers_sync_the_clock():
    assert "pi-clock.py" in (REPO / "laptop" / "hq-tunnel.sh").read_text()
    assert "pi-clock.py" in (REPO / "laptop" / "push-to-pi.sh").read_text()
    assert 'pi-clock.py" "$FROM_PI"' in (REPO / "laptop" / "setup-mac.sh").read_text()


# ── launcher ─────────────────────────────────────────────────────────────────

def test_clock_status_and_warning(tmp_path, monkeypatch):
    state = tmp_path / "clock.json"
    monkeypatch.setattr(L, "CLOCK_STATE", state)
    monkeypatch.setattr(L, "ntp_synced", lambda: False)
    L.cache.clear()
    c = L.clock_status()
    assert not c["trusted"] and "NOT SYNCED" in c["warning"] and abs(c["now"] - time.time()) < 2
    state.write_text(json.dumps({"ts": time.time() - 3600, "source": "hq mac", "skew_s": 1440.0}))
    c = L.clock_status()
    assert c["trusted"] and c["warning"] is None and c["last_set"]["source"] == "hq mac"
    state.write_text(json.dumps({"ts": time.time() - 1, "source": "hq mac", "skew_s": 0.7, "action": "verified"}))
    c = L.clock_status()
    assert c["trusted"] and c["warning"] is None and c["last_set"]["action"] == "verified"
    state.write_text(json.dumps({"ts": time.time() - 25 * 3600, "source": "hq mac", "skew_s": 3}))
    assert not L.clock_status()["trusted"]                                  # older than 24 h
    L.cache.clear()
    monkeypatch.setattr(L, "ntp_synced", lambda: True)
    assert L.clock_status()["trusted"]
    L.cache.clear()
    js = (REPO / "launcher" / "static" / "app.js").read_text() + (REPO / "launcher" / "static" / "common.js").read_text()
    assert "Set Pi clock from this device" in js and "Clock skew" in js and "/api/clock" in js and "verified by " in js


def test_set_clock_endpoint(monkeypatch):
    seen = []
    monkeypatch.setattr(L, "run", lambda cmd, timeout=5: (seen.append(cmd), (0, "clock set from browser 127.0.0.1: corrected 30.0s\n"))[1])
    monkeypatch.setattr(L, "PIN", "4321")
    srv = L.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def post(epoch, pin):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/api/clock", body=json.dumps({"epoch": epoch}),
                  headers={"Content-Type": "application/json", L.CSRF_HEADER: L.CSRF_VALUE, "X-PIN": pin})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    try:
        assert post(time.time() + 30, "0000")[0] == 401
        code, body = post(time.time() + 30, "4321")
        assert code == 200 and body["changed"]
        cmd = seen[-1]
        assert cmd[:3] == ["sudo", "-n", L.CLOCK_HELPER] and cmd[3:7] == ["--source", "browser 127.0.0.1", "--min-skew", "5"]
        n = len(seen)
        code, body = post(time.time() + 2, "4321")                       # within 5 s: no helper call at all
        assert code == 200 and body["changed"] is False and len(seen) == n
        assert post(0, "4321")[0] == 400 and post("soon", "4321")[0] == 400
    finally:
        srv.shutdown(); srv.server_close()


@pytest.mark.skipif(not shutil.which("visudo"), reason="no visudo")
def test_sudoers_is_valid():
    p = subprocess.run(["visudo", "-cf", str(REPO / "deploy" / "isr-launcher.sudoers")], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    s = (REPO / "deploy" / "isr-launcher.sudoers").read_text()
    assert "/opt/pi-profiles/bin/isr-set-clock" in s and "NOPASSWD: ALL" not in s


# ── bootstrap: restart units whose code changed ──────────────────────────────

def test_bootstrap_restarts_changed_units(tmp_path):
    src = (REPO / "bootstrap.sh").read_text()
    start = src.index('INSTALLED_DIR="${ISR_INSTALLED_DIR')
    end = src.index("\n}\n", src.index("restart_if_changed() {")) + 3
    func = src[start:end]
    repo = tmp_path / "blufor-tracker"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                                  capture_output=True, text=True, check=True)
    g("init", "-q"); (repo / "f").write_text("1"); g("add", "f"); g("commit", "-qm", "one")
    stub = tmp_path / "stub"
    (stub / "state").mkdir(parents=True)
    (stub / "state" / "mesh-bridge").write_text("active")
    script = tmp_path / "t.sh"
    script.write_text(f"""set -uo pipefail
SELF_DIR="{REPO}"; ISR_INSTALLED_DIR="{tmp_path / 'installed'}"
git_as() {{ git "$@"; }}
warn() {{ echo "WARNING: $*"; }}
{func}
restart_if_changed blufor-tracker "{repo}"
echo "RESTARTED=$RESTARTED"
""")
    env = dict(os.environ, STUB_DIR=str(stub), PATH=f"{REPO / 'tests' / 'stubs'}:{os.environ['PATH']}")
    run = lambda: subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env).stdout
    out = run()                                                   # no record yet: assume old code is running
    assert "restarted mesh-bridge" in out and "RESTARTED= mesh-bridge" in out
    assert "RESTARTED=\n" in run() or run().strip().endswith("RESTARTED=")   # same commit: nothing
    (repo / "f").write_text("2"); g("commit", "-qam", "two")
    out = run()
    assert "restarted mesh-bridge" in out and " -> " in out
    (stub / "state" / "mesh-bridge").write_text("inactive")
    (repo / "f").write_text("3"); g("commit", "-qam", "three")
    assert "restarted" not in run()                               # not running: left alone
