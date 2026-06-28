# OSELIA Home Assistant Integration — Hearth gateway + OTA

Status: **implemented.** Design contract for a first-party Home
Assistant **custom integration** that adopts the Hearth gateway as its own branded
device (not under the generic "MQTT" integration) and hosts **OTA firmware updates**
via a native HA `update` entity. The integration itself now lives in its own public
repo, **[vmyronovych/oselia-hearth-di16g-ha](https://github.com/vmyronovych/oselia-hearth-di16g-ha)**
(HACS-installable); this doc is its design contract. Sits alongside `README.md` (the
current MQTT-discovery assets), `../firmware/SPEC.md` (firmware behaviour) and
`../firmware/OTA_SPEC.md` (the on-device slot/rollback core this reuses). Where
this and `OTA_SPEC.md` disagree on **on-device** behaviour, `OTA_SPEC.md` wins; this
doc owns the **HA-side** contract.

## Why this exists

1. **Branding / "market ready," not DIY.** With MQTT discovery the gateway is filed
   under the **MQTT** integration tile in Settings → Devices & Services. The *device*
   card already looks branded (manufacturer/model/serial via `device_block`), but the
   integration attribution can't be changed from MQTT discovery — there is no flag for
   it. Owning a domain (`oselia`) is the only way to get our own tile, logo, and a
   real "Add integration" config flow.
2. **OTA needs a home.** HA's native `update` entity (`UpdateEntity`) is the modern,
   in-brand way to surface "Update available → Install" with progress and release
   notes. A custom integration can own it fully: poll our release feed for the latest
   version, host the firmware bytes on HA's own HTTP server, and drive the install —
   superseding the "MQTT `update` via discovery" idea, which would still file under
   MQTT.

## Decisions (confirmed with user)

- **Custom integration**, domain proposed `oselia` (see Open decisions §A).
- **Own MQTT connection** (option A): the integration runs **its own MQTT client
  against the existing Mosquitto broker** — it does **not** depend on HA's `mqtt`
  integration. The device therefore appears **only** under OSELIA. The board is
  unchanged: it keeps publishing to the same broker; the integration is just the
  consumer. (A future evolution to a broker-less embedded TCP listener — option B in
  discussion — is explicitly out of scope for v1.)
- **Entities are owned by the integration** (defined in Python from the protocol
  contract below), **not** ingested from firmware discovery payloads. The firmware's
  *data* and *command* topics are the stable contract; the *discovery* role moves out
  of firmware into the integration. See §"Firmware impact".
- **Distribution: HACS** (custom repository) **+ a `home-assistant/brands` PR** for the
  logo/icon. HA-core inclusion is a possible later step, not a v1 goal.
- **OTA transport** stays the lower-risk **MQTT-chunked** path (no CH9120 retarget),
  reusing the **slot/boot-confirm/auto-revert core** from `OTA_SPEC.md` verbatim. The
  integration adds the HA-side trigger, progress, and (optionally) byte hosting.

---

## Architecture

```
custom_components/oselia/
  manifest.json        # domain "oselia"; iot_class local_push; NO "mqtt" dependency
                       #   (own client); dhcp discovery block; version; codeowners
  config_flow.py       # UI setup + DHCP discovery; collects broker IP/port/creds,
                       #   validates the MQTT connection, one entry per broker
  __init__.py          # set up/tear down the entry; start the OseliaClient
  client.py            # OseliaClient: owns the paho-mqtt connection, subscribes to
                       #   hearth/+/..., parses, and dispatches to per-device handlers
  coordinator.py       # per-gateway state + availability; reconnection/backoff
  entity.py            # OseliaEntity base: device_info from the gateway identity
  event.py sensor.py binary_sensor.py button.py number.py select.py
                       # the entities below, owned by THIS integration
  device_trigger.py    # per-input button_short/double/long_press device triggers
                       #   (HA's event domain provides none), fired off SIGNAL_ACTION
  update.py            # OseliaUpdateEntity: latest_version from release feed,
                       #   async_install -> OTA orchestration, progress, release notes
  ota.py               # bundle hosting (HA HTTP view) + MQTT-chunk publisher + the
                       #   ota/cmd / ota/state contract; drives the on-device A/B core
  const.py strings.json translations/en.json
```

### Connection & multi-gateway

One config entry per broker; one `OseliaClient` (single paho connection) discovers
**all** Hearth gateways on that broker by their retained topics. A gateway is keyed by
`device_id` (the `hearth/<device_id>/…` segment). Each becomes one HA device
(`identifiers=["hearth_<device_id>"]` — same identifier the firmware uses, so existing
registry history is preserved if a unit is migrated). The client maps inbound messages
→ the right device's entities; availability follows `hearth/<id>/status`. The `<id>`
segment is format-validated (`^[A-Za-z0-9_-]{1,64}$`) before a gateway is created, so a
stray publish to `hearth/<garbage>/…` (e.g. an id polluted with boot-serial text) is
dropped with a WARNING rather than spawning a phantom device + entities.

### Discovery of gateways (no firmware discovery payloads)

The integration learns a gateway exists from its **retained** `…/status=online` and
`…/diag/state`. It does **not** parse `homeassistant/.../config` messages. Entity set
is fixed by the protocol: events per input, the diagnostic sensors, the control
entities, and the update entity. Input count comes from `diag/state.boards` (×16) so
the integration creates exactly the live inputs.

---

## Protocol contract (firmware ↔ integration)

All under `BASE_TOPIC=hearth`, `base = hearth/<device_id>`.

> **Firmware ≥ 0.7.0** extends `diag/state` with structured per-MCP root-cause data
> and adds the `…/diag/event` fault stream (see schema below). All new fields are
> additive — older integration builds that read only the original keys keep working.

| Topic | Dir | Retain | Payload | HA entity (owned by `oselia`) |
|---|---|---|---|---|
| `…/status` | dev→HA | yes | `online`/`offline` | availability for all entities |
| `…/board<b>/input<p>/action` | dev→HA | no | `single`/`double`/`long` | `event` per input (≤128) |
| `…/cfg` | dev→HA | yes | `{long_ms,double_gap_ms,debounce_ms,log_level}` | state for the numbers + select |
| `…/diag/state` | dev→HA | yes | structured blob (below) | Diagnostics sensor + per-board MCP entities + counters + diagnostic sensors |
| `…/diag/event` | dev→HA | **no** | one fault record (below) | fault `event` entity (HA logbook timeline) |
| `…/cmd/reboot` `…/cmd/identify` | HA→dev | no | `PRESS` | `button` (restart / identify) |
| `…/cmd/long_ms` `…/cmd/double_gap_ms` `…/cmd/debounce_ms` | HA→dev | no | int (ms) | `number` (config) |
| `…/cmd/log_level` | HA→dev | no | `ERROR`/`WARN`/`INFO`/`DEBUG` | `select` (config) |
| `…/ota/cmd` | HA→dev | no | OTA command JSON (below) | (driven by the `update` entity) |
| `…/ota/state` | dev→HA | yes | `{stage,percent,running_version,target_version,error}` | `update` entity progress/state |

### `diag/state` schema (retained — the canonical, exportable root-cause artifact)

```jsonc
{
  "fw": "0.7.0", "hw": "DI16-G", "uptime_s": 5400, "ip": "192.168.1.200",
  "reset_cause": "wdt",            // power_on | wdt | soft | hard | deepsleep | unknown
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
  "counters": {"int_stuck": 7, "bus_recoveries": 4, "mcp_resets": 2,
               "reconnects": 1, "dropped": 0},
  "last_fault": {"ts": 5212, "component": "mcp", "code": "int_stuck",
                 "detail": "INT held >250ms", "board": 2},
  "recent": [ /* up to DIAG_FAULT_RING fault records, newest last (the timeline) */ ],
  "mem_free": 41200, "temp_c": 44.1,
  // back-compat fields retained: reconnects, dropped, last
  "reconnects": 1, "dropped": 0, "last": "b1/in3 single"
}
```

`diag/event` payload is a single fault record: `{ts, component, code, detail[, board]}`
(published the instant a fault transitions, so HA's logbook shows a real-time timeline
rather than only the latest retained snapshot).

**Stable error-code taxonomy** (`code` / fault `code`; greppable to firmware
`src/mcp_health.py`): `i2c_eio`, `i2c_timeout`, `mcp_absent`, `mcp_init_fail`,
`int_stuck`, `bus_recovered`, `mcp_reset`, `eth_link_lost`, `mqtt_disconnect`,
`mqtt_connack_refused`, `ota_*`. The raw errno/exception text rides in `detail`.

Input count still comes from `diag/state.boards` (×16). The integration should create
input entities for all `boards`, and surface per-board health from `mcp[]` (so a single
down board never hides the others' inputs).

Device identity for `device_info`: `manufacturer=OSELIA`, `model="Hearth (DI16-G)"`,
`hw_version`, `serial_number=<device_id>`, `sw_version` from `diag/state.fw`,
`configuration_url` → the gateway IP from `diag/state.ip`.

---

## OTA over the integration  —  IMPLEMENTED + HW-VERIFIED (2026-06-25)

`update.py` (`UpdateEntity`) + `ota.py` drive OTA from the HA UI: `installed_version`
from `diag/state.fw`, `latest_version` from the release-feed manifest (`CONF_RELEASE_URL`,
set via the **options flow**), progress straight off the device's retained `ota/state`.
A feed that can't be read raises `FeedError` with an installer-facing reason: the options
flow validates the URL/token on submit and rejects it inline, and at runtime the entity
records it as the `release_feed_error` attribute (+ a WARNING) instead of falling back to
"Up to date" — the common case being a **private** repo with no `CONF_GITHUB_TOKEN`. An
**unconfigured** feed (no `CONF_RELEASE_URL`) sets the same attribute with a "feed not
configured" message, so the dashboard renders a warning note (a self-hiding markdown card
under the Firmware tile — `show_empty: false` so it shows only when `release_feed_error` is
set; see `dashboards/generate.py:feed_error_note`) rather than a misleading "Up to date".
`async_install` downloads the bundle and `async_run_ota` streams it over the integration's
own broker connection — resending the QoS0 `cmd` until acked, pacing chunks to the CH9120
UART rate (`OTA_CHUNK_DELAY`), and resending chunks the device NAKs (`ota/nak` →
`SIGNAL_OTA_NAK`). Stage checks are **version-aware** (a stale retained `ota/state` from a
prior install is ignored). Build artifacts: `firmware/tools/ota_build.py` (bundle +
manifest). HW-verified: HA "Install" took board 893922 0.2.0→0.4.0 (download→verify→apply
→reboot→boot-confirm `idle`), update card cleared. Known follow-up: the firmware should
self-abort a stalled download (today a dead publisher leaves it NAKing until a reboot).

## OTA over the integration (original design notes)

Reuses `OTA_SPEC.md`'s on-device core unchanged: `/boot.py` loader, `/slots/{a,b}`,
`/ota/active`, the `pending`/`tries` boot-confirm + auto-revert, `site.json` never
touched. The integration replaces the ad-hoc host tooling (`ota_publish.py`) with the
HA-native flow:

1. **`update` entity.** `installed_version` = `diag/state.fw`. `latest_version` =
   newest release from our feed (GitHub Releases or a JSON manifest at a configured
   URL), polled by the coordinator. HA shows the card when they differ; supports
   `INSTALL`, `PROGRESS`, `RELEASE_NOTES`.
2. **Install.** `async_install` (a) ensures the bundle for the target version is
   available to the LAN — served by an HA **HTTP view** the integration registers
   (`/api/oselia/firmware/<version>/bundle`), or fetched from the release URL — and
   (b) publishes the OTA command to `…/ota/cmd`:
   `{"version":"0.2.0","host":{"ip":[…],"port":8123,"path":"/api/oselia/firmware/0.2.0/bundle"},"sha256":"…","size":N}`
   (numeric IP; CH9120 has no DNS). For the **MQTT-chunked** transport the integration
   instead streams the bundle as ordered chunks on `…/ota/data` and the command names
   chunk count + per-chunk + whole-bundle sha256.
3. **Progress / result.** The device publishes `…/ota/state` retained
   (`downloading → applying → idle`, with `percent`); the entity maps `stage`→
   `in_progress`, `percent`→`update_percentage`, and clears when `fw` reaches target.
   Bad build → on-device auto-revert (per `OTA_SPEC.md`); the entity simply reflects
   `fw` snapping back and surfaces `error`.

Versioning/compat: the integration declares a min/max supported firmware protocol
version (a field added to `diag/state`, default-tolerant) so a HACS update can refuse
to drive an incompatible board rather than mis-render it.

---

## Config flow & DHCP discovery

- **Manual**: "Add integration → OSELIA" → broker IP/port/username/password →
  validate by connecting and waiting for one retained `hearth/+/status`. One entry per
  broker.
- **DHCP discovery** (`manifest.json` `dhcp` block): match the gateway by MAC OUI
  and/or DHCP hostname so HA proposes "OSELIA gateway discovered." The CH9120 can't do
  mDNS, so DHCP is the zero-firmware-change discovery path; the flow still needs broker
  creds (the device itself isn't an HTTP endpoint).
- **Options flow**: release-feed URL/channel (stable/beta), OTA enable, poll interval.

---

## Distribution

- **HACS custom repo**: `hacs.json` + semver release tags; later apply to the HACS
  default store (needs the brands PR + repo hygiene).
- **`home-assistant/brands` PR**: `custom_integrations/oselia/{icon,logo}.png` so the
  tile shows the OSELIA mark instead of the default puzzle piece — the single biggest
  "not DIY" lever. **Assets prepared** in `brands/custom_integrations/oselia/` (icon +
  @2x + logo + @2x, rasterised from `homeassistant/hearth_logo.svg`); see
  `brands/README.md` to submit. (HA serves brand images from the central CDN only — no
  local override — so the device-page logo appears once the PR merges.)
- **Dashboard**: `dashboards/generate.py` builds the per-gateway Sections dashboard
  `/oselia-hearth` (logo + status + inputs-by-board + controls) from the live registry
  and pushes it via the WS API (one `gw-<id>` view per OSELIA gateway). Its
  `build_config` / `push_config` are **reused by the provisioning wizard**
  (`ha_setup.ensure_oselia_dashboard`), so `provision.py` (OSELIA mode, the default) and
  a manual `generate.py` run produce the identical dashboard. `dashboards/oselia.yaml`
  is a reference snapshot. The in-app logo is embedded as a data URI (no CDN needed).
- **Quality scale**: target Silver (config flow, tests, unique ids, availability) for
  the credibility badge.

---

## Migration from today's MQTT-discovery setup

This integration **supersedes** firmware-published discovery and most of
`provisioning/ha_setup.py`:

- **Firmware** *(implemented)*: a `HA_INTEGRATION` config flag (`"mqtt"` default, or
  `"oselia"`) gates whether the firmware publishes `homeassistant/.../config` payloads.
  In `"oselia"` mode `net_task` skips all `publish_*_discovery` calls but still
  subscribes to `…/cmd/#` and seeds `…/cfg`, so commands and the number/select state
  work unchanged. The data/command topics stay; `ha_discovery.py`'s *topic builders*
  remain the contract, its *discovery publishers* are simply not called. Set per unit
  OSELIA mode is now the **default**; pass `provision.py --mqtt` for the legacy path
  (writes `site.json` `"ha_integration":"oselia"` for OSELIA, omits it for MQTT).
  **Clear the retained discovery topics** on migration so HA's MQTT integration drops
  the duplicate device — `provision.py --uninstall-ha` already does exactly this clear.
- **Provisioning**: `provision.py --ha-setup` in OSELIA mode adds the OSELIA config
  flow + entities and builds the `/oselia-hearth` dashboard (`ensure_oselia_dashboard`
  reusing `dashboards/generate.py`); it does not add the HA MQTT integration. The legacy
  `--mqtt` path still adds the MQTT integration + blueprint but no curated dashboard. The
  per-unit `/hearth-di16g` dashboard builder has been removed from `ha_setup.py`.
- **Docs**: update `homeassistant/README.md` and `firmware/CLAUDE.md`'s "Implementation
  status" to describe the integration as the HA front door.

---

## Firmware impact (minimal)

- No wire-format change; `mqtt_packets.py` and the broker session are untouched.
- **Done:** `HA_INTEGRATION` flag (`config.py` + `site.json` overlay) gates discovery
  publishing; `net_task` skips `publish_*_discovery` in `"oselia"` mode while keeping
  the command subscribe + `…/cfg` seed. Provisioning sets it via `--oselia`.
- Add OTA on-device pieces per `OTA_SPEC.md` (loader, slots, `ota.py`, `…/ota/*`).
- Add a protocol-version field to `diag/state`.

## Verification

- **Integration unit tests** (pytest-homeassistant-custom-component): config-flow
  happy/duplicate/cannot-connect; entity creation from a synthesized `diag/state`;
  command round-trip publishes the right `…/cmd/*`; `update` entity version compare +
  `async_install` publishes a well-formed `…/ota/cmd`; progress mapping from
  `…/ota/state`.
- **On hardware** (HW-VERIFY): real gateway appears under OSELIA (not MQTT) with logo;
  inputs fire events; controls write back; trigger an OTA from the update card and watch
  `…/ota/state` go downloading→applying→idle and `fw` advance; rollback drills per
  `OTA_SPEC.md`.

## Open decisions

- **A. Domain/brand name.** `oselia` (company; future-proof for more products) vs
  `hearth` (current product, matches `BASE_TOPIC`/README everywhere). Recommend
  `oselia` with Hearth as the device model. Affects the brands PR and entity_id prefix.
- **B. Bundle delivery for OTA**: HA HTTP-view host (HTTP-pull, needs CH9120 retarget on
  device) vs MQTT-chunk (no retarget). Recommend MQTT-chunk for v1 per `OTA_SPEC.md`
  risk analysis; revisit if throughput is inadequate.
- **C. Release feed**: GitHub Releases vs a self-hosted JSON manifest. Recommend GitHub
  Releases (free hosting, signed tags, release notes for `RELEASE_NOTES`).
