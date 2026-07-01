# OSELIA Hearth — Firmware (RP2040-ETH)

MicroPython firmware for a Waveshare **RP2040-ETH** board that reads 16 isolated
24 V wall-switch inputs via an **MCP23017**, classifies each press as
**single / double / long**, and surfaces them in Home Assistant over MQTT Discovery —
as `event` entities and/or `device_automation` triggers. It also publishes **device
diagnostics** (uptime, free heap, die temperature, link/board health, last log) and
accepts **two-way control** (Restart / Identify buttons, live-tunable gesture timings
and log level). See `docs/spec.md §5`.

> **Networking note:** the RP2040-ETH uses a **CH9120 UART-to-Ethernet bridge**
> (not a W5500). The RP2040 has no socket API — the CH9120 holds the TCP/IP stack.
> See `docs/spec.md` §4. Use a **numeric broker IP** (no DNS).

## Layout

- `docs/spec.md` — the full specification / contract (read this first).
- `CLAUDE.md` — working agreement for implementing the firmware.
- `config.example.py` — copy to `config.py` and edit pins / broker / timings / names.
- `src/` — firmware modules (see `docs/spec.md` §8).
- `../provisioning/` — the host-side **`oselia`** tool (USB): flashes MicroPython,
  provisions a fresh unit, the board toolbox, and the dashboard YAML renderer. See its
  `README.md` and `PROVISIONING_SPEC.md`.
- `tests/` — host-runnable unit tests (CPython): detector, debounce, LED, queue,
  clock, MQTT packets/discovery, and diagnostics builders.
- `../homeassistant/` — HA assets: the OSELIA integration design contract and the
  `/oselia-hearth` dashboard example (render your own with `oselia dashboard render`).
- `docs/hardware.md` — pin map (annotated RP2040-ETH pinout + reference image), powering
  rules, and the POC-confirmed CH9120 / MCP23017 / press-detection facts.
