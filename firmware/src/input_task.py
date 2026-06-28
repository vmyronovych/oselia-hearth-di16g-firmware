"""Core 0 -- real-time input task (multi-board), resilient + observable.

Owns the shared I2C bus and up to 8 MCP23017 chips, resolves the board set at boot
(so the network core is never gated on I2C), debounce + gesture detection for every
input, AND the MCP recovery + health bookkeeping. Detected gestures are pushed onto
the event queue (global 1-based index) for core 1.

Board number = position in the resolved address list (1-based; this task scans the
bus at boot). Global index for board b, pin p (both 1-based) is (b-1)*16 + p.

Input is **pure polling**: every healthy chip's GPIO is read on a fixed cadence
(MCP_POLL_MS) -- there is NO interrupt. The MCP23017 INT/wired-OR line is not used by
the firmware (it was the source of the original freeze + dropped-press faults; a
shared open-drain IRQ across satellite boards is inherently fragile). Polling is
deterministic, self-healing, and -- with the hardware RC+optocoupler debounce --
plenty fast for wall switches.

Resilience (SPEC.md sec.12) -- an MCP fault must never freeze healthy inputs nor
reboot the board: a chip that fails reads is marked down, skipped, and recovered.
Recovery escalates, rate-limited with backoff: L1 = clock the I2C bus to free a
stuck SDA; L2 = pulse the MCP /RESET line. The watchdog lives on core1, so a core0
I2C stall never reboots. Per-board health + counters are snapshotted into
SharedState and published by core1 to diag/state + diag/event.

Touches `machine`; the pure pieces it composes (debounce, press_detector, clock,
event_queue, mcp_health) are host-tested.
"""
import machine
import utime

import config as cfg
from mcp23017 import MCP23017
from debounce import Debouncer
from press_detector import MultiChannelDetector
import mcp_health
from mcp_health import (BoardStatus, FaultRing, classify_oserror,
                        CODE_MCP_ABSENT, CODE_MCP_INIT_FAIL,
                        CODE_BUS_RECOVERED, CODE_MCP_RESET)
import clock
import log

PINS_PER_CHIP = 16
_IDLE_BITS = 0xFFFF             # all-released for active-low (no stuck press when down)

_rst_pin = None                # held reference so the /RESET line stays driven high


def release_mcp_reset():
    """Drive the MCP /RESET line. No-op if PIN_MCP_RESET is None (tied high in HW).

    Pulses LOW briefly to put every chip in a known state, then holds HIGH so the
    chips run. The Pin is kept on a module global so it isn't reconfigured/released
    and the line stays asserted high. Used at boot and as L2 recovery.
    """
    global _rst_pin
    if cfg.PIN_MCP_RESET is None:
        return
    _rst_pin = machine.Pin(cfg.PIN_MCP_RESET, machine.Pin.OUT, value=0)  # assert reset
    utime.sleep_ms(1)                                                    # MCP needs ~1us
    _rst_pin.value(1)                                                    # deassert -> run
    utime.sleep_ms(1)


def _make_i2c():
    """Create the hardware I2C peripheral (no /RESET pulse). A bounded `timeout` caps
    how long any single transaction can block on a dead/floating bus -- an unpowered
    MCP board removes the bus pull-ups, and without a timeout a hung op stalls core0
    for seconds. Fall back if the port build doesn't accept `timeout`."""
    sda = machine.Pin(cfg.PIN_I2C_SDA)
    scl = machine.Pin(cfg.PIN_I2C_SCL)
    to = getattr(cfg, "I2C_TIMEOUT_US", 0)
    if to:
        try:
            return machine.I2C(cfg.I2C_ID, sda=sda, scl=scl, freq=cfg.I2C_FREQ,
                               timeout=to)
        except (TypeError, ValueError):
            pass
    return machine.I2C(cfg.I2C_ID, sda=sda, scl=scl, freq=cfg.I2C_FREQ)


def build_i2c():
    """Reset the chips then build the I2C bus (boot path / convenience)."""
    release_mcp_reset()
    return _make_i2c()


def _bus_recover():
    """L1 recovery: free a slave holding SDA low by clocking SCL up to 9 times as
    GPIO, emit a STOP, then recreate the I2C peripheral. No /RESET pulse. Returns a
    fresh machine.I2C. Quick (tens of us) -- safe within the WDT budget."""
    try:
        scl = machine.Pin(cfg.PIN_I2C_SCL, machine.Pin.OPEN_DRAIN, value=1)
        sda = machine.Pin(cfg.PIN_I2C_SDA, machine.Pin.OPEN_DRAIN, value=1)
        for _ in range(9):
            scl.value(0)
            utime.sleep_us(5)
            scl.value(1)
            utime.sleep_us(5)
            if sda.value():            # SDA released by the slave
                break
        sda.value(0)                   # STOP: SDA low->high while SCL high
        utime.sleep_us(5)
        scl.value(1)
        utime.sleep_us(5)
        sda.value(1)
        utime.sleep_us(5)
    except Exception as e:
        log.error("bus reclock failed: %s" % e, every_ms=5000, key="busrec")
    return _make_i2c()


