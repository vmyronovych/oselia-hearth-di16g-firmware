# OSELIA Hearth — Firmware (RP2040-ETH)

MicroPython firmware for a Waveshare **RP2040-ETH** board that reads 16 isolated
24 V wall-switch inputs via an **MCP23017**, classifies each press as
**single / double / long**, and publishes them to Home Assistant over MQTT. The
first-party **OSELIA** HA integration declares all the entities (inputs, diagnostics,
controls); the firmware publishes only the data + command topics, **not** HA
MQTT-discovery configs. It also publishes **device diagnostics** (uptime, free heap,
die temperature, link/board health, last log) and accepts **two-way control** (Restart
/ Identify buttons, live-tunable gesture timings and log level). See `docs/spec.md §5`.

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
- `docs/upgrading.md` — end-user upgrade guide (bilingual; linked from every release).
- `.claude/skills/hw-test/` — project skill: on-hardware **acceptance** + bring-up/debug,
  driven entirely through the `oselia` CLI. Includes `acceptance-matrix.md` (see below).
- `.claude/agents/hw-runner.md` — cheap (Sonnet) runner subagent for the green path (see below).

## Quick start (development)

```bash
# 1. configure
cp config.example.py config.py        # then edit broker IP, pins, timings

# 2. validate on the host (no hardware needed)
python3 -m py_compile src/*.py        # syntax check
python3 tests/test_press_detector.py  # logic tests

# 3. provision the board (flashes MicroPython if needed, writes site.json, deploys the
#    firmware into the OTA slot layout, confirms it reports online) — see the oselia CLI
oselia provision --broker 192.168.1.104
```

> Deploy is **slot-aware**: the loader boots `/slots/a/app.py`, so copying files to the
> board root is a no-op. Always go through `oselia provision` (never raw `mpremote` copies).

## On-hardware bring-up, testing & debugging

Everything talks to the board and broker through the **`oselia` CLI** (see the
`oselia-provision` skill). Three cooperating layers let you (or Claude) work at whatever
altitude fits the task — plus the things only a human can do:

| Layer | What it is | Reach for it when |
|---|---|---|
| `oselia` CLI | the one tool: flash/provision, `monitor` (USB log), `mqtt watch/pub/cmd/bounce` | you know exactly what to run; CI; no Claude needed |
| `hw-runner` agent (Sonnet) | runs `oselia`, reports, **escalates on failure** — no edit tools | offloading the cheap green-path grind from Opus |
| `hw-test` skill (session model) | provisions a baseline, drives the §10 acceptance suite to a verdict, diagnose→fix→verify, coordinates presses | "prove this change on hardware" / debugging a fault |
| **you** | physical 24 V switch presses, HA UI checks, final calls | the steps automation can't perform |

The `oselia` CLI encodes the macOS/USB quirks once (the firmware watchdog resets the board
on a REPL break-in; the USB CDC re-enumerates after reset; the loader boots `/slots/a`). Run
it by hand from the repo root:

```bash
oselia provision --broker 192.168.1.104        # flash if needed, write site.json, deploy /slots/a
oselia monitor --passive                       # stream the firmware's USB log (listen only)
oselia mqtt watch hearth/<id>/status --for 40  # availability online/offline (LWT)
oselia mqtt watch 'homeassistant/#' --for 6 --expect-absent '.'   # assert NO firmware HA discovery
oselia mqtt watch hearth/<id>/board1/input1/action --for 45       # press switches; single/double/long
oselia mqtt watch hearth/<id>/diag/# --for 15  # diagnostics telemetry (diag/state)
oselia mqtt bounce --down 8                     # broker-outage self-heal check
```

See the `hw-test` skill's `acceptance-matrix.md` for the full §10 criterion→proof table, and
`docs/bringup.md` for the physical/HA bring-up checklist.

### The `hw-test` skill

`.claude/skills/hw-test/SKILL.md` is a Claude Code **project skill** — it ships
with the repo (committed, not personal), so it's available to anyone who opens the
project in Claude Code. It's the judgment layer over `oselia`: it provisions a known
baseline, drives the §10 acceptance suite (`acceptance-matrix.md`) to a per-criterion
verdict — each proven on **both** the USB log and the MQTT wire — interprets the output
(e.g. *two `single`s instead of a `double` → raise `DOUBLE_GAP_MS`*), runs the diagnose →
fix → re-provision → re-verify loop, and prompts you when a step needs a physical press.

