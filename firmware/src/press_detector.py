"""Single / double / long press classifier.

PURE LOGIC -- no `machine`/`network` imports, so it runs under CPython for unit
tests (see tests/test_press_detector.py). The clock is supplied by the caller as
a millisecond integer, so tests can use a fake clock.

Gestures emitted (strings): "single", "double", "long".

Behaviour (see docs/spec.md sec.6):
  * A press held >= long_ms emits "long" once, on crossing the threshold; the
    following release is swallowed (no "single").
  * On release of a short press, a window of double_gap_ms opens. If a new press
    begins within the window, "double" is emitted at the start of that second
    press (responsive) and the rest of that press is swallowed. If the window
    expires with no new press, "single" is emitted.
  * If double_gap_ms <= 0, double detection is DISABLED: "single" is emitted
    immediately on release (no waiting), so single presses feel instant. "double"
    is never produced; "long" is unaffected. This is the low-latency mode for
    installs that don't use double-tap.
"""

# State constants
_IDLE = 0       # released, nothing pending
_DOWN = 1       # first press in progress
_WAIT = 2       # released after short press, waiting for a possible 2nd press
_LONG = 3       # long already emitted, waiting for release
_SWALLOW = 4    # double already emitted, waiting for release


class PressDetector:
    def __init__(self, long_ms=600, double_gap_ms=280):
        self.long_ms = long_ms
        self.double_gap_ms = double_gap_ms
        self._state = _IDLE
        self._t_down = 0
        self._t_up = 0

    def reset(self):
        self._state = _IDLE

    def set_params(self, long_ms, double_gap_ms):
        self.long_ms = long_ms
        self.double_gap_ms = double_gap_ms

    def update(self, active, now_ms):
        """Feed the current debounced state. Returns a gesture string or None.

        `active` is the logical "pressed" boolean (polarity already resolved).
        `now_ms` is a monotonically increasing millisecond timestamp.
        """
        st = self._state

        if st == _IDLE:
            if active:
                self._state = _DOWN
                self._t_down = now_ms
            return None

        if st == _DOWN:
            if active:
                if now_ms - self._t_down >= self.long_ms:
                    self._state = _LONG
                    return "long"
                return None
            # released before long threshold -> candidate single/double
            if self.double_gap_ms <= 0:
                # double detection disabled -> emit single now (no wait)
                self._state = _IDLE
                return "single"
            self._state = _WAIT
            self._t_up = now_ms
            return None

        if st == _WAIT:
            expired = (now_ms - self._t_up) >= self.double_gap_ms
            if active:
                if expired:
                    # The double window already closed: the previous press was a
                    # single, and THIS press starts a fresh sequence. (Robust even
                    # if no sample landed inside the window.)
                    self._state = _DOWN
                    self._t_down = now_ms
                    return "single"
                # second press began within the window -> double
                self._state = _SWALLOW
                return "double"
            if expired:
                self._state = _IDLE
                return "single"
            return None

        if st == _LONG:
            if not active:
                self._state = _IDLE
            return None

        if st == _SWALLOW:
            if not active:
                self._state = _IDLE
            return None

        return None


class MultiChannelDetector:
    """One PressDetector per input index (1..n). Convenience wrapper used by main."""

    def __init__(self, indices, long_ms=600, double_gap_ms=280):
        self._det = {i: PressDetector(long_ms, double_gap_ms) for i in indices}

    def update(self, index, active, now_ms):
        return self._det[index].update(active, now_ms)

    def set_params(self, long_ms, double_gap_ms):
        """Apply new timings to every channel (live re-tune)."""
        for d in self._det.values():
            d.set_params(long_ms, double_gap_ms)

    def update_all(self, active_map, now_ms):
        """active_map: {index: bool}. Yields (index, gesture) for each emission."""
        for i, det in self._det.items():
            g = det.update(active_map[i], now_ms)
            if g is not None:
                yield (i, g)
