"""Home Assistant MQTT Discovery -- device_automation triggers, multi-board.

Up to 8 MCP23017 chips => up to 128 inputs. Each input is addressed by (board, pin),
board = position of the chip in cfg.MCP_ADDRESSES (1-based), pin = 1..16. All
inputs belong to ONE HA device; trigger subtype is "board<b>_input<p>" (overridable
via cfg.INPUT_NAME_OVERRIDES). See docs/spec.md sec.5.

Topic/payload builders are pure (json/string only) for host testing.
"""

try:
    import ujson as json
except ImportError:
    import json

# gesture -> HA device-automation trigger type
GESTURE_TYPE = {
    "single": "button_short_press",
    "double": "button_double_press",
    "long": "button_long_press",
}
GESTURES = ("single", "double", "long")

PINS_PER_CHIP = 16


# ---- global-index <-> (board, pin) helpers ----
def split_index(index):
    """Global 1-based index -> (board, pin), both 1-based."""
    z = index - 1
    return (z // PINS_PER_CHIP + 1, z % PINS_PER_CHIP + 1)


def make_index(board, pin):
    return (board - 1) * PINS_PER_CHIP + (pin - 1) + 1


def input_name(cfg, board, pin):
    ov = getattr(cfg, "INPUT_NAME_OVERRIDES", {})
    if (board, pin) in ov:
        return ov[(board, pin)]
    return "board{}_input{}".format(board, pin)


# ---- topics ----
def base(cfg, device_id):
    return "{}/{}".format(cfg.BASE_TOPIC, device_id)


def availability_topic(cfg, device_id):
    return base(cfg, device_id) + "/status"


def action_topic(cfg, device_id, board, pin):
    return "{}/board{}/input{}/action".format(base(cfg, device_id), board, pin)


def discovery_topic(cfg, device_id, board, pin, gesture):
    return "{}/device_automation/{}/b{}_in{}_{}/config".format(
        cfg.DISCOVERY_PREFIX, device_id, board, pin, gesture)


def event_discovery_topic(cfg, device_id, board, pin):
    return "{}/event/{}/b{}_in{}/config".format(
        cfg.DISCOVERY_PREFIX, device_id, board, pin)


def command_topic(cfg, device_id, name):
    """Topic HA writes to for a command button, e.g. <base>/<id>/cmd/reboot."""
    return "{}/cmd/{}".format(base(cfg, device_id), name)


def command_sub_topic(cfg, device_id):
    """Wildcard the firmware subscribes to for all commands: <base>/<id>/cmd/#."""
    return "{}/cmd/#".format(base(cfg, device_id))


def cfg_state_topic(cfg, device_id):
    """Retained topic carrying the live-tunable values for the HA number/select
    entities to reflect: <base>/<id>/cfg."""
    return "{}/cfg".format(base(cfg, device_id))


def device_block(cfg, device_id):
    """HA device-registry block. Enriched for a native-looking device page:
    serial_number + hw_version land in the registry; identifiers tie every entity
    (triggers, diagnostics) to the one device."""
    return {
        "identifiers": ["{}_{}".format(cfg.BASE_TOPIC, device_id)],
        "name": cfg.DEVICE_NAME,
        "model": cfg.DEVICE_MODEL,
        "manufacturer": cfg.DEVICE_MANUFACTURER,
        "sw_version": cfg.SW_VERSION,
        "hw_version": getattr(cfg, "HW_VERSION", None),
        "serial_number": device_id,
    }


def origin_block(cfg):
    """HA discovery 'origin' (the integration that produced the config). Recommended
    by HA so the entity's source is shown."""
    return {
        "name": cfg.DEVICE_NAME,
        "sw": cfg.SW_VERSION,
        "url": getattr(cfg, "PROJECT_URL", None),
    }


def discovery_payload(cfg, device_id, board, pin, gesture):
    return {
        "automation_type": "trigger",
        "type": GESTURE_TYPE[gesture],
        "subtype": input_name(cfg, board, pin),
        "topic": action_topic(cfg, device_id, board, pin),
        "payload": gesture,
        "device": device_block(cfg, device_id),
        "origin": origin_block(cfg),
    }


def event_discovery_payload(cfg, device_id, board, pin):
    """HA `event` entity for one input. Reuses the existing (non-retained) action
    topic; a value_template wraps the plain `single`/`double`/`long` payload into the
    {"event_type": ...} JSON the event platform expects. Modern, entity-based
    alternative to the device_automation trigger -- shows in dashboards & logbook."""
    return {
        "name": input_name(cfg, board, pin),
        "unique_id": "{}_{}_b{}_in{}_event".format(cfg.BASE_TOPIC, device_id,
                                                    board, pin),
        "state_topic": action_topic(cfg, device_id, board, pin),
        "event_types": list(GESTURES),
        "value_template": "{{ {'event_type': value} | to_json }}",
        "device_class": "button",
        "availability_topic": availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg, device_id),
        "origin": origin_block(cfg),
    }


# ---- command (button) entities for two-way control ----
COMMAND_BUTTONS = (
    # (key, name, command name, extra config)
    ("reboot", "Restart", "reboot",
     {"device_class": "restart", "entity_category": "config"}),
    ("identify", "Identify", "identify",
     {"device_class": "identify", "entity_category": "config"}),
)


def button_discovery_topic(cfg, device_id, key):
    return "{}/button/{}/{}/config".format(cfg.DISCOVERY_PREFIX, device_id, key)


