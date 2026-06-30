# OSELIA Hearth — Specification

## 1. Purpose

Firmware (MicroPython) for a Waveshare **RP2040-ETH** board that reads 16 isolated
24 V digital inputs (wall switches) through an **MCP23017** I²C port expander,
classifies each press as **single / double / long**, and publishes the result to an
MQTT broker in a **Home Assistant**–compatible way using **MQTT Discovery
`device_automation` triggers**.

This document is the contract. The firmware is correct when it satisfies the
Acceptance Criteria in §10.

---

## 2. Hardware overview

```
        HIGH SIDE (24 V)            |   LOW SIDE (3.3 V)
  Wall switch -> RC -> PC817 opto ->|-> MCP23017 input pin (x16 per chip)
                                    |
  Up to 8 MCP23017 chips share one I²C bus:
                                    |
   board1 (main): RP2040 + MCP @0x20 ─┐
   board2 satellite:      MCP @0x21 ─┤  SDA/SCL bus
   board3 satellite:      MCP @0x22 ─┤  + shared wired-OR INT
   board4 satellite:      MCP @0x23 ─┤
   ...                              ─┤
   board8 satellite:      MCP @0x27 ─┘
                                    |
   RP2040 ── I²C ──> all MCPs ;  one INT GPIO <── wired-OR of all INTs
   RP2040 ── UART ─> CH9120 ──> Ethernet ──> MQTT broker
```

- **MCU**: RP2040 (dual Cortex-M0+, 264 KB SRAM, 4 MB flash). One master.
- **Network**: **CH9120** UART-to-Ethernet bridge with onboard TCP/IP stack
  (this board has **no W5500** — see §4).
- **Inputs**: up to **8 × 16 = 128** 24 V digital inputs, hardware-debounced (RC) and
  galvanically isolated via PC817 optocouplers, into MCP23017 GPA0–7 / GPB0–7.
- **Expanders**: 1–8 × MCP23017 on a **shared I²C bus**, each at a distinct address
  (0x20..0x27 via A0–A2 straps). **Board number = position in `MCP_ADDRESSES`.**
- **Interrupt**: all chips' INT tied together (open-drain, wired-OR) to **one**
  RP2040 GPIO; on any INT, every present chip is read. See §2.2 / §7.

### 2.1 CH9120 ↔ RP2040 internal wiring (CONFIRMED via POC)

| CH9120 pin | RP2040 GPIO | Function                                                        |
|------------|-------------|-----------------------------------------------------------------|
| RXD        | GP21        | wired from MCU UART; in MicroPython use `rx=Pin(21)`            |
| TXD        | GP20        | wired to MCU UART; in MicroPython use `tx=Pin(20)`             |
| TCPCS      | GP17        | TCP status (LOW=connected) — **NOT used** (`PIN_CH9120_TCPCS=None`); unvalidated, caused a reconnect flap. Liveness is MQTT keepalive instead |
| CFG0       | GP18        | Config-enable; **LOW = serial configuration mode**              |
| RSTI       | GP19        | Reset; **active LOW**                                            |

UART instance (POC-proven): `UART(1, baudrate=9600, tx=Pin(20), rx=Pin(21))` for
config, then re-open at `baudrate=115200` for transparent mode.

### 2.2 MCP23017 ↔ RP2040 wiring (manufactured `dib-monolith` board)

| Signal       | RP2040 GPIO | Notes                                                   |
|--------------|-------------|---------------------------------------------------------|
| I²C SDA      | GP26        | I2C1 (`sda=Pin(26)`), shared by all chips               |
| I²C SCL      | GP27        | I2C1 (`scl=Pin(27)`), shared by all chips               |
| MCP INT      | GP22        | **Shared wired-OR** of every chip's INT; open-drain, active-low; `IRQ_FALLING` + pull-up |
| MCP RESET    | GP9         | Active-LOW `/RESET`; driven by the MCU (pulsed low then held high at boot) |

> The breadboard POC used I2C0 (GP0/GP1), INT GP2, and tied RESET high. The
> manufactured PCB (`hardware/dib-monolith`) re-routes these — values above are read
> from the PCB netlist and cross-checked against the RP2040-ETH pinout. GP26/GP27 are
> the RP2040's native I2C1 SDA/SCL pair. See `POC_NOTES.md` for the POC→board delta and
> `PINOUT.md` for a visual map of these pins on the RP2040-ETH module.

