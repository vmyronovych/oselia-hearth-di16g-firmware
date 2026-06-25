"""Core 0 -- real-time input task (multi-board).

Owns the shared I2C bus and up to 8 MCP23017 chips, the shared wired-OR INT line,
debounce + gesture detection for every input, and the hardware watchdog. Detected
gestures are pushed onto the event queue (as a global 1-based index) for core 1.

Board number = position in the resolved address list (1-based; main.py scans the
bus and passes it in). Global index for board b,
pin p (both 1-based) is (b-1)*16 + p. One INT line is shared by all chips
(open-drain, wired-OR), so on any interrupt every present chip is read.

Per-chip health: a chip that is absent or glitching is marked not-ok and skipped;
it is re-initialised automatically if it returns. Its input indices stay stable
regardless, so a missing board never shifts another board's numbering.

Touches `machine`; the pure pieces it composes (debounce, press_detector, clock,
event_queue) are host-tested.
"""
import machine
import utime

import config as cfg
from mcp23017 import MCP23017
from debounce import Debouncer
from press_detector import MultiChannelDetector
import clock
import log

PINS_PER_CHIP = 16

_int_flag = False           # ISR-shared flag; ISR only writes this
_rst_pin = None             # held reference so the /RESET line stays driven high


def _on_mcp_int(pin):
    global _int_flag
    _int_flag = True


def release_mcp_reset():
    """Drive the MCP /RESET line. No-op if PIN_MCP_RESET is None (tied high in HW).

    Pulses LOW briefly to put every chip in a known state, then holds HIGH so the
    chips run. Must be called before any I2C traffic. The Pin is kept on a module
    global so it isn't reconfigured/released and the line stays asserted high.
    """
    global _rst_pin
    if cfg.PIN_MCP_RESET is None:
        return
    _rst_pin = machine.Pin(cfg.PIN_MCP_RESET, machine.Pin.OUT, value=0)  # assert reset
    utime.sleep_ms(1)                                                    # MCP needs ~1us
    _rst_pin.value(1)                                                    # deassert -> run
    utime.sleep_ms(1)


def build_i2c():
    release_mcp_reset()        # bring the MCP chips out of reset before we talk to them
    return machine.I2C(cfg.I2C_ID,
                       sda=machine.Pin(cfg.PIN_I2C_SDA),
                       scl=machine.Pin(cfg.PIN_I2C_SCL),
                       freq=cfg.I2C_FREQ)


def _active_from_bit(bit):
    return (bit == 0) if cfg.ACTIVE_LOW else (bit == 1)


class _Slot:
    """One configured chip position (= one board)."""
    def __init__(self, board, addr, i2c):
        self.board = board
        self.addr = addr
        self.mcp = MCP23017(i2c, addr, cfg.MCP_INT_ACTIVE_LOW,
                            retries=cfg.I2C_RETRIES)
        self.ok = False
        self.bits = 0xFFFF       # last read (idle = all released for active-low)

    def try_init(self):
        try:
            if not self.mcp.present():
                self.ok = False
                return False
            self.mcp.init(pullups=cfg.USE_INTERNAL_PULLUPS,
                          open_drain=cfg.MCP_INT_OPEN_DRAIN)
            self.bits = self.mcp.read_all()
            self.ok = True
            log.info("board%d MCP@0x%02x ready" % (self.board, self.addr))
            return True
        except Exception as e:
            self.ok = False
            log.error("board%d MCP@0x%02x init failed: %s" % (self.board, self.addr, e),
                      every_ms=3000, key="init%d" % self.board)
            return False

    def read(self):
        try:
            self.bits = self.mcp.read_all()
            self.ok = True
        except Exception as e:
            self.ok = False
            log.error("board%d read failed: %s" % (self.board, e),
                      every_ms=2000, key="read%d" % self.board)


def run(shared, queue, i2c, mcp_addresses):
    global _int_flag

    mono = clock.from_utime()

    slots = [_Slot(b + 1, addr, i2c) for b, addr in enumerate(mcp_addresses)]
    n = len(slots)
    n_inputs = n * PINS_PER_CHIP

    # Initial init: try all, but don't block boot forever if a satellite is absent.
    for s in slots:
        s.try_init()
    if not any(s.ok for s in slots):
        log.error("no MCP chips responding at boot; will keep retrying")
    shared.set_mcp(all(s.ok for s in slots))

    # Shared wired-OR INT (falling = some chip asserted active-low).
    trig = machine.Pin.IRQ_FALLING if cfg.MCP_INT_ACTIVE_LOW else machine.Pin.IRQ_RISING
    int_pin = machine.Pin(cfg.PIN_MCP_INT, machine.Pin.IN, machine.Pin.PULL_UP)
    int_pin.irq(trigger=trig, handler=_on_mcp_int)

    # One debouncer + detector slot per global input index (1..n_inputs).
    indices = tuple(range(1, n_inputs + 1))
    debouncers = {i: Debouncer(cfg.DEBOUNCE_MS, initial=False) for i in indices}
    detector = MultiChannelDetector(indices, cfg.LONG_MS, cfg.DOUBLE_GAP_MS)
    applied_tune_ver = shared.tune_version    # live-tune: re-apply when core1 bumps it

    _int_flag = True                 # force a first full read
    wdt = None
    last_health = utime.ticks_ms()

    while True:
        now = mono.ms()

        # Live re-tune: cheap unlocked int compare each pass; only take the lock to
        # read the new set when core1 actually changed something.
        if shared.tune_version != applied_tune_ver:
            ver, lm, dg, db = shared.tunables()
            detector.set_params(lm, dg)
            for d in debouncers.values():
                d.debounce_ms = db
            applied_tune_ver = ver
            log.info("tunables: long=%d double=%d debounce=%d" % (lm, dg, db))

        if wdt is None and cfg.WDT_ENABLE and shared.ready:
            wdt = machine.WDT(timeout=cfg.WDT_TIMEOUT_MS)
            log.info("watchdog armed")

        # Shared INT: we don't know which chip fired, so read every healthy chip.
        # (Reading GPIO clears each chip's interrupt and releases the wired-OR line.)
        if _int_flag:
            _int_flag = False
            for s in slots:
                if s.ok:
                    s.read()

        # Periodic health/recovery: re-init any chip that's down (hot-add on return).
        if utime.ticks_diff(utime.ticks_ms(), last_health) > cfg.MCP_HEALTHCHECK_MS:
            last_health = utime.ticks_ms()
            for s in slots:
                if not s.ok:
                    s.try_init()
            shared.set_mcp(all(s.ok for s in slots))

        # Debounce + detect every pass (time-based gestures), per global index.
        for s in slots:
            base = (s.board - 1) * PINS_PER_CHIP
            bits = s.bits
            for p in range(PINS_PER_CHIP):
                idx = base + p + 1
                act = debouncers[idx].update(_active_from_bit((bits >> p) & 1), now)
                g = detector.update(idx, act, now)
                if g is not None:
                    queue.put((idx, g))
                    log.debug("gesture idx%d=%s" % (idx, g))

        # Feed watchdog only while core1 is alive.
        if wdt is not None:
            if utime.ticks_diff(utime.ticks_ms(), shared.core1_heartbeat) \
                    < cfg.CORE1_STALL_MS:
                wdt.feed()
            else:
                log.error("core1 stalled; withholding WDT feed",
                          every_ms=2000, key="core1stall")

        utime.sleep_ms(2)            # yield (releases GIL so core1 runs)
