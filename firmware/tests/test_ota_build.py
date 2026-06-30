"""Host tests for the OTA bundle *builder* (tools/ota_build.py), CPython, no board.

Run:  python3 tests/test_ota_build.py
Covers the .mpy compilation path (default) and the raw-.py fallback (--no-mpy), checking
that bundles round-trip through the device serializer (ota.parse_bundle) with the right
file names. mpy-cross is an external dependency, so the .mpy tests SKIP (not fail) when it
is not installed -- CI installs it (mpy-cross==1.27.0.post2). CPython cannot *import* a
.mpy, so these validate structure + serializer integrity only; actual import is HW-verify.
"""
import os
import sys

HERE = os.path.dirname(__file__)
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "tools"))
sys.path.insert(0, SRC)
sys.path.insert(0, TOOLS)

import ota          # noqa: E402  -- the device-shared serializer
import ota_build    # noqa: E402  -- the host builder under test


def _have_mpy():
    ok, _ = ota_build._check_mpy_cross()
    return ok


# ---- raw .py fallback (no external dependency) ----
def test_no_mpy_bundles_py():
    bundle, names = ota_build.build_bundle(SRC, use_mpy=False)
    assert names, "expected at least one bundled module"
    assert all(n.endswith(".py") for n in names), names
    assert "main.py" not in names                 # main.py is the loader; never bundled
    assert "app.py" in names                       # the app entry MUST be bundled
    files = ota.parse_bundle(bundle)              # verifies per-file sha + round-trip
    assert [n for n, _ in files] == names


# ---- .mpy path (default) -- guarded on mpy-cross availability ----
def test_mpy_bundles_mpy():
    if not _have_mpy():
        print("SKIP test_mpy_bundles_mpy (mpy-cross not installed)")
        return
    bundle, names = ota_build.build_bundle(SRC, use_mpy=True)
    assert names, "expected at least one bundled module"
    assert all(n.endswith(".mpy") for n in names), names
    assert "main.mpy" not in names                 # main.py is the loader; never bundled
    assert "app.mpy" in names                       # the app entry MUST be bundled
    files = ota.parse_bundle(bundle)              # verifies per-file sha + round-trip
    assert [n for n, _ in files] == names
    for n, content in files:
        assert content, "empty .mpy for %s" % n
        assert content[0] == 0x4D, "%s missing .mpy magic 'M'" % n   # b"M"


def test_mpy_smaller_than_py():
    if not _have_mpy():
        print("SKIP test_mpy_smaller_than_py (mpy-cross not installed)")
        return
    mpy_bundle, _ = ota_build.build_bundle(SRC, use_mpy=True)
    py_bundle, _ = ota_build.build_bundle(SRC, use_mpy=False)
    # The whole point: bytecode is much smaller than commented source. A soft floor of
    # "strictly smaller" also catches a silent no-op compile.
    assert len(mpy_bundle) < len(py_bundle), (len(mpy_bundle), len(py_bundle))


def test_missing_mpy_cross_raises():
    orig = ota_build._mpy_cross_cmd
    ota_build._mpy_cross_cmd = lambda: [sys.executable, "-m", "no_such_mpy_cross_pkg"]
    try:
        ota_build.build_bundle(SRC, use_mpy=True)
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit when mpy-cross is unavailable")
    finally:
        ota_build._mpy_cross_cmd = orig


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