Addresses: the full A0–A2 strap range `0x20..0x27` (1–8 chips). The firmware
auto-discovers which respond at boot (`MCP_AUTODISCOVER`); `MCP_ADDRESSES` is the
fallback/explicit list. Each MCP needs a distinct A0–A2 strap. Because INT is shared and
**open-drain**, `IOCON.ODR` must be set (`0x44` = MIRROR+ODR) — see §7 — and the
INT net needs a pull-up (GP22 internal pull-up is on; add an external ~4.7 kΩ for
more than ~2 chips). All values live in `config.py`.

> The onboard WS2812 status LED is on **GP25**, **RGB** wire order — see §7a.

---

## 3. Functional behaviour

1. On boot: configure CH9120 (TCP client → broker IP:port), bring up every
   MCP23017, publish MQTT Discovery configs and availability `online`.
2. Any chip raises the **shared INT** when an input changes (interrupt-on-change).
3. RP2040 ISR sets a flag (does no I²C in the ISR).
4. The input loop, on the flag, reads **every present chip's 16 inputs** (reading
   `GPIO` also clears that chip's interrupt and releases the wired-OR INT line).
   Inputs are addressed globally as `(board-1)*16 + pin`.
5. Each input is fed through software debounce, then a **press-type detector**
   state machine.
6. When a gesture completes (single / double / long), it is queued to core 1 and
   published to that input's **action topic** (`…/board<b>/input<p>/action`).
7. Home Assistant, having ingested the discovery configs, fires the matching
   device-automation trigger.

### 3.1 Why a continuous loop (not pure interrupt)

Double- and long-press detection are **time-based**. The interrupt only says
"an edge happened — go read." Timers for the gap between presses and for the
long-press threshold are advanced every iteration of the input loop. So the loop
must run continuously and cheaply, servicing both the IRQ flag and the per-channel
timers.

## 3a. Concurrency model (dual-core)

The RP2040 has two Cortex-M0+ cores. MicroPython runs a second thread on **core 1**
via `_thread`. There is a **GIL**, so the two cores don't execute Python bytecode in
true parallel — but the GIL is released during blocking I/O (UART writes,
`sleep_ms`, I²C waits), which is exactly what the network side does. The value here
is **isolation**, not raw throughput: a slow/blocked network operation must never
delay input sampling or gesture timing.

- **Core 0 — `input_task`** (this is the main thread): I²C + MCP23017 + the MCP INT
  IRQ + debounce + gesture detection. Pushes `(index, gesture)` to the event queue.
  Stays light; bounded I²C ops so a dead bus can't wedge it. Does **not** own the
  watchdog (so an MCP/I²C stall can never reboot the board).
- **Core 1 — `net_task`** (spawned thread): CH9120 link, MQTT session, HA discovery,
  draining the event queue → publishing, the **status LED**, and the **watchdog**
  (fed from `_beat`). Allowed to block (reconnect waits, etc.); the WDT therefore
  trips only if the *network* core itself wedges.
- **Channels between cores**: a thread-safe **`EventQueue`** (gestures, core0→core1)
  and a lock-guarded **`SharedState`** (health flags + a heartbeat). Each core
  `sleep_ms`-yields every pass so the other core gets the GIL.

> For *hard* real-time determinism with true parallel cores and no GIL, the C/Pico
> SDK is the better tool — out of scope here since the firmware is MicroPython.

### 3.2 Wrap-safe timing

`utime.ticks_ms()` wraps, so the pure detector/debounce modules are fed an
ever-increasing millisecond value from `clock.Monotonic` (built on `ticks_diff`).
Watchdog/health timers use `ticks_diff` directly.

---

## 4. Networking design (CH9120, not W5500)

The CH9120 owns the TCP/IP stack. The RP2040 does **not** open sockets. Instead:

- **Configuration phase** (once, at boot): pull `CFG0` LOW to enter serial config
  mode, send the CH9120 command frames to set: mode = **TCP client**, target IP =
  broker IP, target port = broker port (1883), local IP / gateway / subnet (static)
  or DHCP, and UART baud (default 115200). Then return `CFG0` HIGH to enter
  **transparent transmission** mode.
- **Transparent phase**: every byte written to the UART is sent over TCP to the
  broker; every byte received from the broker arrives on the UART. **The UART
  stream is the raw MQTT byte stream.**

