# Home Assistant assets for the Hearth

The gateway is consumed in Home Assistant by the first-party **OSELIA custom integration**
(its own repo, **[vmyronovych/oselia-hearth-di16g-ha](https://github.com/vmyronovych/oselia-hearth-di16g-ha)**,
HACS-installable). It owns the device + entities (so the gateway appears under OSELIA) and
adds a native firmware `update` (OTA) entity. The firmware default (`HA_INTEGRATION =
"oselia"`) **skips** publishing MQTT discovery; the `oselia` provisioning tool always
provisions this mode. Design contract: `INTEGRATION_SPEC.md`.

> A legacy `"mqtt"` mode (firmware publishes HA MQTT discovery; device appears under HA's
> MQTT integration) still exists in the firmware for a hand-set `site.json`, but the tool no
> longer provisions it.

## What the integration registers

- **Device** "Hearth" with `hw_version` / `serial_number` / origin.
- **Inputs** — one HA `event` entity per input (`event.hearth_board<b>_input<p>`),
  event types `single` / `double` / `long`. Also available as device-automation triggers.
- **Diagnostics** — uptime, IP, free memory, die temperature, boards online, board
  addresses, reconnects, dropped events, Ethernet link, last input, last log.
- **Controls** — Restart / Identify buttons, live-tunable timings + log level, firmware OTA.

## `dashboards/oselia-hearth.example.yaml`

A ready Lovelace **Sections** dashboard for one gateway (status + per-board inputs +
controls), as an example. The host tool renders one for *your* device id with:

```bash
oselia dashboard render --id <6hex> --boards N > oselia-hearth.yaml
```

Then paste it into Home Assistant: new dashboard → **Edit → raw configuration editor** (or a
YAML-mode dashboard). The tool does **not** push dashboards to HA — render and upload by hand.

## `blueprints/automation/oselia/dib_switch.yaml`

Automation blueprint: **map one switch input → actions per gesture**. Pick the input's
`event` entity, then drop in actions for single / double / long. Install by copying to
`<config>/blueprints/automation/oselia/` (or **Settings → Automations → Blueprints →
Import** with the `source_url`). Verified end-to-end on HA: each gesture routes to its own
action branch.

## Firmware OTA feed

The OSELIA integration carries the firmware release feed, configured in HA (OSELIA →
**Configure** → *Firmware release feed URL*, + a GitHub token for a private repo). Set it
once and every gateway updates from the HA UI. See `../firmware/docs/releasing.md`.
