# CLAUDE.md — Working agreement for this firmware

You are implementing MicroPython firmware for a **Waveshare RP2040-ETH** board that
turns 16 isolated 24 V wall-switch inputs into Home-Assistant MQTT events.

**Read `docs/spec.md` first** (the contract), then **`docs/hardware.md`** (hardware facts
confirmed on real hardware by a working proof-of-concept). This file is *how* to
work. When `docs/spec.md` and `docs/hardware.md` agree, that detail is settled — don't re-litigate it.

## Ground truth you must not get wrong

- The board's network chip is a **CH9120 UART-to-Ethernet bridge**, **NOT a W5500**.
  There is **no socket API on the RP2040**. The CH9120 holds the TCP/IP stack; you
  configure it as a **TCP client** to the broker, then the **UART byte stream is the
  raw MQTT/TCP payload**. Never `import usocket` to reach the broker.
- Target = MicroPython on RP2040 (Thonny / `mpremote` / drag-drop UF2). This board
  runs **MicroPython 1.28.0** (RPI_PICO build) — see `docs/flashing.md` for the pinned UF2
  version and how to reflash.
- The broker must be addressed by **numeric IP** (CH9120 does no DNS).

## Hardware pins (manufactured `dib-monolith` board)

**Canonical pin map, powering rules, and device-init facts: [`docs/hardware.md`](docs/hardware.md).**
All pins live in `config.py` — **never hard-code**. The essentials you must not get wrong:

- **I2C1** `sda=GP26 / scl=GP27` (RP2040 native pair) — MCP23017 shared bus; INT **GP22**
  (wired-OR, `IOCON=0x44` = MIRROR+ODR), `/RESET` **GP9** (active-low).
- **CH9120** `UART(1, tx=GP20, rx=GP21)` (internal to the module); CFG0 GP18, RST GP19;
  TCPCS GP17 **disabled** (`=None` — liveness is MQTT keepalive).
- **WS2812 LED** GP25, **RGB** order, driven via PIO (build lacks `neopixel`/`bitstream`).
- Active-low inputs (0 = pressed). Board = position in the resolved `MCP_ADDRESSES`
  (0x20..0x27), 1-based; global index `(board-1)*16 + pin`, up to 128 inputs.

> The breadboard POC used different pins (I2C0 GP0/GP1, INT GP2, no RESET). The PCB
> re-routed them — see `docs/hardware.md`; **don't "restore" POC pin values.**

## Concurrency (dual-core) — see docs/spec.md §3a

- Core 0 = `input_task` (main thread): MCP IRQ + debounce + detect → queue. Does NOT own
  the WDT (an MCP/I²C stall must never reboot); bounded I²C so a dead bus can't wedge it.
  Also live-applies tunable timings when core 1 bumps `SharedState.tune_version`.
- Core 1 = `net_task` (spawned thread): CH9120 + MQTT + discovery + LED; drains queue;
  also diagnostics telemetry, the log mirror, **two-way control** (subscribes to
  `…/cmd/#`, handles Restart/Identify/live-tune **after** draining the queue so a
  command never delays an action publish), and **owns the watchdog** (fed from `_beat`).
- Cross-core only via `EventQueue` (gestures) + `SharedState` (health + heartbeat +
  live-tunable timings with a version counter).
