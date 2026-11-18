#!/usr/bin/env python3
"""Rehearsal CoT relay that EMULATES OpenTAKServer 1.8.3's plain-TCP rules (contract v5), so the
Mac rehearsal catches the failures the real Pi would show:

  * Registration: the first event on a connection that carries <contact> sets that connection's
    uid and creates an "EUD row", unless the callsign already belongs to a different uid
    (euds.callsign is UNIQUE in OTS: silently no row).
  * Binding: a connection receives other clients' CoT only if that first <contact> event arrived
    at least BIND_DELAY seconds after it connected (OTS's async RabbitMQ channel), and its takv
    platform isn't Meshtastic / OpenTAK ICU / DMRCOT. Never retried on that connection.
  * Store-and-route: CoT whose sender uid (the connection's uid, else the event's own uid) has no
    EUD row is DROPPED (Postgres FK cot.sender_uid -> euds.uid), not relayed.
  * Relay: accepted CoT goes to every OTHER bound connection, never back to its own sender uid.

Every event is logged to JSONL with accepted/dropped and the reason.

    cot_relay.py PORT LOG.jsonl [--bind-delay 2.5] [--lax]     (--lax = old behaviour: relay everything)
"""
import json
import re
import socket
import sys
import threading
import time

EV_END = re.compile(rb"</event>|<event[^>]*/>", re.S)
NO_BIND_PLATFORMS = {"Meshtastic", "OpenTAK ICU", "DMRCOT"}


def attr(xml, name, tag=None):
    if tag:
        m = re.search(rf"<{tag}\b[^>]*\b{name}=\"([^\"]*)\"", xml)
    else:
        m = re.search(rf'\b{name}="([^"]*)"', xml)
    return m.group(1) if m else ""


class Conn:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.t0 = time.time()
        self.uid = None          # set by the first <contact> event, like EudHandler.uid
        self.bound = False
        self.why_unbound = "no <contact> event yet"
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.sock.sendall(data)


class Relay:
    def __init__(self, port, log_path, bind_delay=2.5, lax=False):
        self.port, self.bind_delay, self.lax = port, bind_delay, lax
        self.log = open(log_path, "a", buffering=1)
        self.conns = set()
        self.euds = {}           # uid -> callsign  (persists across connections, like the DB)
        self.lock = threading.Lock()

    def serve(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(64)
        print(f"cot relay on 127.0.0.1:{self.port} ({'LAX' if self.lax else 'OTS-1.8.3 rules'}, "
              f"bind delay {self.bind_delay}s)", flush=True)
        while True:
            sock, addr = s.accept()
            c = Conn(sock, addr)
            with self.lock:
                self.conns.add(c)
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        buf = b""
        try:
            while True:
                data = c.sock.recv(65536)
                if not data:
                    break
                buf += data
                while True:
                    start = buf.find(b"<event")
                    if start < 0:
                        buf = b""
                        break
                    m = EV_END.search(buf, start)
                    if not m:
                        buf = buf[start:]
                        break
                    ev, buf = buf[start:m.end()], buf[m.end():]
                    self.on_event(ev, c)
        except OSError:
            pass
        finally:
            with self.lock:
                self.conns.discard(c)
            c.sock.close()

    def register(self, c, x, uid):
        """parse_device_info on the first <contact> event of a connection."""
        callsign = attr(x, "callsign", "contact")
        platform = attr(x, "platform", "takv")
        c.uid = uid
        with self.lock:
            owner = next((u for u, cs in self.euds.items() if cs == callsign and u != uid), None)
            if owner:
                reg = f"REJECTED: callsign {callsign!r} already belongs to {owner} (UNIQUE): no EUD row"
            else:
                self.euds[uid] = callsign
                reg = f"registered {uid} as {callsign!r}"
        dt = time.time() - c.t0
        if platform in NO_BIND_PLATFORMS:
            c.bound, c.why_unbound = False, f"platform {platform!r} is never bound"
        elif dt < self.bind_delay:
            c.bound, c.why_unbound = False, f"first <contact> {dt:.2f}s after connect (< {self.bind_delay}s): never bound"
        else:
            c.bound, c.why_unbound = True, ""
        return reg, dt

    def on_event(self, ev, c):
        x = ev.decode("utf-8", "replace")
        uid = attr(x, "uid")
        reg, dt = None, None
        if c.uid is None and re.search(r"<contact\b", x) and not uid.endswith("ping"):
            reg, dt = self.register(c, x, uid)
        sender = c.uid or uid
        with self.lock:
            has_row = sender in self.euds
        accepted = self.lax or has_row
        reason = "" if accepted else f"DROPPED: sender {sender!r} has no EUD row (FK)"
        rem = re.search(r"<remarks[^>]*>(.*?)</remarks>", x, re.S)
        self.log.write(json.dumps({
            "t": time.time(), "from_port": c.addr[1], "conn_uid": c.uid, "sender": sender,
            "uid": uid, "type": attr(x, "type"), "how": attr(x, "how"),
            "callsign": attr(x, "callsign", "contact") or attr(x, "callsign"),
            "takv_platform": attr(x, "platform", "takv"),
            "remarks": (rem.group(1).strip() if rem else "")[:500], "len": len(ev),
            "accepted": accepted, "reason": reason,
            "registration": reg, "reg_delay_s": None if dt is None else round(dt, 2),
            "bound": c.bound, "unbound_reason": c.why_unbound if reg else None,
            "raw": x[:1500]}) + "\n")
        if not accepted:
            return
        with self.lock:
            targets = [t for t in self.conns if t is not c and (self.lax or t.bound) and t.uid != sender]
        for t in targets:
            try:
                t.send(ev)
            except OSError:
                pass


if __name__ == "__main__":
    args = sys.argv[1:]
    delay = float(args[args.index("--bind-delay") + 1]) if "--bind-delay" in args else 2.5
    Relay(int(args[0]), args[1], bind_delay=delay, lax="--lax" in args).serve()
