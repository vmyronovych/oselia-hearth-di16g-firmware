"""Host tests for oselia_provision.mqtt wire encoders (pure, no socket).

Cross-checks the host-side MQTT 3.1.1 encode against the firmware's own
src/mqtt_packets.py so the tool and the board agree on the wire format.

Run:  python tests/test_oselia_mqtt.py
"""
import os
import sys

FW_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..",
                                       "firmware", "src"))
sys.path.insert(0, FW_SRC)

from oselia_provision import mqtt as m              # noqa: E402
import mqtt_packets as fw                           # noqa: E402


def test_encode_remaining_length():
    # Golden values from the MQTT spec encoding (7-bit groups, MSB continuation).
    assert m._encode_rl(0) == b"\x00"
    assert m._encode_rl(127) == b"\x7f"
    assert m._encode_rl(128) == b"\x80\x01"
    assert m._encode_rl(16383) == b"\xff\x7f"
    assert m._encode_rl(128) == fw.encode_remaining_length(128)


def test_build_connect_matches_firmware():
    # keepalive 15, clean session, no creds -> identical bytes both sides.
    ours = m.build_connect("cid", keepalive=15)
    theirs = fw.build_connect("cid", keepalive=15, clean=True)
    assert ours == theirs, (ours, theirs)
    assert ours[0] == 0x10                          # CONNECT control byte
    assert b"MQTT" in ours


def test_build_connect_with_creds_sets_flags():
    pkt = m.build_connect("cid", user="u", password="p")
    # flags byte sits right after the 6-byte protocol-name header + level byte.
    # username(0x80)+password(0x40)+clean(0x02) = 0xC2
    idx = pkt.index(b"MQTT") + 5
    assert pkt[idx] == 0xC2, hex(pkt[idx])


def test_build_publish_retain_flag():
    p0 = m.build_publish("t/x", b"online", retain=False)
    p1 = m.build_publish("t/x", b"online", retain=True)
    assert p0[0] == 0x30 and p1[0] == 0x31
    assert m.build_publish("t/x", b"online") == fw.build_publish("t/x", b"online")


def test_build_subscribe():
    assert m.build_subscribe(1, "a/b")[0] == 0x82   # SUBSCRIBE control byte
    assert m.build_subscribe(1, "a/b") == fw.build_subscribe(1, "a/b")


def test_split_publish_roundtrip():
    body = m._mqtt_str("topic/seg") + b"payload"
    topic, payload = m._split_publish(body)
    assert topic == "topic/seg" and payload == b"payload"


class _FakeSock:
    """A scripted socket: recv() pops from a byte buffer, raising socket.timeout when
    drained (mirrors a quiet broker). Records everything sent."""
    def __init__(self, buffer=b""):
        self._buf = bytearray(buffer)
        self.sent = bytearray()

    def settimeout(self, _):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._buf:
            import socket as _s
            raise _s.timeout()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def close(self):
        pass


_CONNACK = b"\x20\x02\x00\x00"          # CONNACK, return code 0 (accepted)


def _patch_conn(monkey_sock):
    import socket
    orig = socket.create_connection
    socket.create_connection = lambda *a, **k: monkey_sock
    return orig, socket


def test_publish_sends_connect_then_publish_and_coerces_str():
    fake = _FakeSock(_CONNACK)
    import socket
    orig = socket.create_connection
    socket.create_connection = lambda *a, **k: fake
    try:
        ok = m.publish("1.2.3.4", 1883, None, None, "hearth/1/cmd/Restart", "go", retain=True)
    finally:
        socket.create_connection = orig
    assert ok is True
    # a CONNECT (0x10) went out, then a retained PUBLISH (0x31) carrying the coerced payload.
    assert fake.sent[0] == 0x10
    assert b"\x31" in fake.sent and b"hearth/1/cmd/Restart" in fake.sent and b"go" in fake.sent


def test_watch_collects_publishes_and_invokes_callback():
    pub = m.build_publish("hearth/893922/status", b"online")
    fake = _FakeSock(_CONNACK + pub)
    import socket
    orig = socket.create_connection
    socket.create_connection = lambda *a, **k: fake
    seen = []
    try:
        msgs = m.watch("1.2.3.4", 1883, None, None, ["hearth/+/status"], duration=0.2,
                       on_message=lambda t, p, e: seen.append((t, p)))
    finally:
        socket.create_connection = orig
    assert msgs and msgs[0][0] == "hearth/893922/status" and msgs[0][1] == b"online"
    assert seen and seen[0][0] == "hearth/893922/status"
    # a SUBSCRIBE (0x82) for the requested filter was actually sent.
    assert b"\x82" in fake.sent and b"hearth/+/status" in fake.sent


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d mqtt tests" % len(fns))


if __name__ == "__main__":
    _run()
