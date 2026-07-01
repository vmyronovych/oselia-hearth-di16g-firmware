"""Diagnostics telemetry -- a small retained JSON snapshot + HA discovery sensors.

The firmware publishes one compact retained message to `<base>/<id>/diag/state`
(firmware version, uptime, link/broker/board health, free heap, reconnect and
dropped-event counters, last input) and a handful of HA MQTT-discovery entities
(entity_category=diagnostic) that render it under the existing device. So the
customer sees basic operating parameters in the Home Assistant app with no extra
service.

Sending is OFF-able per install (`cfg.DIAG_ENABLE`, set by the provisioning wizard)
and -- crucially -- is gated in net_task so it never delays a button publish: the
caller only emits state when the gesture queue is empty and at most every
`DIAG_INTERVAL_S` (see docs/spec.md sec.5.2 and net_task).

Builders here are pure (json/string only) so they run under CPython for host tests.
"""
import ha_discovery as ha

try:
    import ujson as json
except ImportError:
    import json


# ---- state snapshot ----
def state_topic(cfg, device_id):
    return ha.base(cfg, device_id) + "/diag/state"


def build_state(fw, uptime_s, ip, eth, mqtt, boards, mem_free,
                reconnects, dropped, last, temp_c=None, board_addrs=None,
                hw=None, reset_cause=None, health=None, boards_total=None,
                boards_ok=None, mcp=None, counters=None, last_fault=None,
                recent=None):
    """Assemble the telemetry dict. Pure -- caller json.dumps it.

    The first ten positional args + temp_c/board_addrs are the original, stable
    contract (older HA entities still read them). The keyword args carry the
    structured root-cause observability (schema in INTEGRATION_SPEC.md):
      hw           -- hardware version string.
      reset_cause  -- "power_on"|"wdt"|"unknown" (why we last booted; on RP2040 "wdt" also
                      covers any deliberate machine.reset(), not only a watchdog stall).
      health       -- "ok"|"degraded"|"mcp_fault"|"net_fault" (HA Diagnostics state).
      boards       -- resolved board count (input entities exist for all of them).
      boards_ok    -- how many are currently responding.
      mcp          -- per-board list: {board,addr,ok,code,detail,fails,last_ok_s,
                      recoveries} (the diag `mcp[]`).
      counters     -- {int_stuck,bus_recoveries,mcp_resets,reconnects,dropped}.
      last_fault   -- most recent fault record, or None.
      recent       -- bounded ring of recent fault records (the timeline).
    `last` is a short human string for the most recent gesture; `temp_c` is the
    RP2040 die temperature (None -> HA shows unknown).
    """
    return {
        "fw": fw,
        "hw": hw,
        "uptime_s": uptime_s,
        "ip": ip,
        "reset_cause": reset_cause if reset_cause is not None else "unknown",
        "health": health if health is not None else "ok",
        "eth": bool(eth),
        "mqtt": bool(mqtt),
        "boards": boards,
        "boards_total": boards_total if boards_total is not None else boards,
        "boards_ok": boards_ok if boards_ok is not None else boards,
        "board_addrs": board_addrs if board_addrs is not None else [],
        "mcp": mcp if mcp is not None else [],
        "counters": counters if counters is not None else {},
        "last_fault": last_fault,
        "recent": recent if recent is not None else [],
        "mem_free": mem_free,
        "reconnects": reconnects,
        "dropped": dropped,
        "last": last,
        "temp_c": temp_c,
    }


# Reset-cause: pure int->name map (the read of machine.reset_cause() is hardware,
# done in main.py and passed here). Unknown / unsupported port -> "unknown".
def reset_cause_name(cause, names):
    if cause is None:
        return "unknown"
    return names.get(cause, "unknown")


# ---- fault event stream (diag/event, NON-retained) ----
def event_topic(cfg, device_id):
    """Non-retained per-fault stream so HA gets a real-time timeline (logbook),
    not just the latest retained snapshot."""
    return ha.base(cfg, device_id) + "/diag/event"


def build_event(record):
    """Pure passthrough/normaliser for a fault record -> diag/event payload.
    `record` is {ts, component, code, detail[, board]}."""
    out = {
        "ts": record.get("ts", 0),
        "component": record.get("component", ""),
        "code": record.get("code", ""),
        "detail": record.get("detail", ""),
    }
    if record.get("board") is not None:
        out["board"] = record["board"]
    return out


def publish_event(client, cfg, device_id, record):
    client.publish(event_topic(cfg, device_id), json.dumps(build_event(record)),
                   retain=False)


def rp2040_temp_c(raw_u16):
    """Convert a 16-bit ADC reading of the RP2040 internal temperature sensor
    (ADC channel 4) to degrees Celsius, per the RP2040 datasheet:
    V = raw/65535 * 3.3 ; T = 27 - (V - 0.706) / 0.001721. This is the die
    (chip) temperature -- a coarse trend / overheat signal, not a precise reading.

    This board's ADC reads with a fixed offset (effective VREF < 3.3 V), so the
    raw formula lands ~60 C low and reports negative. We take the magnitude
    (abs) so the customer sees a plausible positive die temperature rather than a
    broken-looking negative; it is NOT a calibrated absolute value. Pure so it is
    host-testable."""
    v = raw_u16 * 3.3 / 65535
    return round(abs(27 - (v - 0.706) / 0.001721), 1)


