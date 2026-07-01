"""MCP23017 driver tailored to this project.

All 16 pins are inputs with pull-ups; the firmware reads by POLLING (input_task), so the
chip's interrupt machinery is left off (GPINTEN=0 -- the shared INT caused the original
freeze/dropped-press faults; see docs/spec.md sec.7/12). `read_all()` reads both ports in one
transaction. Pins/address come from config.py.
"""

# Register addresses (IOCON.BANK = 0, the power-on default)
IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPINTENA, GPINTENB = 0x04, 0x05
IOCON = 0x0A
GPPUA, GPPUB = 0x0C, 0x0D
GPIOA, GPIOB = 0x12, 0x13


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
        """Configure as 16 inputs with pull-ups; interrupts off (we poll).

        Sets IODIR (inputs), IPOL (no HW inversion), GPPU (pull-ups), GPINTEN=0 (no INT),
        IOCON=0, then verifies IODIR/GPPU read-back (see below).
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
        # VERIFY the input-critical config actually took. A glitchy I2C transaction --
        # common on the just-reclocked/reset bus during an MCP recovery -- can corrupt a
        # write, e.g. drop a bit of a register ADDRESS so 0x00 lands in IODIRA, turning
        # PORTA into OUTPUTS. The chip still ACKs reads, so those inputs go SILENTLY DEAD
        # (they read the output latch, never the switches; HW-FOUND). Read IODIR + GPPU
        # back and raise on mismatch so try_init marks the chip not-ok and re-inits next
        # pass, when the bus has settled -- never leaving a half-configured chip "healthy".
        iodir = self._r(IODIRA, 2)
        gp = self._r(GPPUA, 2)
        if not (iodir[0] == 0xFF and iodir[1] == 0xFF and gp[0] == gppu and gp[1] == gppu):
            raise OSError("MCP@0x%02x config verify failed: IODIR=%02x%02x GPPU=%02x%02x"
                          % (self.addr, iodir[0], iodir[1], gp[0], gp[1]))
        self.read_all()

    def read_all(self):
        """Read both ports in one I2C transaction.

        Returns a 16-bit int: bit i = pin (0..7 = PORTA, 8..15 = PORTB).
        """
        data = self._r(GPIOA, 2)     # GPIOA then GPIOB (adjacent)
        return data[0] | (data[1] << 8)
