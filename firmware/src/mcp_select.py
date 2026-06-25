"""Resolve which MCP23017 chips (= boards) the firmware should drive.

PURE: no `machine`/`utime` imports, so it runs under CPython for host tests (same
pattern as press_detector / debounce). The actual bus scan + retry lives in
main.py (which has `machine`); this just turns a scan result into the board list.

Board number = position in the returned list (1-based), matching the rest of the
firmware ("board = position in MCP_ADDRESSES"). For the normal contiguous wiring
(0x20, 0x21, ...) the sorted scan result is identical to an explicit list.
"""

MCP_ADDR_MIN = 0x20
MCP_ADDR_MAX = 0x27       # MCP23017 strap range A0..A2 -> 8 possible devices
MAX_BOARDS = MCP_ADDR_MAX - MCP_ADDR_MIN + 1      # 8 (whole strap range)


def select_addresses(scanned, explicit, autodiscover, max_boards=MAX_BOARDS):
    """Return the resolved list of MCP I2C addresses.

    autodiscover=False -> use `explicit` verbatim (installer-pinned order).
    autodiscover=True  -> sorted bus responders across the whole 0x20..0x27 strap
                          range (up to max_boards = 8); if the scan found none, fall
                          back to `explicit` so the device still advertises board1
                          and the health loop can recover the chip when it appears.
    """
    if not autodiscover:
        return list(explicit)
    found = sorted(a for a in scanned if MCP_ADDR_MIN <= a <= MCP_ADDR_MAX)
    if not found:
        return list(explicit)
    return found[:max_boards]
