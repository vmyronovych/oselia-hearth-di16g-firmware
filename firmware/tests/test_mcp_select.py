"""Host tests for mcp_select.select_addresses (CPython, no hardware).

Run:  python3 tests/test_mcp_select.py
The board set is resolved at boot from an I2C scan; this is the pure logic that
turns a scan result into the address list (board = position in it).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_select import select_addresses             # noqa: E402


def test_autodiscover_filters_sorts():
    # Unrelated bus devices (e.g. 0x3c OLED) are ignored; MCP range is sorted.
    scan = [0x3c, 0x22, 0x20, 0x21]
    assert select_addresses(scan, [0x20], True) == [0x20, 0x21, 0x22]


def test_autodiscover_single():
    assert select_addresses([0x20], [0x20], True) == [0x20]


def test_autodiscover_full_8_board_range():
    # All 8 MCP strap addresses 0x20..0x27 are usable, not just the first 5.
    scan = [0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27]
    assert select_addresses(scan, [0x20], True) == scan


def test_autodiscover_ignores_out_of_range():
    # 0x28 and a non-MCP device are not MCP strap addresses -> excluded.
    scan = [0x20, 0x27, 0x28, 0x3c]
    assert select_addresses(scan, [0x20], True) == [0x20, 0x27]


def test_autodiscover_empty_falls_back_to_explicit():
    assert select_addresses([], [0x20], True) == [0x20]
    assert select_addresses([0x3c], [0x20, 0x21], True) == [0x20, 0x21]


def test_pinned_uses_explicit_verbatim():
    # autodiscover off -> explicit list as-is, even if the bus shows more chips.
    assert select_addresses([0x20, 0x21, 0x22], [0x20], False) == [0x20]


def test_returns_new_list():
    explicit = [0x20]
    out = select_addresses([], explicit, True)
    out.append(0x21)
    assert explicit == [0x20], "must not mutate the caller's list"


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
