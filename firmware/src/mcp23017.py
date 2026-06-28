"""MCP23017 driver tailored to this project.

Configured for: all 16 pins as inputs with pull-ups, INTERRUPTS DISABLED -- the
firmware reads by POLLING (input_task), so the INT/wired-OR machinery is left off
(it caused the original freeze + dropped-press faults). `read_all()` reads both
ports in one transaction.

See SPEC.md sec.7. Pins/address come from config.py. The INT-flavoured helpers
(`read_changes`, `clear_interrupt`) are retained but unused under polling.
"""

# Register addresses (IOCON.BANK = 0, the power-on default)
IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPINTENA, GPINTENB = 0x04, 0x05
DEFVALA, DEFVALB = 0x06, 0x07
INTCONA, INTCONB = 0x08, 0x09
IOCON = 0x0A            # (0x0B is the mirror address of IOCON)
GPPUA, GPPUB = 0x0C, 0x0D
INTFA, INTFB = 0x0E, 0x0F
INTCAPA, INTCAPB = 0x10, 0x11
GPIOA, GPIOB = 0x12, 0x13

# IOCON bits
IOCON_MIRROR = 0x40
IOCON_ODR = 0x04        # INT pin open-drain
IOCON_INTPOL = 0x02     # INT polarity (active-high) when ODR=0


class MCP23017:
    def __init__(self, i2c, addr, retries=3):
        self.i2c = i2c
        self.addr = addr
        self.retries = retries

    # --- low-level helpers (with bounded retry for bus glitches) ---
    def _w(self, reg, val):
        last = None
        for _ in range(self.retries):
            try:
                self.i2c.writeto_mem(self.addr, reg, bytes([val & 0xFF]))
                return
            except OSError as e:
                last = e
        raise last

    def _r(self, reg, n=1):
        last = None
        for _ in range(self.retries):
            try:
                return self.i2c.readfrom_mem(self.addr, reg, n)
            except OSError as e:
                last = e
        raise last

    def present(self):
        """True if the MCP ACKs a quick single-address probe (for health checks /
        re-init). Deliberately NOT i2c.scan(): scanning all 112 addresses can stall
        the caller (core0) for seconds on a dead/floating bus -- e.g. an unpowered
        satellite removes the bus pull-ups -- which previously starved the watchdog.
        A one-address read, bounded by the I2C timeout, fails fast instead."""
        try:
            self.i2c.readfrom(self.addr, 1)
            return True
        except Exception:
            return False

    def init(self, pullups=True):
        """Configure as 16 inputs with pull-ups, INTERRUPTS DISABLED.

        The firmware reads inputs by POLLING (see input_task), so the MCP's
        interrupt-on-change / wired-OR INT machinery is deliberately left off:
        GPINTEN=0 (no INT asserted -- nothing drives the shared INT line) and
        IOCON=0 (defaults; no MIRROR/ODR). This removes the fragile shared-IRQ
        dependency that caused the original freeze + dropped-press faults. Only
        IODIR (inputs), IPOL (no HW inversion), and GPPU (pull-ups) are set.
        IOCON.BANK defaults to 0, so A/B registers are adjacent.
        """
        gppu = 0xFF if pullups else 0x00
        self._w(IODIRA, 0xFF)        # all inputs
        self._w(IODIRB, 0xFF)
        self._w(IPOLA, 0x00)         # no HW inversion (polarity in software)
        self._w(IPOLB, 0x00)
        self._w(GPPUA, gppu)
        self._w(GPPUB, gppu)
        self._w(GPINTENA, 0x00)      # interrupts OFF (we poll); don't drive INT
        self._w(GPINTENB, 0x00)
        self._w(IOCON, 0x00)         # defaults: no MIRROR/ODR
        self.read_all()

    def read_all(self):
        """Read both ports in one I2C transaction; clears the interrupt.

        Returns a 16-bit int: bit i = pin (0..7 = PORTA, 8..15 = PORTB).
        Reading GPIOA..GPIOB also clears a pending interrupt.
        """
        data = self._r(GPIOA, 2)     # GPIOA then GPIOB (adjacent)
        return data[0] | (data[1] << 8)

    def read_changes(self):
        """Like the POC: read INTF (which pins changed) + INTCAP (captured values).

        Returns dict {pin_index: level}. Empty if nothing flagged. Reading INTCAP
        clears the interrupt. Useful when you want edge info rather than a full
        re-poll; the default main loop uses read_all() instead.
        """
        intf = self._r(INTFA, 2)
        intf_mask = intf[0] | (intf[1] << 8)
        if intf_mask == 0:
            return {}
        cap = self._r(INTCAPA, 2)
        cap_val = cap[0] | (cap[1] << 8)
        changes = {}
        for i in range(16):
            if intf_mask & (1 << i):
                changes[i] = 1 if (cap_val & (1 << i)) else 0
        return changes

    def clear_interrupt(self):
        """Reading INTCAP/GPIO clears INT; provided as an explicit hook."""
        self.read_all()
