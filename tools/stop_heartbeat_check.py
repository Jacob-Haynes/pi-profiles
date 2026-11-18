#!/usr/bin/env python3
"""Gate check: does each tool publish a retained "stopped" heartbeat when it gets SIGTERM?

For every repo that ships scripts/run-laptop.sh, this starts the tool against a throwaway
local MQTT broker (built in, below), waits for its first heartbeat, sends SIGTERM to the
process group, and checks that a *retained* isrkit/heartbeat/<tool> with status "stopped"
arrived before the broker connection dropped.

    tools/stop_heartbeat_check.py --src DIR [repo ...]   # DIR/<repo> checkouts
    tools/stop_heartbeat_check.py --src DIR --first-heartbeat-timeout 900 fpv-vision

Isolation: each tool gets HOME set to a temporary directory, so venvs, caches and data go
there and not into your real home. The first run downloads each tool's Python deps, which can
take a while for the YOLO tools. Nothing talks to the real Pi: TAK points at a closed local
port and MQTT at the built-in broker.

Low priority by design: a tool that fails this still goes "offline" in the launcher after the
heartbeat timeout. It just doesn't say "stopped".
"""
import argparse
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

# Extra args per repo to keep the run light (simulate/replay, no camera).
RUN_ARGS = {
    "change-detect": ["--simulate"],
    "op-sentry": ["--simulate"],
}
RUN_ENV = {
    "fpv-vision": {"FPV_SOURCE": "simulate"},
    "acoustic-detector": {"ACOUSTIC_INPUT": "simulate"},
}


