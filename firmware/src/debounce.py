"""Per-channel software debounce.

PURE LOGIC -- no hardware imports; clock injected for host testing.

The hardware already has an RC + optocoupler stage; this is a cheap guard against
residual chatter on the MCP pin. A raw reading must remain stable for
`debounce_ms` before it is accepted as the new stable value.
"""


class Debouncer:
    def __init__(self, debounce_ms=25, initial=False):
        self.debounce_ms = debounce_ms
        self._stable = initial
        self._candidate = initial
        self._t_change = 0

    @property
    def value(self):
        return self._stable

    def update(self, raw, now_ms):
        """Feed a raw boolean reading. Returns the stable boolean value."""
        if raw != self._candidate:
            self._candidate = raw
            self._t_change = now_ms
        elif raw != self._stable and (now_ms - self._t_change) >= self.debounce_ms:
            self._stable = raw
        return self._stable