Use it by **describing the task in plain language** and Claude pulls it in — e.g.
*"prove this change on hardware"*, *"run the broker-bounce reconnect check"*, *"watch the
action topics while I press switches"* — or invoke it explicitly with `/hw-test`. A newly
added project skill registers at session start, so reload/restart Claude Code if you just
pulled it. Note: gesture tests still require you to physically actuate the 24 V inputs.

### Delegating the grind (cost routing)

`.claude/agents/hw-runner.md` is a **subagent pinned to a cheaper model (Sonnet)** for the
mechanical green-path work — provisioning, the `online`/no-discovery checks, the bounce
reconnect check, USB-log capture. It runs `oselia` and **reports only**: no edit tools, and
it escalates to the orchestrating model on any failure, which keeps diagnosis and fixes on
the higher tier. Drive it via the main session, e.g. *"have the hw-runner provision and run
the bounce check."* Reserve Opus for planning new functionality and debugging failures.
(Cheaper models cut cost, but the hardware soaks — provision settle, gesture windows, the
bounce watch — are fixed wall-clock either way.)

### Putting it together — a typical change→ship loop

1. **Edit** firmware in `src/` (you / Opus).
2. **Host gate** (never deploy red): `python3 -m py_compile src/*.py` and
   `for t in tests/test_*.py; do python3 "$t"; done`.
3. **Delegate the on-hardware run to the cheap tier** — in your session say
   *"have the hw-runner provision and run the bounce check."* The Sonnet runner executes
   `oselia provision` → `oselia mqtt watch …/status` → `oselia mqtt bounce` and returns a
   compact `RAN / RESULT / EVIDENCE / ESCALATE` report.
4. **Branch on the result:** `PASS` → done. `FAIL` or `NEEDS-HUMAN` → the runner escalates;
   the `hw-test` skill (on your session model, e.g. Opus) diagnoses, fixes in `src/`,
   re-provisions, and re-verifies — prompting you to press switches when needed.
5. **Commit** — the skill and the agent ship in-repo, so the next person (or machine) gets
   the same flow.

For a fully manual run, skip Claude entirely and use the `oselia` commands above; for an
interactive bring-up of a fresh board, follow `docs/bringup.md`.

> **First-use note:** the `hw-test` skill and the `hw-runner` agent are discovered
> at **session start**. After first cloning the repo (or right after adding them),
> reload/restart Claude Code before plain-language skill triggers and
> `hw-runner` delegation become available.

## MQTT topics

`<id>` = device id — the last 6 hex of the RP2040 `unique_id` (e.g. `893922`), or
`DEVICE_ID` from `config.py` if set. The topic prefix comes from `config.py`:
`BASE_TOPIC` (default `hearth`). Boards are
`board1`…`boardN`, inputs `input1`…`input16`, gestures `single` / `double` / `long`.

The most common few (full table, payloads, and the `diag/state`
schema are in **[`docs/mqtt-contract.md`](docs/mqtt-contract.md)** — the canonical wire
contract):

| Purpose | Topic | Payload | Retained |
|---|---|---|---|
| Button press (action) | `hearth/<id>/board<B>/input<N>/action` | `single` \| `double` \| `long` | no |
| Availability (LWT) | `hearth/<id>/status` | `online` \| `offline` | yes |
| Command (HA→dev) | `hearth/<id>/cmd/<name>` | e.g. `PRESS`, a number, a level | no |
| Tunable state | `hearth/<id>/cfg` | JSON of the live timings + log level | yes |

Example: `hearth/893922/board1/input2/action` → `single`. The firmware publishes **no**
`homeassistant/.../config` discovery — the OSELIA integration declares the entities.

To inspect on the broker:
- `oselia mqtt watch 'hearth/<id>/#' --for 20` — availability, presses (live, not retained),
  `diag/*`, `cfg`.
- `oselia mqtt watch 'hearth/<id>/diag/#' --for 15` — the diagnostics topics specifically.

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
**verified on hardware against a local HA 2025.11** (`docs/spec.md §5.1–5.4`). Hardware
facts are confirmed from a working POC (`docs/hardware.md`). Host tests cover the detector,
debounce, status LED, event queue, monotonic clock, MQTT packet framing, and the
diagnostics/discovery builders.