def button_discovery_payload(cfg, device_id, key, name, cmd, extra):
    payload = {
        "name": name,
        "unique_id": "{}_{}_{}".format(cfg.BASE_TOPIC, device_id, key),
        "command_topic": command_topic(cfg, device_id, cmd),
        "payload_press": "PRESS",
        "availability_topic": availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg, device_id),
        "origin": origin_block(cfg),
    }
    payload.update(extra)
    return payload


def publish_command_discovery(client, cfg, device_id, settle_ms=None):
    """Publish the command `button` entities (retained)."""
    for key, name, cmd, extra in COMMAND_BUTTONS:
        topic = button_discovery_topic(cfg, device_id, key)
        payload = json.dumps(
            button_discovery_payload(cfg, device_id, key, name, cmd, extra))
        client.publish(topic, payload, retain=True)
        if settle_ms:
            settle_ms()


# ---- live-tunable entities (number/select) ----
LOG_LEVEL_OPTIONS = ("ERROR", "WARN", "INFO", "DEBUG")

# (key, name, min, max, step) for the gesture-timing numbers. Command name == key;
# limits are reused by net_task to clamp inbound values.
TUNABLE_NUMBERS = (
    ("long_ms", "Long press time", 100, 2000, 50),
    ("double_gap_ms", "Double-tap window", 0, 1000, 50),
    ("debounce_ms", "Debounce time", 0, 100, 5),
)
TUNABLE_LIMITS = {k: (lo, hi) for k, _, lo, hi, _ in TUNABLE_NUMBERS}


def number_discovery_payload(cfg, device_id, key, name, lo, hi, step):
    return {
        "name": name,
        "unique_id": "{}_{}_{}".format(cfg.BASE_TOPIC, device_id, key),
        "command_topic": command_topic(cfg, device_id, key),
        "state_topic": cfg_state_topic(cfg, device_id),
        "value_template": "{{ value_json.%s }}" % key,
        "min": lo, "max": hi, "step": step, "mode": "box",
        "unit_of_measurement": "ms",
        "entity_category": "config",
        "availability_topic": availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg, device_id),
        "origin": origin_block(cfg),
    }


def log_level_select_payload(cfg, device_id):
    opts = list(LOG_LEVEL_OPTIONS)
    return {
        "name": "Log level",
        "unique_id": "{}_{}_log_level".format(cfg.BASE_TOPIC, device_id),
        "command_topic": command_topic(cfg, device_id, "log_level"),
        "state_topic": cfg_state_topic(cfg, device_id),
        "value_template": "{{ %s[value_json.log_level] }}" % (opts,),
        "options": opts,
        "entity_category": "config",
        "icon": "mdi:bug-outline",
        "availability_topic": availability_topic(cfg, device_id),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block(cfg, device_id),
        "origin": origin_block(cfg),
    }


def publish_tunable_discovery(client, cfg, device_id, settle_ms=None):
    """Publish the `number` timing entities + the log-level `select` (retained)."""
    for key, name, lo, hi, step in TUNABLE_NUMBERS:
        topic = "{}/number/{}/{}/config".format(cfg.DISCOVERY_PREFIX, device_id, key)
        client.publish(topic, json.dumps(
            number_discovery_payload(cfg, device_id, key, name, lo, hi, step)),
            retain=True)
        if settle_ms:
            settle_ms()
    client.publish(
        "{}/select/{}/log_level/config".format(cfg.DISCOVERY_PREFIX, device_id),
        json.dumps(log_level_select_payload(cfg, device_id)), retain=True)
    if settle_ms:
        settle_ms()


def cfg_state_payload(long_ms, double_gap_ms, debounce_ms, log_level):
    return {"long_ms": long_ms, "double_gap_ms": double_gap_ms,
            "debounce_ms": debounce_ms, "log_level": log_level}


# ---- publishing ----
def publish_event_discovery(client, cfg, device_id, n_boards, settle_ms=None):
    """One `event` entity per (board, pin). settle_ms paces the CH9120."""
    for board in range(1, n_boards + 1):
        for pin in range(1, PINS_PER_CHIP + 1):
            topic = event_discovery_topic(cfg, device_id, board, pin)
            payload = json.dumps(event_discovery_payload(cfg, device_id, board, pin))
            client.publish(topic, payload, retain=True)
            if settle_ms:
                settle_ms()


def publish_discovery(client, cfg, device_id, n_boards, settle_ms=None):
    """Publish all (board x pin x gesture) trigger configs, retained.

    settle_ms: optional callable/int to pause between publishes so the CH9120 can
    flush (caller usually passes a small sleep). Kept caller-driven to avoid
    importing utime here.
    """
    for board in range(1, n_boards + 1):
        for pin in range(1, PINS_PER_CHIP + 1):
            for gesture in GESTURES:
                topic = discovery_topic(cfg, device_id, board, pin, gesture)
                payload = json.dumps(
                    discovery_payload(cfg, device_id, board, pin, gesture))
                client.publish(topic, payload, retain=True)
                if settle_ms:
                    settle_ms()


def publish_available(client, cfg, device_id, online=True):
    client.publish(availability_topic(cfg, device_id),
                   "online" if online else "offline", retain=True)


def publish_action(client, cfg, device_id, board, pin, gesture):
    client.publish(action_topic(cfg, device_id, board, pin), gesture, retain=False)