Implications baked into the design:

- **No DNS** — the broker must be addressed by **numeric IP** in `config.py`.
- **Single TCP connection** — one broker connection at a time.
- MQTT is implemented over a **socket-like adapter wrapping the UART**
  (`net_stream.py`), so the MQTT client code stays close to standard `umqtt`.
- **Connection state** is observed via **MQTT-level keepalive** — a periodic PINGREQ on a
  fixed cycle (~70% of keepalive) with a PINGRESP-timeout liveness check, plus CONNACK on
  connect. The `TCPCS` GPIO is **NOT used** (it was never HW-validated and a false "down"
  caused a reconnect flap; see `PIN_CH9120_TCPCS = None`). A dead link is detected by a
  missed PINGRESP (or a failed CONNACK); recovery re-runs the CH9120 bring-up to
  re-establish the TCP client socket, then re-runs CONNECT.

---

## 5. MQTT contract

All topics derive from a configurable base. Defaults:

| Purpose            | Topic                                             | Payload / Retain |
|--------------------|---------------------------------------------------|------------------|
| Availability (LWT) | `hearth/<device_id>/status`                    | `online` / `offline`, **retained** |
| Input action       | `hearth/<device_id>/board<b>/input<p>/action`  | `single` / `double` / `long`, not retained |
| Discovery (trigger)| `homeassistant/device_automation/<device_id>/b<b>_in<p>_<gesture>/config` | JSON, **retained** |

- `<device_id>` = stable id derived from the RP2040 unique ID (e.g. last 6 hex).
- `<b>` = board `1..8` (chip position in `MCP_ADDRESSES`); `<p>` = pin `1..16`.
- All inputs belong to **one** HA device; the board/pin shows up in the trigger
  `subtype` (`board<b>_input<p>`, overridable via `INPUT_NAME_OVERRIDES`).
- LWT registered in CONNECT so HA marks the device unavailable on disconnect.

### 5.1 Discovery payload (device_automation trigger)

One config message per **(board × pin × gesture)** = `n_boards × 16 × 3` (up to
**8 × 16 × 3 = 384** at the 8-board max; e.g. 48 for a single advertised board),
published with a small inter-message settle so the CH9120 keeps up. Only
*advertised* boards count — with `MCP_AUTODISCOVER` that's the chips that actually
responded. Example for board 2, pin 5, single press:

```json
{
  "automation_type": "trigger",
  "type": "button_short_press",
  "subtype": "board2_input5",
  "topic": "hearth/AABBCC/board2/input5/action",
  "payload": "single",
  "device": {
    "identifiers": ["hearth_AABBCC"],
    "name": "Hearth",
    "model": "Hearth (DI16-G)",
    "manufacturer": "DIY",
    "sw_version": "0.1.0"
  }
}
```

Gesture → HA trigger `type` mapping:

| Gesture | `type`               | `payload` |
|---------|----------------------|-----------|
| single  | `button_short_press` | `single`  |
| double  | `button_double_press`| `double`  |
| long    | `button_long_press`  | `long`    |

`subtype` = `board<b>_input<p>` (or override). All discovery configs are **retained**
so HA repopulates them after a restart.

### 5.1a Input `event` entities (`INPUT_DISCOVERY`)

In addition to (or instead of) the device-automation triggers, each input can be
published as a modern HA **`event` entity** (`homeassistant/event/<id>/b<b>_in<p>/config`,
`device_class: button`, `event_types: [single, double, long]`). It reuses the existing
(non-retained) action topic; a `value_template` wraps the plain payload into the
`{"event_type": …}` JSON HA expects. Event entities show in dashboards/logbook and are
what the shipped blueprint (`homeassistant/blueprints/.../dib_switch.yaml`) targets.
`INPUT_DISCOVERY` = `"event"` / `"trigger"` / `"both"` (default `both`; `both` doubles
the per-connect discovery burst). HA-verified: entities register and fire with the
correct `event_type`.

### 5.2 Diagnostics telemetry (optional, `DIAG_ENABLE`)

A small **retained** JSON snapshot published to `hearth/<device_id>/diag/state`
plus a handful of HA-discovery **diagnostic entities** (`entity_category:
diagnostic`) attached to the same device, so the customer sees basic operating
parameters in the Home Assistant app with no extra service. Built in `diag.py`
(pure builders), published by core 1.

