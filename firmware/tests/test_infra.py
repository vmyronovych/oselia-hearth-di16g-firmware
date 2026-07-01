"""Host tests for the pure infra modules: event_queue, clock, mqtt_packets."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from event_queue import EventQueue            # noqa: E402
from clock import Monotonic                   # noqa: E402
import mqtt_packets as pkt                     # noqa: E402
import ha_discovery as ha                      # noqa: E402


class _Cfg:
    BASE_TOPIC = "hearth"


# ---------------- event_queue ----------------
def test_queue_fifo():
    q = EventQueue(4)
    q.put((1, "single")); q.put((2, "double"))
    assert q.get() == (1, "single")
    assert q.get() == (2, "double")
    assert q.get() is None


def test_queue_drop_oldest_when_full():
    q = EventQueue(3)
    for i in range(3):
        q.put((i, "x"))
    assert len(q) == 3
    q.put((99, "x"))                 # overflow -> drops oldest (0)
    assert q.dropped == 1
    assert q.get() == (1, "x")       # 0 was dropped
    assert q.get() == (2, "x")
    assert q.get() == (99, "x")


def test_queue_wraparound():
    q = EventQueue(2)
    q.put("a"); q.put("b")
    assert q.get() == "a"
    q.put("c")                       # reuse slot
    assert q.get() == "b"
    assert q.get() == "c"


# ---------------- clock (wrap-safe) ----------------
def test_monotonic_handles_wrap():
    # simulate ticks_ms that wraps at 2**30 like MicroPython
    WRAP = 1 << 30
    seq = [WRAP - 10, WRAP - 5, 2, 7]      # wraps between idx 1 and 2

    def ticks():
        return seq.pop(0)

    def ticks_diff(a, b):
        # MicroPython-style signed diff over a 30-bit space
        d = (a - b) & (WRAP - 1)
        if d >= WRAP // 2:
            d -= WRAP
        return d

    m = Monotonic(ticks, ticks_diff)         # consumes seq[0] as initial _last
    assert m.ms() == 5         # (WRAP-5) - (WRAP-10) = 5
    assert m.ms() == 12        # +7 across the wrap (2 - (WRAP-5) = 7)
    assert m.ms() == 17        # +5 (7 - 2)


# ---------------- mqtt_packets ----------------
def test_remaining_length_roundtrip():
    for n in (0, 1, 127, 128, 16383, 16384, 200000):
        enc = pkt.encode_remaining_length(n)
        it = iter(enc)
        dec, _ = pkt.decode_remaining_length(lambda: next(it))
        assert dec == n, n


def test_publish_matches_poc_format():
    # POC: fixed 0x30 + remlen + (topiclen_hi, topiclen_lo) + topic + payload
    p = pkt.build_publish("home/rp2040/temp", "23.5")
    assert p[0] == 0x30
    topic = b"home/rp2040/temp"
    body = bytes([0x00, len(topic)]) + topic + b"23.5"
    assert p == bytes([0x30]) + pkt.encode_remaining_length(len(body)) + body


def test_publish_retain_bit():
    p = pkt.build_publish("t", "x", retain=True)
    assert p[0] & 0x01 == 0x01


def test_connect_has_will_and_auth_flags():
    p = pkt.build_connect("cid", keepalive=30, user="u", password="pw",
                          lwt_topic="stat", lwt_msg="offline", lwt_retain=True)
    assert p[0] == 0x10
    assert b"MQTT" in p
    # connect flags byte is at: [0]=type [1..]=remlen ... var header = 0004 'MQTT' 04 FLAGS
    i = 1
    while p[i] & 0x80:
        i += 1
    i += 1                          # now at start of variable header
    flags = p[i + 2 + 4 + 1]        # skip 0004 + 'MQTT'(4) + level(1)
    assert flags & 0x04            # will
    assert flags & 0x20            # will retain
    assert flags & 0x80            # username
    assert flags & 0x40            # password
    assert flags & 0x02            # clean session


def test_connack_rc():
    assert pkt.connack_return_code(b"\x00\x00") == 0
    assert pkt.connack_return_code(b"\x00\x05") == 5


def test_subscribe_packet():
    sub = pkt.build_subscribe(7, "hearth/AABBCC/cmd/#")
    assert sub[0] == pkt.SUBSCRIBE            # 0x82, fixed flags
    assert b"hearth/AABBCC/cmd/#" in sub
    # packet id 7 sits right after the fixed header + remaining length
    it = iter(sub[1:])
    _, n = pkt.decode_remaining_length(lambda: next(it))
    assert sub[1 + n] == 0 and sub[1 + n + 1] == 7


def test_parse_publish_roundtrip():
    p = pkt.build_publish("hearth/AABBCC/cmd/reboot", "PRESS")
    it = iter(p[1:])
    _, n = pkt.decode_remaining_length(lambda: next(it))
    topic, payload = pkt.parse_publish(p[0], p[1 + n:])
    assert topic == "hearth/AABBCC/cmd/reboot"
    assert payload == b"PRESS"


# ---------------- multi-board indexing / topics ----------------
def test_index_split_roundtrip():
    for board in range(1, 9):              # up to 8 boards
        for pin in range(1, 17):
            idx = (board - 1) * 16 + pin
            assert ha.split_index(idx) == (board, pin), (board, pin)
    assert ha.split_index(1) == (1, 1)
    assert ha.split_index(128) == (8, 16)  # 8 chips x 16
    assert ha.split_index(17) == (2, 1)    # first pin of board 2


def test_action_topic_board_pin():
    c = _Cfg()
    assert ha.action_topic(c, "AABBCC", 2, 5) == \
        "hearth/AABBCC/board2/input5/action"


def test_tunable_limits_and_cfg_state():
    c = _Cfg()
    assert ha.cfg_state_topic(c, "AABBCC") == "hearth/AABBCC/cfg"
    # the clamp limits the command handler enforces (mirrored by the integration)
    assert ha.TUNABLE_LIMITS["debounce_ms"] == (0, 100)
    assert ha.TUNABLE_LIMITS["long_ms"] == (100, 2000)
    assert ha.TUNABLE_LIMITS["double_gap_ms"] == (0, 1000)
    assert ha.LOG_LEVEL_OPTIONS == ("ERROR", "WARN", "INFO", "DEBUG")
    cfg = ha.cfg_state_payload(400, 0, 5, 2)
    assert cfg == {"long_ms": 400, "double_gap_ms": 0, "debounce_ms": 5,
                   "log_level": 2}


def test_detector_set_params_live_retune():
    from press_detector import PressDetector, MultiChannelDetector
    d = PressDetector(long_ms=1000, double_gap_ms=0)
    d.set_params(100, 0)
    assert d.long_ms == 100 and d.double_gap_ms == 0
    # behaviour: with long_ms=100, a 150 ms hold now classifies as long
    assert d.update(True, 0) is None
    assert d.update(True, 150) == "long"
    m = MultiChannelDetector((1, 2), 1000, 0)
    m.set_params(120, 30)
    assert m._det[1].long_ms == 120 and m._det[2].double_gap_ms == 30


def test_command_sub_topic():
    c = _Cfg()
    assert ha.command_sub_topic(c, "AABBCC") == "hearth/AABBCC/cmd/#"


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


if __name__ == "__main__":
    failures = 0
    for t in _all_tests():
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failures += 1
            print("FAIL", t.__name__, "-", e)
        except Exception as e:
            failures += 1
            print("ERROR", t.__name__, "-", repr(e))
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