def ip_str(use_dhcp, local_ip):
    """Best-effort IP for display. DHCP-leased address isn't known to the MCU
    (the CH9120 owns the stack), so report "dhcp"; static is formatted out."""
    if use_dhcp:
        return "dhcp"
    return ".".join(str(x) for x in local_ip)


def format_ip(leased_ip, use_dhcp, local_ip):
    """The address to report: the CH9120 DHCP lease read back at boot (4-tuple) if
    we have it, else the static / "dhcp" fallback from ip_str."""
    if leased_ip:
        return ".".join(str(x) for x in leased_ip)
    return ip_str(use_dhcp, local_ip)


# ---- HA discovery for the diagnostic entities ----
# (key, friendly name, component, value_template, extra config fields)
DIAG_SENSORS = (
    ("uptime", "Uptime", "sensor", "{{ value_json.uptime_s }}",
     {"unit_of_measurement": "s", "device_class": "duration",
      "state_class": "measurement"}),
    ("mem_free", "Free memory", "sensor", "{{ value_json.mem_free }}",
     {"unit_of_measurement": "B", "device_class": "data_size",
      "state_class": "measurement"}),
    ("temperature", "Temperature", "sensor", "{{ value_json.temp_c }}",
     {"unit_of_measurement": "°C", "device_class": "temperature",
      "state_class": "measurement"}),
    ("reconnects", "Reconnects", "sensor", "{{ value_json.reconnects }}",
     {"state_class": "total_increasing", "icon": "mdi:restart"}),
    ("dropped", "Dropped events", "sensor", "{{ value_json.dropped }}",
     {"state_class": "total_increasing", "icon": "mdi:trash-can-outline"}),
    ("boards", "Input boards online", "sensor", "{{ value_json.boards }}",
     {"icon": "mdi:chip"}),
    ("board_addrs", "Board addresses", "sensor",
     "{{ value_json.board_addrs | join(', ') }}",
     {"icon": "mdi:identifier"}),
    ("last_input", "Last input", "sensor", "{{ value_json.last }}",
     {"icon": "mdi:gesture-tap-button"}),
    ("ip", "IP address", "sensor", "{{ value_json.ip }}",
     {"icon": "mdi:ip-network"}),
    ("ethernet", "Ethernet link", "binary_sensor",
     "{{ 'ON' if value_json.eth else 'OFF' }}",
     {"device_class": "connectivity", "payload_on": "ON", "payload_off": "OFF"}),
)


def diag_discovery_topic(cfg, device_id, component, key):
    return "{}/{}/{}/diag_{}/config".format(
        cfg.DISCOVERY_PREFIX, component, device_id, key)


def diag_discovery_payload(cfg, device_id, key, name, value_template, extra):
    payload = {
        "name": name,
        "unique_id": "{}_{}_diag_{}".format(cfg.BASE_TOPIC, device_id, key),
        "state_topic": state_topic(cfg, device_id),
        "value_template": value_template,
        "availability_topic": ha.availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "entity_category": "diagnostic",
        # Go "unavailable" if telemetry stops (e.g. a wedged board), at 3x the
        # publish interval. These all ride the periodic diag/state message.
        "expire_after": getattr(cfg, "DIAG_INTERVAL_S", 10) * 3,
        "device": ha.device_block(cfg, device_id),
        "origin": ha.origin_block(cfg),
    }
    payload.update(extra)
    return payload


# ---- log mirror (last WARN/ERROR line surfaced in HA) ----
def log_topic(cfg, device_id):
    return ha.base(cfg, device_id) + "/diag/log"


def log_discovery_payload(cfg, device_id):
    # No expire_after: logs are event-driven (published only on a new line), so the
    # last line should persist rather than go "unavailable" during quiet periods.
    return {
        "name": "Last log",
        "unique_id": "{}_{}_diag_log".format(cfg.BASE_TOPIC, device_id),
        "state_topic": log_topic(cfg, device_id),
        "value_template": "{{ value_json.line }}",
        "json_attributes_topic": log_topic(cfg, device_id),
        "availability_topic": ha.availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "entity_category": "diagnostic",
        "icon": "mdi:text-box-outline",
        "device": ha.device_block(cfg, device_id),
        "origin": ha.origin_block(cfg),
    }


def publish_diag_discovery(client, cfg, device_id, settle_ms=None):
    """Publish the diagnostic entities' discovery configs (retained), once per
    connect alongside the action discovery. `settle_ms` paces the CH9120 like the
    action-discovery path does."""
    for key, name, component, tmpl, extra in DIAG_SENSORS:
        topic = diag_discovery_topic(cfg, device_id, component, key)
        payload = json.dumps(
            diag_discovery_payload(cfg, device_id, key, name, tmpl, extra))
        client.publish(topic, payload, retain=True)
        if settle_ms:
            settle_ms()
    # the "Last log" sensor (separate state topic from diag/state)
    client.publish(
        "{}/sensor/{}/diag_log/config".format(cfg.DISCOVERY_PREFIX, device_id),
        json.dumps(log_discovery_payload(cfg, device_id)), retain=True)
    if settle_ms:
        settle_ms()


def publish_state(client, cfg, device_id, state):
    client.publish(state_topic(cfg, device_id), json.dumps(state), retain=True)


def build_log(level_name, msg, ts):
    """Pure builder for the log mirror payload."""
    return {"line": "[%s] %s" % (level_name, msg), "level": level_name, "ts": ts}


def publish_log(client, cfg, device_id, level_name, msg, ts):
    client.publish(log_topic(cfg, device_id),
                   json.dumps(build_log(level_name, msg, ts)), retain=True)