| Field | Entity | Notes |
|-------|--------|-------|
| `fw` | (device `sw_version`) | firmware version |
| `uptime_s` | sensor (duration) | seconds since net task start |
| `ip` | sensor | static IP, or the **DHCP lease** read back from the CH9120 once at boot (`0x61`); `"dhcp"` if that read fails. See §4 |
| `eth` | binary_sensor (connectivity) | CH9120 TCP link up |
| `mqtt` | — | broker session up |
| `boards` | sensor | input boards responding (count; 0 if MCPs not responding) |
| `board_addrs` | sensor | driven MCP I²C addresses, e.g. `["0x20"]` (static topology) |
| `mem_free` | sensor (data_size) | `gc.mem_free()` |
| `temp_c` | sensor (temperature) | RP2040 internal **die** temp (ADC ch4); coarse trend/overheat signal. This board's ADC offset makes the raw formula read ~60 °C low/negative, so `rp2040_temp_c` reports the **magnitude** — plausible & positive, **not** calibrated |
| `reconnects` | sensor (total_increasing) | successful reconnects since boot |
| `dropped` | sensor (total_increasing) | event-queue drop counter |
| `last` | sensor | last published gesture, e.g. `b1/in3 single` |

**Structured root-cause observability (fw ≥ 0.7.0).** `diag/state` is extended with
`hw`, `reset_cause` (rp2: `power_on`/`wdt`/`unknown`; `wdt` also covers any `machine.reset()`),
`health` (`ok`/`degraded`/`mcp_fault`/
`net_fault`), `boards_total`/`boards_ok`, a per-board `mcp[]` array (`{board,addr,ok,
code,detail,fails,last_ok_s,recoveries}`), a `counters` block (`bus_recoveries`,
`mcp_resets`, `reconnects`, `dropped`), and a `last_fault` + bounded
`recent[]` fault ring (the timeline). A non-retained `…/diag/event` topic carries each
fault record the instant it transitions (HA logbook). `code` values come from the stable
taxonomy in `mcp_health.py`. In **oselia** mode the OSELIA integration renders these as a
Diagnostics sensor, per-board MCP entities, counters, and a fault `event`; full schema in
`homeassistant/INTEGRATION_SPEC.md`. The state is republished **immediately** on a
health/fault change (not only every `DIAG_INTERVAL_S`), still queue-gated.

**Latency guarantee (critical):** diagnostics must never delay a button publish.
The single CH9120 TCP pipe is shared, so the publish is gated in `net_task` to the
**lowest priority**: it is emitted only when the gesture queue is **fully drained**
(`len(queue) == 0`), at most every `DIAG_INTERVAL_S`, as one small fire-and-forget
(QoS0) retained message — so it can never sit in front of a queued gesture. The
state snapshot is taken from `SharedState.health()` (under the lock) and
`json.dumps`'d **outside** the lock. Discovery for the diagnostic entities is
published once per connect, after the action discovery (the queue buffers presses
through that one-time burst, as it already does for the `n_boards × 16 × 3` action
configs).

`DIAG_ENABLE` is a per-install toggle: the provisioning tool writes `"diag":
false` into `site.json` (`oselia provision --no-diag`) and the config overlay turns the
whole feature off — no `diag/state` publishes and no diagnostic entities.

**Log mirror.** WARN/ERROR log lines are also surfaced in HA: `log.set_sink` stashes
the last such line (callable from either core — a bare slot write), and core 1
publishes it (retained, **queue-gated** like everything else) to
`hearth/<id>/diag/log` as `{"line","level","ts"}`, rendered by a **"Last log"**
diagnostic sensor. Event-driven, so no `expire_after` (the last line persists).

**Native-feel discovery.** Every config (triggers + diagnostics) carries an `origin`
block and an enriched `device` (adds `hw_version`, `serial_number`); diagnostic
entities set `expire_after = 3 × DIAG_INTERVAL_S` so they go *unavailable* if the
board wedges and telemetry stops.

### 5.3 Two-way control (`CONTROL_ENABLE`)

The firmware also **subscribes** (the CH9120 transparent stream is bidirectional):
on each connect it subscribes to `hearth/<id>/cmd/#` (clean-session, so it
re-subscribes every reconnect) and publishes HA **`button`** entities:

