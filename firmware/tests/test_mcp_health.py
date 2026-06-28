"""Host tests for mcp_health.py pure logic (CPython, no board).

Run:  python3 tests/test_mcp_health.py
Covers the error-code classifier, the per-board status record (edges, recovery
counting, serialisation), the bounded fault ring, the health summary, the
reset-cause map, and the rate-limited L1->L2 recovery escalation policy -- the
off-board decision logic that drives input_task's MCP recovery.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mcp_health as mh                                # noqa: E402


# ---- classify_oserror ----
def test_classify_eio():
    code, detail = mh.classify_oserror(OSError(5, "I/O error"))
    assert code == mh.CODE_I2C_EIO
    assert "OSError" in detail


def test_classify_timeout():
    code, _ = mh.classify_oserror(OSError(110, "timed out"))
    assert code == mh.CODE_I2C_TIMEOUT


def test_classify_non_oserror_defaults_eio():
    code, detail = mh.classify_oserror(ValueError("bad CONNACK"))
    assert code == mh.CODE_I2C_EIO
    assert "ValueError" in detail


# ---- BoardStatus ----
def test_board_status_edges_and_recovery_count():
    bs = mh.BoardStatus(1, 0x20)
    assert bs.ok is False
    assert bs.mark_ok(100) is True          # not-ok -> ok edge (first init)
    assert bs.recoveries == 0               # first healthy state is not a "recovery"
    assert bs.mark_ok(150) is False         # ok -> ok, no edge
    assert bs.mark_fail(mh.CODE_I2C_EIO, "boom") is True   # ok -> not-ok edge
    assert bs.fails == 1
    assert bs.mark_fail(mh.CODE_I2C_EIO, "boom") is False  # stays down, no edge
    assert bs.fails == 2
    assert bs.mark_ok(300) is True          # came back
    assert bs.recoveries == 1               # ...counts as a recovery (was healthy)
    assert bs.fails == 0


def test_board_status_as_dict():
    bs = mh.BoardStatus(2, 0x21)
    bs.mark_ok(1000)
    d = bs.as_dict(3000)
    assert d["board"] == 2 and d["addr"] == "0x21"
    assert d["ok"] is True and d["code"] == "" and d["detail"] == ""
    assert d["last_ok_s"] == 2               # (3000-1000)//1000
    bs.mark_fail(mh.CODE_MCP_ABSENT, "no ACK")
    d = bs.as_dict(3000)
    assert d["ok"] is False and d["code"] == mh.CODE_MCP_ABSENT
    assert d["detail"] == "no ACK" and d["fails"] == 1


def test_board_status_last_ok_none_before_first():
    bs = mh.BoardStatus(1, 0x20)
    assert bs.as_dict(500)["last_ok_s"] is None


# ---- FaultRing ----
def test_fault_ring_bounded_newest_last():
    ring = mh.FaultRing(2)
    ring.add(1, "mcp", "i2c_eio", "a", board=1)
    ring.add(2, "mcp", "int_stuck", "b", board=2)
    ring.add(3, "mcp", "mcp_reset", "c")
    recent = ring.recent()
    assert len(recent) == 2                  # dropped the oldest
    assert recent[0]["code"] == "int_stuck"
    assert recent[-1]["code"] == "mcp_reset"
    assert "board" not in recent[-1]         # board omitted when None
    assert ring.last()["code"] == "mcp_reset"


def test_fault_ring_empty_last_is_none():
    assert mh.FaultRing(4).last() is None


# ---- health_summary ----
def test_health_summary():
    assert mh.health_summary(True, True, 3, 3) == "ok"
    assert mh.health_summary(True, True, 3, 0) == "mcp_fault"
    assert mh.health_summary(True, True, 3, 2) == "degraded"
    assert mh.health_summary(False, True, 3, 3) == "net_fault"
    assert mh.health_summary(True, False, 0, 0) == "net_fault"
    assert mh.health_summary(True, True, 0, 0) == "ok"   # no boards configured


# ---- reset_cause_name ----
def test_reset_cause_name():
    names = {1: "wdt", 3: "power_on"}
    assert mh.reset_cause_name(1, names) == "wdt"
    assert mh.reset_cause_name(99, names) == "unknown"
    assert mh.reset_cause_name(None, names) == "unknown"


# ---- RecoveryPolicy ----
def test_policy_healthy_does_nothing():
    p = mh.RecoveryPolicy(after_fails=3, min_interval_ms=1000)
    assert p.decide(0, failing=False, fail_streak=0, int_stuck=False) == 0


def test_policy_waits_for_fail_streak():
    p = mh.RecoveryPolicy(3, 1000)
    assert p.decide(0, True, 1, False) == 0      # not bad enough yet
    assert p.decide(0, True, 2, False) == 0


def test_policy_escalates_l1_then_l2_rate_limited():
    p = mh.RecoveryPolicy(3, 1000)
    assert p.decide(0, True, 3, False) == 1      # first eligible attempt -> L1
    assert p.decide(500, True, 3, False) == 0    # rate-limited (<1000ms)
    assert p.decide(1000, True, 3, False) == 2   # escalate -> L2
    assert p.decide(2000, True, 3, False) == 2   # stays at L2 while failing


def test_policy_int_stuck_bypasses_streak_gate():
    p = mh.RecoveryPolicy(3, 1000)
    assert p.decide(0, True, 0, int_stuck=True) == 1   # stuck -> act despite streak 0


def test_policy_resets_ladder_when_healthy():
    p = mh.RecoveryPolicy(3, 1000)
    assert p.decide(0, True, 3, False) == 1
    assert p.decide(1000, True, 3, False) == 2
    assert p.decide(2000, False, 0, False) == 0        # healthy -> reset ladder
    assert p.decide(4000, True, 3, False) == 1         # next fault starts at L1 again


def test_policy_note_recovered_resets_ladder():
    p = mh.RecoveryPolicy(3, 1000)
    assert p.decide(0, True, 3, False) == 1
    p.note_recovered()
    assert p.decide(2000, True, 3, False) == 1         # back to L1 after recovery


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
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