def _probe(i2c, addr):
    """Single-address ACK check, bounded by the I2C timeout. Used instead of
    i2c.scan() (which probes 112 addresses and can stall core0 for seconds on a
    dead/floating bus) -- we only ever care about the MCP strap range anyway."""
    try:
        i2c.readfrom(addr, 1)
        return True
    except Exception:
        return False


def _resolve_addresses(i2c):
    """Probe the MCP strap range (unless pinned) and return the address list to drive.

    Probes only 0x20..0x27 individually (not a full 112-address bus scan), with bounded
    retry because satellite chips may power up just after the MCU. Falls back to
    cfg.MCP_ADDRESSES inside select_addresses if nothing answers.
    """
    import mcp_select
    if not cfg.MCP_AUTODISCOVER:
        return list(cfg.MCP_ADDRESSES)
    found = []
    for _ in range(5):
        found = [a for a in range(mcp_select.MCP_ADDR_MIN, mcp_select.MCP_ADDR_MAX + 1)
                 if _probe(i2c, a)]
        if found:
            break
        utime.sleep_ms(200)
    return mcp_select.select_addresses(found, cfg.MCP_ADDRESSES,
                                       cfg.MCP_AUTODISCOVER)


def _active_from_bit(bit):
    return (bit == 0) if cfg.ACTIVE_LOW else (bit == 1)


class _Slot:
    """One configured chip position (= one board) + its health record."""
    def __init__(self, board, addr, i2c):
        self.board = board
        self.addr = addr
        self.mcp = MCP23017(i2c, addr, retries=cfg.I2C_RETRIES)
        self.status = BoardStatus(board, addr)
        self.bits = _IDLE_BITS       # last read (idle = all released for active-low)

    def try_init(self, now_ms):
        """(Re)initialise the chip. Returns (ok, edge); edge True on a not-ok->ok
        transition (a recovery)."""
        try:
            if not self.mcp.present():
                self.bits = _IDLE_BITS
                return False, self.status.mark_fail(CODE_MCP_ABSENT,
                                                    "no ACK on bus scan")
            self.mcp.init(pullups=cfg.USE_INTERNAL_PULLUPS)
            self.bits = self.mcp.read_all()
            edge = self.status.mark_ok(now_ms)
            if edge:
                log.info("board%d MCP@0x%02x ready" % (self.board, self.addr))
            return True, edge
        except Exception as e:
            self.bits = _IDLE_BITS
            _code, detail = classify_oserror(e)
            edge = self.status.mark_fail(CODE_MCP_INIT_FAIL, detail)
            log.error("board%d MCP@0x%02x init failed: %s"
                      % (self.board, self.addr, e),
                      every_ms=3000, key="init%d" % self.board)
            return False, edge

    def read(self, now_ms):
        """Read a healthy chip. Returns True on a down edge (ok->not-ok)."""
        try:
            self.bits = self.mcp.read_all()
            self.status.mark_ok(now_ms)        # ok->ok is not an edge
            return False
        except Exception as e:
            self.bits = _IDLE_BITS
            code, detail = classify_oserror(e)
            edge = self.status.mark_fail(code, detail)
            if edge:
                log.error("board%d read failed: %s" % (self.board, e),
                          every_ms=2000, key="read%d" % self.board)
            return edge


