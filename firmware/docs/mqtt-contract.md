# MQTT / protocol contract — Hearth ↔ Home Assistant

**Canonical wire contract.** The firmware is the source of truth for every topic,
payload, and schema below (it is the emitter). `spec.md §5` describes *how* the firmware
builds and gates these; the HA integration (`../../homeassistant/INTEGRATION_SPEC.md`)
owns only the **HA-entity mapping** — which entity each topic drives — and links here for
the wire format. When this and any other doc disagree on the wire, **this file wins.**

## Topic derivation

All topics derive from a configurable base. Prefixes come from `config.py`: `BASE_TOPIC`
(default `hearth`) and `DISCOVERY_PREFIX` (default `homeassistant`).

- `base = <BASE_TOPIC>/<device_id>`, e.g. `hearth/AABBCC`.
- `<device_id>` = stable id from the RP2040 unique ID (last 6 hex), or `DEVICE_ID` from
  `config.py` if set.
- `<b>` = board `1..8` (chip position in the resolved `MCP_ADDRESSES`); `<p>` = pin
  `1..16`. Only *advertised* boards count (with `MCP_AUTODISCOVER`, the chips that
  actually responded). Global input index = `(b-1)*16 + p`; up to 128 inputs.

## Topics

| Topic | Dir | Retain | Payload | HA entity (owned by the integration) |
|---|---|---|---|---|
| `…/status` | dev→HA | yes | `online` / `offline` (LWT) | availability for all entities |
| `…/board<b>/input<p>/action` | dev→HA | no | `single` / `double` / `long` | `event` per input |
| `…/cfg` | dev→HA | yes | `{long_ms,double_gap_ms,debounce_ms,log_level}` | state for the numbers + select |
| `…/diag/state` | dev→HA | yes | structured blob ([schema](#diagstate-schema)) | Diagnostics sensor + per-board MCP entities + counters |
| `…/diag/event` | dev→HA | **no** | one fault record ([schema](#diagstate-schema)) | fault `event` (HA logbook timeline) |
| `…/diag/log` | dev→HA | yes | `{line,level,ts}` | "Last log" diagnostic sensor |
| `…/cmd/reboot` `…/cmd/identify` | HA→dev | no | `PRESS` | `button` (restart / identify) |
| `…/cmd/maintenance` | HA→dev | no | `PRESS` | (no entity — provisioning quiesce, sent by `oselia`) |
| `…/cmd/long_ms` `…/cmd/double_gap_ms` `…/cmd/debounce_ms` | HA→dev | no | int (ms) | `number` (config) |
| `…/cmd/log_level` | HA→dev | no | `ERROR`/`WARN`/`INFO`/`DEBUG` | `select` (config) |
| `…/ota/cmd` | HA→dev | no | OTA command JSON ([below](#ota-topics)) | (driven by the `update` entity) |
| `…/ota/state` | dev→HA | yes | `{stage,percent,running_version,target_version,error}` | `update` entity progress/state |
| `<DISCOVERY_PREFIX>/<component>/<id>/…/config` | dev→HA | yes | discovery JSON ([below](#discovery)) | registers all of the above |

- LWT (`…/status` → `offline`) is registered in CONNECT so HA marks the device
  unavailable on disconnect.
- `<component>` covers `device_automation` (triggers), `event` (per-input), `sensor` /
  `binary_sensor` (diagnostics + Last log), `button` (restart/identify), `number`
  (timings), `select` (log level), `update` (OTA).

## Discovery

One config message per **(board × pin × gesture)** = `n_boards × 16 × 3` (up to
**8 × 16 × 3 = 384** at the 8-board max; e.g. 48 for one board), published with a small
inter-message settle so the CH9120 keeps up. All discovery configs are **retained** so HA
repopulates after a restart.

**Device-automation trigger** — example for board 2, pin 5, single press:

```json
{
  "automation_type": "trigger",
  "type": "button_short_press",
  "subtype": "board2_input5",
  "topic": "hearth/AABBCC/board2/input5/action",
  "payload": "single",
  "device": {
    "identifiers": ["hearth_AABBCC"],
    "name": "Hearth", "model": "Hearth (DI16-G)",
    "manufacturer": "OSELIA", "sw_version": "0.1.0"
  }
}
```

Gesture → HA trigger `type` / `payload`:

| Gesture | `type` | `payload` |
|---------|--------|-----------|
| single  | `button_short_press`  | `single` |
| double  | `button_double_press` | `double` |
| long    | `button_long_press`   | `long`   |

`subtype` = `board<b>_input<p>` (overridable via `INPUT_NAME_OVERRIDES`).

**`event` entities (`INPUT_DISCOVERY`)** — each input can also (or instead) publish a
modern HA **`event` entity** (`<DISCOVERY_PREFIX>/event/<id>/b<b>_in<p>/config`,
`device_class: button`, `event_types: [single, double, long]`), reusing the same
non-retained action topic with a `value_template` that wraps the plain payload into
`{"event_type": …}`. `INPUT_DISCOVERY` = `event` / `trigger` / `both` (default `both`).
The shipped blueprint (`homeassistant/blueprints/.../dib_switch.yaml`) targets the event
entities.

Every config carries an `origin` block and an enriched `device` (`hw_version`,
`serial_number`); diagnostic entities set `expire_after = 3 × DIAG_INTERVAL_S`.

**Device identity for `device_info`:** `manufacturer=OSELIA`, `model="Hearth (DI16-G)"`,
`hw_version`, `serial_number=<device_id>`, `sw_version` from `diag/state.fw`,
`configuration_url` → the gateway IP from `diag/state.ip`.

## `diag/state` schema

Retained — the canonical, exportable root-cause artifact. All fields additive since
fw 0.7.0; older readers that use only the original keys keep working.

```jsonc
{
  "fw": "0.7.0", "hw": "DI16-G", "uptime_s": 5400, "ip": "192.168.1.200",
  "reset_cause": "wdt",            // rp2: power_on | wdt | unknown ("wdt" also = any machine.reset())
  "health": "mcp_fault",           // ok | degraded | mcp_fault | net_fault
  "eth": true, "mqtt": true,
  "boards": 5,                     // resolved board count (= boards_total; input entities for all)
  "boards_total": 5, "boards_ok": 4,
  "board_addrs": ["0x20", "0x21", "..."],
  "mcp": [                         // one entry per board, in board order
    {"board": 1, "addr": "0x20", "ok": true,  "code": "",        "detail": "",
     "fails": 0,  "last_ok_s": 1,   "recoveries": 0},
    {"board": 2, "addr": "0x21", "ok": false, "code": "i2c_eio", "detail": "OSError 5 read",
     "fails": 12, "last_ok_s": 480, "recoveries": 3}
  ],
  "counters": {"bus_recoveries": 4, "mcp_resets": 2, "reconnects": 1, "dropped": 0},
  "last_fault": {"ts": 5212, "component": "mcp", "code": "i2c_eio",
                 "detail": "OSError 5 read", "board": 2},
  "recent": [ /* up to DIAG_FAULT_RING fault records, newest last (the timeline) */ ],
  "mem_free": 41200, "temp_c": 44.1,
  // back-compat fields retained:
  "reconnects": 1, "dropped": 0, "last": "b1/in3 single"
}
```

`diag/event` payload is a single fault record: `{ts, component, code, detail[, board]}`,
published the instant a fault transitions (real-time HA logbook timeline rather than only
the latest retained snapshot). `diag/state` is also republished immediately on any
health/fault change, still queue-gated.

`temp_c` is the RP2040 die temp (ADC ch4) — a coarse trend signal, reported as a positive
**magnitude**, not calibrated (this board's ADC offset makes the raw formula read low).

### Stable error-code taxonomy

`code` values (greppable to firmware `src/mcp_health.py`): `i2c_eio`, `i2c_timeout`,
`mcp_absent`, `mcp_init_fail`, `bus_recovered`, `mcp_reset`, `eth_link_lost`,
`mqtt_disconnect`, `mqtt_connack_refused`, `ota_*`. The raw errno/exception text rides in
`detail`. (Input is pure-polling; there is no INT, so no `int_stuck`.)

The integration should create input entities for all `boards` and surface per-board
health from `mcp[]`, so a single down board never hides the others' inputs.

## OTA topics

See `ota.md` for the mechanism (slots, boot-confirm, auto-revert). Wire contract:

- **`…/ota/cmd`** — HA→dev, **non-retained** JSON:
  `{"version":"0.2.0","host":{"ip":[192,168,1,10],"port":8080,"path":"/dib/0.2.0/bundle"},"sha256":"…","size":N}`.
  IP is numeric (no DNS); a version guard makes re-delivery a no-op.
- **`…/ota/state`** — dev→HA, retained progress/result
  (`{stage,percent,running_version,target_version,error}`), reflected by the HA `update`
  entity.
