"""MQTT 3.1.1 packet builders/parsers -- PURE, no I/O.

Kept separate from mqtt_client so the wire format is unit-testable on a host and
can be checked against the known-good bytes from the POC. What this client needs:
CONNECT (+auth +LWT), PUBLISH (QoS0), PINGREQ, DISCONNECT, SUBSCRIBE, inbound-PUBLISH
parsing (for two-way control), plus remaining-length encode/decode.
"""

# Control packet type bytes (with flags where fixed)
CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30          # QoS0, no DUP/RETAIN here (RETAIN added per-call)
PUBACK = 0x40
SUBSCRIBE = 0x82        # fixed lower nibble 0b0010 is required by the spec
SUBACK = 0x90
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0

PINGREQ_PKT = b"\xC0\x00"
DISCONNECT_PKT = b"\xE0\x00"

# CONNECT flag bits
_F_CLEAN = 0x02
_F_WILL = 0x04
_F_WILL_RETAIN = 0x20
_F_PASSWORD = 0x40
_F_USERNAME = 0x80


def encode_remaining_length(n):
    out = bytearray()
    while True:
        digit = n & 0x7F
        n >>= 7
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n == 0:
            return bytes(out)


def decode_remaining_length(read_byte):
    """read_byte() -> int (0..255) or None. Returns (length, num_bytes) or None."""
    multiplier = 1
    value = 0
    count = 0
    while True:
        b = read_byte()
        if b is None:
            return None
        value += (b & 0x7F) * multiplier
        count += 1
        if (b & 0x80) == 0:
            return (value, count)
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("malformed remaining length")


def _str(s):
    if isinstance(s, str):
        s = s.encode()
    return bytes([len(s) >> 8, len(s) & 0xFF]) + s


def build_connect(client_id, keepalive=30, clean=True,
                  user=None, password=None,
                  lwt_topic=None, lwt_msg=None, lwt_qos=0, lwt_retain=False):
    flags = 0
    if clean:
        flags |= _F_CLEAN
    if lwt_topic is not None:
        flags |= _F_WILL | ((lwt_qos & 0x03) << 3)
        if lwt_retain:
            flags |= _F_WILL_RETAIN
    if user is not None:
        flags |= _F_USERNAME
    if password is not None:
        flags |= _F_PASSWORD

    var = bytearray()
    var += _str("MQTT")                 # protocol name
    var.append(0x04)                    # protocol level 4 (3.1.1)
    var.append(flags)
    var += bytes([keepalive >> 8, keepalive & 0xFF])

    payload = bytearray()
    payload += _str(client_id)
    if lwt_topic is not None:
        payload += _str(lwt_topic)
        payload += _str(lwt_msg if lwt_msg is not None else "")
    if user is not None:
        payload += _str(user)
    if password is not None:
        payload += _str(password)

    body = bytes(var) + bytes(payload)
    return bytes([CONNECT]) + encode_remaining_length(len(body)) + body


def build_publish(topic, payload, retain=False, qos=0):
    header = PUBLISH | (0x01 if retain else 0x00) | ((qos & 0x03) << 1)
    if isinstance(payload, str):
        payload = payload.encode()
    body = _str(topic) + bytes(payload)   # QoS0 -> no packet identifier
    return bytes([header]) + encode_remaining_length(len(body)) + body


def build_subscribe(packet_id, topic, qos=0):
    """SUBSCRIBE for a single topic filter. packet_id is any non-zero 16-bit int."""
    body = bytes([packet_id >> 8, packet_id & 0xFF]) + _str(topic) + bytes([qos & 0x03])
    return bytes([SUBSCRIBE]) + encode_remaining_length(len(body)) + body


def parse_publish(hdr, body):
    """Parse an inbound PUBLISH. hdr = full first byte (for the QoS bits), body =
    the remaining bytes. Returns (topic_str, payload_bytes). QoS>0 packet id (if
    any) is skipped; we only ever subscribe at QoS0."""
    qos = (hdr >> 1) & 0x03
    tlen = (body[0] << 8) | body[1]
    topic = body[2:2 + tlen]
    idx = 2 + tlen
    if qos > 0:
        idx += 2                         # skip the 2-byte packet identifier
    payload = body[idx:]
    try:
        topic = topic.decode()
    except Exception:
        pass
    return (topic, payload)


def connack_return_code(body):
    """body = the CONNACK variable header (>=2 bytes). Returns the return code."""
    if len(body) < 2:
        raise ValueError("short CONNACK")
    return body[1]
