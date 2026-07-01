"""Minimal stdlib MQTT 3.1.1 client used for broker validation, bring-up confirmation,
broker/LAN discovery probes, the cooperative-quiesce command, and retained-status
clearing. Mirrors the firmware's wire format (src/mqtt_packets.py).

The pure encoders (build_connect / build_subscribe / build_publish / _encode_rl) carry
no I/O and are unit-tested on the host.
"""
import socket
import struct
import time

from .constants import DEFAULT_BROKER_PORT


# ---- minimal MQTT 3.1.1 encode (pure) -------------------------------------
def _mqtt_str(s):
    if isinstance(s, str):
        s = s.encode()
    return struct.pack(">H", len(s)) + s


def _encode_rl(n):
    out = bytearray()
    while True:
        d = n & 0x7F
        n >>= 7
        if n:
            d |= 0x80
        out.append(d)
        if not n:
            return bytes(out)


def build_connect(client_id, keepalive=15, user=None, password=None):
    flags = 0x02                                # clean session
    if user is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    var = _mqtt_str("MQTT") + bytes([0x04, flags]) + struct.pack(">H", keepalive)
    payload = _mqtt_str(client_id)
    if user is not None:
        payload += _mqtt_str(user)
    if password is not None:
        payload += _mqtt_str(password)
    body = var + payload
    return bytes([0x10]) + _encode_rl(len(body)) + body


def build_subscribe(packet_id, topic):
    body = struct.pack(">H", packet_id) + _mqtt_str(topic) + b"\x00"   # QoS0
    return bytes([0x82]) + _encode_rl(len(body)) + body


def build_publish(topic, payload=b"", retain=False):
    body = _mqtt_str(topic) + payload                  # QoS0 -> no packet id
    header = 0x30 | (0x01 if retain else 0x00)
    return bytes([header]) + _encode_rl(len(body)) + body


# ---- I/O wrappers ---------------------------------------------------------
def _read_packet(sock):
    """Read one MQTT packet -> (type_byte, body_bytes) or None on EOF/timeout."""
    try:
        hdr = sock.recv(1)
        if not hdr:
            return None
        mult, length = 1, 0
        while True:
            b = sock.recv(1)
            if not b:
                return None
            length += (b[0] & 0x7F) * mult
            if not (b[0] & 0x80):
                break
            mult *= 128
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return (hdr[0], body)
    except socket.timeout:
        return None


def validate(ip, port, user, password, timeout=5.0):
    """TCP-connect and (if creds given) MQTT CONNECT. -> (ok, detail)."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        return (False, "cannot reach %s:%d (%s)" % (ip, port, e))
    try:
        sock.settimeout(timeout)
        sock.sendall(build_connect("provision-check", user=user, password=password))
        pkt = _read_packet(sock)
        if pkt is None or pkt[0] != 0x20 or len(pkt[1]) < 2:
            return (False, "no/invalid CONNACK from broker (is it MQTT?)")
        code = pkt[1][1]
        if code == 0:
            return (True, "broker accepted the connection")
        reasons = {1: "unacceptable protocol version", 2: "client id rejected",
                   3: "server unavailable", 4: "bad username or password",
                   5: "not authorized"}
        return (False, "broker refused: %s" % reasons.get(code, "code %d" % code))
    finally:
        _disconnect(sock)


def wait_online(ip, port, user, password, base_topic, timeout=45.0):
    """Subscribe to <base>/+/status and wait for a published/retained 'online'. This is
    the authoritative bring-up PASS signal (broker truth, independent of USB serial).
    -> (ok, device_id_or_None).

    keepalive=0 so the broker never drops this read-only watcher for inactivity before a
    slow board (DHCP lease + discovery burst) publishes 'online'."""
    topic = base_topic + "/+/status"
    deadline = time.time() + timeout
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return (False, None)
    try:
        sock.settimeout(2.0)
        sock.sendall(build_connect("provision-watch", keepalive=0,
                                   user=user, password=password))
        pkt = _read_packet(sock)
        if pkt is None or pkt[0] != 0x20:
            return (False, None)
        sock.sendall(build_subscribe(1, topic))
        while time.time() < deadline:
            pkt = _read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30:            # PUBLISH
                ptopic, payload = _split_publish(pkt[1])
                if payload == b"online" and ptopic.endswith("/status"):
                    parts = ptopic.split("/")
                    return (True, parts[1] if len(parts) >= 3 else None)
        return (False, None)
    finally:
        _disconnect(sock)


def list_online(ip, port, user, password, base_topic, timeout=5.0):
    """Device ids currently 'online' on <base>/+/status (collected over a short window).
    NETWORK-only -- does not touch USB, so a running unit's MQTT session stays alive."""
    topic = base_topic + "/+/status"
    deadline = time.time() + timeout
    state = {}
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return []
    try:
        sock.settimeout(2.0)
        sock.sendall(build_connect("provision-scan", keepalive=0,
                                   user=user, password=password))
        if (_read_packet(sock) or (None,))[0] != 0x20:
            return []
        sock.sendall(build_subscribe(1, topic))
        while time.time() < deadline:
            pkt = _read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30:
                ptopic, payload = _split_publish(pkt[1])
                parts = ptopic.split("/")
                if ptopic.endswith("/status") and len(parts) >= 3:
                    state[parts[1]] = (payload == b"online")
        return [d for d, up in state.items() if up]
    finally:
        _disconnect(sock)


