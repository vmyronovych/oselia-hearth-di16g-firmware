# Shared config + helpers for the on-hardware test scripts. `source` this.
#
# All scripts auto-detect the board and use MQTT topic wildcards, so nothing is
# hardcoded to one board. Override anything via env:
#   PORT=/dev/cu.usbmodemXXXX  BROKER=192.168.1.104  BROKER_PORT=1883
#   MOSQ_CONTAINER=mosquitto   SETTLE=6
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${BROKER:=192.168.1.104}"      # MQTT broker (this Mac, per the test rig)
: "${BROKER_PORT:=1883}"
: "${MOSQ_CONTAINER:=mosquitto}"  # Docker container running the broker

# Bounded run -- macOS has no `timeout(1)`.  Usage: _t SECONDS cmd args...
# Hitting the time limit is the *expected* end of a bounded watch, so the SIGALRM
# kill must not look like a failure (callers judge by captured output, and this
# keeps `set -e` from aborting on a normal timeout).
_t() { local s="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$s" "$@" || true; }

# Sleep without tripping the agent's foreground-sleep guard.  Usage: _settle [secs]
_settle() { perl -e 'select(undef,undef,undef,'"${1:-5}"')'; }

# First connected MicroPython board, or empty.  Honors $PORT if set.
detect_port() {
  if [ -n "${PORT:-}" ]; then echo "$PORT"; return; fi
  mpremote connect list 2>/dev/null | awk '/MicroPython/{print $1; exit}' || true
}

# Process holding the port (USB drops if VS Code MicroPico / Thonny has it), or empty.
port_holder() {
  local p="$1"; lsof "${p/cu./tty.}" 2>/dev/null | awk 'NR>1{print $1" (pid "$2")"; exit}' || true
}
