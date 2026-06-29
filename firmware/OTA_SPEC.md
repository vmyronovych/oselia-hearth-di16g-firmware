# OTA Firmware Updates — Hearth (RP2040-ETH)

Status: **working — HW-verified end to end** (see Implementation status below). This
is the design contract for remote application updates,
alongside `SPEC.md` (firmware behaviour) and `../provisioning/PROVISIONING_SPEC.md`
(installer experience). Where this and `SPEC.md` disagree on firmware behaviour,
`SPEC.md` wins.

**Implementation status (2026-06-25): WORKING + HW-VERIFIED end to end.**
- `src/ota.py` core (boot-confirm/auto-revert state machine, bundle build/parse/verify,
  loss-tolerant `OtaReceiver`, streaming `apply_bundle_file`); `tests/test_ota.py` (19).
- `src/boot.py` loader: boot-confirm gate + **safe-mode** (drops to REPL after
  `_MAX_CRASHES` failed boots so a bad slot can't reset-loop forever); installed at root,
  never bundled.
- `src/net_task.py` receiver: `ota/cmd` (version-guarded), `ota/data` chunks, NAK
  re-request of dropped chunks (`ota/nak`), staged-slot apply + reset, and `confirm()`
  once online+healthy for `OTA_BOOT_CONFIRM_MS`.
- `tools/ota_publish.py`: reference publisher (builds bundle, resends the QoS0 `cmd`
  until acked, paces chunks to the CH9120 UART rate, resends NAK'd chunks).
- **HW-VERIFIED 2026-06-25 on board 893922**: 0.1.0→0.2.0 (download→verify→apply→boot-
  confirm `idle`); a deliberately-broken 0.3.0 **auto-reverted** to 0.2.0 (no brick).
  Driven **from the HA `update` card** (the OSELIA integration's `ota.py`), not just the
  `ota_publish.py` reference tool — the full HA-initiated round trip is confirmed.
- **Transport: MQTT-chunked** (supersedes the HTTP-pull design below) over the live
  broker session — no CH9120 retarget.
- **Key HW learnings:** board subscribes `ota/data`/`ota/cmd` at **QoS0** → individual
  chunks AND the command can drop → publisher resends `cmd` till acked + the board NAKs
  missing chunks. Stream must be **paced to ~the 115200-baud UART rate** (~100-200ms per
  1KB chunk); pacing plus a large **RP2040 UART RX ring buffer** (`UART_RXBUF=8192`,
  `ch9120.py` — default is only 256 B) keep the inbound byte stream from overrunning while
  the firmware is busy writing a chunk to flash (the buffer is RP2040-side RAM, not on the
  CH9120). **Caveat:** it's a permanent allocation — on a low-heap unit, `mem_free` during
  an OTA was observed dipping to ~1.5 KB (board 893922, MCP fault loop), so validate the
  size against a real OTA (consider 4096) before relying on it. `machine.reset()` on this board can drop
  USB-CDC until a cold BOOTSEL reflash — irrelevant to OTA (network), but it's why USB
  deploys are flaky; recover via flash_nuke + reflash + the resumable slot deploy.
- **Remaining:** wire the integration `UpdateEntity` to drive OTA from the HA UI
  (download bundle from a release feed + publish via the `ota_publish` logic);
  provisioning to lay down the slot layout (`boot.py` + `/slots/a`) at install time.
  See `homeassistant/INTEGRATION_SPEC.md`.

## Context

The gateway is installed in walls/cabinets across houses; pulling each unit to a
USB cable for a code change does not scale. We want **remote, unattended firmware
updates** of the application code over the LAN, with safe rollback so a bad build
or a power cut mid-update can never strand a unit.

The board's constraints fully shape the design (see `SPEC.md §4`):

- **No socket / no DNS / no HTTP stack on the RP2040.** The only live external link
  is the CH9120 bridge, configured as a *single TCP client to the broker*. To fetch
  bytes over HTTP we must **reconfigure the CH9120 target to a file server, pull,
  then reconfigure back** — reusing the proven `ch9120.configure()` path.
- **"Firmware" = a set of `.py` files on littlefs**, not a UF2. We can replace app
  modules; we **cannot** replace the MicroPython interpreter remotely (that is a
  physical BOOTSEL UF2). Interpreter OTA is **out of scope** (would need a custom
  dual-bank bootloader; brick risk).
- The MQTT client now **supports SUBSCRIBE + inbound-PUBLISH dispatch** (added for
  two-way control — `SPEC.md §5.3`): it subscribes to `…/cmd/#` and routes commands
  to a handler. An OTA *trigger* over MQTT can reuse this exact path (e.g. a
  `…/cmd/ota` command), so the receive-side prerequisite is already met.
- Dual-core with a watchdog gated on core1's heartbeat (`SPEC.md §3a/§12`). The
  download is long and blocking → it runs on **core1 (`net_task`)**, chunked, with
  `_beat()` between chunks so the WDT stays fed.
- A machine-owned `site.json` at root holds broker IP/credentials/identity
  (`config.py` overlay). **OTA must never touch `site.json`** so identity survives
  updates — same principle the provisioning wizard already relies on.

**Decisions (confirmed with user):** delivery = **HTTP pull (reconfig bridge)**;
scope = **app `.py` only**; safety = **auto-revert on bad boot**.

---

## Architecture

### On-device slot layout (new) + thin root loader

Today the app is a flat set of modules copied to `/` and started by `/main.py`.
For atomic A/B activation we move the **app** into versioned slot dirs and keep a
tiny, OTA-immutable loader at root:

```
/boot.py              # loader (installed once via USB; NEVER in an OTA bundle)
/site.json            # machine-owned; never touched by OTA
/slots/a/  <app .py>  # one full copy of src/*.py + config.py
/slots/b/  <app .py>  # the other copy
/ota/active           # text: "a" or "b"  (atomic pointer)
/ota/state            # text: boot-confirm state machine fields (see below)
```

`boot.py` (runs at reset): read `/ota/active`, run the **boot-confirm gate** (below),
`sys.path.insert(0, "/slots/<active>")`, then `import main; main.main()`. The loader
is deliberately ~50 lines and never updated remotely, so no OTA can brick the boot
path. `config.py`'s `open("site.json")` stays relative to cwd (`/`) → works from any
slot, unchanged.

### Boot-confirm / auto-revert state machine (the safety core)

`/ota/state` holds `active`, `previous`, `pending` (bool), `tries` (int).

- After a successful download+verify, OTA writes the new app into the **inactive**
  slot, sets `previous=<old active>`, `active=<new>`, `pending=true`, `tries=0`,
  then **soft-resets**.
- On every boot, `boot.py`: if `pending` → `tries += 1`; if `tries > OTA_MAX_BOOT_TRIES`
  (e.g. 2) → **revert**: `active=previous`, clear `pending`, boot the old slot.
  Otherwise boot the (new) active slot.
- The running app, once it reaches **network-online** (mqtt + ethernet) continuously
  for `OTA_BOOT_CONFIRM_MS` (e.g. 20 s), calls `ota.confirm()` → clears `pending`,
  `tries=0`. A build that crashes early, hangs (WDT resets it), or never reaches the
  broker thus auto-reverts on the next reset — unattended, power-loss safe.
  Boot-confirm requires only **network** health, NOT MCP: as of 0.7.x a degraded MCP is
  a normal running state (reported + recovered, never a reboot), so it must not block a
  good build from confirming on exactly the units that have an MCP fault.

This reuses existing health signals: `SharedState.ready` + `health()["mqtt"]`
already tell `net_task` exactly when "online + healthy" holds.

### Update flow end to end

1. Operator runs the host tool (LAN). It builds a **bundle** from `src/*.py` +
   `config.py`, computes per-file + whole-bundle sha256, serves it over a tiny HTTP
   server (numeric IP — no DNS), and publishes the OTA command to the broker.
   **Modules are compiled to MicroPython bytecode (`.mpy`)** before packaging
   (`tools/ota_build.py`, default; `--no-mpy` for raw `.py`): the bundle is ~70% smaller
   → fewer chunks → less loss exposure, and the device imports `.mpy` transparently
   (`boot.py` `import main`). The bundle format and on-device contract (manifest
   `[[name,size,sha256],…]` + per-file/whole sha) are **unchanged** — only the file
   `name`s carry a `.mpy` extension. The cross-compiler must emit a `.mpy` version the
   interpreter accepts (v6.3 for MicroPython 1.23+, which the board's 1.28.0 uses);
   `boot.py` (the root loader) is never bundled and stays `.py`.
2. Device (`net_task`, subscribed to `…/ota/cmd`) receives the command, guards on
   version (no-op if already running target), publishes `…/ota/state` =
   `downloading` (**retained**, so observers see it's intentional not a crash).
3. Device tears down MQTT, **reconfigures CH9120** target → file server, raw
   `GET` over the UART stream, streams the body to the inactive slot's staging dir
   while hashing, beating between chunks.
4. Verify whole-bundle + per-file sha256. On success: flip the slot pointer, set
   `pending`, publish `applying`, soft-reset. On any failure: reconfigure CH9120
   back to broker, publish `error:<reason>`, stay on current version.
5. New build boots, reconnects, runs healthy for the confirm window → `confirm()`,
   publishes `idle` + new `version`. (Bad build → auto-revert per above.)

---

## Components / files

### 1. MQTT receive path (prerequisite — shared by any trigger mechanism)
- `src/mqtt_packets.py`: add `build_subscribe(packet_id, topic, qos=1)`,
  `parse_suback(body)`, and a PUBLISH parser (topic + payload; ignore packet id at
  QoS0). Pure, unit-tested alongside the existing builders.
- `src/mqtt_client.py`: add `subscribe(topic, qos=1)` (send + await SUBACK, like
  `connect()` awaits CONNACK) and an `on_message` callback. In `service()`, the
  branch that currently *ignores* inbound PUBLISH (line ~139) dispatches to
  `on_message(topic, payload)`.
- `src/net_task.py`: after `client.connect()`, `client.subscribe(OTA_CMD_TOPIC)`
  and re-subscribe on every reconnect (next to the discovery-republish block,
  `net_task.py:94`). Set `client.on_message = ota.on_command`.

### 2. Slot layout + loader + auto-revert
- New `src/boot.py` loader (root-installed). Tiny, dependency-free, robust.
- New `src/ota.py`: owns `/ota/state` read/write, `confirm()`, slot helpers, the
  command handler, the CH9120-reconfig + HTTP-GET + stream-to-flash + verify +
  swap. The HTTP client is a minimal `GET`/HTTP-1.0 reader over `UartStream`
  (reuse `net_stream.UartStream`, add a `read(n)`-driven body reader); parses
  status line + `Content-Length`.
- `tools/deploy.sh` + `provisioning/provision.py`: install the loader at root and
  the app into `/slots/a/`, write `/ota/active=a`. (One-time layout change; this is
  the only place that changes how files land on the board.)

### 3. CH9120 reconfig for the pull
- Reuse `ch9120.bring_up()` / `CH9120.configure()` (`src/ch9120.py:69,94`) but
  parameterized with the **file-server** IP/port for the download, then called again
  with the broker IP/port to restore. Add a thin `ch9120.retarget(cfg, ip, port)`
  wrapper so OTA doesn't duplicate the config sequence. Beat around it
  (`net_task._beat`) — it blocks a few seconds like the existing boot bring-up.

### 4. State reporting + LED
- `src/status_led.py`: add an `updating` state (e.g. cyan pulse) so a bystander sees
  the unit is intentionally updating, not hung. Pure color logic → extend
  `tests/test_status_led.py`.
- `…/ota/state` retained JSON: `{stage, percent, running_version, target_version,
  error}`. Doubles as the feed for an optional HA MQTT `update` entity (follow-up,
  not required for this plan).

### 5. Host side — new `ota/` sibling dir (mirrors `provisioning/`)
- `ota/ota_publish.py`: build bundle (manifest = `[[name,size,sha256],…]` then
  concatenated file bytes — no tar lib needed on device), start a stdlib
  `http.server` bound to a numeric LAN IP, publish the command (via `mosquitto_pub`,
  already used in `tools/`, to avoid a new paho dependency), and tail `…/ota/state`
  (via `mosquitto_sub`) for progress/result.
- `ota/OTA_SPEC.md`: the contract (topics, command JSON, bundle format, slot/rollback
  semantics) — this document, moved/duplicated there if the host tooling lands in a
  sibling dir.

### 6. Config additions (`config.example.py` + `src/config.py`, with `site.json` overlay where per-site)
- `OTA_ENABLE`, `OTA_CMD_TOPIC` / `OTA_STATE_TOPIC` (derived from `BASE_TOPIC`/id),
  `OTA_BOOT_CONFIRM_MS`, `OTA_MAX_BOOT_TRIES`, `OTA_HTTP_CHUNK`,
  `OTA_DOWNLOAD_TIMEOUT_MS`, slot/staging paths.

### Command + topics (contract)
- Sub: `hearth/<id>/ota/cmd` — **non-retained** JSON
  `{"version":"0.2.0","host":{"ip":[192,168,1,10],"port":8080,"path":"/dib/0.2.0/bundle"},"sha256":"…","size":N}`.
  IP is numeric (no DNS). Version guard makes re-delivery a no-op.
- Pub: `hearth/<id>/ota/state` — retained progress/result.

---

## Failure handling & watchdog interplay
- Download fully on **core1**, chunked with `_beat()` between reads/flash writes so
  `CORE1_STALL_MS` (6 s) is never exceeded; core0 input loop keeps classifying
  (gestures buffer in the bounded queue; OK if some drop during the update).
- Any download/verify failure → restore CH9120 to broker, publish `error`, keep
  running the current version. No swap unless verify passes.
- Power loss mid-download → inactive slot is garbage but `active` pointer is
  untouched → next boot runs the current version normally.
- Power loss after swap but before confirm → `pending` + `tries` gate auto-reverts
  if the new build can't prove itself.
- Loader (`boot.py`) and `site.json` are never in a bundle → unbrickable boot path
  and preserved identity/credentials.

## Out of scope (deferred)
- **gzip/deflate of the bundle.** `.mpy` already removes most of the bytes; gzipping the
  `.mpy` bundle would shave a further ~40% but requires reworking the loss-tolerant
  `OtaReceiver`, the streaming per-file `apply_bundle_file`, and the whole-bundle-sha
  domain (decompress before parse). Revisit only if `.mpy` alone isn't small enough.
- MicroPython interpreter / full UF2 OTA (needs a custom bootloader; physical BOOTSEL
  only for now).
- TLS for the HTTP pull (LAN, plaintext; sha256 over MQTT command gives integrity,
  not confidentiality). Note as a security caveat.

## Verification
- **Host unit tests** (CPython): new `mqtt_packets` SUBSCRIBE/SUBACK/PUBLISH parsers;
  bundle build + manifest/sha256 round-trip; loader slot/rollback state-machine logic
  (factor the pure decision fn so it tests without `machine`); LED `updating` state.
  `python3 -m py_compile src/*.py` + run all `tests/test_*.py`.
- **On hardware** (HW-VERIFY): trigger an OTA from `ota_publish.py`; watch
  `…/ota/state` go downloading→applying→idle and the device come back on the new
  `version`; confirm CH9120 retarget+restore works and the broker session recovers.
- **Rollback drills**: (a) ship a bundle whose `main` raises on import → board
  auto-reverts after `OTA_MAX_BOOT_TRIES`; (b) yank power mid-download → board boots
  current version; (c) ship a build that connects but never goes healthy → reverts
  after the confirm window. Validate WDT isn't tripped during a normal download.

## Open items to confirm on hardware
- CH9120 mid-run retarget reliably re-establishes the TCP client both to the file
  server and back to the broker (extends the existing `TCPCS`/reconnect HW-VERIFY).
- littlefs free space / write throughput for a full slot during download while
  beating; tune `OTA_HTTP_CHUNK`.
- Whether the file server must use HTTP/1.0 + `Connection: close` for the minimal
  reader to cleanly detect end-of-body.
