# tools/ — on-hardware flash / test / debug helpers

Deterministic shell helpers for bring-up and regression testing on a real
RP2040-ETH board, validated on macOS. They encode the fiddly bits once: macOS has
no `timeout(1)` (we use `perl alarm`), the USB CDC re-enumerates after `reset`
(settle before the next command), `mpremote` copies can silently fail (verify
sizes), and MQTT topic wildcards avoid hardcoding the device id.

Prereqs: `mpremote`, `mosquitto_sub`, `docker` (for the broker), board flashed with
MicroPython. See `../BRINGUP.md` for the full human checklist and the test-rig
facts (board port, broker = this Mac at 192.168.1.104, only board1 @0x20 wired).

| Script | What it does |
|---|---|
| `deploy.sh` | Verify port free → copy `src/*.py` → verify every size landed → reset → settle |
| `watch.sh [status\|actions\|discovery\|diag\|all] [secs]` | Tail broker traffic (uses `+` device-id wildcards); `diag` = diagnostics telemetry |
| `serial.sh [secs]` | Capture a fresh boot's serial logs over USB |
| `bounce-test.sh` | **Regression test**: restart broker, assert board self-heals offline→online |

Override defaults via env: `PORT=`, `BROKER=`, `BROKER_PORT=`, `MOSQ_CONTAINER=`,
`SETTLE=`, `WATCH=`.

Typical loop:
```sh
tools/deploy.sh                 # flash
tools/watch.sh status 10        # confirm it comes online
tools/watch.sh actions 45       # press switches; see single/double/long
tools/bounce-test.sh            # prove broker-outage recovery   (exit 0 = pass)
```

Gesture tests need a physical 24 V switch press — automation can't actuate them.
