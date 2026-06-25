"""Host tests for the OTA pure core + file I/O (CPython, no board).

Run:  python3 tests/test_ota.py
Covers the boot-confirm/auto-revert state machine, the bundle build/parse/verify
round-trip, and the slot install + state read/write (using a temp dir). The on-device
parts that need `machine` (reset) are not exercised here.
"""
import os
import sys
import tempfile

SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import ota  # noqa: E402


# ---- boot-confirm / auto-revert state machine ----
def test_boot_normal_when_not_pending():
    st = {"active": "b", "previous": "a", "pending": False, "tries": 0}
    slot, new, reverted = ota.boot_decision(st, max_tries=2)
    assert slot == "b" and not reverted and new is st


def test_boot_pending_increments_tries():
    st = {"active": "b", "previous": "a", "pending": True, "tries": 0}
    slot, new, reverted = ota.boot_decision(st, max_tries=2)
    assert slot == "b" and not reverted and new["tries"] == 1 and new["pending"] is True


def test_boot_reverts_after_max_tries():
    st = {"active": "b", "previous": "a", "pending": True, "tries": 2}
    slot, new, reverted = ota.boot_decision(st, max_tries=2)   # this is try 3 -> revert
    assert reverted and slot == "a"
    assert new["active"] == "a" and new["pending"] is False and new["tries"] == 0


def test_confirm_clears_pending():
    st = {"active": "b", "previous": "a", "pending": True, "tries": 1}
    new = ota.confirm_state(st)
    assert new["pending"] is False and new["tries"] == 0 and new["active"] == "b"


def test_staged_state_targets_new_slot():
    st = {"active": "a", "previous": "a", "pending": False, "tries": 0}
    new = ota.staged_state(st, "b")
    assert new["active"] == "b" and new["previous"] == "a"
    assert new["pending"] is True and new["tries"] == 0


def test_other_slot():
    assert ota.other_slot("a") == "b" and ota.other_slot("b") == "a"


# ---- bundle build / parse / verify ----
def test_bundle_roundtrip():
    files = [("main.py", b"print('hi')\n"), ("config.py", b"X = 1\n")]
    blob = ota.build_bundle(files)
    out = ota.parse_bundle(blob)
    assert out == files


def test_bundle_detects_corruption():
    blob = bytearray(ota.build_bundle([("a.py", b"hello world")]))
    blob[-1] ^= 0xFF                        # flip a payload byte
    try:
        ota.parse_bundle(bytes(blob))
        assert False, "expected sha mismatch"
    except ValueError as e:
        assert "sha mismatch" in str(e)


def test_bundle_detects_truncation():
    blob = ota.build_bundle([("a.py", b"hello world")])
    try:
        ota.parse_bundle(blob[:-3])         # drop payload bytes
        assert False, "expected short-file error"
    except ValueError:
        pass


def test_bundle_rejects_no_manifest():
    try:
        ota.parse_bundle(b"no newline here")
        assert False
    except ValueError:
        pass


# ---- slot install + state I/O (temp dir) ----
def test_apply_bundle_writes_slot():
    files = [("main.py", b"def main():\n  pass\n"), ("x.py", b"Y=2\n")]
    blob = ota.build_bundle(files)
    d = tempfile.mkdtemp()
    slot = os.path.join(d, "slots", "b")
    os.makedirs(slot)
    names = ota.apply_bundle(blob, slot)
    assert sorted(names) == ["main.py", "x.py"]
    with open(os.path.join(slot, "main.py"), "rb") as f:
        assert f.read() == files[0][1]


def test_apply_bundle_replaces_existing():
    d = tempfile.mkdtemp()
    slot = os.path.join(d, "b")
    os.makedirs(slot)
    with open(os.path.join(slot, "stale.py"), "wb") as f:
        f.write(b"old")
    ota.apply_bundle(ota.build_bundle([("main.py", b"new")]), slot)
    assert os.listdir(slot) == ["main.py"]   # stale file gone