- `docs/mqtt-contract.md` — the canonical wire contract (topics, discovery, `diag/state`).
- `docs/ota.md` — OTA mechanism (A/B slots, boot-confirm, auto-revert).
- `docs/flashing.md` — which MicroPython UF2 to flash on a new RP2040-ETH, and how.
- `docs/bringup.md` — bench bring-up checklist (the physical/HA steps scripts can't do).
- `docs/releasing.md` — cut a firmware release (GitHub → HA OTA feed).
- `UPGRADING.md` — end-user upgrade guide (bilingual; linked from every release).
- `tools/` — on-hardware flash/test/debug scripts (`tools/README.md`).
- `.claude/skills/hw-test/` — project skill that orchestrates those scripts (see below).
- `.claude/agents/hw-runner.md` — cheap (Sonnet) runner subagent for the green path (see below).

## Quick start (development)

```bash
# 1. configure
cp config.example.py config.py        # then edit broker IP, pins, timings

# 2. validate on the host (no hardware needed)
python3 -m py_compile src/*.py        # syntax check
python3 tests/test_press_detector.py  # logic tests

# 3. deploy to the board (MicroPython already flashed — see docs/flashing.md) — copy to root
mpremote connect /dev/ttyACM0 fs cp config.py :
mpremote connect /dev/ttyACM0 fs cp src/*.py :
mpremote connect /dev/ttyACM0 reset      # runs main.py
```

## On-hardware bring-up, testing & debugging

Three cooperating layers let you (or Claude) work at whatever altitude fits the
task — plus the things only a human can do:

| Layer | What it is | Reach for it when |
|---|---|---|
| `tools/*.sh` | deterministic scripts: flash, watch, bounce regression, serial | you know exactly what to run; CI; no Claude needed |
| `hw-runner` agent (Sonnet) | runs the scripts, reports, **escalates on failure** — no edit tools | offloading the cheap green-path grind from Opus |
| `hw-test` skill (session model) | picks the script, reads the output, drives diagnose→fix→verify, coordinates presses | "just make it work" / debugging a fault |
| **you** | physical 24 V switch presses, HA UI checks, final calls | the steps automation can't perform |

**The scripts** encode the macOS/USB quirks once (macOS has no `timeout(1)`; the USB
CDC re-enumerates after `reset`; `mpremote` copies can silently fail; MQTT topic
wildcards avoid hardcoding the device id). Run them by hand from the repo root:

```bash
tools/deploy.sh                 # flash src/*.py, verify every size, reset, settle
tools/watch.sh status 10        # availability online/offline (LWT)
tools/watch.sh discovery 4      # retained HA discovery configs
tools/watch.sh actions 45       # press switches; see single / double / long
tools/watch.sh diag 15          # diagnostics telemetry (diag/state + diag entities)
tools/bounce-test.sh            # broker-outage self-heal regression (exit 0 = pass)
tools/serial.sh 25              # capture a fresh boot's serial logs
```

Defaults (auto-detected board port, broker, container) live in `tools/_common.sh`
and are env-overridable: `PORT= BROKER= BROKER_PORT= MOSQ_CONTAINER= WATCH=`. See
`tools/README.md` for details and `docs/bringup.md` for the full checklist.

### The `hw-test` skill

`.claude/skills/hw-test/SKILL.md` is a Claude Code **project skill** — it ships
with the repo (committed, not personal), so it's available to anyone who opens the
project in Claude Code. It's the judgment layer over the scripts above: it picks
the right one, interprets the output (e.g. *two `single`s instead of a `double` →
raise `DOUBLE_GAP_MS`*), runs the diagnose → fix → redeploy → re-verify loop, and
prompts you when a step needs a physical switch press.

Use it by **describing the task in plain language** and Claude pulls it in — e.g.
*"flash the firmware and confirm it's online"*, *"run the broker-bounce
regression"*, *"watch the action topics while I press switches"* — or invoke it
explicitly with `/hw-test`. A newly added project skill registers at session
start, so reload/restart Claude Code if you just pulled it. Note: gesture tests
still require you to physically actuate the 24 V inputs — automation can't.

### Delegating the grind (cost routing)

`.claude/agents/hw-runner.md` is a **subagent pinned to a cheaper model (Sonnet)**
for the mechanical green-path work — flashing, status/discovery checks, the bounce
regression, serial capture. It runs the `tools/` scripts and **reports only**: it
has no edit tools and escalates to the orchestrating model on any failure, which
keeps diagnosis and fixes on the higher tier. Drive it via the main session, e.g.
*"have the hw-runner flash and run the bounce regression."* Reserve Opus for
planning new functionality and debugging failures. (Note: cheaper models cut cost,
but the hardware soaks — reset settle, gesture windows, the 60 s bounce watch — are
fixed wall-clock either way.)

### Putting it together — a typical change→ship loop

1. **Edit** firmware in `src/` (you / Opus).
2. **Host gate** (never deploy red): `python3 -m py_compile src/*.py` and
   `for t in tests/test_*.py; do python3 "$t"; done`.
3. **Delegate the on-hardware run to the cheap tier** — in your session say
   *"have the hw-runner flash and run the bounce regression."* The Sonnet runner
   executes `deploy.sh` → `watch.sh status` → `bounce-test.sh` and returns a compact
   `RAN / RESULT / EVIDENCE / ESCALATE` report.
4. **Branch on the result:** `PASS` → done. `FAIL` or `NEEDS-HUMAN` → the runner
   escalates; the `hw-test` skill (on your session model, e.g. Opus) diagnoses,
   fixes in `src/`, redeploys, and re-verifies — prompting you to press switches
   when a gesture test needs it.
5. **Commit** — `tools/`, the skill, and the agent all ship in-repo, so the next
   person (or machine) gets the same flow.

For a fully manual run, skip Claude entirely and use the `tools/*.sh` commands
above; for an interactive bring-up of a fresh board, follow `docs/bringup.md`.

> **First-use note:** the `hw-test` skill and the `hw-runner` agent are discovered
> at **session start**. After first cloning the repo (or right after adding them),
> reload/restart Claude Code before plain-language skill triggers and
> `hw-runner` delegation become available.

## MQTT topics

`<id>` = device id — the last 6 hex of the RP2040 `unique_id` (e.g. `893922`), or
`DEVICE_ID` from `config.py` if set. Prefixes come from `config.py`: `BASE_TOPIC`
(default `hearth`) and `DISCOVERY_PREFIX` (default `homeassistant`). Boards are
`board1`…`boardN`, inputs `input1`…`input16`, gestures `single` / `double` / `long`.

The most common few (full table, payloads, discovery configs, and the `diag/state`
schema are in **[`docs/mqtt-contract.md`](docs/mqtt-contract.md)** — the canonical wire
contract):

| Purpose | Topic | Payload | Retained |
|---|---|---|---|
| Button press (action) | `hearth/<id>/board<B>/input<N>/action` | `single` \| `double` \| `long` | no |
| Availability (LWT) | `hearth/<id>/status` | `online` \| `offline` | yes |
| HA discovery | `homeassistant/<component>/<id>/…/config` | discovery JSON | yes |

Examples: `hearth/893922/board1/input2/action` → `single`;
`homeassistant/event/893922/b1_in2/config`.

To inspect on the broker (MQTTX or `mosquitto_sub -h <broker> -v`):
- `hearth/<id>/#` — availability, presses (live, not retained), `diag/*`, `cfg`.
- `homeassistant/#` — all retained discovery configs for every component above.
- Or use `tools/watch.sh diag` for the diagnostics topics specifically.

## Status LED

The board has **one** RGB LED (WS2812). It encodes health as **colour + blink
rate**, showing the single highest-priority issue (root-cause first) — quick reference
here; canonical logic in [`docs/spec.md §7a`](docs/spec.md):

| LED | Pattern | Meaning | Priority |
|---|---|---|---|
| 🔵 Blue | solid | Booting / initialising | startup |
| 🟠 Orange | medium blink (~0.6 s) | MQTT broker / link down | 1 (root cause) |
| 🟡 Yellow | fast blink (~0.3 s) | An MCP23017 not responding | 2 |
| 🟢 Green | **solid** | All healthy | — |
| ⚪ White | brief flash (~90 ms) | A gesture was just published (activity) | overrides |

How to read it:
- **Only the top active fault shows.** Red outranks orange outranks yellow — fix the
  shown one and the next may appear. Blink *speed* also tells them apart (slow→fast =
  ethernet→mqtt→mcp).
- **Solid green = everything healthy.** Each button press briefly flashes **white**,
  then returns to green.
- **Stuck on yellow and never reaching green?** Every chip listed in `MCP_ADDRESSES`
  is treated as required — a declared-but-unwired MCP keeps the MCP fault on. List
  only the boards you've actually wired (see `docs/bringup.md` §8).
- **Colours look swapped?** `config.py` sets `PIN_STATUS_LED=25`,
  `STATUS_LED_BRIGHTNESS=0.2`, `STATUS_LED_ORDER="RGB"` (HW-confirmed on this board —
  a standard GRB driver renders green as red here). If red/green appear swapped on a
  different unit, flip `STATUS_LED_ORDER` back to `"GRB"`. The WS2812 is driven over
  the RP2040 PIO (this board's MicroPython build has no `neopixel` module).

## Architecture

Dual-core (see `docs/spec.md` §3a). **Core 0** runs the real-time input task
(MCP23017 IRQ → debounce → single/double/long detection → event queue), owns the
watchdog, and live-applies tunable timings. **Core 1** runs networking (CH9120 link,
MQTT with LWT/keepalive, HA discovery, status LED), drains the queue, and handles
diagnostics, the log mirror, and inbound commands — all **after** the queue drain so
control/telemetry never delay a press. They share only a thread-safe queue and a
lock-guarded health/heartbeat/tunables struct. Industrial robustness throughout:
watchdog with cross-core heartbeat gating, reconnect with backoff, I²C retries,
bounded queue, wrap-safe timing — see `docs/spec.md` §12.

## Status

**Implementation complete and host-tested**, with the full HA-integration layer
(diagnostics, `event` entities, two-way control, live tuning, provisioning auto-setup)
**verified on hardware against a local HA 2025.11** (`docs/spec.md §5.1a–5.4`). Hardware
facts are confirmed from a working POC (`docs/hardware.md`). Host tests cover the detector,
debounce, status LED, event queue, monotonic clock, MQTT packet framing, and the
diagnostics/discovery builders.
