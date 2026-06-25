"""Host-runnable tests for press_detector + debounce (CPython, no hardware).

Run:  python3 tests/test_press_detector.py     (plain, prints PASS/FAIL)
  or: python3 -m pytest tests/                  (if pytest installed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from press_detector import PressDetector       # noqa: E402
from debounce import Debouncer                 # noqa: E402

LONG_MS = 600
GAP_MS = 280


def run_sequence(events, long_ms=LONG_MS, gap_ms=GAP_MS):
    """events: list of (time_ms, active_bool). Returns list of emitted gestures.

    A trailing 'settle' tick past the double-gap window is appended automatically
    so a pending single is flushed.
    """
    d = PressDetector(long_ms, gap_ms)
    out = []
    last_t = events[-1][0]
    seq = list(events) + [(last_t + gap_ms + 50, events[-1][1])]
    for t, active in seq:
        g = d.update(active, t)
        if g:
            out.append(g)
    return out


def test_single():
    # press at 0, release at 50, nothing else -> single
    assert run_sequence([(0, True), (50, False)]) == ["single"], "single"


def test_long():
    # held past LONG_MS -> long, no single
    assert run_sequence([(0, True), (LONG_MS + 10, True), (LONG_MS + 60, False)]) \
        == ["long"], "long"


def test_long_boundary_just_under():
    # released just under threshold -> single, not long
    assert run_sequence([(0, True), (LONG_MS - 10, True), (LONG_MS - 5, False)]) \
        == ["single"], "just-under-long"


def test_double():
    # tap, release, tap again within the gap -> double (no stray single)
    seq = [(0, True), (40, False), (40 + 100, True), (40 + 140, False)]
    assert run_sequence(seq) == ["double"], "double"


def test_double_gap_expired_is_two_singles():
    # second tap arrives AFTER the gap -> two separate singles
    seq = [(0, True), (40, False),
           (40 + GAP_MS + 60, True), (40 + GAP_MS + 100, False)]
    assert run_sequence(seq) == ["single", "single"], "two-singles"


def test_double_disabled_single_is_instant():
    # double_gap_ms=0 -> "single" emitted on the release sample, no waiting.
    d = PressDetector(LONG_MS, 0)
    assert d.update(True, 0) is None        # press
    assert d.update(True, 30) is None       # held briefly
    assert d.update(False, 60) == "single"  # release -> single immediately


def test_double_disabled_long_still_works():
    d = PressDetector(LONG_MS, 0)
    assert d.update(True, 0) is None
    assert d.update(True, LONG_MS + 5) == "long"   # threshold while held
    assert d.update(False, LONG_MS + 40) is None   # release swallowed, no single


def test_double_disabled_two_taps_are_two_singles():
    d = PressDetector(LONG_MS, 0)
    assert d.update(True, 0) is None
    assert d.update(False, 40) == "single"          # first tap -> single
    assert d.update(True, 80) is None               # second tap (would be double if enabled)
    assert d.update(False, 120) == "single"         # -> just another single, never "double"


def test_debounce_zero_accepts_on_next_sample():
    # debounce_ms=0 -> a change is accepted on the next consecutive sample (no
    # 25ms wait), leaning on the hardware RC debounce.
    d = Debouncer(debounce_ms=0, initial=False)
    assert d.update(True, 0) is False       # first sight of change: candidate set
    assert d.update(True, 1) is True        # next sample confirms -> flips
    assert d.update(False, 2) is True       # release: candidate set, not yet flipped
    assert d.update(False, 3) is False      # next sample confirms release -> flips


def test_debounce_rejects_chatter():
    d = Debouncer(debounce_ms=25, initial=False)
    # chatter that never holds 25ms stays False
    assert d.update(True, 0) is False
    assert d.update(False, 5) is False
    assert d.update(True, 10) is False
    # now hold True for >=25ms -> becomes True
    assert d.update(True, 10) is False
    assert d.update(True, 40) is True


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
