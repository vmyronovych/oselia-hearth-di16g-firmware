"""MCP23017 driver tailored to this project.

Configured for: all 16 pins as inputs, interrupt-on-change, INTA/INTB mirrored
onto a single INT line (IOCON.MIRROR=1). Reading the captured state also clears
the interrupt.

See SPEC.md sec.7. Pins/address/polarity come from config.py.
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
    def __init__(self, i2c, addr, int_active_low=True, retries=3):
        self.i2c = i2c
        self.addr = addr
        self.int_active_low = int_active_low
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

    def init(self, pullups=True, open_drain=False):
        """Configure as 16 inputs with mirrored interrupt-on-change.

        Register sequence CONFIRMED working in the POC (dib/app
        rp2040-eth-mqtt-dip-v1.py): inputs + pull-ups + compare-to-previous
        interrupt-on-change, IOCON MIRROR + active-low INT. Note IOCON.BANK
        defaults to 0 here, so 16-bit registers are A/B-adjacent.

        `open_drain=True` sets IOCON.ODR (0x04) so multiple chips can share one
        wired-OR INT line into a single RP2040 GPIO. With ODR set the INT pin is
        open-drain active-low (INTPOL is then ignored).
        """
        iocon = 0x40 | (0x04 if open_drain else 0x00)   # MIRROR (+ODR)
        gppu = 0xFF if pullups else 0x00
        self._w(IODIRA, 0xFF)        # all inputs
        self._w(IODIRB, 0xFF)
        self._w(IPOLA, 0x00)         # no HW inversion (polarity in software)
        self._w(IPOLB, 0x00)
        self._w(GPPUA, gppu)
        self._w(GPPUB, gppu)
        self._w(INTCONA, 0x00)       # compare to previous value
        self._w(INTCONB, 0x00)
        self._w(GPINTENA, 0xFF)      # interrupt on change, all pins
        self._w(GPINTENB, 0xFF)
        self._w(IOCON, iocon)        # MIRROR (+ODR for shared INT)
        self.read_all()              # clear any latched interrupt

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
