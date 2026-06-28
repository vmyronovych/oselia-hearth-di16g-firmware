#!/usr/bin/env python3
"""Generate the OSELIA Hearth Lovelace dashboard from the live entities.

Enumerates every OSELIA gateway (HA devices whose identifier is `hearth_<id>`),
reads their entities from the registry, and builds one Sections view per gateway
(logo + status incl. the structured Diagnostics health + recovery counters + a fault
logbook; inputs grouped by board, each board led by its MCP-chip health + last-error;
controls). Pushes it as the storage-mode dashboard `oselia-hearth` via the HA WebSocket
API -- the same mechanism the provisioning wizard uses (`provisioning/ha_setup.py`).

Robust to any number of gateways and boards; entity_ids come from the registry (not
constructed), so name-collision suffixes (_2, ...) are handled.

Usage:
    python3 generate.py [--ha-url http://localhost:8123] [--token <LLT>]
Token resolution mirrors provisioning: --token, then $OSELIA_HA_TOKEN, then
~/.config/oselia/ha_token.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_SVG = os.path.normpath(os.path.join(HERE, "..", "..", "hearth_logo.svg"))
URL_PATH = "oselia-hearth"
TITLE = "OSELIA Hearth"

# Diagnostic sensors shown in the Status block, in order: (unique-id key, label).
# Keys map to the firmware diag/state via the integration's `diag_<key>` entities.
STATUS_SENSORS = [
    ("ip", "IP address"),
    ("uptime", "Uptime"),
    ("temperature", "Temperature"),
    ("boards", "Input boards"),
    ("boards_ok", "Boards responding"),
    ("reset_cause", "Last reset"),
    ("reconnects", "Reconnects"),
    ("dropped", "Dropped"),
    ("bus_recoveries", "I²C bus recoveries"),
    ("mcp_resets", "MCP resets"),
]
_EVENT_RE = re.compile(r"_b(\d+)_in(\d+)_event$")
# Per-board MCP health entities (fw >= 0.7.0): connectivity + last-error, per board.
_MCP_RE = re.compile(r"_board(\d+)_mcp$")
_MCPERR_RE = re.compile(r"_board(\d+)_mcp_error$")


# ---------------------------------------------------------------------------
# minimal HA WebSocket client (stdlib only; mirrors ha_setup.py)
# ---------------------------------------------------------------------------
class HAWS:
    def __init__(self, host, port, token, use_tls=False):
        raw = socket.create_connection((host, port), timeout=10)
        if use_tls:
            import ssl
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.s = raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            (f"GET /api/websocket HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket"
             f"\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
             f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(1)
        self._id = 0
        self._recv()                              # auth_required
        self._send({"type": "auth", "access_token": token})
        if self._recv().get("type") != "auth_ok":
            raise SystemExit("HA rejected the token (auth failed)")

    def _send(self, obj):
        p = json.dumps(obj).encode(); h = bytearray([0x81]); n = len(p)
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126); h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127); h += struct.pack(">Q", n)
        m = os.urandom(4)
        self.s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

    def _frame(self):
        b0 = self.s.recv(1)[0]; fin = b0 & 0x80; n = self.s.recv(1)[0] & 0x7f
        if n == 126:
            n = struct.unpack(">H", self.s.recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.s.recv(8))[0]
        d = b""
        while len(d) < n:
            d += self.s.recv(n - len(d))
        return fin, d

    def _recv(self):
        fin, d = self._frame()
        while not fin:
            f2, d2 = self._frame(); d += d2; fin = f2
        return json.loads(d.decode())

    def call(self, type_, **kw):
        self._id += 1
        self._send({"id": self._id, "type": type_, **kw})
        while True:
            r = self._recv()
            if r.get("id") == self._id and r.get("type") == "result":
                if not r.get("success"):
                    raise SystemExit("WS %s failed: %s" % (type_, r.get("error")))
                return r.get("result")


# ---------------------------------------------------------------------------
# card builders
# ---------------------------------------------------------------------------
def _logo_data_uri():
    try:
        with open(LOGO_SVG, "rb") as f:
            return "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return None


def tile(entity, name, **extra):
    return {"type": "tile", "entity": entity, "name": name, **extra}


def heading(text, icon=None):
    c = {"type": "heading", "heading": text, "heading_style": "title"}
    if icon:
        c["icon"] = icon
    return c


def feed_error_note(fw_entity):
    """A warning card shown only when the firmware update feed reports a problem.

    The Firmware tile alone shows a misleading "Up to date" when the feed is unconfigured
    or unreadable (private repo without a token, bad token, no published release, missing
    bundle asset). This markdown card surfaces the entity's `release_feed_error` attribute
    (see update.py) so the installer sees *why* no update is offered.

    A self-hiding markdown card (not a conditional card): card-level conditions can't test
    "attribute is set" (the conditional card has no `template` condition), so the content
    template renders to an empty string when the feed reads cleanly (attribute None) and
    `show_empty: false` then hides the card entirely.
    """
    expr = "state_attr('%s', 'release_feed_error')" % fw_entity
    return {
        "type": "markdown",
        "show_empty": False,
        "content": ("{% set e = " + expr + " %}"
                    "{% if e %}### ⚠️ Firmware updates unavailable\n\n{{ e }}{% endif %}"),
    }


def fault_timeline_card(diag_entity):
    """A 'Recent faults' markdown card with the ACTUAL codes + details.

    HA's logbook for an `event` entity only renders a generic "detected an event"
    (no event_type, no attributes), so instead render the Diagnostics sensor's
    `last_fault` + `recent[]` fault ring (each {code, detail, board}) -- newest first.
    """
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


def build_view(gw_id, friendly, by_role, inputs_by_board, mcp_by_board, logo, broker):
    """One Sections view for a single gateway."""
    status_cards = []
    if logo:
        status_cards.append({"type": "picture", "image": logo,
                             "tap_action": {"action": "none"}, "alt_text": TITLE})
    status_cards.append(heading("Status", "mdi:heart-pulse"))
    if "diagnostics" in by_role:                 # health summary (ok/degraded/mcp_fault/…)
        status_cards.append(tile(by_role["diagnostics"], "Health"))
    if broker:                                   # integration's MQTT link (hub device)
        status_cards.append(tile(broker, "Broker"))
    if "firmware" in by_role:
        status_cards.append(tile(by_role["firmware"], "Firmware"))
        status_cards.append(feed_error_note(by_role["firmware"]))
    if "ethernet" in by_role:
        status_cards.append(tile(by_role["ethernet"], "Ethernet"))
    for key, label in STATUS_SENSORS:
        if key in by_role:
            status_cards.append(tile(by_role[key], label))
    # Fault timeline with real codes/details (the HA logbook for an event entity only
    # shows a generic "detected an event"). Prefer the Diagnostics-attribute markdown;
    # fall back to an event tile (shows the latest code as its state) if it's missing.
    if "diagnostics" in by_role:
        status_cards.append(fault_timeline_card(by_role["diagnostics"]))
    elif "fault" in by_role:
        status_cards.append(tile(by_role["fault"], "Last fault"))

    sections = [{"type": "grid", "cards": status_cards}]

    # One section per board: its MCP health (connectivity + last error) then its inputs.
    for board in sorted(set(inputs_by_board) | set(mcp_by_board)):
        cards = [heading("Wall switches · board %d" % board, "mdi:light-switch")]
        mcp = mcp_by_board.get(board, {})
        if mcp.get("mcp"):
            cards.append(tile(mcp["mcp"], "MCP chip"))
        if mcp.get("err"):
            cards.append(tile(mcp["err"], "MCP last error"))
        for pin, ent in sorted(inputs_by_board.get(board, [])):
            cards.append(tile(ent, "Input %d" % pin, icon="mdi:gesture-tap-button"))
        sections.append({"type": "grid", "cards": cards})

    control_cards = [heading("Controls", "mdi:tune-vertical")]
    if "reboot" in by_role:
        control_cards.append(tile(by_role["reboot"], "Restart"))
    if "identify" in by_role:
        control_cards.append(tile(by_role["identify"], "Identify"))
    tuning = [(by_role.get(k), lbl) for k, lbl in (
        ("long_ms", "Long press"), ("double_gap_ms", "Double-tap window"),
        ("debounce_ms", "Debounce"), ("log_level", "Log level"))]
    tuning = [{"entity": e, "name": lbl} for e, lbl in tuning if e]
    if tuning:
        control_cards.append({"type": "entities", "title": "Gesture tuning",
                              "show_header_toggle": False, "entities": tuning})
    topo = [(by_role.get(k), lbl) for k, lbl in (
        ("board_addrs", "Board addresses"), ("last_input", "Last input"))]
    topo = [{"entity": e, "name": lbl} for e, lbl in topo if e]
    if topo:
        control_cards.append({"type": "entities", "title": "Topology",
                              "show_header_toggle": False, "entities": topo})
    sections.append({"type": "grid", "cards": control_cards})

    return {"type": "sections", "title": friendly, "path": "gw-%s" % gw_id.lower(),
            "icon": "mdi:home-lightning-bolt", "max_columns": 3, "sections": sections}


# ---------------------------------------------------------------------------
# registry -> per-gateway role map
# ---------------------------------------------------------------------------
def _role_of(unique_id, gw_id):
    """Map an entity unique_id to a dashboard role key (or None)."""
    pre = "hearth_%s_" % gw_id
    if not unique_id.startswith(pre):
        return None
    rest = unique_id[len(pre):]
    if rest.startswith("diag_"):
        key = rest[len("diag_"):]
        return {"board_addrs": "board_addrs"}.get(key, key)  # ethernet, ip, uptime...
    if rest == "diagnostics":            # the structured root-cause Diagnostics sensor
        return "diagnostics"
    if rest == "fault":                  # the device-level fault event entity
        return "fault"
    if rest == "firmware":
        return "firmware"
    if rest == "log_level":
        return "log_level"
    if rest in ("reboot", "identify", "long_ms", "double_gap_ms", "debounce_ms"):
        return rest
    return None


def gateways(ws):
    """-> list of (gw_id, friendly_name, {role: entity_id}, {board: [(pin, eid)]})."""
    devices = ws.call("config/device_registry/list")
    ents = ws.call("config/entity_registry/list")
    by_device = {}
    for e in ents:
        by_device.setdefault(e.get("device_id"), []).append(e)

    out = []
    for d in devices:
        if d.get("disabled_by"):
            continue                          # hidden/retired unit -> not on the dashboard
        gw_id = None
        for ident in d.get("identifiers", []):
            # identifiers serialize as ["oselia","hearth_<id>"]; require the OSELIA
            # domain so a stale MQTT-discovery device for the same unit isn't matched.
            if (len(ident) == 2 and ident[0] == "oselia"
                    and str(ident[1]).startswith("hearth_")):
                gw_id = ident[1][len("hearth_"):]
        if not gw_id:
            continue
        by_role, inputs_by_board, mcp_by_board = {}, {}, {}
        for e in by_device.get(d["id"], []):
            if e.get("disabled_by"):
                continue
            uid, eid = e.get("unique_id", ""), e["entity_id"]
            m = _EVENT_RE.search(uid)
            if m and eid.startswith("event."):
                b, p = int(m.group(1)), int(m.group(2))
                inputs_by_board.setdefault(b, []).append((p, eid))
                continue
            me = _MCPERR_RE.search(uid)            # per-board MCP last-error sensor
            if me and eid.startswith("sensor."):
                mcp_by_board.setdefault(int(me.group(1)), {})["err"] = eid
                continue
            mc = _MCP_RE.search(uid)               # per-board MCP connectivity
            if mc and eid.startswith("binary_sensor."):
                mcp_by_board.setdefault(int(mc.group(1)), {})["mcp"] = eid
                continue
            role = _role_of(uid, gw_id)
            if role:
                by_role[role] = eid
        friendly = d.get("name_by_user") or d.get("name") or "Hearth %s" % gw_id
        out.append((gw_id, friendly, by_role, inputs_by_board, mcp_by_board))
    return sorted(out, key=lambda g: g[0])


def build_config(ws):
    """Build the OSELIA Hearth dashboard config from HA's live registry.

    Returns (config_dict, [gw_id, ...]). The gateway-id list is empty (and the config
    holds zero views) when no OSELIA gateways are present yet -- callers decide whether
    that is an error (the standalone tool) or a soft skip (the provisioning wizard,
    which may run a beat before discovery finishes). Pure aside from the `ws` reads, so
    the wizard (`ha_setup.ensure_oselia_dashboard`) and this CLI build it identically."""
    logo = _logo_data_uri()
    # The broker-connection sensor lives on the hub device (unique_id oselia_broker_*);
    # surface it on every gateway view's Status block.
    broker = next((e["entity_id"] for e in ws.call("config/entity_registry/list")
                   if str(e.get("unique_id", "")).startswith("oselia_broker_")), None)
    gws = gateways(ws)
    views = [build_view(gid, name, roles, inputs, mcp, logo, broker)
             for gid, name, roles, inputs, mcp in gws]
    return {"title": TITLE, "views": views}, [g[0] for g in gws]


def push_config(ws, config):
    """Create the /oselia-hearth storage-mode dashboard if absent, then save `config`."""
    existing = {d["url_path"] for d in ws.call("lovelace/dashboards/list")}
    if URL_PATH not in existing:
        ws.call("lovelace/dashboards/create", url_path=URL_PATH, title=TITLE,
                mode="storage", show_in_sidebar=True, require_admin=False,
                icon="mdi:home-lightning-bolt")
    ws.call("lovelace/config/save", url_path=URL_PATH, config=config)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate the OSELIA Hearth dashboard.")
    ap.add_argument("--ha-url", default="http://localhost:8123")
    ap.add_argument("--token")
    args = ap.parse_args(argv)

    token = (args.token or os.environ.get("OSELIA_HA_TOKEN")
             or _file_token())
    if not token:
        raise SystemExit("no HA token (use --token / $OSELIA_HA_TOKEN / "
                         "~/.config/oselia/ha_token)")
    u = args.ha_url.rstrip("/")
    use_tls = u.startswith("https://")
    host, _, port = u.split("://", 1)[-1].partition(":")
    ws = HAWS(host, int(port) if port else (443 if use_tls else 8123), token,
              use_tls=use_tls)

    config, gw_ids = build_config(ws)
    if not gw_ids:
        raise SystemExit("no OSELIA gateways found in HA (is the integration set up?)")
    push_config(ws, config)
    print("Dashboard /%s updated: %d gateway view(s) [%s]"
          % (URL_PATH, len(gw_ids), ", ".join(gw_ids)))


def _file_token():
    path = os.path.expanduser("~/.config/oselia/ha_token")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return None


if __name__ == "__main__":
    main()
