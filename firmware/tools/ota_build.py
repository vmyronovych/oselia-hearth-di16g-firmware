#!/usr/bin/env python3
"""Build an OTA bundle artifact (+ optional release manifest) from a firmware src dir.

The bundle is what the HA integration downloads and streams to the device; the device
verifies its sha256 before applying (firmware/OTA_SPEC.md). Use in a release pipeline:
build the bundle, upload it (e.g. a GitHub Release asset), and publish a manifest JSON
that the integration's release feed (CONF_RELEASE_URL) points at.

By default each bundled module is compiled to MicroPython bytecode (`.mpy`) with
`mpy-cross` before packaging: the device imports `.mpy` transparently, the bundle gets
~70% smaller (fewer MQTT chunks => less loss exposure), and the on-device OTA contract
(manifest names + per-file/whole sha256) is unchanged. The cross-compiler must emit a
`.mpy` version the interpreter accepts (v6.3 for MicroPython 1.23+, which the board's
1.28.0 uses); install with `pip install mpy-cross==1.27.0.post2`. Pass `--no-mpy` to
bundle raw `.py` instead (e.g. quick local iteration without mpy-cross installed).

Example:
    python3 tools/ota_build.py --out hearth-0.2.0.bundle \
        --url https://example/hearth-0.2.0.bundle --manifest manifest.json
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
import ota  # noqa: E402  -- the firmware's own bundle builder (single source of truth)


def read_version(src):
    try:
        with open(os.path.join(src, "config.py")) as f:
            for line in f:
                if line.strip().startswith("SW_VERSION"):
                    return line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
    except OSError:
        pass
    return "0.0.0"


def _mpy_cross_cmd():
    # Run via `-m` against the active interpreter so the version is whatever this env
    # pins; avoids PATH/shim ambiguity between a `mpy-cross` binary and the wheel.
    return [sys.executable, "-m", "mpy_cross"]


def _check_mpy_cross():
    """-> (ok, version_str). version_str is mpy-cross's --version banner (incl. the
    emitted .mpy version, e.g. 'mpy-cross emitting mpy v6.3')."""
    try:
        r = subprocess.run(_mpy_cross_cmd() + ["--version"],
                           capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, ValueError):
        return False, ""


def _compile_mpy(py_path, out_dir):
    """Compile one source file to <name>.mpy. -> (name.mpy, bytes). Raises on failure
    or empty output so a bad compile aborts the build before any bundle is written."""
    base = os.path.splitext(os.path.basename(py_path))[0]
    out = os.path.join(out_dir, base + ".mpy")
    r = subprocess.run(_mpy_cross_cmd() + ["-o", out, py_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("mpy-cross failed for %s:\n%s"
                           % (py_path, (r.stdout + r.stderr).strip()))
    with open(out, "rb") as f:
        data = f.read()
    if not data:
        raise RuntimeError("mpy-cross produced empty output for %s" % py_path)
    return base + ".mpy", data


def build_bundle(src, use_mpy=True):
    py_files = [p for p in sorted(glob.glob(os.path.join(src, "*.py")))
                if os.path.basename(p) != "main.py"]  # main.py is the loader; never bundled
    files = []
    if use_mpy:
        ok, ver = _check_mpy_cross()
        if not ok:
            raise SystemExit(
                "mpy-cross not found. Install it (`pip install mpy-cross==1.27.0.post2`)"
                " or pass --no-mpy to bundle raw .py.")
        print("compiling with %s" % ver)
        tmp = tempfile.mkdtemp(prefix="ota_mpy_")
        try:
            for p in py_files:
                files.append(_compile_mpy(p, tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        for p in py_files:
            with open(p, "rb") as f:
                files.append((os.path.basename(p), f.read()))
    return ota.build_bundle(files), [n for n, _ in files]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build an OTA bundle + manifest.")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--out", required=True, help="bundle artifact output path")
    ap.add_argument("--version", help="override (else read from src/config.py)")
    ap.add_argument("--url", help="bundle URL to record in the manifest")
    ap.add_argument("--manifest", help="write a manifest JSON to this path")
    ap.add_argument("--notes", default="", help="release notes for the manifest")
    ap.add_argument("--release-url", help="release page URL (shown by the HA update card)")
    ap.add_argument("--no-mpy", dest="mpy", action="store_false",
                    help="bundle raw .py instead of precompiled .mpy")
    ap.set_defaults(mpy=True)
    args = ap.parse_args(argv)

    bundle, names = build_bundle(args.src, use_mpy=args.mpy)
    version = args.version or read_version(args.src)
    sha = hashlib.sha256(bundle).hexdigest()
    with open(args.out, "wb") as f:
        f.write(bundle)
    print("bundle: %s  (%d files, %d bytes, version %s)"
          % (args.out, len(names), len(bundle), version))

    if args.manifest:
        manifest = {"version": version, "size": len(bundle), "sha256": sha,
                    "url": args.url or os.path.basename(args.out),
                    "release_notes": args.notes}
        if args.release_url:
            manifest["release_url"] = args.release_url
        with open(args.manifest, "w") as f:
            json.dump(manifest, f, indent=2)
        print("manifest: %s -> %s" % (args.manifest, json.dumps(manifest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
