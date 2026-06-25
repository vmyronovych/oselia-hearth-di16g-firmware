"""Wrap-safe monotonic millisecond clock.

`utime.ticks_ms()` wraps (~2**30 on MicroPython), so raw subtraction across a wrap
is wrong. The pure logic modules (debounce, press_detector) use plain `a - b`
comparisons, so they must be fed an ever-increasing millisecond value. Monotonic
converts ticks into that value using ticks_diff, accumulating elapsed time.

Pure: the tick functions are injected, so this is unit-testable on a host.
On device: Monotonic(utime.ticks_ms, utime.ticks_diff).
"""


class Monotonic:
    def __init__(self, ticks_ms_fn, ticks_diff_fn):
        self._ticks_ms = ticks_ms_fn
        self._ticks_diff = ticks_diff_fn
        self._last = ticks_ms_fn()
        self._acc = 0

    def ms(self):
        t = self._ticks_ms()
        d = self._ticks_diff(t, self._last)
        if d < 0:          # defensive; ticks_diff should already be signed-correct
            d = 0
        self._acc += d
        self._last = t
        return self._acc


def from_utime():
    """Convenience factory on-device: import utime and wrap it."""
    import utime
    return Monotonic(utime.ticks_ms, utime.ticks_diff)
