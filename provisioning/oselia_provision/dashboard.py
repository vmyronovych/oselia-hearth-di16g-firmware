"""Render the OSELIA Hearth Lovelace dashboard as a STATIC YAML document you paste into
Home Assistant by hand -- no live HA connection, no WebSocket push.

The old tool built this from HA's live entity registry and pushed it over the WS API.
That coupling is gone: this produces the same Sections view purely from a device id +
board/input counts, because the firmware's entity ids are deterministic
(`<domain>.hearth_<id>_<role>`). Paste the output into a new dashboard via HA's
"Edit dashboard -> raw configuration editor", or save it as a YAML-mode dashboard.

If the firmware ever renames an entity, update the role table below.
"""
import base64

import yaml

from .paths import LOGO_SVG

TITLE = "OSELIA Hearth"

# Status block diagnostic sensors, in display order: (entity-id suffix, label).
_STATUS_SENSORS = [
    ("sensor.{p}_ip_address", "IP address"),
    ("sensor.{p}_uptime", "Uptime"),
    ("sensor.{p}_temperature", "Temperature"),
    ("sensor.{p}_input_boards", "Input boards"),
    ("sensor.{p}_input_boards_responding", "Boards responding"),
    ("sensor.{p}_last_reset_cause", "Last reset"),
    ("sensor.{p}_reconnects", "Reconnects"),
    ("sensor.{p}_dropped_events", "Dropped"),
    ("sensor.{p}_i2c_bus_recoveries", "I²C bus recoveries"),
    ("sensor.{p}_mcp_resets", "MCP resets"),
]


def _tile(entity, name, **extra):
    d = {"type": "tile", "entity": entity, "name": name}
    d.update(extra)
    return d


def _heading(text, icon=None):
    c = {"type": "heading", "heading": text, "heading_style": "title"}
    if icon:
        c["icon"] = icon
    return c


def _logo_data_uri():
    try:
        with open(LOGO_SVG, "rb") as f:
            return "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return None


def _feed_error_note(fw_entity):
    expr = "state_attr('%s', 'release_feed_error')" % fw_entity
    return {
        "type": "markdown",
        "show_empty": False,
        "content": ("{% set e = " + expr + " %}"
                    "{% if e %}### ⚠️ Firmware updates unavailable\n\n{{ e }}{% endif %}"),
    }


def _fault_timeline(diag_entity):
    d = diag_entity
    content = (
        "{% set lf = state_attr('" + d + "', 'last_fault') %}"
        "{% if lf %}**Now:** `{{ lf.code }}`"
        "{% if lf.get('board') is not none %} · board {{ lf.board }}{% endif %}"
        " — {{ lf.detail }}\n\n{% endif %}"
        "{% set r = state_attr('" + d + "', 'recent') or [] %}"
        "{% if r %}{% for f in (r[-12:] | reverse) %}"
        "- `{{ f.code }}`{% if f.get('board') is not none %} (b{{ f.board }}){% endif %}"
        " — {{ f.detail }}\n"
        "{% endfor %}{% else %}_No faults recorded._{% endif %}"
    )
    return {"type": "markdown", "title": "Recent faults", "content": content}


def build_view(device_id, friendly=None, boards=1, inputs_per_board=16, logo=True):
    """Build one Sections view dict for a single gateway. `device_id` is the 6-hex unit id
    (any case); entity ids are lowercased to match HA's slugging."""
    gid = device_id.lower()
    p = "hearth_%s" % gid                       # entity-id prefix
    friendly = friendly or ("Hearth %s" % device_id)

    diag = "sensor.%s_diagnostics" % p
    fw = "update.%s_firmware" % p

    status = []
    if logo:
        uri = _logo_data_uri()
        if uri:
            status.append({"type": "picture", "image": uri,
                           "tap_action": {"action": "none"}, "alt_text": TITLE})
    status.append(_heading("Status", "mdi:heart-pulse"))
    status.append(_tile(diag, "Health"))
    status.append(_tile(fw, "Firmware"))
    status.append(_feed_error_note(fw))
    status.append(_tile("binary_sensor.%s_ethernet_link" % p, "Ethernet"))
    for tmpl, label in _STATUS_SENSORS:
        status.append(_tile(tmpl.format(p=p), label))
    status.append(_fault_timeline(diag))

    sections = [{"type": "grid", "cards": status}]

    for b in range(1, boards + 1):
        cards = [_heading("Wall switches · board %d" % b, "mdi:light-switch"),
                 _tile("binary_sensor.%s_board_%d_mcp" % (p, b), "MCP chip"),
                 _tile("sensor.%s_board_%d_mcp_error" % (p, b), "MCP last error")]
        for pin in range(1, inputs_per_board + 1):
            cards.append(_tile("event.%s_board_%d_input_%d" % (p, b, pin),
                               "Input %d" % pin, icon="mdi:gesture-tap-button"))
        sections.append({"type": "grid", "cards": cards})

    controls = [
        _heading("Controls", "mdi:tune-vertical"),
        _tile("button.%s_restart" % p, "Restart"),
        _tile("button.%s_identify" % p, "Identify"),
        {"type": "entities", "title": "Gesture tuning", "show_header_toggle": False,
         "entities": [
             {"entity": "number.%s_long_press_time" % p, "name": "Long press"},
             {"entity": "number.%s_double_tap_window" % p, "name": "Double-tap window"},
             {"entity": "number.%s_debounce_time" % p, "name": "Debounce"},
             {"entity": "select.%s_log_level" % p, "name": "Log level"},
         ]},
        {"type": "entities", "title": "Topology", "show_header_toggle": False,
         "entities": [
             {"entity": "sensor.%s_board_addresses" % p, "name": "Board addresses"},
             {"entity": "sensor.%s_last_input" % p, "name": "Last input"},
         ]},
    ]
    sections.append({"type": "grid", "cards": controls})

    return {"type": "sections", "title": friendly, "path": "gw-%s" % gid,
            "icon": "mdi:home-lightning-bolt", "max_columns": 3, "sections": sections}


def build_config(device_id, friendly=None, boards=1, inputs_per_board=16, logo=True):
    """Full dashboard config dict ({title, views:[view]}) for one gateway."""
    view = build_view(device_id, friendly, boards, inputs_per_board, logo)
    return {"title": TITLE, "views": [view]}


def render_yaml(device_id, friendly=None, boards=1, inputs_per_board=16, logo=True):
    """The dashboard as a YAML string ready to paste into HA's raw config editor."""
    cfg = build_config(device_id, friendly, boards, inputs_per_board, logo)
    header = ("# OSELIA Hearth dashboard -- generated by `oselia dashboard render`.\n"
              "# Paste into Home Assistant: new dashboard -> Edit -> raw configuration\n"
              "# editor (or a YAML-mode dashboard). Adjust entity ids if you renamed the\n"
              "# device. Logo is embedded as a self-contained data URI.\n")
    body = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False,
                          allow_unicode=True, width=100)
    return header + body
