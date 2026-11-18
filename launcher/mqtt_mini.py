"""Minimal MQTT 3.1.1 client (stdlib only): CONNECT, SUBSCRIBE (QoS 0), PUBLISH (QoS 0,
optional retain), PINGREQ. That's all the launcher needs to read heartbeats and publish its
own, without a venv or paho on the Pi."""
import socket
import struct
import time
from typing import Callable, Optional, Tuple

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 8, 9, 12, 13, 14

CONNACK_ERRORS = {
    1: "unacceptable protocol version",
    2: "client id rejected",
    3: "server unavailable",
    4: "bad username or password",
    5: "not authorised",
}


class MQTTError(Exception):
    pass


def _enc_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + _enc_len(len(body)) + body


def connect_packet(client_id: str, user: str = "", password: str = "", keepalive: int = 30) -> bytes:
    flags = 0x02  # clean session
    payload = _enc_str(client_id)
    if user:
        flags |= 0x80
        payload += _enc_str(user)
        if password:
            flags |= 0x40
            payload += _enc_str(password)
    var = _enc_str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    return packet(CONNECT, 0, var + payload)


def subscribe_packet(packet_id: int, topic: str) -> bytes:
    return packet(SUBSCRIBE, 0x02, struct.pack("!H", packet_id) + _enc_str(topic) + b"\x00")


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    return packet(PUBLISH, 0x01 if retain else 0x00, _enc_str(topic) + payload)


def parse_publish(flags: int, body: bytes) -> Tuple[str, bytes, bool]:
    tlen = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    rest = body[2 + tlen:]
    if (flags >> 1) & 0x03:  # QoS > 0 carries a packet id (we subscribe at QoS 0, but be safe)
        rest = rest[2:]
    return topic, rest, bool(flags & 0x01)


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class MiniMQTT:
    def __init__(self, host: str, port: int = 1883, user: str = "", password: str = "",
                 client_id: str = "isr-launcher", keepalive: int = 30, timeout: float = 5.0):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.client_id, self.keepalive, self.timeout = client_id, keepalive, timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._pid = 0
        self.last_tx = 0.0

    # ── socket plumbing ──
    def _send(self, data: bytes):
        assert self.sock is not None
        self.sock.sendall(data)
        self.last_tx = time.monotonic()

    def _try_parse(self) -> Optional[Tuple[int, int, bytes]]:
        """Take one complete packet off the buffer, or return None if it isn't all here yet.
        Nothing is consumed until the whole packet has arrived, so a read timeout mid-packet
        loses nothing."""
        buf = self._buf
        if len(buf) < 2:
            return None
        mult, length, i = 1, 0, 1
        while True:
            if i >= len(buf):
                return None
            b = buf[i]
            length += (b & 0x7F) * mult
            i += 1
            if not b & 0x80:
                break
            mult *= 128
            if i > 4:
                raise MQTTError("malformed remaining length")
        if len(buf) < i + length:
            return None
        h = buf[0]
        body = buf[i:i + length]
        self._buf = buf[i + length:]
        return h >> 4, h & 0x0F, body

    def read_packet(self) -> Tuple[int, int, bytes]:
        assert self.sock is not None
        while True:
            pkt = self._try_parse()
            if pkt is not None:
                return pkt
            chunk = self.sock.recv(4096)   # may raise socket.timeout; buffer stays intact
            if not chunk:
                raise MQTTError("connection closed by broker")
            self._buf += chunk

    # ── protocol ──
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._send(connect_packet(self.client_id, self.user, self.password, self.keepalive))
        ptype, _, body = self.read_packet()
        if ptype != CONNACK or len(body) < 2:
            raise MQTTError("no CONNACK")
        if body[1] != 0:
            raise MQTTError(CONNACK_ERRORS.get(body[1], f"refused ({body[1]})"))

    def subscribe(self, topic: str):
        self._pid = (self._pid % 65535) + 1
        self._send(subscribe_packet(self._pid, topic))

    def publish(self, topic: str, payload: bytes, retain: bool = False):
        self._send(publish_packet(topic, payload, retain))

    def ping(self):
        self._send(packet(PINGREQ, 0, b""))

    def close(self):
        if self.sock:
            try:
                self._send(packet(DISCONNECT, 0, b""))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def loop(self, on_message: Callable[[str, bytes, bool], None], tick: Callable[[], None] = lambda: None,
             tick_every: float = 1.0):
        """Read until the connection fails. Calls tick() about every tick_every seconds."""
        assert self.sock is not None
        self.sock.settimeout(tick_every)
        while True:
            try:
                ptype, flags, body = self.read_packet()
            except socket.timeout:
                ptype = None
            if ptype == PUBLISH:
                topic, payload, retained = parse_publish(flags, body)
                on_message(topic, payload, retained)
            elif ptype == SUBACK and body[-1:] == b"\x80":
                raise MQTTError("subscription refused (topic permissions?)")
            tick()
            if time.monotonic() - self.last_tx > self.keepalive / 2:
                self.ping()


def check_login(host: str, port: int, user: str, password: str) -> Optional[str]:
    """Return None if the broker accepts the credentials, else an error string."""
    c = MiniMQTT(host, port, user, password, client_id=f"isr-check-{int(time.time())}")
    try:
        c.connect()
        return None
    except (OSError, MQTTError) as e:
        return str(e)
    finally:
        c.close()
