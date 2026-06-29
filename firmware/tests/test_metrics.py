"""Host tests for the pure metrics core (CPython, no board).

Run:  python3 tests/test_metrics.py
Covers: the never-freeze serializer (round-trip + escaping + buffer reuse), the fault ring
(eviction, boot anchoring, net+mcp routing), the fixed counter set, gauges, board mapping,
persistence load/reconcile (max, corrupt-safe), the crash record, and the never-raise contract.
"""
import json
import os
import sys

SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import metrics                                        # noqa: E402
import metrics_schema as S                            # noqa: E402


# ---- serializer ----
def test_dumps_round_trips_via_json():
    obj = {"a": 1, "b": [True, False, None], "c": {"x": 1.5}, "s": "hi"}
    assert json.loads(metrics.dumps(obj).decode()) == obj


def test_dumps_escapes_quotes_newlines_and_unicode():
    obj = {"d": 'he said "x"\nline\tend', "u": "cap…"}
    out = metrics.dumps(obj).decode()
    assert json.loads(out) == obj          # parses back identically
    assert '\\"' in out and "\\n" in out and "\\t" in out


def test_dumps_bool_not_int():
    # isinstance(True, int) is True -> must serialise as true/false, not 1/0
    assert metrics.dumps({"k": True}).decode() == '{"k":true}'
    assert metrics.dumps({"k": 1}).decode() == '{"k":1}'


def test_writer_reuses_buffer_across_sizes():
    buf = bytearray(8)
    big = metrics.dumps({"k": "x" * 200}, buf)        # grows the buffer
    assert json.loads(big.decode())["k"] == "x" * 200
    small = metrics.dumps({"k": "y"}, buf)            # reuses; no stale tail
    assert json.loads(small.decode()) == {"k": "y"}


# ---- fault ring ----
def test_ring_evicts_oldest_keeps_newest():
    r = metrics.FaultRing(3)
    for i in range(5):
        r.add(up=i, boot=1, component="mcp", code="i2c_eio", detail=str(i))
    items = r.items()
    assert len(items) == 3
    assert [it["up"] for it in items] == [2, 3, 4]    # newest survive
    assert r.last()["up"] == 4


def test_ring_anchors_boot_and_omits_board_when_absent():
    r = metrics.FaultRing(4)
    r.add(up=10, boot=7, component="net", code="mqtt_disconnect", detail="x")
    r.add(up=11, boot=7, component="mcp", code="i2c_eio", detail="y", board=2)
    assert r.items()[0]["boot"] == 7 and "board" not in r.items()[0]
    assert r.items()[1]["board"] == 2


# ---- registry: counters / gauges / boards ----
def test_fixed_counter_set_initialised_zero():
    m = metrics.Metrics()
    snap = m.snapshot()
    assert set(snap[S.K_COUNTERS].keys()) == set(S.COUNTER_KEYS)
    assert all(v == 0 for v in snap[S.K_COUNTERS].values())


def test_inc_and_gauge_and_seq_increment():
    m = metrics.Metrics()
    m.inc(S.C_BUS_REC)
    m.inc(S.C_BUS_REC, 2)
    m.set_gauge(S.K_MEM, 41200)
    out1 = json.loads(m.serialize().decode())
    out2 = json.loads(m.serialize().decode())
    assert out1[S.K_COUNTERS][S.C_BUS_REC] == 3
    assert out1[S.K_MEM] == 41200
    assert out2[S.K_SEQ] == out1[S.K_SEQ] + 1          # seq advances per publish
    assert out1[S.K_V] == S.SCHEMA_VERSION


def test_net_and_mcp_faults_both_reach_the_ring():
    m = metrics.Metrics()
    m.add_fault(up=1, boot=1, component="mcp", code="i2c_timeout", detail="t", board=1)
    m.add_fault(up=2, boot=1, component="net", code="eth_link_lost", detail="down")
    snap = json.loads(m.serialize().decode())
    comps = [it[S.F_COMP] for it in snap[S.K_RING]]
    assert "mcp" in comps and "net" in comps           # net faults are NOT dropped
    assert snap[S.K_LASTFAULT][S.F_CODE] == "eth_link_lost"


