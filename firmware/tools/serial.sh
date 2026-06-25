#!/usr/bin/env bash
# Capture board serial logs for N seconds by running a fresh main(). This Ctrl-C's
# the autorun program and streams a clean boot's logs (CH9120 + MQTT + gestures).
# Usage: serial.sh [seconds]   (default 20)
source "$(dirname "$0")/_common.sh"

PORT="$(detect_port)"
[ -n "$PORT" ] || { echo "No MicroPython board found."; exit 1; }
secs="${1:-20}"
echo "Capturing ${secs}s of serial from $PORT (fresh main()) ..."
_t "$secs" mpremote connect "$PORT" exec "import main; main.main()" 2>&1 || true
echo "(window closed; board left stopped -- run deploy.sh or 'mpremote reset' to resume autorun)"