- GIL is real: don't expect parallel compute. Both loops must `sleep_ms`-yield.
- Never block core 0. Network blocking lives on core 1; its waits are chunked so the
  watchdog (fed from core 1's `_beat`) keeps ticking — if the *network* core wedges,
  the board resets by design. A core-0/MCP stall does NOT reset (WDT isn't on core 0).
- Feed the detector/debounce a value from `clock.Monotonic` (wrap-safe), not raw
  `ticks_ms`.

## Implementation status

Complete and host-tested; the full feature set is also **HA-verified on hardware**
(local HA 2025.11). Beyond the core (detector, debounce, queue, clock, mqtt_packets,
CH9120 driver, MCP driver, MQTT client, both task loops, `main`), the firmware now
includes a **Home Assistant integration layer** (see docs/spec.md §5.1–5.4):

- `diag.py` — diagnostics telemetry (`…/diag/state`, retained) + a "Last log" mirror.
  Queue-gated so it never delays an action. The OSELIA integration renders the HA
  diagnostic entities (uptime, free heap, RP2040 die temp, board addresses, reconnects,
  dropped, Ethernet, last input) from these topics.
- **Input publishing** — each press to `…/board<b>/input<p>/action`; the integration
  declares one `event` entity per input.
- **Two-way control** — `mqtt_client` SUBSCRIBEs; `…/cmd/#` → Restart/Identify and
  live-tune timings + log level, **persisted to `site.json`** and applied cross-core.
- **No firmware-published HA discovery** — the legacy `HA_INTEGRATION="mqtt"` path and
  all `publish_*_discovery` builders were removed; the OSELIA integration owns every entity.
- **CH9120 IP read-back** (`0x61`) so DHCP units self-report their leased IP.
- The `oselia` host tool flashes/provisions the unit. It does **not** push HA assets: the
  OSELIA integration is installed via HACS and the `/oselia-hearth` dashboard is rendered
  with `oselia dashboard render` for manual upload (`provisioning/oselia_provision/dashboard.py`).

Keep the POC's proven MQTT wire format in `mqtt_packets` — it's the known-good
reference. Any command/diagnostic publish must stay **behind the gesture-queue drain**
(latency guarantee); the firmware publishes only data/command topics, never HA discovery.

## Coding conventions

- MicroPython idioms: `machine.Pin`, `machine.I2C`, `machine.UART`, `utime`.
- **No work in ISRs.** The MCP INT handler sets a flag/`micropython.schedule`
  only. All I²C reads and MQTT publishes happen in the main loop.
- **No allocation in the ISR.** Pre-allocate buffers; use `viper`/`bytes` carefully.
- Keep `press_detector.py` and `debounce.py` **pure** (no `machine`/`network`
  imports) so they run under CPython for unit tests. Inject the clock as a
  parameter or a callable.
- One responsibility per module per `docs/spec.md §8`. Don't merge networking into main.
- Fail safe: if the broker is unreachable, keep retrying with backoff; never crash
  the loop. Log to USB serial.

## Build / run / verify loop

1. **Syntax-check** after every edit:
   `python3 -m py_compile src/*.py` (validates syntax without importing `machine`).
2. **Unit-test** on the host (all must pass):
   `for t in tests/test_*.py; do python3 "$t"; done`.
3. **Deploy** to the board with the `oselia` CLI (slot-aware — the loader boots
   `/slots/a/app.py`, so raw `fs cp` to the board root is a no-op): `oselia provision
   --broker <ip>`, which writes `site.json`, deploys into `/slots/a`, and resets.
4. **On-device checks** per `docs/spec.md §9` via the `hw-test` skill: link/keepalive
   healthy, `oselia mqtt watch` sees action/status/diag topics, HA shows the device &
   triggers, LED states. Every acceptance criterion is proven on **both** USB log + MQTT.

Do not mark a task done if py_compile fails, host tests fail, or an implementation
is partial (`docs/spec.md §10`).

## Hardware acceptance — hard requirements (non-negotiable)

On-hardware acceptance is run via the `hw-test` skill, which **enforces** these. They are
the contract; do not work around them:

1. **`oselia` CLI only** — never `mpremote`/`mosquitto_*`/`tools/*.sh` for board or broker.
2. **Missing `oselia` capability → STOP and flag** — add it to the CLI, never a one-off tool.
3. **No USB logs → STOP** — without real logs you are guessing, and guessing is not acceptance.
4. **Logs must *prove* new functionality** — if they can't, propose a firmware debug-log addition.
5. **Dual proof** — a criterion PASSes only when confirmed on **both** the USB log **and** the
   MQTT wire, and the two agree.

Full rules + verdict taxonomy + the §10 evidence matrix live in
`.claude/skills/hw-test/SKILL.md` and `.claude/skills/hw-test/acceptance-matrix.md`.

## Definition of done

Everything in **docs/spec.md §10 Acceptance criteria** holds. When something can only be
confirmed on hardware (`docs/spec.md §11`), implement to the documented assumption,
leave a clearly-marked `# HW-VERIFY:` comment, and note it for the user.

## Remaining work (the code is otherwise complete)

1. `cp config.example.py config.py`; set broker IP, network, input names.
2. `oselia provision --broker <ip>` — flashes MicroPython v1.28.0 if needed (see
   `docs/flashing.md`), writes `site.json`, and deploys the firmware into `/slots/a`.
3. On-hardware bring-up: confirm CH9120 connects, watch the LED state machine,
   verify `oselia mqtt watch` sees the per-gesture action topics (and **no** firmware
   HA discovery).
4. Confirm HA renders the 48 device-automation triggers; wire a test automation.
5. Tune `DEBOUNCE_MS` / `LONG_MS` / `DOUBLE_GAP_MS` to feel; validate the watchdog
   by forcing a stall; validate reconnect by bouncing the broker.