def test_apply_bundle_rejects_bad_before_write():
    d = tempfile.mkdtemp()
    slot = os.path.join(d, "b")
    os.makedirs(slot)
    bad = bytearray(ota.build_bundle([("a.py", b"data")]))
    bad[-1] ^= 0xFF
    try:
        ota.apply_bundle(bytes(bad), slot)
        assert False
    except ValueError:
        pass
    assert os.listdir(slot) == []            # nothing written on a bad bundle


def test_state_roundtrip_and_default():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "state")
    assert ota.read_state(p) == ota.default_state()   # missing -> default
    st = {"active": "b", "previous": "a", "pending": True, "tries": 1}
    ota.write_state(p, st)
    assert ota.read_state(p) == st
    with open(p, "w") as f:
        f.write("{corrupt")
    assert ota.read_state(p) == ota.default_state()   # corrupt -> default


# ---- chunked receiver + streaming install ----
def _chunks(blob, size):
    return [blob[i:i + size] for i in range(0, len(blob), size)] or [b""]


def test_receiver_happy_path():
    blob = ota.build_bundle([("main.py", b"A" * 2500), ("c.py", b"B" * 100)])
    chunks = _chunks(blob, 1024)
    d = tempfile.mkdtemp()
    staging = os.path.join(d, "staging.bin")
    rx = ota.OtaReceiver(staging, len(chunks), len(blob), ota.sha256_hex(blob), 1024)
    for i, c in enumerate(chunks):
        rx.add_chunk(i, c)
    assert rx.complete and rx.percent() == 100 and rx.missing() == []
    rx.finish()                                  # verifies whole-bundle sha
    slot = os.path.join(d, "b"); os.makedirs(slot)
    names = ota.apply_bundle_file(staging, slot)
    assert sorted(names) == ["c.py", "main.py"]
    with open(os.path.join(slot, "main.py"), "rb") as f:
        assert f.read() == b"A" * 2500


def test_receiver_tolerates_out_of_order_and_missing():
    blob = ota.build_bundle([("a.py", b"x" * 3500)])   # 4 chunks @1024
    chunks = _chunks(blob, 1024)
    d = tempfile.mkdtemp()
    staging = os.path.join(d, "s.bin")
    rx = ota.OtaReceiver(staging, len(chunks), len(blob), ota.sha256_hex(blob), 1024)
    # arrive out of order, skipping index 1
    rx.add_chunk(0, chunks[0])
    rx.add_chunk(2, chunks[2])
    rx.add_chunk(3, chunks[3])
    assert not rx.complete and rx.missing() == [1]
    rx.add_chunk(1, chunks[1])                    # NAK -> resend the missing one
    assert rx.complete and rx.missing() == []
    rx.finish()
    slot = os.path.join(d, "b"); os.makedirs(slot)
    assert ota.apply_bundle_file(staging, slot) == ["a.py"]


def test_receiver_ignores_duplicates():
    blob = ota.build_bundle([("a.py", b"y" * 1500)])   # 2 chunks
    chunks = _chunks(blob, 1024)
    d = tempfile.mkdtemp()
    rx = ota.OtaReceiver(os.path.join(d, "s.bin"), len(chunks), len(blob),
                         ota.sha256_hex(blob), 1024)
    rx.add_chunk(0, chunks[0])
    rx.add_chunk(0, chunks[0])                    # duplicate -> ignored
    rx.add_chunk(1, chunks[1])
    assert rx.complete
    rx.finish()


def test_receiver_detects_corrupt_stream():
    blob = ota.build_bundle([("a.py", b"hello" * 500)])
    chunks = _chunks(bytes(blob), 1024)
    bad = bytearray(chunks[1]); bad[0] ^= 0xFF
    chunks[1] = bytes(bad)
    d = tempfile.mkdtemp()
    staging = os.path.join(d, "s.bin")
    rx = ota.OtaReceiver(staging, len(chunks), len(blob), ota.sha256_hex(blob), 1024)
    for i, c in enumerate(chunks):
        rx.add_chunk(i, c)
    try:
        rx.finish()
        assert False, "expected sha mismatch"
    except ValueError:
        pass


def test_file_sha256_matches():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "f")
    data = b"some bytes here" * 200
    with open(p, "wb") as f:
        f.write(data)
    assert ota.file_sha256(p) == ota.sha256_hex(data)


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
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