class Broker:
    """Minimal MQTT 3.1.1 broker: enough for paho/mini clients to CONNECT, SUBSCRIBE and
    PUBLISH (QoS 0/1). Records every PUBLISH with its retain flag."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.lock = threading.Lock()
        self.publishes = []   # (time, topic, payload bytes, retain)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _packet(self, conn):
        h = self._read(conn, 1)[0]
        mult, length = 1, 0
        while True:
            b = self._read(conn, 1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return h >> 4, h & 0x0F, self._read(conn, length) if length else b""

    def _client(self, conn):
        try:
            while True:
                ptype, flags, body = self._packet(conn)
                if ptype == 1:      # CONNECT
                    level = body[6] if len(body) > 6 else 4
                    if level == 5:  # MQTT 5 CONNACK: flags, reason, zero-length properties
                        conn.sendall(bytes([0x20, 3, 0, 0, 0]))
                    else:
                        conn.sendall(bytes([0x20, 2, 0, 0]))
                    self.v5 = level == 5
                elif ptype == 3:    # PUBLISH
                    qos = (flags >> 1) & 3
                    tlen = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tlen].decode("utf-8", "replace")
                    rest = body[2 + tlen:]
                    pid = None
                    if qos:
                        pid, rest = rest[:2], rest[2:]
                    if getattr(self, "v5", False) and rest:   # skip MQTT 5 properties
                        plen, mult, i = 0, 1, 0
                        while True:
                            b = rest[i]
                            plen += (b & 0x7F) * mult
                            i += 1
                            if not b & 0x80:
                                break
                            mult *= 128
                        rest = rest[i + plen:]
                    with self.lock:
                        self.publishes.append((time.time(), topic, rest, bool(flags & 1)))
                    if qos == 1:
                        conn.sendall(bytes([0x40, 2]) + pid)
                    elif qos == 2:
                        conn.sendall(bytes([0x50, 2]) + pid)
                elif ptype == 6:    # PUBREL
                    conn.sendall(bytes([0x70, 2]) + body[:2])
                elif ptype == 8:    # SUBSCRIBE -> grant QoS 0 for each filter
                    pid, rest, n = body[:2], body[2:], 0
                    if getattr(self, "v5", False):
                        rest = rest[1 + rest[0]:] if rest and rest[0] < 0x80 else rest
                    while rest:
                        tl = struct.unpack("!H", rest[:2])[0]
                        rest = rest[2 + tl + 1:]
                        n += 1
                    extra = b"\x00" if getattr(self, "v5", False) else b""
                    payload = pid + extra + bytes([0] * max(n, 1))
                    conn.sendall(bytes([0x90, len(payload)]) + payload)
                elif ptype == 12:   # PINGREQ
                    conn.sendall(bytes([0xD0, 0]))
                elif ptype == 14:   # DISCONNECT
                    break
        except (ConnectionError, OSError, IndexError):
            pass
        finally:
            conn.close()

    def heartbeats(self, tool):
        with self.lock:
            out = []
            for t, topic, payload, retain in self.publishes:
                if topic == f"isrkit/heartbeat/{tool}":
                    try:
                        out.append((t, json.loads(payload.decode("utf-8", "replace")), retain))
                    except ValueError:
                        out.append((t, {"_raw": payload[:80]}, retain))
            return out


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check(repo_dir, repo, first_timeout, stop_timeout):
    script = os.path.join(repo_dir, "scripts", "run-laptop.sh")
    if not os.path.exists(script):
        return "skip", "no scripts/run-laptop.sh"
    broker = Broker()
    home = tempfile.mkdtemp(prefix=f"isr-stopcheck-{repo}-")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "TMPDIR": home,
        "TAK_HOST": "127.0.0.1", "TAK_PORT": str(closed_port()),
        "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(broker.port), "MQTT_USER": "isrkit", "MQTT_PASS": "test",
        "VLM_URL": "http://127.0.0.1:1", "LLM_URL": "http://127.0.0.1:1", "GEMINI_API_KEY": "",
        "PYTHONUNBUFFERED": "1",
    }
    env.update(RUN_ENV.get(repo, {}))
    if os.environ.get("UV_CACHE_DIR"):
        env["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    log = open(os.path.join(home, "run.log"), "wb")
    p = subprocess.Popen(["bash", script] + RUN_ARGS.get(repo, []), cwd=repo_dir, env=env,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + first_timeout
    while time.time() < deadline and not broker.heartbeats(repo) and p.poll() is None:
        time.sleep(1)
    if not broker.heartbeats(repo):
        rc = p.poll()
        _kill(p)
        tail = open(os.path.join(home, "run.log"), "rb").read()[-400:].decode("utf-8", "replace")
        return "fail", (f"no heartbeat within {first_timeout}s" if rc is None else f"exited rc={rc} before a heartbeat") \
            + f"; log {home}/run.log: ...{tail.strip()[-200:]}"
    first = broker.heartbeats(repo)[0][1]
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(timeout=stop_timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        return "fail", f"still running {stop_timeout}s after SIGTERM"
    time.sleep(0.5)
    hbs = broker.heartbeats(repo)
    last_t, last, retained = hbs[-1]
    stopped = [h for h in hbs if h[1].get("status") == "stopped"]
    if stopped and stopped[-1][2]:
        return "pass", f"retained status=stopped (first heartbeat mode={first.get('mode')}, exit rc={p.returncode})"
    if stopped:
        return "fail", "status=stopped published but NOT retained"
    return "fail", f"no stopped heartbeat after SIGTERM (last status={last.get('status')!r}, exit rc={p.returncode})"


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--first-heartbeat-timeout", type=int, default=600)
    ap.add_argument("--stop-timeout", type=int, default=20)
    ap.add_argument("repos", nargs="*")
    a = ap.parse_args()
    repos = a.repos or sorted(d for d in os.listdir(a.src) if os.path.isdir(os.path.join(a.src, d, ".git")))
    bad = 0
    for r in repos:
        status, msg = check(os.path.join(a.src, r), r, a.first_heartbeat_timeout, a.stop_timeout)
        print(f"[{status.upper():4}] {r:18} {msg}", flush=True)
        bad += status == "fail"
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
