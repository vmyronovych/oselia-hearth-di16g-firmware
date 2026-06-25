#!/usr/bin/env python3
"""Build an OTA bundle artifact (+ optional release manifest) from a firmware src dir.

The bundle is what the HA integration downloads and streams to the device; the device
verifies its sha256 before applying (firmware/OTA_SPEC.md). Use in a release pipeline:
build the bundle, upload it (e.g. a GitHub Release asset), and publish a manifest JSON
that the integration's release feed (CONF_RELEASE_URL) points at.

Example:
    python3 tools/ota_build.py --out hearth-0.2.0.bundle \
        --url https://example/hearth-0.2.0.bundle --manifest manifest.json
"""
import argparse
import glob
import hashlib
import json
import os
import sys

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


def build_bundle(src):
    files = []
    for p in sorted(glob.glob(os.path.join(src, "*.py"))):
        name = os.path.basename(p)
        if name == "boot.py":               # the loader is never part of a bundle
            continue
        with open(p, "rb") as f:
            files.append((name, f.read()))
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
    args = ap.parse_args(argv)

    bundle, names = build_bundle(args.src)
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
