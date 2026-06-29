# Metrics framework (Stage 1) — design, status, and how to continue

> **Status: Stage 1 implemented + hardware-verified; PARKED to prioritise the USB/reconnect
> robustness work. Stage 2 (compact wire + HA cutover) not started.** This PR preserves the work.

## Why
Foundation for a fleet "stability loop": the firmware's `diag/state` telemetry must be a
*trustworthy, complete* metric set before anything analyses it. This reworks the scattered
telemetry into one namespace, makes the values correct and persistent, and keeps the door open
to a compact wire format — without ever being able to freeze/crash the firmware.

## Three NON-NEGOTIABLE requirements (from the product owner)
1. **Self-contained namespace** — all metrics logic in its own module(s) with a small API; the
   rest of the firmware only *calls* it.
2. **Never freeze the firmware** — non-blocking, bounded work/memory, errors degrade to
   "no metrics", never raise into the loops; must not starve the watchdog.
3. **Tiniest wire representation** — short nested JSON keys (e.g. `c.br`), with a documented
   key dictionary. (Implemented but DORMANT until Stage 2 — see staging below.)

## Architecture (new files)
- `src/metrics_schema.py` — *pure*: short-key dictionary, `SCHEMA_VERSION`, wire mappers.
- `src/metrics.py` — *pure*: the `Metrics` registry (counter/gauge/event), fault ring,
  `snapshot()`, and a never-freeze `serialize()` that writes into a **reused buffer** (no
  `json.dumps` transient on a fragmented heap). Hardware store is injected → host-testable.
- `src/metrics_store.py` — RP2040 persistence: `ScratchStore` (watchdog SCRATCH0–3 via
  `machine.mem32`, survives watchdog/soft reset), `FlashStore` (atomic littlefs file, survives
  power loss), `PersistentStore` (tiered, CRC-validated, boot reconciliation by max). Pure
  helpers (CRC/pack/rate-limit/reconcile) are host-tested with fakes.

API: `inc(key)` / `set_gauge(key,v)` / `add_fault(up,boot,comp,code,detail[,board])` /
`note_crash(...)` / `set_boards(...)` (mutate, cheap, single-writer-core) and
`snapshot()`/`serialize()`/`load()`/`checkpoint()`/`flush()` (read+lifecycle, core1 only).

## Wiring (Stage 1 — keeps the existing verbose wire keys, so HA is UNCHANGED)
- `main.py` builds `Metrics`+`PersistentStore`, `load()`s on boot, surfaces a boot.py crash,
  `alloc_emergency_exception_buf(100)`. **Metrics setup is fully contained** — any failure falls
  back to an in-RAM registry and keeps booting (a crash here would reach boot.py → reset → and on
  this board a reset can drop USB-CDC; see USB issue below).
- `input_task.py` (core0) — counters + faults go through the shared registry (persisted).
- `net_task.py` (core1) — net counters sourced from the registry; **MQTT-disconnect faults now
  land in the timeline** (were previously never recorded); gauges + `mem_free_min`;
  `checkpoint()`/`flush()`; serialises to the **existing verbose schema** via `diag.build_state`
  (+ additive new fields). The compact `metrics.serialize()` stays dormant until Stage 2.
- `boot.py` — one dependency-free change: persist the crash traceback excerpt into `/ota/state`
  so the app can surface it as `last_crash` on the recovered boot. (Integrates with boot.py's
  existing crash counter + safe-mode; does NOT add a second guard.)

## Defects fixed (vs the pre-existing telemetry)
- **`int_stuck` removed** — the firmware is pure-polling; the MCP INT line is deliberately unused,
  so a "stuck INT" is impossible. The counter was a phantom (always absent in live telemetry).
- **Net/MQTT faults now reach `recent[]`** — `_fault` previously hardcoded `component="mcp"`.
- **Counters + `boot_count` persist across reboot** (were zeroed every boot).
- **`mem_free_min` low-water**, **`mqtt_disconnects`/`eth_link_losses`** counters added.
- `temp_c` documented as an uncalibrated die-temp trend (value unchanged).

## Hardware-verified (on the bench, fw 0.8.0)
Board published a correct Stage-1 `diag/state`: `boot_count:1`, `mem_free_min` < `mem_free`,
new net counters present, **no `int_stuck`**, `recent[]` faults carry the `boot` anchor, live
recovery counters (`bus_recoveries`/`mcp_resets`) — and **USB survived the deploy/reset** (the
containment hardening held). The metrics path never misbehaved.

## KNOWN ISSUE (why this is parked) — reconnect wedge, NOT a metrics regression
A hard broker bounce (`docker restart mosquitto`) **wedged the board**: `net_task` (core1) hung
during the reconnect → watchdog reset → USB-CDC dropped → did not self-heal. The metrics code
does not run in the disconnect path, so this points at the **CH9120 UART↔Ethernet reconnect
path** (TCPCS flow-control is disabled; stale buffered bytes can desync the session — the connect
handler already catches "malformed CONNACK from stale link bytes"). Related: the PARKED
`fix/ota-rxbuf` PR enlarging the UART RX buffer. **Needs serial-captured diagnosis** before fully
clearing the metrics changes. This is the robustness issue being prioritised next.

## Tests
27 new host tests (`tests/test_metrics.py`, `tests/test_metrics_store.py`) — serializer
round-trip/escaping/buffer-reuse, ring eviction + boot anchoring + net-fault routing, fixed
counter set, persistence reconcile (max/CRC reject), never-raise contract. Full existing
`tests/*.py` suite stays green. Gate: `python3 -m py_compile src/*.py && for t in tests/test_*.py; do python3 "$t"; done`.

## To continue later
- **Finish hardware verification** (deferred by the USB issue): persistence-across-reboot via a
  clean **power-cycle** (avoids the reconnect path); crash-capture test (needs the OTA `/boot.py`
  + `/slots` layout, i.e. a provisioned board, not a bare `deploy.sh`).
- **Stage 2** — flip firmware to the compact short-key wire (`metrics.serialize()`,
  `SCHEMA_VERSION`) + coordinated HA cutover (sensor/binary_sensor/client/entity dotted paths,
  dashboards, INTEGRATION_SPEC, ship the key dictionary). Blast radius mapped; do after Stage 1
  is fully proven on hardware.
- Full design history lives in the working notes under `~/.claude/plans/stability-metrics-*.md`.