def send_command(ip, port, user, password, base_topic, device_id, name, payload=b""):
    """Publish <base>/<id>/cmd/<name> (QoS0, not retained) -> ok bool."""
    topic = "%s/%s/cmd/%s" % (base_topic, device_id, name)
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return False
    try:
        sock.settimeout(5.0)
        sock.sendall(build_connect("provision-cmd", user=user, password=password))
        if _read_packet(sock) is None:
            return False
        sock.sendall(build_publish(topic, payload))
        time.sleep(0.3)
        return True
    finally:
        _disconnect(sock)


def clear_retained_status(ip, port, user, password, base_topic, device_id):
    """Clear the retained status message for this device (empty retained payload) so a
    post-reset wait can't false-pass on a stale 'online' from a prior run."""
    topic = "%s/%s/status" % (base_topic, device_id)
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return
    try:
        sock.settimeout(5.0)
        sock.sendall(build_connect("provision-clear", user=user, password=password))
        if _read_packet(sock) is None:
            return
        sock.sendall(build_publish(topic, b"", retain=True))
        time.sleep(0.3)
    finally:
        _disconnect(sock)


def watch(ip, port, user, password, topics, duration=10.0, on_message=None):
    """Subscribe to `topics` and collect PUBLISHes for `duration` seconds. NETWORK-only
    (never touches USB, so a running unit's own MQTT session stays alive). Returns a list
    of (topic, payload_bytes, elapsed_s). Retained messages arrive first (elapsed ~0); the
    caller disambiguates retained-vs-live by elapsed time / correlation with a trigger.

    keepalive=0 so the broker never drops this read-only watcher for inactivity."""
    start = time.time()
    deadline = start + duration
    out = []
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return out
    try:
        sock.settimeout(1.0)
        sock.sendall(build_connect("oselia-watch", keepalive=0, user=user, password=password))
        if (_read_packet(sock) or (None,))[0] != 0x20:
            return out
        for i, t in enumerate(topics, 1):
            sock.sendall(build_subscribe(i, t))
        while time.time() < deadline:
            pkt = _read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30:            # PUBLISH (QoS0 -> no packet id)
                ptopic, payload = _split_publish(pkt[1])
                elapsed = time.time() - start
                out.append((ptopic, payload, elapsed))
                if on_message:
                    on_message(ptopic, payload, elapsed)
        return out
    finally:
        _disconnect(sock)


def publish(ip, port, user, password, topic, payload=b"", retain=False):
    """Publish an arbitrary topic/payload (QoS0). Generalises send_command for the
    acceptance suite (drive a `…/cmd/*`, seed/clear a retained topic). -> ok bool."""
    if isinstance(payload, str):
        payload = payload.encode()
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return False
    try:
        sock.settimeout(5.0)
        sock.sendall(build_connect("oselia-pub", user=user, password=password))
        if _read_packet(sock) is None:
            return False
        sock.sendall(build_publish(topic, payload, retain=retain))
        time.sleep(0.3)
        return True
    finally:
        _disconnect(sock)


def probe_broker(host):
    """(host, 1883) if it speaks MQTT (TCP open + a CONNACK to our CONNECT), else None."""
    try:
        sock = socket.create_connection((host, DEFAULT_BROKER_PORT), timeout=0.5)
    except OSError:
        return None
    try:
        sock.settimeout(1.0)
        sock.sendall(build_connect("oselia-scan"))
        hdr = sock.recv(1)
        if hdr and hdr[0] == 0x20:           # CONNACK -> confirmed broker
            return (host, DEFAULT_BROKER_PORT)
    except OSError:
        pass
    finally:
        _disconnect(sock)
    return None


# ---- helpers --------------------------------------------------------------
def _split_publish(body):
    tlen = struct.unpack(">H", body[:2])[0]
    return (body[2:2 + tlen].decode("utf-8", "replace"), body[2 + tlen:])


def _disconnect(sock):
    try:
        sock.sendall(b"\xE0\x00")           # DISCONNECT
    except OSError:
        pass
    sock.close()
