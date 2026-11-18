"""The rehearsal CoT relay must behave like OTS 1.8.3 on PostgreSQL (contract v5), or the Mac
rehearsal can't catch the failures the real Pi shows."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RELAY = REPO / "tools" / "rehearsal" / "cot_relay.py"
MEASURE = REPO / "tools" / "rehearsal" / "measure.py"
BIND = 0.6   # short bind delay keeps the test fast; the default is 2.5 s


def ts(off=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + off)) + ".000Z"


def sa(uid, callsign, platform="isr-kit", unknown_pos=False, stale_bug=False):
    now = ts()
    stale = now if unknown_pos and not stale_bug else ts(120)
    lat, lon = ("0", "0") if unknown_pos else ("51.0", "-1.0")
    return (f'<event version="2.0" uid="{uid}" type="a-f-G-E-S" how="m-g" time="{now}" start="{now}" '
            f'stale="{stale}"><point lat="{lat}" lon="{lon}" hae="9999999" ce="9999999" le="9999999"/>'
            f'<detail><contact callsign="{callsign}" endpoint="*:-1:stcp"/><__group name="Cyan" role="Team Member"/>'
            f'<takv platform="{platform}" device="test" os="linux" version="1"/></detail></event>').encode()


def marker(uid, remarks="SIMULATED: test"):
    return (f'<event version="2.0" uid="{uid}" type="a-u-G" how="m-g" time="{ts()}" start="{ts()}" '
            f'stale="{ts(60)}"><point lat="51.0" lon="-1.0" hae="0" ce="10" le="10"/>'
            f'<detail><remarks>{remarks}</remarks></detail></event>').encode()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def relay(tmp_path):
    port = free_port()
    log = tmp_path / "cot.jsonl"
    p = subprocess.Popen([sys.executable, str(RELAY), str(port), str(log), "--bind-delay", str(BIND)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port, log
    p.terminate()
    p.wait(timeout=5)


def connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.settimeout(0.3)
    return s


def drain(s, wait=0.6):
    end, got = time.time() + wait, b""
    while time.time() < end:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        except socket.timeout:
            pass
    return got


def log_rows(log):
    return [json.loads(l) for l in log.read_text().splitlines()]


def test_ots_rules(relay):
    port, log = relay
    # Listener that waits before registering: bound.
    rx = connect(port)
    time.sleep(BIND + 0.2)
    rx.sendall(sa("isrkit-tac-advisor-isr-pi", "TAC-ADV isr-pi"))
    # Listener that registers immediately: never bound.
    hasty = connect(port)
    hasty.sendall(sa("isrkit-hasty-isr-pi", "HASTY isr-pi"))
    # A sensor that registers properly, then sends a marker on the same connection.
    good = connect(port)
    time.sleep(BIND + 0.2)
    good.sendall(sa("isrkit-drone-detector-isr-pi", "DRONE-DET isr-pi"))
    time.sleep(0.2)
    good.sendall(marker("DRONE-SIM-1"))
    # A sensor that never registers: dropped.
    bare = connect(port)
    bare.sendall(marker("ACOUSTIC-OP1-UAV"))
    # A second tool that reuses a taken callsign under a new uid: no EUD row, so its marker is dropped.
    dup = connect(port)
    time.sleep(BIND + 0.2)
    dup.sendall(sa("isrkit-op-sentry-isr-pi", "DRONE-DET isr-pi"))
    time.sleep(0.2)
    dup.sendall(marker("OPSENTRY-1"))

    got_rx, got_hasty, got_good = drain(rx), drain(hasty), drain(good, 0.2)
    assert b"DRONE-SIM-1" in got_rx and b"isrkit-drone-detector-isr-pi" in got_rx    # SA + marker relayed
    assert b"ACOUSTIC-OP1-UAV" not in got_rx and b"OPSENTRY-1" not in got_rx         # drops
    assert got_hasty == b""                                                           # unbound
    assert b"DRONE-SIM-1" not in got_good                                             # never echoed to sender

    rows = {r["uid"]: r for r in log_rows(log)}
    assert rows["DRONE-SIM-1"]["accepted"] and rows["DRONE-SIM-1"]["sender"] == "isrkit-drone-detector-isr-pi"
    assert not rows["ACOUSTIC-OP1-UAV"]["accepted"] and "no EUD row" in rows["ACOUSTIC-OP1-UAV"]["reason"]
    assert rows["isrkit-op-sentry-isr-pi"]["registration"].startswith("REJECTED")
    assert not rows["OPSENTRY-1"]["accepted"]
    assert rows["isrkit-hasty-isr-pi"]["bound"] is False and "never bound" in rows["isrkit-hasty-isr-pi"]["unbound_reason"]
    assert rows["isrkit-tac-advisor-isr-pi"]["bound"] is True
    for s in (rx, hasty, good, bare, dup):
        s.close()


def test_platform_not_bound_and_reconnect_keeps_row(relay):
    port, log = relay
    m = connect(port)
    time.sleep(BIND + 0.2)
    m.sendall(sa("isrkit-mesh-isr-pi", "MESH isr-pi", platform="Meshtastic"))
    time.sleep(0.2)
    m.close()
    # Same uid + same callsign on a new connection: the EUD row is reused, not a duplicate.
    again = connect(port)
    time.sleep(BIND + 0.2)
    again.sendall(sa("isrkit-mesh-isr-pi", "MESH isr-pi"))
    time.sleep(0.2)
    rows = [r for r in log_rows(log) if r["uid"] == "isrkit-mesh-isr-pi"]
    assert rows[0]["bound"] is False and "never bound" in rows[0]["unbound_reason"]
    assert rows[1]["registration"].startswith("registered") and rows[1]["bound"] is True
    again.close()


def test_measure_scores_v5(relay, tmp_path):
    port, log = relay
    good = connect(port)
    time.sleep(BIND + 0.2)
    good.sendall(sa("isrkit-drone-detector-isr-pi", "DRONE-DET isr-pi"))
    time.sleep(0.2)
    good.sendall(marker("DRONE-SIM-2"))
    bare = connect(port)
    bare.sendall(marker("RFSPEC-DRONE", "SIMULATED: RF"))
    time.sleep(0.4)
    good.close()
    bare.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cot.jsonl").write_text(log.read_text())
    (logs / "mqtt.jsonl").write_text("")
    p = subprocess.run([sys.executable, str(MEASURE), "--logs", str(logs)], capture_output=True, text=True)
    lines = [l.split() for l in p.stdout.splitlines() if l.startswith("[")]
    res = {(l[1] + " " + l[2], l[3]): l[0] for l in lines if len(l) > 3}
    assert res[("C5.2 registered", "drone-detector")] == "[PASS]"
    assert res[("C5.2 registered", "rf-spectrum")] == "[FAIL]"          # markers but no registration
    assert any(l[0] == "[FAIL]" and l[1:3] == ["C5.3", "not"] and "rf-spectrum" in l for l in lines)
    assert p.returncode == 1


def test_unknown_position_sa(relay, tmp_path):
    """The stale-on-arrival 0,0 SA is the one allowed 0,0 (C4.2 exception); a non-stale 0,0 SA fails."""
    port, log = relay
    ok = connect(port)
    time.sleep(BIND + 0.2)
    ok.sendall(sa("isrkit-rf-spectrum-isr-pi", "RF-SPEC isr-pi", unknown_pos=True))
    bad = connect(port)
    time.sleep(BIND + 0.2)
    bad.sendall(sa("isrkit-op-sentry-isr-pi", "OP-SENTRY isr-pi", unknown_pos=True, stale_bug=True))
    time.sleep(0.4)
    ok.close()
    bad.close()
    rows = {r["uid"]: r for r in log_rows(log)}
    assert rows["isrkit-rf-spectrum-isr-pi"]["registration"].startswith("registered")   # still registers
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cot.jsonl").write_text(log.read_text())
    (logs / "mqtt.jsonl").write_text("")
    out = subprocess.run([sys.executable, str(MEASURE), "--logs", str(logs)], capture_output=True, text=True).stdout
    bad_lines = [l for l in out.splitlines() if "unknown-pos SA" in l]
    assert len(bad_lines) == 1 and "op-sentry" in bad_lines[0] and bad_lines[0].startswith("[FAIL]")


def test_gate_heartbeat_name_and_model_checks():
    sys.path.insert(0, str(REPO / "tools"))
    import contract_gate as g
    assert g.heartbeat_name_issues('TOPIC = f"isrkit/heartbeat/{TOOL}"\nTOOL = "geo-locate"', "photoAnalysis")
    assert g.heartbeat_name_issues('{"tool": "geo-locate", "host": h}', "photoAnalysis")
    assert not g.heartbeat_name_issues('TOOL = "photoAnalysis"\n"isrkit/heartbeat/photoAnalysis"', "photoAnalysis")
    assert not g.heartbeat_name_issues('"isrkit/heartbeat/op-sentry-stream"', "op-sentry")   # contract exception
    assert g.model_issues('VLM_MODEL=qwen3-vl:8b\n') and not g.model_issues('VLM_MODEL=qwen3-vl:8b-instruct\n')
    assert g.model_issues('body = {"format": "json"}')
    assert not g.model_issues('# default used to be qwen3-vl:8b\n# never send {"format": "json"} to MLX models\nX = 1  # was qwen3-vl:8b')