def run(shared, queue):
    mono = clock.from_utime()

    # Bring the bus up and resolve the board set HERE, on core 0, so the network
    # core (already spawned) is never gated on I2C. Publish the result for core 1.
    release_mcp_reset()
    i2c = _make_i2c()
    mcp_addresses = _resolve_addresses(i2c)
    n = len(mcp_addresses)
    addr_strs = ["0x%02x" % a for a in mcp_addresses]
    shared.set_boards(n, addr_strs)
    log.info("MCP boards: %d (%s)%s" % (
        n, ",".join(addr_strs),
        " autodiscover" if cfg.MCP_AUTODISCOVER else " pinned"))

    slots = [_Slot(b + 1, addr, i2c) for b, addr in enumerate(mcp_addresses)]
    n_inputs = n * PINS_PER_CHIP

    counters = {"bus_recoveries": 0, "mcp_resets": 0}
    ring = FaultRing(cfg.DIAG_FAULT_RING)
    policy = mcp_health.RecoveryPolicy(
        cfg.MCP_RECOVERY_AFTER_FAILS, cfg.MCP_RECOVERY_MIN_INTERVAL_MS,
        getattr(cfg, "MCP_RECOVERY_MAX_INTERVAL_MS", None))

    def _snapshot(now_ms):
        boards_ok = 0
        mcp = []
        for s in slots:
            if s.status.ok:
                boards_ok += 1
            mcp.append(s.status.as_dict(now_ms))
        return {
            "mcp": mcp,
            "boards_total": len(slots),
            "boards_ok": boards_ok,
            "counters": {"bus_recoveries": counters["bus_recoveries"],
                         "mcp_resets": counters["mcp_resets"]},
            "last_fault": ring.last(),
            "recent": ring.recent(),
        }

    def _fault(now_ms, code, detail, board=None):
        rec = ring.add(now_ms // 1000, "mcp", code, detail, board)
        shared.note_fault(rec)

    def _all_ok():
        for s in slots:
            if not s.status.ok:
                return False
        return True

    def _do_recovery(level, now_ms):
        # nonlocal i2c so a recreated peripheral is used by subsequent reads.
        nonlocal i2c
        if level == 1:
            i2c = _bus_recover()
            counters["bus_recoveries"] += 1
            _fault(now_ms, CODE_BUS_RECOVERED, "L1 I2C bus reclock")
            log.warn("MCP recovery L1: I2C bus reclock", every_ms=2000, key="rec1")
        else:
            release_mcp_reset()
            i2c = _make_i2c()
            counters["mcp_resets"] += 1
            _fault(now_ms, CODE_MCP_RESET, "L2 /RESET pulse")
            log.warn("MCP recovery L2: /RESET pulse", every_ms=2000, key="rec2")
        for s in slots:
            s.mcp.i2c = i2c                 # repoint each chip at the new bus
            s.try_init(now_ms)
        shared.set_mcp(_all_ok())
        shared.set_mcp_diag(_snapshot(now_ms), changed=True)
        if _all_ok():
            policy.note_recovered()

    # Initial init: try all, but don't block boot if a satellite is absent.
    for s in slots:
        s.try_init(mono.ms())
    if not any(s.status.ok for s in slots):
        log.error("no MCP chips responding at boot; will keep retrying")
    shared.set_mcp(_all_ok())
    shared.set_mcp_diag(_snapshot(mono.ms()), changed=True)

    # One debouncer + detector slot per global input index (1..n_inputs).
    indices = tuple(range(1, n_inputs + 1))
    debouncers = {i: Debouncer(cfg.DEBOUNCE_MS, initial=False) for i in indices}
    detector = MultiChannelDetector(indices, cfg.LONG_MS, cfg.DOUBLE_GAP_MS)
    applied_tune_ver = shared.tune_version    # live-tune: re-apply when core1 bumps it

    last_health = mono.ms()
    last_poll = mono.ms() - cfg.MCP_POLL_MS    # poll on the first pass

    # NOTE: input is PURE POLLING -- there is no MCP interrupt. core0 also does NOT own
    # the watchdog (it's on core1), so an MCP/I2C stall on this core can never reboot
    # the board -- a hung bus is reported and recovered, never reset (SPEC.md sec.12).
    # Bounded I2C ops keep this loop responsive regardless.

    while True:
        now = mono.ms()

        # Live re-tune: cheap unlocked int compare each pass; lock only on change.
        if shared.tune_version != applied_tune_ver:
            ver, lm, dg, db = shared.tunables()
            detector.set_params(lm, dg)
            for d in debouncers.values():
                d.debounce_ms = db
            applied_tune_ver = ver
            log.info("tunables: long=%d double=%d debounce=%d" % (lm, dg, db))

        # Read every healthy chip on a fixed cadence -- the sole input mechanism (no
        # INT). Deterministic and self-healing: a chip that glitches simply shows up
        # in the next poll; a dead chip is marked down (below) and skipped. Cheap: a
        # 2-byte read per healthy chip every MCP_POLL_MS.
        if (now - last_poll) >= cfg.MCP_POLL_MS:
            last_poll = now
            edge_any = False
            for s in slots:
                if s.status.ok and s.read(now):     # True == down edge
                    edge_any = True
                    _fault(now, s.status.code, s.status.detail, s.board)
            if edge_any:
                shared.set_mcp(_all_ok())
                shared.set_mcp_diag(_snapshot(now), changed=True)

        # Recovery escalation (rate-limited with backoff inside the policy). Fires only
        # when a chip is actually FAILING (unreadable); when all healthy, decide() still
        # runs to reset the backoff ladder.
        failing = not _all_ok()
        if failing:
            fail_streak = 0
            for s in slots:
                if s.status.fails > fail_streak:
                    fail_streak = s.status.fails
            level = policy.decide(now, True, fail_streak, False)
            if level:
                _do_recovery(level, now)
        else:
            policy.decide(now, False, 0, False)   # healthy -> reset the backoff ladder

        # Periodic health/recovery: re-init any chip that's down (hot-add on return).
        if (now - last_health) > cfg.MCP_HEALTHCHECK_MS:
            last_health = now
            edge_any = False
            for s in slots:
                if not s.status.ok:
                    _ok, edge = s.try_init(now)
                    if edge:                        # not-ok -> ok recovery
                        edge_any = True
            shared.set_mcp(_all_ok())
            shared.set_mcp_diag(_snapshot(now), changed=edge_any)

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

        utime.sleep_ms(2)            # yield (releases GIL so core1 runs)
