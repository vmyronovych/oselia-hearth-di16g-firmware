"""Host-runnable tests for status_led colour/priority logic (CPython)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import status_led as s          # noqa: E402

HEALTHY = {"boot": False, "ethernet": True, "mqtt": True, "mcp": True}


def _on_phase(period):
    # a time at which _blink_on is True (start of period)
    return 0


def _off_phase(period):
    return period // 2          # past the 50% duty point


def test_all_healthy_is_green():
    assert s.compute_color(HEALTHY, 0) == s.GREEN, "healthy->green"


def test_boot_is_blue_solid():
    st = dict(HEALTHY, boot=True)
    assert s.compute_color(st, 0) == s.BLUE
    assert s.compute_color(st, 12345) == s.BLUE, "boot is solid"


def test_ethernet_down_red_blink():
    st = dict(HEALTHY, ethernet=False)
    assert s.compute_color(st, _on_phase(1000)) == s.RED
    assert s.compute_color(st, _off_phase(1000)) == s.BLACK, "blink off-phase"


def test_priority_ethernet_over_mqtt():
    # both down -> ethernet (root cause) wins
    st = dict(HEALTHY, ethernet=False, mqtt=False)
    assert s.compute_color(st, 0) == s.RED


def test_mqtt_down_orange():
    st = dict(HEALTHY, mqtt=False)
    assert s.compute_color(st, _on_phase(600)) == s.ORANGE


def test_mcp_down_yellow():
    st = dict(HEALTHY, mcp=False)
    assert s.compute_color(st, _on_phase(300)) == s.YELLOW


def test_activity_flash_overrides_fault():
    st = dict(HEALTHY, ethernet=False)
    # fault present, but within flash window -> white
    assert s.compute_color(st, 1000, last_activity_ms=1000) == s.WHITE
    # after flash window -> back to fault behaviour
    assert s.compute_color(st, 1000 + s.FLASH_MS + 1, last_activity_ms=1000) \
        in (s.RED, s.BLACK)


def test_scale():
    assert s.scale(s.GREEN, 0.2) == (0, 51, 0)
    assert s.scale(s.WHITE, 0.0) == (0, 0, 0)
    assert s.scale(s.WHITE, 2.0) == (255, 255, 255)  # clamped


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
