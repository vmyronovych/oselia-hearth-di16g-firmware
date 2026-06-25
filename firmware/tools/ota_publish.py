#!/usr/bin/env python3
"""Build an OTA bundle from a firmware src dir and stream it to a board over MQTT.

Reference / test publisher for the on-device OTA receiver (OTA_SPEC.md): it mirrors
exactly what the OSELIA HA integration's UpdateEntity will do. Builds a bundle with
`ota.build_bundle` (the same code the device verifies against), publishes the command
to `<base>/<id>/ota/cmd`, streams indexed chunks to `<base>/<id>/ota/data`, and tails
`<base>/<id>/ota/state` for progress.

Example:
    python3 tools/ota_publish.py --broker 192.168.1.104 --device 893922
"""
import argparse
import glob
import hashlib
import json
import os
import struct
import sys
import time

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
import ota  # noqa: E402  -- the firmware's own bundle builder


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
    """All src/*.py EXCEPT boot.py (the loader is never part of a bundle)."""
    files = []
    for p in sorted(glob.glob(os.path.join(src, "*.py"))):
        name = os.path.basename(p)
        if name == "boot.py":
            continue
        with open(p, "rb") as f:
            files.append((name, f.read()))
    return ota.build_bundle(files), [n for n, _ in files]


def _client():
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    return mqtt.Client()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stream an OTA bundle to a Hearth board.")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--device", required=True, help="device id, e.g. 893922")
    ap.add_argument("--base", default="hearth")
    ap.add_argument("--src", default=SRC, help="firmware src dir to bundle")
    ap.add_argument("--version", help="override version (else read from config.py)")
    ap.add_argument("--chunk", type=int, default=1024)
    ap.add_argument("--delay", type=float, default=0.1,
                    help="seconds between chunks; must be >= chunk/UART-rate so the "
                         "CH9120 buffer doesn't overflow (~0.1s for 1KB @115200)")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--watch", type=float, default=12.0, help="seconds to tail state")
    args = ap.parse_args(argv)

    bundle, names = build_bundle(args.src)
    version = args.version or read_version(args.src)
    sha = hashlib.sha256(bundle).hexdigest()
    n = (len(bundle) + args.chunk - 1) // args.chunk
    base = "%s/%s/ota" % (args.base, args.device)
    print("bundle: %d files, %d bytes, %d chunks, version %s"
          % (len(names), len(bundle), n, version))

    state = {"stage": None}
    naks = []                               # missing-index lists requested by the board

    def _on_msg(cl, u, m):
        payload = m.payload.decode("utf-8", "replace")
        if m.topic.endswith("/nak"):
            try:
                naks.append(json.loads(payload))
            except ValueError:
                pass
        else:
            print("  state:", payload)
            try:
                state["stage"] = json.loads(payload).get("stage")
            except ValueError:
                pass

    c = _client()
    if args.username:
        c.username_pw_set(args.username, args.password or "")
    c.on_message = _on_msg
    c.connect(args.broker, args.port, 30)
    c.loop_start()
    c.subscribe(base + "/state", qos=0)
    c.subscribe(base + "/nak", qos=0)

    def _send(indices):
        for i in indices:
            data = bundle[i * args.chunk:(i + 1) * args.chunk]
            info = c.publish(base + "/data", struct.pack(">I", i) + data, qos=1)
            info.wait_for_publish(timeout=5)
            time.sleep(args.delay)          # pace to the CH9120 UART drain rate

    cmd = {"version": version, "size": len(bundle), "chunks": n,
           "chunk_size": args.chunk, "sha256": sha}
    # cmd is also QoS0 broker->board, so resend until the device acks with a state.
    print("-> cmd", json.dumps(cmd))
    for attempt in range(8):
        c.publish(base + "/cmd", json.dumps(cmd), qos=1)
        for _ in range(20):
            if state["stage"] is not None:
                break
            time.sleep(0.1)
        if state["stage"] is not None:
            break
        print("   no ack, resending cmd (%d) ..." % (attempt + 1))
    if state["stage"] is None:
        print("device never acked the OTA command -- aborting"); c.loop_stop(); return 2

    print("-> streaming %d chunks at %.0fms/chunk (~%.0fs) ..."
          % (n, args.delay * 1000, n * args.delay))
    _send(range(n))

    # Resend any chunks the board NAKs (QoS0 drops) until it applies or we time out.
    print("-> all sent; handling NAKs / watching state (up to %ss) ..." % args.watch)
    deadline = time.time() + args.watch
    while time.time() < deadline:
        if state["stage"] in ("applying", "idle"):
            print("-> device reached '%s' -- update accepted" % state["stage"])
            break
        if naks:
            miss = naks.pop(0)
            if miss:
                print("   resending %d NAK'd chunks: %s%s"
                      % (len(miss), miss[:8], " ..." if len(miss) > 8 else ""))
                _send(miss)
        else:
            time.sleep(0.3)
    c.loop_stop()
    c.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
