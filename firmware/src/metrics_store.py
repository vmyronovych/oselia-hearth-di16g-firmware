"""Persistent backing for the metrics registry -- tiered, RP2040/MicroPython-specific.

Per the research (stability-metrics-design.md): `machine.RTC().memory()` does NOT exist on the
rp2 port, so cross-reboot persistence uses two tiers:

  * SCRATCH tier -- the RP2040 WATCHDOG SCRATCH0..3 registers (`machine.mem32`). Survive a
    watchdog reset and `machine.reset()` but NOT power loss. Cheap to write -> checkpoint often.
    Holds only boot_count + a crash marker (16 bytes total), guarded by magic+CRC.
  * FLASH tier -- a tiny atomic littlefs file. Survives power loss. Multi-ms to write (stalls
    both cores via the flash lock) -> rate-limited to a slow cadence, one sector.

The orchestration (CRC, pack/unpack, flush rate-limit, boot reconciliation) is PURE and host-
tested with fakes; only `ScratchStore`/`FlashStore` touch hardware and are marked HW-VERIFY.

HW-VERIFY (flagged by research, confirm on this MP 1.28 / rp2 build):
  * SCRATCH0..3 are not written by the MicroPython runtime itself (we reject on magic/CRC).
  * Prefer the watchdog REASON register over machine.reset_cause() for "was this a watchdog
    reset?" (reset_cause() is incomplete on rp2).
"""
try:
    import ustruct as struct
except ImportError:
    import struct

try:
    import ujson as json
except ImportError:
    import json


# ---- pure helpers (host-tested) ----
SCRATCH_MAGIC = 0x05E11A00          # high 24 bits = "OSELIA" marker; low byte = version
_MASK = 0xFFFFFFFF


def crc32(data, crc=0xFFFFFFFF):
    """Bitwise CRC-32 (no table -> tiny code). Pure."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1))
    return (crc ^ 0xFFFFFFFF) & _MASK


def pack_scratch(boot_count, crash_code, version=1):
    """-> (w0,w1,w2,w3) for SCRATCH0..3: magic|version, boot_count, crash_code, crc. Pure."""
    w0 = (SCRATCH_MAGIC | (version & 0xFF)) & _MASK
    w1 = boot_count & _MASK
    w2 = crash_code & _MASK
    w3 = crc32(struct.pack("<III", w0, w1, w2))
    return (w0, w1, w2, w3)


def unpack_scratch(words):
    """Validate magic + CRC; -> {version, bc, crash_code} or None if untrusted. Pure."""
    if len(words) < 4:
        return None
    w0, w1, w2, w3 = words[0], words[1], words[2], words[3]
    if (w0 & 0xFFFFFF00) != SCRATCH_MAGIC:
        return None
    if crc32(struct.pack("<III", w0, w1, w2)) != w3:
        return None
    return {"version": w0 & 0xFF, "bc": w1, "crash_code": w2}


def crash_code(crash_rec):
    """Tiny int marker for the scratch tier: 0 = clean, else a nonzero flag. Pure."""
    return 1 if crash_rec else 0


def should_flush(now_ms, last_ms, interval_ms):
    """Rate-limit gate for the flash tier. First call always flushes; then every interval. Pure."""
    if last_ms is None:
        return True
    return (now_ms - last_ms) >= interval_ms


# ---- hardware tiers (HW-VERIFY; import machine/os lazily so the module stays host-importable) ----
class ScratchStore:
    """RP2040 watchdog SCRATCH0..3 via machine.mem32. HW-VERIFY on real hardware."""
    BASE = 0x40058000
    REASON = BASE + 0x08
    SCRATCH0 = BASE + 0x0C          # SCRATCH0..3 = 0x0c,0x10,0x14,0x18 (4..7 reserved by bootrom)

    def __init__(self):
        import machine
        self._mem = machine.mem32

    def was_watchdog(self):
        # REASON bit0 = TIMER (watchdog fired). Preferred over machine.reset_cause() on rp2.
        return bool(self._mem[self.REASON] & 0x1)

    def save(self, boot_count, code, version=1):
        for i, word in enumerate(pack_scratch(boot_count, code, version)):
            self._mem[self.SCRATCH0 + 4 * i] = word

    def load(self):
        words = [self._mem[self.SCRATCH0 + 4 * i] for i in range(4)]
        return unpack_scratch(words)


class FlashStore:
    """Atomic tiny-file persistence on littlefs: write tmp -> sync -> rename. HW-VERIFY."""

    def __init__(self, path="/metrics_state.json"):
        self.path = path

    def save(self, payload):
        import os
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(payload))
            f.flush()
            try:
                os.sync()
            except (AttributeError, OSError):
                pass
        os.rename(tmp, self.path)       # atomic commit on littlefs

    def load(self):
        try:
            with open(self.path) as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            return None


# ---- orchestrator (pure logic; inject scratch/flash/clock -> host-testable) ----
class PersistentStore:
    """Combines the tiers behind the metrics store interface: load()/checkpoint()/flush()."""

    def __init__(self, scratch=None, flash=None, clock_ms=None, flush_interval_ms=300000):
        self.scratch = scratch
        self.flash = flash
        self.clock_ms = clock_ms
        self.flush_interval_ms = flush_interval_ms
        self._last_flush = None

    def load(self):
        """Full payload from flash (power-loss durable); if the last reset was a watchdog and the
        scratch record is valid, trust its (fresher) boot_count by taking the max."""
        data = self.flash.load() if self.flash else None
        if self.scratch is not None:
            try:
                sc = self.scratch.load()
                if sc and self.scratch.was_watchdog():
                    data = data or {}
                    data["bc"] = max(data.get("bc", 0), sc.get("bc", 0))
            except Exception:
                pass
        return data

    def checkpoint(self, payload):
        """Cheap scratch write -- call often (e.g. each publish)."""
        if self.scratch is not None:
            self.scratch.save(payload.get("bc", 0), crash_code(payload.get("cr")))

    def flush(self, payload):
        """Durable flash write -- rate-limited; always refreshes scratch too."""
        now = self.clock_ms() if self.clock_ms else 0
        if self.flash is not None and should_flush(now, self._last_flush, self.flush_interval_ms):
            self.flash.save(payload)
            self._last_flush = now
        self.checkpoint(payload)
