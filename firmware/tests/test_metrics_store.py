"""Host tests for metrics_store pure logic (CPython, no board).

Run:  python3 tests/test_metrics_store.py
Covers CRC reject, scratch pack/unpack, the flash rate-limit gate, and PersistentStore
reconciliation (watchdog -> trust scratch boot_count via max) with fake tiers.
The hardware classes (ScratchStore/FlashStore) are HW-VERIFY and not exercised here.
"""
import os
import sys

SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import metrics_store as ms                            # noqa: E402


def test_pack_unpack_round_trip():
    words = ms.pack_scratch(boot_count=42, crash_code=1, version=1)
    got = ms.unpack_scratch(words)
    assert got["bc"] == 42 and got["crash_code"] == 1 and got["version"] == 1


def test_unpack_rejects_bad_magic():
    assert ms.unpack_scratch([0xDEADBEEF, 1, 0, 0]) is None


def test_unpack_rejects_bad_crc():
    w0, w1, w2, w3 = ms.pack_scratch(7, 0)
    assert ms.unpack_scratch([w0, w1, w2, w3 ^ 0xFF]) is None        # corrupted crc


def test_crc32_known_value():
    # CRC-32 of b"123456789" is the standard 0xCBF43926.
    assert ms.crc32(b"123456789") == 0xCBF43926


def test_crash_code():
    assert ms.crash_code(None) == 0
    assert ms.crash_code({"rc": "wdt"}) == 1


def test_should_flush_first_then_interval():
    assert ms.should_flush(1000, None, 300000) is True              # first time
    assert ms.should_flush(1000, 1000, 300000) is False             # too soon
    assert ms.should_flush(301000, 1000, 300000) is True            # interval elapsed


# ---- PersistentStore with fakes ----
class _FakeScratch:
    def __init__(self, rec=None, watchdog=False):
        self.rec = rec
        self.watchdog = watchdog
        self.saved = None

    def was_watchdog(self):
        return self.watchdog

    def save(self, boot_count, code, version=1):
        self.saved = (boot_count, code)

    def load(self):
        return self.rec


class _FakeFlash:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    def save(self, payload):
        self.data = payload
        self.saves += 1

    def load(self):
        return self.data


def test_load_prefers_flash_payload():
    flash = _FakeFlash({"bc": 5, "c": {"re": 9}})
    store = ms.PersistentStore(flash=flash)
    assert store.load()["c"]["re"] == 9


def test_watchdog_reset_takes_max_boot_count_from_scratch():
    flash = _FakeFlash({"bc": 5, "c": {}})
    scratch = _FakeScratch(rec={"bc": 8, "crash_code": 1}, watchdog=True)
    store = ms.PersistentStore(scratch=scratch, flash=flash)
    assert store.load()["bc"] == 8                  # scratch is fresher after a wdt reset


def test_non_watchdog_ignores_scratch_boot_count():
    flash = _FakeFlash({"bc": 5, "c": {}})
    scratch = _FakeScratch(rec={"bc": 8, "crash_code": 0}, watchdog=False)
    store = ms.PersistentStore(scratch=scratch, flash=flash)
    assert store.load()["bc"] == 5                  # power-on: trust flash


def test_flush_rate_limited_but_checkpoint_always():
    clock = [0]
    flash = _FakeFlash()
    scratch = _FakeScratch()
    store = ms.PersistentStore(scratch=scratch, flash=flash,
                               clock_ms=lambda: clock[0], flush_interval_ms=300000)
    store.flush({"bc": 1, "c": {}, "cr": None})     # first -> flushes
    assert flash.saves == 1 and scratch.saved[0] == 1
    clock[0] = 1000
    store.flush({"bc": 2, "c": {}, "cr": None})     # too soon -> no flash, but scratch updates
    assert flash.saves == 1 and scratch.saved[0] == 2
    clock[0] = 400000
    store.flush({"bc": 3, "c": {}, "cr": None})     # interval elapsed -> flushes
    assert flash.saves == 2


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


if __name__ == "__main__":
    failures = 0
    for t in _all_tests():
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failures += 1
            print("FAIL", t.__name__, "-", e)
        except Exception as e:
            failures += 1
            print("ERROR", t.__name__, "-", repr(e))
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
