"""Firmware side of the OSELIA MQTT wire contract -- topic builders + payloads.

Up to 8 MCP23017 chips => up to 128 inputs. Each input is addressed by (board, pin),
board = position of the chip in cfg.MCP_ADDRESSES (1-based), pin = 1..16.

The firmware publishes NO Home Assistant MQTT-discovery configs: the first-party
OSELIA integration (its own repo, vmyronovych/oselia-hearth-di16g-ha) declares every
entity itself. What lives here is the stable data/command wire contract the
integration mirrors -- action/availability/cfg topics, the command subscribe wildcard,
and the live-tunable limits/options the command handler enforces. All builders are pure
(string/dict only) so they run under CPython for host tests.
"""

PINS_PER_CHIP = 16


# ---- global-index <-> (board, pin) helpers ----
def split_index(index):
    """Global 1-based index -> (board, pin), both 1-based."""
    z = index - 1
    return (z // PINS_PER_CHIP + 1, z % PINS_PER_CHIP + 1)


# ---- topics ----
def base(cfg, device_id):
    return "{}/{}".format(cfg.BASE_TOPIC, device_id)


def availability_topic(cfg, device_id):
    return base(cfg, device_id) + "/status"


def action_topic(cfg, device_id, board, pin):
    return "{}/board{}/input{}/action".format(base(cfg, device_id), board, pin)


def command_sub_topic(cfg, device_id):
    """Wildcard the firmware subscribes to for all commands: <base>/<id>/cmd/#."""
    return "{}/cmd/#".format(base(cfg, device_id))


def cfg_state_topic(cfg, device_id):
    """Retained topic carrying the live-tunable values for the HA number/select
    entities to reflect: <base>/<id>/cfg."""
    return "{}/cfg".format(base(cfg, device_id))


# ---- live-tunable contract (enforced by net_task's command handler) ----
LOG_LEVEL_OPTIONS = ("ERROR", "WARN", "INFO", "DEBUG")

# Inbound-value clamp limits for the gesture-timing `number` commands (command
# name == key). The OSELIA integration exposes matching `number` entities with the
# same limits; the firmware clamps to these regardless.
TUNABLE_LIMITS = {
    "long_ms": (100, 2000),
    "double_gap_ms": (0, 1000),
    "debounce_ms": (0, 100),
}


def cfg_state_payload(long_ms, double_gap_ms, debounce_ms, log_level):
    return {"long_ms": long_ms, "double_gap_ms": double_gap_ms,
            "debounce_ms": debounce_ms, "log_level": log_level}


# ---- publishing ----
def publish_available(client, cfg, device_id, online=True):
    client.publish(availability_topic(cfg, device_id),
                   "online" if online else "offline", retain=True)


def publish_action(client, cfg, device_id, board, pin, gesture):
    client.publish(action_topic(cfg, device_id, board, pin), gesture, retain=False)
