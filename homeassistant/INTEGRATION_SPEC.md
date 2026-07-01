# OSELIA Home Assistant Integration — Hearth gateway + OTA

Status: **implemented.** Design contract for a first-party Home
Assistant **custom integration** that adopts the Hearth gateway as its own branded
device (not under the generic "MQTT" integration) and hosts **OTA firmware updates**
via a native HA `update` entity. The integration itself now lives in its own public
repo, **[vmyronovych/oselia-hearth-di16g-ha](https://github.com/vmyronovych/oselia-hearth-di16g-ha)**
(HACS-installable); this doc is its design contract. Sits alongside `README.md` (the
current MQTT-discovery assets), `../firmware/docs/spec.md` (firmware behaviour) and
`../firmware/docs/ota.md` (the on-device slot/rollback core this reuses). Where
this and `../firmware/docs/ota.md` disagree on **on-device** behaviour, `../firmware/docs/ota.md` wins; this
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
  reusing the **slot/boot-confirm/auto-revert core** from `../firmware/docs/ota.md` verbatim. The
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

> **The wire contract is owned by the firmware.** Every topic, payload, discovery
> config, the full `diag/state` schema, and the error-code taxonomy live in
> [`../firmware/docs/mqtt-contract.md`](../firmware/docs/mqtt-contract.md) — the firmware
> is the emitter, so it is the source of truth. **This section owns only the HA-entity
> mapping** (which entity the integration builds from each topic); read the contract for
> the on-the-wire shapes.

| Topic | HA entity the integration builds |
|---|---|
| `…/status` | availability for all entities |
| `…/board<b>/input<p>/action` | `event` per input (≤128; count = `diag/state.boards` × 16) |
| `…/cfg` | state for the `number`s + `select` |
| `…/diag/state` | Diagnostics sensor + per-board MCP entities (from `mcp[]`) + counters |
| `…/diag/event` | fault `event` entity (HA logbook timeline) |
| `…/diag/log` | "Last log" diagnostic sensor |
| `…/cmd/reboot` `…/cmd/identify` | `button` (restart / identify) |
| `…/cmd/long_ms` `…/cmd/double_gap_ms` `…/cmd/debounce_ms` | `number` (config) |
| `…/cmd/log_level` | `select` (config) |
| `…/ota/cmd` `…/ota/state` | `update` entity (drives the command, reflects progress) |

Integration-specific consumption rules: create input entities for **all** `boards` and
surface per-board health from `mcp[]`, so a single down board never hides the others'
inputs. `device_info`: `manufacturer=OSELIA`, `model="Hearth (DI16-G)"`, `hw_version`,
`serial_number=<device_id>`, `sw_version` from `diag/state.fw`, `configuration_url` → the
gateway IP from `diag/state.ip`. (fw ≥ 0.7.0 `diag/state` fields are additive — older
integration builds that read only the original keys keep working.)

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
set; see the `feed_error_note` card in `provisioning/oselia_provision/dashboard.py`) rather
than a misleading "Up to date".
`async_install` downloads the bundle and `async_run_ota` streams it over the integration's
own broker connection — resending the QoS0 `cmd` until acked, pacing chunks to the CH9120
UART rate (`OTA_CHUNK_DELAY`), and resending chunks the device NAKs (`ota/nak` →
`SIGNAL_OTA_NAK`). Stage checks are **version-aware** (a stale retained `ota/state` from a
prior install is ignored). Build artifacts: `firmware/tools/ota_build.py` (bundle +
manifest). HW-verified: HA "Install" took board 893922 0.2.0→0.4.0 (download→verify→apply
→reboot→boot-confirm `idle`), update card cleared. Known follow-up: the firmware should
self-abort a stalled download (today a dead publisher leaves it NAKing until a reboot).

## OTA over the integration (original design notes)

Reuses `../firmware/docs/ota.md`'s on-device core unchanged: `/main.py` loader, `/slots/{a,b}`,
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
   Bad build → on-device auto-revert (per `../firmware/docs/ota.md`); the entity simply reflects
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
- **Dashboard**: the `oselia` host tool renders a per-gateway Sections dashboard
  `/oselia-hearth` (logo + status + inputs-by-board + controls) as **YAML for manual
  upload** — `oselia dashboard render --id <id>` (entity ids are deterministic, so no live
  HA is needed). `dashboards/oselia-hearth.example.yaml` is a committed reference. The logo
  is embedded as a data URI (no CDN needed). The tool does not push dashboards over the API.
- **Quality scale**: target Silver (config flow, tests, unique ids, availability) for
  the credibility badge.

---

## Migration from today's MQTT-discovery setup

This integration **supersedes** firmware-published discovery:

- **Firmware** *(implemented)*: a `HA_INTEGRATION` config flag (`"oselia"` default, or the
  legacy `"mqtt"`) gates whether the firmware publishes `homeassistant/.../config` payloads.
  In `"oselia"` mode `net_task` skips all `publish_*_discovery` calls but still
  subscribes to `…/cmd/#` and seeds `…/cfg`, so commands and the number/select state
  work unchanged. The data/command topics stay; `ha_discovery.py`'s *topic builders*
  remain the contract, its *discovery publishers* are simply not called. The `oselia` tool
  always writes `site.json` `"ha_integration":"oselia"`. If migrating a unit that previously
  ran MQTT-discovery mode, **clear its retained discovery topics** so HA's MQTT integration
  drops the duplicate device (`oselia board exec` an empty-retained publish, or clear via
  `mosquitto_pub`).
- **Provisioning**: the host tool flashes/provisions the unit (always OSELIA mode) and does
  **not** touch Home Assistant. The OSELIA integration is installed via HACS and configured
  in HA (broker + firmware release feed); the dashboard is rendered with `oselia dashboard
  render` and uploaded by hand.
- **Docs**: update `homeassistant/README.md` and `firmware/CLAUDE.md`'s "Implementation
  status" to describe the integration as the HA front door.

---

## Firmware impact (minimal)

- No wire-format change; `mqtt_packets.py` and the broker session are untouched.
- **Done:** `HA_INTEGRATION` flag (`config.py` + `site.json` overlay) gates discovery
  publishing; `net_task` skips `publish_*_discovery` in `"oselia"` mode while keeping
  the command subscribe + `…/cfg` seed. Provisioning sets it via `--oselia`.
- Add OTA on-device pieces per `../firmware/docs/ota.md` (loader, slots, `ota.py`, `…/ota/*`).
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
  `../firmware/docs/ota.md`.

## Open decisions

- **A. Domain/brand name.** `oselia` (company; future-proof for more products) vs
  `hearth` (current product, matches `BASE_TOPIC`/README everywhere). Recommend
  `oselia` with Hearth as the device model. Affects the brands PR and entity_id prefix.
- **B. Bundle delivery for OTA**: HA HTTP-view host (HTTP-pull, needs CH9120 retarget on
  device) vs MQTT-chunk (no retarget). Recommend MQTT-chunk for v1 per `../firmware/docs/ota.md`
  risk analysis; revisit if throughput is inadequate.
- **C. Release feed**: GitHub Releases vs a self-hosted JSON manifest. Recommend GitHub
  Releases (free hosting, signed tags, release notes for `RELEASE_NOTES`).
