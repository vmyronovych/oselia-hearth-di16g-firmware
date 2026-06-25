#!/usr/bin/env bash
# Flash src/*.py to the board: check the port is free, copy, verify every file's
# size landed (copies can silently fail on a USB drop), reset, then settle through
# the USB re-enumeration. Run from anywhere.
source "$(dirname "$0")/_common.sh"

PORT="$(detect_port)"
[ -n "$PORT" ] || { echo "No MicroPython board found. Is the port held by VS Code/Thonny?"; exit 1; }
holder="$(port_holder "$PORT")"
[ -z "$holder" ] || { echo "Port busy: $holder -- disconnect MicroPico/Thonny first."; exit 1; }
echo "Board: $PORT"

echo "Copying src/*.py ..."
mpremote connect "$PORT" fs cp "$REPO_ROOT"/src/*.py :

echo "Verifying sizes ..."
board_ls="$(mpremote connect "$PORT" fs ls 2>/dev/null)"
fail=0
for f in "$REPO_ROOT"/src/*.py; do
  name="$(basename "$f")"
  local_sz="$(wc -c < "$f" | tr -d ' ')"
  board_sz="$(echo "$board_ls" | awk -v n="$name" '$2==n{print $1}')"
  if [ "$local_sz" != "$board_sz" ]; then
    echo "  MISMATCH $name: local=$local_sz board=${board_sz:-MISSING}"; fail=1
  fi
done
[ "$fail" = 0 ] || { echo "Deploy verification FAILED."; exit 1; }
echo "  all $(ls "$REPO_ROOT"/src/*.py | wc -l | tr -d ' ') files match"

echo "Reset + settle (${SETTLE:-6}s for USB re-enumeration) ..."
mpremote connect "$PORT" reset
_settle "${SETTLE:-6}"
echo "Done -- board rebooting on fresh firmware."
