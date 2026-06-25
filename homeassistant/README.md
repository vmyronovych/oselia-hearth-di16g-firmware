# Home Assistant assets for the Hearth

The gateway integrates with Home Assistant in one of two modes (firmware
`HA_INTEGRATION`, set per unit by `provision.py`):

- **`mqtt`** (default) — the gateway integrates over **MQTT discovery**: plug it in,
  point it at the broker (see `../provisioning`), and the device plus all its entities
  appear automatically under HA's MQTT integration. The files in this folder add the
  optional "nice UX" layer for this mode.
- **`oselia`** (`provision.py --oselia`) — the first-party **OSELIA custom integration**
  (its own repo, **[vmyronovych/oselia-hearth-di16g](https://github.com/vmyronovych/oselia-hearth-di16g)**,
  HACS-installable) owns the entities, so the device appears under OSELIA, not MQTT, and
  gains a native firmware `update` (OTA) entity. The firmware then skips publishing MQTT
  discovery. Design contract: `INTEGRATION_SPEC.md`.

The rest of this doc describes the **`mqtt`**-mode assets.

## What appears automatically (no setup)

- **Device** "Hearth" with `hw_version` / `serial_number` / origin.
- **Inputs** — one HA `event` entity per input (`event.hearth_board<b>_input<p>`),
  event types `single` / `double` / `long`. Also published as device-automation
  triggers (`INPUT_DISCOVERY` on the firmware selects `event` / `trigger` / `both`).
- **Diagnostics** — uptime, IP, free memory, die temperature, boards online, board
  addresses, reconnects, dropped events, Ethernet link, last input, last log.

## `blueprints/automation/oselia/dib_switch.yaml`

Automation blueprint: **map one switch input → actions per gesture**. Pick the input's
`event` entity, then drop in actions for single / double / long. Install by copying to
`<config>/blueprints/automation/oselia/` (or **Settings → Automations → Blueprints →
Import** with the `source_url`). Verified end-to-end on HA: each gesture routes to its
own action branch.

## `dashboards/hearth.yaml`

A ready Lovelace dashboard (Switches + Diagnostics views). Portable for a single
gateway; see the header for multi-gateway notes.

## Provisioning auto-install

`provision.py --ha-setup` sets HA up automatically — no manual HA steps. By default
(the OSELIA integration) it adds/updates the integration, sets the firmware release
feed, and (re)builds the multi-gateway **`/oselia-hearth`** dashboard (one `gw-<id>`
Sections view per gateway), reusing `dashboards/generate.py` so the wizard
and a manual `generate.py` run produce the identical dashboard. The legacy `--mqtt`
path instead adds HA's MQTT integration + this blueprint (no curated dashboard). Pushing
the dashboard/blueprint uses the HA **WebSocket API** (REST can't install those) with a
long-lived token (`--ha-token`, `$OSELIA_HA_TOKEN`, or `~/.config/oselia/ha_token`); see
`../provisioning/PROVISIONING_SPEC.md §6.1`. Verified end-to-end against HA 2025.11.