| Button | Command topic | Action |
|--------|---------------|--------|
| Restart (`device_class: restart`) | `…/cmd/reboot` | `machine.reset()` (publishes `offline` first) |
| Identify (`device_class: identify`) | `…/cmd/identify` | flash the status LED white ~3 s |
| (maintenance — no HA entity) | `…/cmd/maintenance` | park the loader (`main.py`→`main.py.provbak`) + `machine.reset()` → boot **bare** (no app, no watchdog) |

The **maintenance** command is the cooperative provisioning quiesce (no HA button; sent by
the `oselia` tool over the broker): it lets the host re-provision a **running** unit over USB
without holding a host REPL session on the watchdog-guarded unit (which the WDT hard-resets
once armed). The firmware renames the loader and resets *itself*, so the board comes up bare
for the host to
rewrite; provisioning restores the loader after. See
`provisioning/PROVISIONING_SPEC.md` sec.3.1.

Inbound PUBLISH is parsed in `mqtt_client.service()` and dispatched to a handler —
which runs **after the gesture queue is drained each pass**, so a command never
delays a button publish. HA-verified: pressing *Restart* takes the board
`offline → online` with uptime reset; *Identify* is accepted without disrupting the
link.

### 5.4 Live tuning (`number` / `select`)

The gesture timings and log level are editable from HA at runtime:

| Entity | Command | Range |
|--------|---------|-------|
| `number` Long press time | `…/cmd/long_ms` | 100–2000 ms |
| `number` Double-tap window | `…/cmd/double_gap_ms` | 0–1000 ms |
| `number` Debounce time | `…/cmd/debounce_ms` | 0–100 ms |
| `select` Log level | `…/cmd/log_level` | ERROR/WARN/INFO/DEBUG |

Current values are published (retained) to `hearth/<id>/cfg`, which the entities
reflect via `value_template`. On a command, core 1 clamps the value, updates
`SharedState` (bumping `tune_version`) and the log level, **persists** it into the
board's `site.json` (atomic temp+rename), and republishes `cfg`. **Core 0** notices
the version bump on its next pass (cheap unlocked compare) and re-applies the timings
to every channel's detector/debouncer — no restart needed. Persisted values override
the hardware defaults on the next boot via the `config.py` overlay. HA-verified:
setting *Long press time* = 750 ms and *Log level* = DEBUG applied live and **survived
a reboot** (checked after clearing the retained `cfg`).

---

## 6. Press-type detection

Per channel, an independent state machine driven by debounced edges + a clock:

- **Debounce**: ignore changes faster than `DEBOUNCE_MS` (default 25 ms). Hardware
  RC already helps; this guards against residual chatter.
- **Long press**: input held active continuously for ≥ `LONG_MS` (default 600 ms)
  → emit `long` (emitted once, on threshold crossing; the subsequent release is
  swallowed and does not produce a single).
- **Single vs double**: on release of a (non-long) press, start a window of
  `DOUBLE_GAP_MS` (default 280 ms). If a new press begins within the window →
  emit `double`. If the window expires with no new press → emit `single`.
- Timings are config constants; the detector takes the clock as an argument so it
  is unit-testable on a host with a fake clock (see §9).

> Polarity note: define in `config.py` whether "active" = MCP pin HIGH or LOW.
> With PC817 + pull-up, a pressed switch typically pulls the MCP input LOW
> (active-low). The detector works on a boolean "active", computed once from the
> raw pin and `ACTIVE_LOW`.

---

## 7. MCP23017 configuration

Applied identically to **every** chip on the bus:

- `IOCON = 0x44` → `MIRROR=1` (INTA/INTB OR'd to one pin) **+ `ODR=1`
  (open-drain)** so all chips can share one wired-OR INT line. (Single-chip POC
  used `0x40` push-pull; multi-chip needs ODR. Set via `MCP_INT_OPEN_DRAIN`.)
- `IODIRA/B = 0xFF` → all 16 pins are inputs.
- `GPPUA/B` pull-ups per the optocoupler output stage (enabled for active-low).
- `GPINTENA/B = 0xFF`, `INTCONA/B = 0x00` → interrupt-on-change (compare to
  previous value, not DEFVAL).
- Read `GPIOA/B` to capture state **and clear** the interrupt. With a shared INT,
  every present chip is read on each INT so the wired-OR line is fully released.
- The INT net needs a pull-up (GP22 internal pull-up + external ~4.7 kΩ for >2 chips).

---

## 7a. Status LED (onboard WS2812)

The board has **one** addressable RGB LED (WS2812) on **GP25**, **RGB** wire order
(`STATUS_LED_ORDER`; HW-confirmed on this board — a standard GRB driver shows
green-as-red here). A single pixel can't show every subsystem at once, so status is
encoded as **colour + blink pattern**, displaying the **highest-priority unhealthy
subsystem (root cause first)**. Driven non-blocking from the main loop via
`status_led.update(now_ms)`. The WS2812 is clocked out over the **RP2040 PIO** (the
board's MicroPython build lacked `neopixel`/`machine.bitstream`; PIO is portable and
build-independent), with byte order set by `STATUS_LED_ORDER`.

| Condition (priority order)      | Colour  | Pattern        |
|---------------------------------|---------|----------------|
| Booting / initialising          | Blue    | solid          |
| Ethernet (CH9120 TCP) down      | Red     | slow blink (1 s)   |
| MQTT broker session down        | Orange  | medium blink (0.6 s) |
| MCP23017 not responding         | Yellow  | fast blink (0.3 s)   |
| All subsystems healthy          | Green   | solid          |
| Gesture published (any input)   | White   | ~90 ms flash (overrides) |

Brightness, pin, and colour order live in `config.py`. The colour/priority logic
is pure and unit-tested (`tests/test_status_led.py`); only the WS2812 write touches
hardware.

## 8. Module / file layout

```
dib-gateway-fw/
├── SPEC.md / CLAUDE.md / README.md / POC_NOTES.md
├── config.example.py        # copy to config.py and edit
├── src/
│   ├── main.py              # orchestrator: spawn core1, run core0, validate config
│   ├── input_task.py        # CORE 0: MCP IRQ + debounce + detect -> queue; WDT;
│   │                        #   live-applies tunable timings on tune_version bump
│   ├── net_task.py          # CORE 1: CH9120 + MQTT + discovery + LED; drains queue;
│   │                        #   diagnostics, log mirror, command/control handler
│   ├── ch9120.py            # CH9120 config + transparent-mode driver; IP read-back (0x61)
│   ├── net_stream.py        # byte-stream adapter over the CH9120 UART
│   ├── mqtt_client.py       # robust MQTT over a stream (CONNACK/LWT/keepalive +
│   │                        #   SUBSCRIBE + inbound-PUBLISH dispatch for control)
│   ├── mqtt_packets.py      # pure MQTT packet builders/parsers incl. SUBSCRIBE (tested)
│   ├── mcp23017.py          # MCP23017 driver (mirror INT, I2C retry, presence)
│   ├── debounce.py          # per-channel debounce (pure)
│   ├── press_detector.py    # single/double/long state machine (pure; set_params)
│   ├── ha_discovery.py      # discovery (triggers, event, button, number, select) +
│   │                        #   action/command/cfg topics (pure builders)
│   ├── diag.py              # diagnostics state + HA diag entities + log mirror (pure)
│   ├── status_led.py        # WS2812 status indicator (pure colour logic + writer)
│   ├── event_queue.py       # thread-safe bounded ring buffer (pure, tested)
│   ├── shared_state.py      # cross-core health + heartbeat + live-tunable timings
│   ├── clock.py             # wrap-safe monotonic ms (pure, tested)
│   └── log.py               # leveled, rate-limited logger (serial + optional HA sink)
├── tests/                   # host-runnable (CPython): detector, led, queue, clock, mqtt, diag
└── tools/flash_notes.md
```

The OSELIA custom integration lives in its own repo
(**vmyronovych/oselia-hearth-di16g-ha**, HACS-installable; design contract in
`homeassistant/INTEGRATION_SPEC.md`). The in-repo HA assets — the dashboard example in
`homeassistant/dashboards/` (render your own with `oselia dashboard render`) and
`homeassistant/blueprints/…` — live outside `src/`. The host tool does not push HA assets;
the integration is installed via HACS and the dashboard YAML is uploaded by hand.

Pure modules (no `machine`/`network` imports): `press_detector`, `debounce`,
`event_queue`, `clock`, `mqtt_packets`, `shared_state`, and the builders in
`ha_discovery`/`diag`/`status_led` — all unit-tested under CPython.

---

## 9. Testing strategy

- **Host unit tests** (CPython, no hardware): drive `press_detector` with a fake
  clock and a scripted sequence of edges; assert the emitted gesture for single,
  double, long, and boundary timings (just under/over each threshold). Run with
  `python3 -m pytest tests/` or plain `python3 tests/test_press_detector.py`.
- **Syntax check** all `src/*.py` with `python3 -m py_compile` (won't import
  `machine`/`network`, just validates syntax).
- **On-device smoke test**: configure the CH9120 as a TCP client, confirm the broker
  accepts the MQTT CONNECT (CONNACK) and the retained `…/status` goes `online`, publish a
  manual test message, watch it in `mosquitto_sub`. (Liveness is the MQTT keepalive
  PINGREQ/PINGRESP cycle — the `TCPCS` GPIO is not used.)
- **HA integration test**: confirm the device appears under Settings → Devices with
  its inputs (as `event` entities and/or automation triggers), the diagnostic
  entities, and the control entities (Restart/Identify buttons, timing `number`s, log
  `select`). Verify a control round-trip (e.g. *Restart* → offline→online; a `number`
  change updates `…/cfg` and survives a reboot), and that the OSELIA integration (installed
  via HACS) shows the device + entities and the rendered `/oselia-hearth` dashboard works.
  (Done against a local HA 2025.11 this cycle.)

---

## 10. Acceptance criteria

1. Board boots, configures CH9120 as TCP client to the broker IP:port, and
   `TCPCS` reads LOW (connected).
2. MQTT CONNECT succeeds with LWT; `…/status` shows `online` (retained).
3. All 48 discovery configs are published (retained); HA shows one device with 16
   inputs and short/double/long triggers each.
4. Pressing a wall switch produces exactly one gesture event with correct
   classification:
   - quick tap → `single`
   - two quick taps within `DOUBLE_GAP_MS` → `double` (and **no** stray `single`)
   - hold ≥ `LONG_MS` → `long` (and **no** `single`/`double`)
5. Simultaneous presses on multiple inputs are each classified independently.
6. The ISR performs no I²C/allocation; all heavy work happens in the input loop.
7. Host unit tests pass (detector, debounce, LED, queue, clock, MQTT packets) and
   all `src/*.py` pass `py_compile`.
8. On network drop, firmware detects it (keepalive/PINGRESP) and reconnects with
   backoff without a manual reset; availability returns to `online` and discovery
   is republished.
9. While the network task is busy reconnecting, **input timing is unaffected**:
   gestures are still classified correctly and buffered in the queue, then flushed
   on reconnect.
10. A hung core (either one) triggers a watchdog reset (verify by forcing a stall).
11. MCP I²C glitches are retried; a removed/unresponsive MCP is reflected on the LED
    and recovered (re-init) when it returns.
12. With multiple chips: presses on every board are detected and published with the
    correct `board<b>/input<p>`; one absent/failed board does not shift another
    board's numbering or stop the others.

---

## 11. Open items to confirm on real hardware

Resolved by the POC (see `POC_NOTES.md`): CH9120 tx/rx pins + config baud, MCP
register init, active-low polarity, static IP (no DNS), and working long/double
timings (400/300 ms).

Confirmed on the **manufactured board** (this hardware, 2026-06): MCP23017 detected on
the re-routed bus (**I2C1 GP26/27**, INT **GP22**, `/RESET` **GP9**); MQTT online +
retained discovery configs publishing; status LED working — WS2812 on **GP25**, **RGB**
wire order (GRB shows green-as-red here), driven via PIO. Interpreter pinned to
MicroPython **1.28.0** (`FLASHING.md`).

Still to confirm:

- **TCPCS (GP17)** as a live connection-status signal — **now DISABLED**
  (`PIN_CH9120_TCPCS=None`). It was never HW-validated and acting on it produced a
  connect→publish→forced-reconnect FLAP (false "down" churned the broker status
  online/offline). The LED "ethernet" state + dead-link detection now use **MQTT keepalive
  PINGREQ/PINGRESP** (HW-independent). To re-enable TCPCS, first verify its polarity/timing
  on hardware, then set the pin back to 17.
- **Reconnect behaviour**: the POC is fire-and-forget (no CONNACK/LWT). Confirm the
  CH9120 auto-reconnects the TCP session and that re-issuing CONNECT works.
- **DEBOUNCE_MS** feel given the hardware RC stage (start at 25 ms).
- HA **device_automation** trigger rendering end-to-end (the POC used binary_sensor).
- **Shared wired-OR INT** across multiple chips (POC was single-chip): confirm the
  open-drain INT + pull-up behave, and that reading all chips on each INT reliably
  releases the line. I²C bus integrity with up to 8 chips (pull-up sizing, lengths).

---

## 12. Industrial-grade robustness (implemented)

- **Network-first boot**: core 1 (CH9120 + MQTT) is spawned **before** any I²C work;
  core 0 builds the bus, resolves the board set, and publishes it via `SharedState`.
  An MCP fault (or a wedged bus at boot) can therefore never delay or block the
  network — core 1 waits at most `NET_BOARD_WAIT_MS` for the count, then falls back to
  the config list.
- **Watchdog** (`machine.WDT`, ≤8388 ms): **owned and fed by core 1** (`net_task`,
  from `_beat`), so it guards only the *network* core. An MCP/I²C stall on core 0 —
  even a fully wedged or unpowered bus — **never** reboots the board; it is reported
  and recovered instead (this was a real requirement). Armed only after `net_task`
  signals `ready`, so the multi-second bring-up can't trip a spurious reset. To keep
  core 0 itself responsive on a bad bus, every I²C transaction is bounded by
  `I2C_TIMEOUT_US` and presence checks probe only the MCP strap range (not a 112-
  address `i2c.scan()`), so a single op fails in ~tens of ms instead of hanging.
- **Pure-polling input (no interrupt)**: every healthy chip's GPIO is read on a fixed
  cadence (`MCP_POLL_MS`, ~20 ms). The MCP23017 INT / shared wired-OR IRQ is **not used**
  — it was the source of the original freeze and dropped-press faults (a shared
  open-drain IRQ across satellite boards is inherently fragile, and a missed/quirky INT
  silently drops presses). Polling is deterministic and self-healing: a chip that
  glitches just shows up in the next poll; latency ≤ `MCP_POLL_MS` is imperceptible for
  wall switches (the RC+optocoupler already debounces). MCP init leaves interrupts off
  (`GPINTEN=0`, `IOCON=0`), so nothing drives the INT line.
- **MCP fault tolerance + active recovery**: a chip that fails reads is marked down,
  skipped (so it can't affect the healthy boards), and recovered. Recovery runs only
  when a chip is actually failing and escalates, rate-limited with exponential backoff
  (`MCP_RECOVERY_AFTER_FAILS`, `MCP_RECOVERY_MIN/MAX_INTERVAL_MS`): **L1** clocks the I²C
  bus (≤9 SCL pulses + STOP) to free a stuck SDA, then recreates the peripheral; **L2**
  pulses the MCP `/RESET` line (GP9). Backoff keeps a persistently-absent chip from
  re-pulsing the shared `/RESET` (which would reset the healthy boards). Per-board health,
  error codes, and `bus_recoveries`/`mcp_resets` counters are surfaced in `diag/state` +
  `diag/event` (§5.2). Pure decision logic lives in host-tested `mcp_health.py`.
- **Reconnect with exponential backoff** (`RECONNECT_BACKOFF_MIN/MAX_MS`), LWT
  (`offline`), availability `online`, and discovery republish on reconnect. Backoff
  waits are chunked so the heartbeat keeps ticking during them.
- **MQTT liveness**: PINGREQ at ~70% of keepalive; if no PINGRESP within
  `PING_RESPONSE_TIMEOUT_MS`, the session is declared dead and rebuilt. CONNACK
  return code is validated.
- **I²C resilience**: bounded retries on every MCP read/write; periodic presence
  check; auto re-init when the device returns; bus/`/RESET` recovery (above).
- **Bounded event queue** with drop-oldest + dropped counter (newest button activity
  always survives a backlog); graceful degradation — detection continues offline and
  flushes on reconnect.
- **Wrap-safe timing** via `clock.Monotonic`; **no allocation in the ISR**; periodic
  `gc.collect()` at safe points on core 1; `_thread.stack_size` raised for core 1.
- **Leveled, rate-limited logging** so fault loops don't flood the console.
- **Config validation** at boot (IP tuple shapes, port range, WDT bounds).