def test_board_mapping_to_short_keys():
    m = metrics.Metrics()
    m.set_boards([{"board": 2, "addr": "0x21", "ok": False, "code": "i2c_eio",
                   "detail": "OSError 5", "fails": 4, "last_ok_s": 30,
                   "recoveries": 1, "fail_total": 9}], boards_total=2, boards_ok=1)
    b = json.loads(m.serialize().decode())[S.K_BOARDS_ARR][0]
    assert b[S.B_BOARD] == 2 and b[S.B_ADDR] == "0x21" and b[S.B_OK] is False
    assert b[S.B_FAILTOTAL] == 9 and b[S.B_RECOV] == 1


def test_detail_is_length_capped():
    m = metrics.Metrics()
    m.add_fault(up=1, boot=1, component="mcp", code="x", detail="z" * 500)
    d = json.loads(m.serialize().decode())[S.K_RING][0][S.F_DETAIL]
    assert len(d) <= S.DETAIL_MAX


# ---- persistence ----
class _FakeStore:
    def __init__(self, data=None, fail=False):
        self.data = data
        self.fail = fail
        self.saved = None

    def load(self):
        if self.fail:
            raise RuntimeError("boom")
        return self.data

    def checkpoint(self, payload):
        self.saved = payload

    def flush(self, payload):
        self.saved = payload


def test_load_increments_boot_and_takes_max_counter():
    store = _FakeStore({"bc": 4, "c": {S.C_RECONNECTS: 10}, "cr": None, "r": []})
    m = metrics.Metrics(store=store)
    m.inc(S.C_RECONNECTS, 3)            # live value 3, persisted 10 -> max wins
    m.load()
    assert m.boot_count == 5
    assert m.counter(S.C_RECONNECTS) == 10


def test_load_restores_ring_and_crash():
    store = _FakeStore({"bc": 1, "c": {},
                        "cr": {S.CR_CAUSE: "wdt", S.CR_EXC: "trace"},
                        "r": [{"up": 9, "boot": 1, "component": "sys", "code": "crash",
                               "detail": "d"}]})
    m = metrics.Metrics(store=store)
    m.load()
    snap = m.snapshot()
    assert snap[S.K_CRASH][S.CR_CAUSE] == "wdt"
    assert snap[S.K_RING][0][S.F_CODE] == "crash"


def test_load_corrupt_store_does_not_crash():
    m = metrics.Metrics(store=_FakeStore(fail=True))
    m.load()                            # must not raise
    assert m.boot_count == 1


def test_checkpoint_and_flush_emit_payload():
    store = _FakeStore()
    m = metrics.Metrics(store=store)
    m.inc(S.C_MCP_RESET, 2)
    m.checkpoint()
    assert store.saved["c"][S.C_MCP_RESET] == 2
    m.flush()
    assert store.saved["bc"] == m.boot_count


# ---- never-raise contract ----
def test_serialize_never_raises_on_unencodable():
    m = metrics.Metrics()
    m.set_gauge(S.K_MEM, object())      # not JSON-encodable as int/str cleanly
    out = m.serialize()
    # either encodes via str() fallback or returns None -- but MUST NOT raise
    assert out is None or isinstance(out, bytes)


def test_mutators_never_raise_on_bad_input():
    m = metrics.Metrics()
    m.inc(None)                         # bad key
    m.set_gauge(None, object())
    m.add_fault(up=1, boot=1, component="x", code="y", detail=None)
    # reaching here without an exception is the assertion


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
        except Exception as e:                 # a crash in a test is also a failure
            failures += 1
            print("ERROR", t.__name__, "-", repr(e))
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
