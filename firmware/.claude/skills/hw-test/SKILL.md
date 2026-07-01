---
name: hw-test
description: >-
  Flash, test, and debug the RP2040-ETH switch firmware on the real board over
  mpremote + a local MQTT broker. Use when asked to flash/deploy to the board,
  bring it up on hardware, watch MQTT action/availability/diag topics, test
  gestures, run the broker-bounce reconnect regression, or capture board serial
  logs. Orchestrates the deterministic helpers in tools/.
---

# hw-test — on-hardware bring-up & debug

The mechanics live in `tools/*.sh` (they encode the macOS/USB quirks). This skill
is the judgment layer: pick the right script, interpret output, run the
diagnose→fix→re-verify loop, and choreograph the human press-test. **Always run
the host gate first** (`python3 -m py_compile src/*.py` and `tests/test_*.py`)
before touching the board — never deploy code that fails it (CLAUDE.md).

## Test rig (see tools/_common.sh defaults; all env-overridable)
- Board auto-detected via `mpremote connect list` (first MicroPython device).
- Broker = this Mac, `192.168.1.104:1883`, no auth, Docker container `mosquitto`.
- A local **Home Assistant** runs in Docker (`homeassistant`, `http://localhost:8123`,
  2025.11). A long-lived token is at `~/.config/oselia/ha_token` (outside the repo).
- `MCP_AUTODISCOVER=True` and only board1 `@0x20` is wired, so the firmware serves
  **one** board: `…/board1/input1..16/action` topics (plus `diag/state`, `cfg`, and the
  `…/cmd/#` command subscribe). With more boards wired it scales per board.
- **The firmware publishes no HA MQTT discovery** — `watch.sh discovery` shows nothing and
  no device auto-appears under HA's MQTT integration. The **OSELIA HACS integration** owns
  the entities (installed in HA separately). Data/command topics (`…/action`, `…/cmd/#`,
  `diag/state`) are what to watch; there are no discovery configs to inspect.
- **Gestures need a physical 24 V switch press — you cannot actuate them.** Ask the
  user to press, and watch. (For wiring-independent checks you can publish to the
  action topic with `mosquitto_pub` to drive the HA `event` entity / a blueprint.)

## Workflows

**Deploy / bring-up**
1. `tools/deploy.sh` — flashes `src/*.py`, verifies sizes, resets, settles.
   - "Port busy" → tell the user to disconnect VS Code MicroPico / Thonny.
   - Boot now takes a few seconds longer when DHCP is on (the firmware reads its
     leased IP back from the CH9120 once at boot — `DHCP_LEASE_SETTLE_MS`).
2. `tools/watch.sh status 10` — expect `online` (an `offline` first is the retained
   LWT from the prior run; fine). After a fresh flash give it ~20–35 s: the boot is
   longer and `diag/state`/`cfg` are retained, so an early read shows a **stale**
   snapshot — wait for a low `uptime_s` before trusting it.
3. `tools/watch.sh discovery 4 | grep -c config` — expect **0**: the firmware publishes
   no `homeassistant/.../config` discovery (the OSELIA integration declares the entities).

**Diagnostics / control / HA integration**
- `tools/watch.sh diag 15` — the retained `diag/state` JSON.
- HA registry check (no flashing): the device + entities live in HA — read them with
  `curl -H "Authorization: Bearer $(cat ~/.config/oselia/ha_token)" http://localhost:8123/api/states`
  and filter `*.hearth*` / `event.*`.
- Control round-trips (drive via HA REST `button.press` / `number.set_value` /
  `select.select_option`, or `mosquitto_pub` to `…/cmd/<name>`): Restart → board goes
  `offline→online` with `uptime_s` reset; a `number` change updates `…/cfg` and
  **survives a reboot** (clear the retained `cfg` first, then reboot, to prove it came
  from `site.json`). Re-tuning never needs a reflash.
- HA integration + dashboard: the OSELIA custom integration is installed in HA via HACS and
  configured there (it owns the entities; the firmware publishes no MQTT discovery). The
  dashboard is rendered locally with `oselia dashboard render --id <id>`
  and pasted into HA — the host tool no longer pushes HA assets.

**Gesture test (needs the user)**
- `tools/watch.sh actions 45`, and ask the user to press input N: one short tap
  (→`single`), two quick taps <`DOUBLE_GAP_MS` apart (→`double`), one >`LONG_MS`
  hold (→`long`). Two `single`s instead of a `double` ⇒ raise `DOUBLE_GAP_MS` in
  `config.py` (note the latency tradeoff), redeploy, re-test.

**Broker-bounce reconnect regression**
- `tools/bounce-test.sh` — exit 0 means the board self-healed `offline→online`.
  A FAIL with `offline_seen=0` means it never noticed in time (raise `WATCH=`); a
  FAIL ending `offline` means it's stuck — check `net_task.py` re-runs
  `ch9120.bring_up()` on TCPCS-down (the known failure mode) and read serial.

**Serial / deep debug**
- `tools/serial.sh 25` captures a fresh boot's logs. Note it leaves the board
  **stopped** (it ran main() over the REPL) — finish with `tools/deploy.sh` or
  `mpremote reset` to resume autorun. To read TCPCS directly:
  `mpremote connect <port> exec "from machine import Pin; print(Pin(17,Pin.IN).value())"`
  (0 = TCP connected, 1 = disconnected).

## Debug loop
On a hardware fault: reproduce with the relevant script → capture serial →
localize in `src/` → fix → `py_compile` + host tests → `deploy.sh` → re-run the
script to prove it. Mark hardware-confirmed assumptions with `# HW-VERIFY:` and
keep the proven MQTT wire format in `mqtt_packets` intact.
