#!/usr/bin/env python3
"""Rehearsal MQTT broker (stdlib): MQTT 3.1.1 and 5, QoS 0/1 in (delivered as QoS 0),
retained messages, + and # wildcards, no auth (any login accepted). Logs every PUBLISH to a
JSONL file. For the Mac integration rehearsal only; it stands in for the Pi's RabbitMQ.

    broker.py PORT LOG.jsonl [--rabbit-retained]

--rabbit-retained behaves like RabbitMQ's MQTT plugin: retained messages are replayed only to
subscriptions without wildcards (isrkit/station/mac-x gets it, isrkit/station/+ doesn't)."""
import json
import socket
import struct
import sys
import threading
import time


def enc_len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def dec_varint(buf, i):
    mult, val = 1, 0
    while True:
        b = buf[i]
        val += (b & 0x7F) * mult
        i += 1
        if not b & 0x80:
            return val, i
        mult *= 128


def match(filt, topic):
    f, t = filt.split("/"), topic.split("/")
    for i, p in enumerate(f):
        if p == "#":
            return True
        if i >= len(t) or (p != "+" and p != t[i]):
            return False
    return len(f) == len(t)


class Client:
    def __init__(self, conn, addr):
        self.conn, self.addr = conn, addr
        self.v5 = False
        self.subs = set()
        self.cid = "?"
        self.wlock = threading.Lock()

    def send(self, data):
        with self.wlock:
            self.conn.sendall(data)

    def publish(self, topic, payload, retain):
        t = topic.encode()
        body = struct.pack("!H", len(t)) + t + (b"\x00" if self.v5 else b"") + payload
        self.send(bytes([0x30 | (1 if retain else 0)]) + enc_len(len(body)) + body)


class Broker:
    def __init__(self, port, log_path, rabbit_retained=False):
        self.port, self.rabbit_retained = port, rabbit_retained
        self.log = open(log_path, "a", buffering=1)
        self.clients = set()
        self.retained = {}
        self.lock = threading.Lock()

    def serve(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(64)
        print(f"broker on 127.0.0.1:{self.port}", flush=True)
        while True:
            conn, addr = s.accept()
            threading.Thread(target=self.handle, args=(Client(conn, addr),), daemon=True).start()

    def read(self, c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def packet(self, c):
        h = self.read(c, 1)[0]
        mult, length = 1, 0
        while True:
            b = self.read(c, 1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return h >> 4, h & 0x0F, self.read(c, length) if length else b""

    def route(self, topic, payload, retain, src):
        self.log.write(json.dumps({"t": time.time(), "topic": topic, "retain": retain, "from": src,
                                   "payload": payload.decode("utf-8", "replace")[:2000]}) + "\n")
        with self.lock:
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [c for c in self.clients if any(match(f, topic) for f in c.subs)]
        for c in targets:
            try:
                c.publish(topic, payload, False)
            except OSError:
                pass

    def handle(self, c):
        try:
            while True:
                ptype, flags, body = self.packet(c)
                if ptype == 1:  # CONNECT
                    plen = struct.unpack("!H", body[:2])[0]
                    level = body[2 + plen]
                    c.v5 = level == 5
                    i = 2 + plen + 1 + 1 + 2
                    if c.v5:
                        n, i = dec_varint(body, i)
                        i += n
                    cl = struct.unpack("!H", body[i:i + 2])[0]
                    c.cid = body[i + 2:i + 2 + cl].decode("utf-8", "replace") or f"anon-{c.addr[1]}"
                    c.send(bytes([0x20, 3, 0, 0, 0]) if c.v5 else bytes([0x20, 2, 0, 0]))
                    with self.lock:
                        self.clients.add(c)
                elif ptype == 3:  # PUBLISH
                    qos = (flags >> 1) & 3
                    tl = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tl].decode("utf-8", "replace")
                    i = 2 + tl
                    pid = None
                    if qos:
                        pid = body[i:i + 2]
                        i += 2
                    if c.v5:
                        n, i = dec_varint(body, i)
                        i += n
                    self.route(topic, body[i:], bool(flags & 1), c.cid)
                    if qos == 1:
                        c.send(bytes([0x40, 2]) + pid)
                    elif qos == 2:
                        c.send(bytes([0x50, 2]) + pid)
                elif ptype == 6:  # PUBREL
                    c.send(bytes([0x70, 2]) + body[:2])
                elif ptype == 8:  # SUBSCRIBE
                    pid, i, filters = body[:2], 2, []
                    if c.v5:
                        n, i = dec_varint(body, i)
                        i += n
                    while i < len(body):
                        tl = struct.unpack("!H", body[i:i + 2])[0]
                        filters.append(body[i + 2:i + 2 + tl].decode("utf-8", "replace"))
                        i += 2 + tl + 1
                    payload = pid + (b"\x00" if c.v5 else b"") + bytes([0] * len(filters))
                    c.send(bytes([0x90]) + enc_len(len(payload)) + payload)
                    with self.lock:
                        c.subs.update(filters)
                        replay = [f for f in filters if not (self.rabbit_retained and ("+" in f or "#" in f))]
                        ret = [(t, p) for t, p in self.retained.items() if any(match(f, t) for f in replay)]
                    for t, p in ret:
                        c.publish(t, p, True)
                elif ptype == 10:  # UNSUBSCRIBE
                    c.send(bytes([0xB0, 2]) + body[:2] if not c.v5 else bytes([0xB0, 3]) + body[:2] + b"\x00")
                elif ptype == 12:  # PINGREQ
                    c.send(bytes([0xD0, 0]))
                elif ptype == 14:  # DISCONNECT
                    break
        except (ConnectionError, OSError, IndexError, struct.error):
            pass
        finally:
            with self.lock:
                self.clients.discard(c)
            try:
                c.conn.close()
            except OSError:
                pass


if __name__ == "__main__":
    Broker(int(sys.argv[1]), sys.argv[2], rabbit_retained="--rabbit-retained" in sys.argv[3:]).serve()
