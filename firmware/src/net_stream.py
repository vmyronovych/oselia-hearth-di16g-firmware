"""Byte-stream adapter over the CH9120 UART.

In transparent mode the CH9120 UART carries the raw TCP payload to/from the broker,
so this wraps the UART with simple, robust write-all / read-available semantics for
the MQTT client. See SPEC.md sec.4.
"""
try:
    import utime as _t
except ImportError:
    import time as _t
    if not hasattr(_t, "sleep_ms"):
        _t.sleep_ms = lambda ms: _t.sleep(ms / 1000.0)


class UartStream:
    def __init__(self, uart, ch9120=None):
        self.uart = uart
        self.ch9120 = ch9120        # for link-status checks

    def write(self, data):
        """Write all bytes (blocking until flushed). Returns count."""
        mv = memoryview(data)
        total = len(data)
        n = 0
        guard = 0
        while n < total:
            w = self.uart.write(mv[n:])
            if w:
                n += w
            else:
                _t.sleep_ms(1)
                guard += 1
                if guard > 1000:        # ~1s with no progress -> give up
                    raise OSError("uart write stalled")
        try:
            self.uart.flush()           # not on all ports; ignore if absent
        except (AttributeError, OSError):
            pass
        return n

    def any(self):
        try:
            return self.uart.any()
        except AttributeError:
            return 0

    def read(self, n):
        """Read up to n bytes; returns b'' if nothing available right now."""
        data = self.uart.read(n)
        return data if data else b""

    def read_byte(self):
        """Return one int 0..255, or None if nothing available."""
        data = self.uart.read(1)
        if not data:
            return None
        return data[0]

    def link_up(self):
        if self.ch9120 is not None:
            return self.ch9120.is_connected()
        return True
