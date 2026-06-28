"""Copy this file to config.py and edit for your installation.

config.py is the ONLY place pins, timings, broker, and input names are defined.
Nothing else in the firmware should hard-code these values.
"""

# ---------------------------------------------------------------------------
# Network / MQTT broker  (CH9120 does NO DNS -> use a numeric IP)
# ---------------------------------------------------------------------------
BROKER_IP = (192, 168, 1, 104)   # MQTT broker, numeric IP only
BROKER_PORT = 1883
MQTT_USER = None                # or "username"
MQTT_PASS = None                # or "password"
MQTT_KEEPALIVE_S = 30

# CH9120 own network identity
USE_DHCP = True
LOCAL_IP = (192, 168, 1, 200)
GATEWAY = (192, 168, 1, 1)
SUBNET_MASK = (255, 255, 255, 0)
CH9120_LOCAL_PORT = 1000       # CH9120 local source port (POC: 1000)
UART_BAUD = 115200             # transparent-mode baud (config mode runs at 9600)
UART_CONFIG_BAUD = 9600        # CH9120 serial-config-mode baud (per POC)

# ---------------------------------------------------------------------------
# RP2040 pin map  (values CONFIRMED against the working POC unless noted)
# ---------------------------------------------------------------------------
# CH9120 (fixed on the RP2040-ETH board)
PIN_CH9120_UART_ID = 1
PIN_CH9120_TX = 20             # MCU TX -> CH9120 RXD   (POC: tx=Pin(20))
PIN_CH9120_RX = 21             # CH9120 TXD -> MCU RX   (POC: rx=Pin(21))
# CH9120 TCP-status pin. DISABLED (None): it was never HW-validated (the POC didn't use
# it), and trusting it caused a false-"down" reconnect FLAP (connect -> publish -> forced
# reconnect, repeatedly, which churned the broker status online/offline). Liveness now comes
# from MQTT keepalive/PINGRESP + CONNACK (net_task / mqtt_client), which is HW-independent.
# Set back to 17 only after verifying TCPCS polarity/timing on hardware.
PIN_CH9120_TCPCS = None         # was 17 -- see note above (HW-VERIFY before re-enabling)
PIN_CH9120_CFG0 = 18           # LOW = config mode (POC: Pin(18))
PIN_CH9120_RST = 19            # active LOW (POC: Pin(19))

# MCP23017 chips on the shared I2C bus (CONFIRMED against POC).
# Up to 8 boards: one "main" (RP2040 + MCP) plus satellite MCP-only boards.
# Each chip needs a distinct address via its A0..A2 strap pins (0x20..0x27 = 8).
# BOARD NUMBER = position in the resolved list (1-based).
I2C_ID = 1                     # manufactured dib-monolith uses I2C1 (GP26/GP27)
PIN_I2C_SDA = 26               # board net SDA -> RP2040-ETH ADC0 pad = GP26 (I2C1 SDA)
PIN_I2C_SCL = 27               # board net SCK -> RP2040-ETH ADC1 pad = GP27 (I2C1 SCL)
I2C_FREQ = 400_000             # POC used 100_000; 400k is fine for a short bus
# Board discovery: with MCP_AUTODISCOVER the firmware scans the bus at boot and
# drives exactly the chips that respond (0x20..0x27) -- no count to configure, and
# an unwired board simply isn't advertised (no permanent MCP fault). MCP_ADDRESSES
# is then only the fallback used if a scan finds nothing. Set MCP_AUTODISCOVER=False
# (the wizard's --boards / site.json board_count does this) to pin an exact list.
MCP_AUTODISCOVER = True
MCP_ADDRESSES = [0x20]         # fallback / explicit list (board1 = 0x20)
PIN_MCP_INT = 22               # SHARED wired-OR INT line; board net INTA -> GP22
                               # (POC used GP2; manufactured board routes it to GP22)
PIN_MCP_RESET = 9              # board net RESET -> MCP /RESET (pin 18) on GP9.
                               # Driven HIGH (deasserted) at boot, pulsed LOW once to
                               # reset the chips. None = tied high in hardware (POC).
MCP_INT_ACTIVE_LOW = True      # active-low INT (IOCON bit)
MCP_INT_OPEN_DRAIN = True      # IOCON ODR=1 so all chips can share one INT line
                               # (needs a pull-up on the INT net; GP22 internal
                               # pull-up is enabled, add external 4.7k for >2 chips)

# Onboard WS2812 status LED (single addressable RGB pixel)
PIN_STATUS_LED = 25            # WS2812 data pin (POC: LED_PIN = 25)
STATUS_LED_ENABLE = True
STATUS_LED_BRIGHTNESS = 0.2    # 0.0..1.0 (WS2812 at full scale is blinding)
STATUS_LED_ORDER = "RGB"       # this board's WS2812 is RGB-order (HW-CONFIRMED:
                               # GRB showed green-as-red; RGB renders R/G/B correctly)

# ---------------------------------------------------------------------------
# Input behaviour
# ---------------------------------------------------------------------------
ACTIVE_LOW = True              # True: pressed switch pulls MCP pin LOW
USE_INTERNAL_PULLUPS = True    # MCP GPPU per optocoupler output stage

DEBOUNCE_MS = 0                # software debounce, ms. Inputs are already RC +
                               # optocoupler debounced in hardware, so 0 leans on
                               # that and accepts a change on the next sample (~one
                               # loop pass) for minimal latency. Raise (e.g. 10-25)
                               # if residual chatter causes spurious gestures.
LONG_MS = 400                  # hold >= this -> long   (POC-proven value)
DOUBLE_GAP_MS = 0              # double-tap detection window, ms.
                               # >0  : a single press is held back this long to see
                               #       if a 2nd tap arrives (=> the single-press
                               #       latency). e.g. 250 for responsive double-tap.
                               # 0   : double detection DISABLED -> single fires
                               #       immediately on release (instant feel, latency
                               #       ~= DEBOUNCE_MS). "long" still works; "double"
                               #       is never emitted. Set >0 only if you actually
                               #       use double-tap on some input.

# ---------------------------------------------------------------------------
# Identity & topics
# ---------------------------------------------------------------------------
# DEVICE_ID is normally derived from machine.unique_id() at runtime; this is a
# fallback / override.
DEVICE_ID = None               # None -> derive from unique_id() last 6 hex
DEVICE_NAME = "Hearth"
DEVICE_MODEL = "Hearth (DI16-G)"
DEVICE_MANUFACTURER = "OSELIA"
SW_VERSION = "0.7.0"
HW_VERSION = "DI16-G"                   # board model (shown as Hardware in HA)
PROJECT_URL = "https://github.com/vmyronovych/oselia-hearth-di16g-firmware"  # HA discovery origin

BASE_TOPIC = "hearth"             # -> hearth/<device_id>/...
DISCOVERY_PREFIX = "homeassistant"     # HA default

# How inputs appear in HA: "event" (modern entity per input; shows in dashboards &
# logbook, and what the shipped blueprint targets), "trigger" (device_automation
# triggers, the original), or "both". "both" doubles discovery traffic on connect.
INPUT_DISCOVERY = "both"

# Which Home Assistant integration consumes this device:
#   "mqtt"   -> the firmware publishes HA MQTT-discovery configs (homeassistant/.../
#               config, retained); the device appears under HA's built-in MQTT
#               integration. This is the original behaviour and the default.
#   "oselia" -> the firmware SKIPS publishing those discovery configs; the first-party
#               OSELIA custom integration (its own repo, vmyronovych/oselia-hearth-di16g-ha)
#               creates the entities itself, so the device appears under OSELIA, not MQTT.
# The data + command topics are IDENTICAL in both modes -- only discovery publishing
# differs -- so a unit can switch modes with no other change. Set per install via the
# wizard (`provision.py --oselia`). See homeassistant/INTEGRATION_SPEC.md.
HA_INTEGRATION = "mqtt"

# Friendly-name overrides keyed by (board, pin), both 1-based (pin 1..16).
# Anything not listed defaults to "board<b>_input<p>". Example:
#   INPUT_NAME_OVERRIDES = {(1, 1): "kitchen_main", (2, 5): "garage_door"}
INPUT_NAME_OVERRIDES = {}

# ---------------------------------------------------------------------------
# Robustness / dual-core (industrial-grade)
# ---------------------------------------------------------------------------
# Watchdog: hardware WDT auto-reboots if not fed. RP2040 max timeout ~8388 ms.
WDT_ENABLE = True
WDT_TIMEOUT_MS = 8000
# Core0 only feeds the WDT while core1's heartbeat is fresh; if core1 stalls
# longer than this, the WDT is left unfed and the board resets.
CORE1_STALL_MS = 6000

# Inter-core event queue (core0 detects -> core1 publishes).
EVENT_QUEUE_SIZE = 256         # gestures buffered while offline; drop-oldest beyond
                               # (headroom over 8 chips x 16 = 128 inputs)

# MQTT connection management
MQTT_CONNECT_TIMEOUT_MS = 4000     # wait for CONNACK
PING_RESPONSE_TIMEOUT_MS = 5000    # no PINGRESP within this -> treat link dead
RECONNECT_BACKOFF_MIN_MS = 1000    # first retry delay
RECONNECT_BACKOFF_MAX_MS = 30000   # cap
DISCOVERY_REPUBLISH_ON_RECONNECT = True

# I2C resilience + MCP recovery (an MCP fault must never freeze inputs or reboot)
I2C_RETRIES = 3                # retry count for MCP reads/writes
MCP_HEALTHCHECK_MS = 2000      # how often core0 re-verifies/re-inits a down MCP
MCP_INT_STUCK_MS = 250         # shared INT held asserted this long despite reading
                               # every healthy chip -> a dead chip is holding the
                               # wired-OR line: count it + trigger recovery
MCP_RECOVERY_AFTER_FAILS = 3   # consecutive failed health checks on a board before
                               # escalating to a bus/reset recovery (a stuck INT
                               # bypasses this gate)
MCP_RECOVERY_MIN_INTERVAL_MS = 10000  # min spacing between recovery actions (no
                               # thrash); escalates L1 (I2C reclock) -> L2 (/RESET)
NET_BOARD_WAIT_MS = 3000       # core1 waits this long for core0 to resolve the board
                               # set before its first discovery/diag, then falls back
                               # to the config list (network is never gated on I2C)

# Logging: 0=ERROR 1=WARN 2=INFO 3=DEBUG
LOG_LEVEL = 2

# ---------------------------------------------------------------------------
# Diagnostics telemetry (optional)
# ---------------------------------------------------------------------------
# Publishes a small retained JSON to <base>/<id>/diag/state plus HA discovery
# diagnostic entities (uptime, free heap, link/board health, reconnect & dropped
# counters, last input) so the customer sees basic parameters in the HA app.
# Sending is gated in net_task so it NEVER delays a button publish (only sent when
# the gesture queue is empty, at most every DIAG_INTERVAL_S). Turn OFF per install
# via the wizard (`provision.py --no-diag` -> site.json "diag": false).
DIAG_ENABLE = True
DIAG_INTERVAL_S = 10           # how often to refresh the diag/state snapshot
DIAG_FAULT_RING = 16           # recent[] fault-history length in the diag blob (one
                               # retained export then carries the fault timeline)
# Two-way control: subscribe to <base>/<id>/cmd/# and expose HA `button` entities
# (Restart, Identify). Commands are handled on core1 after the gesture queue is
# drained, so they never delay a button publish.
CONTROL_ENABLE = True
# With DHCP the MCU doesn't know its leased IP; net_task reads it once from the
# CH9120 at boot (read-only config round-trip) after this settle for the lease.
# Only happens on the initial bring-up, never on reconnect. 0 disables the read.
DHCP_LEASE_SETTLE_MS = 4000

# ---------------------------------------------------------------------------
# OTA application updates (over MQTT, no CH9120 retarget) -- see OTA_SPEC.md
# ---------------------------------------------------------------------------
# Replaces the app .py files via an A/B slot layout with boot-confirm/auto-revert.
# The interpreter itself is NOT updated over the air (physical BOOTSEL only). The
# loader (/boot.py) and /site.json are never part of a bundle, so OTA can't brick the
# boot path or lose identity. Bytes stream as chunks over the live broker session.
OTA_ENABLE = True
OTA_MAX_BOOT_TRIES = 2          # boots a pending build gets to prove itself before
                               # auto-revert (MUST match _MAX_TRIES in boot.py)
OTA_BOOT_CONFIRM_MS = 20000    # how long MQTT-online + all-healthy before confirm()
OTA_CHUNK_SIZE = 1024          # bytes per ota/data chunk (must match the publisher)
OTA_NAK_STALL_MS = 1500        # no chunk for this long -> NAK the still-missing ones
                               # (the board subscribes ota/data at QoS0; NAK recovers
                               # dropped chunks without re-sending the whole bundle)
OTA_DOWNLOAD_TIMEOUT_MS = 60000  # abort the download if no chunk arrives for this long
SLOTS_DIR = "/slots"           # /slots/a , /slots/b
OTA_STATE_PATH = "/ota/state"
OTA_STAGING_PATH = "/ota/staging.bin"

# ---------------------------------------------------------------------------
# Installer overlay (generated by provision.py; do NOT hand-edit)
# ---------------------------------------------------------------------------
# The host-side wizard writes the small set of per-install values into a
# machine-owned `site.json`. Everything above is fixed hardware defaults; the
# block below overlays the installer's answers on top. If `site.json` is absent
# (bench / dev), the defaults above stand unchanged. Only stdlib `json` is used,
# so config.py still imports cleanly under CPython for the host unit tests.
try:
    import json as _json

    def _ip4(s):
        return tuple(int(x) for x in s.split("."))

    with open("site.json") as _f:
        _site = _json.load(_f)
    if "broker_ip" in _site:
        BROKER_IP = _ip4(_site["broker_ip"])
    if "broker_port" in _site:
        BROKER_PORT = int(_site["broker_port"])
    if "mqtt_user" in _site:
        MQTT_USER = _site["mqtt_user"] or None
    if "mqtt_pass" in _site:
        MQTT_PASS = _site["mqtt_pass"] or None
    if "use_dhcp" in _site:
        USE_DHCP = bool(_site["use_dhcp"])
    if "board_count" in _site:
        # An explicit board_count pins the list and disables autodiscovery.
        MCP_ADDRESSES = [0x20, 0x21, 0x22, 0x23,
                         0x24, 0x25, 0x26, 0x27][:int(_site["board_count"])]
        MCP_AUTODISCOVER = False
    if "diag" in _site:
        DIAG_ENABLE = bool(_site["diag"])
    if "ha_integration" in _site:
        # "mqtt" (publish HA discovery) or "oselia" (custom integration owns entities).
        HA_INTEGRATION = _site["ha_integration"]
    # Live-tunable values persisted by the board itself when changed from HA
    # (number/select entities). They override the hardware defaults on next boot.
    if "long_ms" in _site:
        LONG_MS = int(_site["long_ms"])
    if "double_gap_ms" in _site:
        DOUBLE_GAP_MS = int(_site["double_gap_ms"])
    if "debounce_ms" in _site:
        DEBOUNCE_MS = int(_site["debounce_ms"])
    if "log_level" in _site:
        LOG_LEVEL = int(_site["log_level"])
    if _site.get("static"):
        _s = _site["static"]
        USE_DHCP = False
        LOCAL_IP = _ip4(_s["ip"])
        GATEWAY = _ip4(_s["gateway"])
        SUBNET_MASK = _ip4(_s["mask"])
    if _site.get("names"):                 # rows of [board, pin, name] (1-based)
        INPUT_NAME_OVERRIDES = {(int(b), int(p)): n for b, p, n in _site["names"]}
    del _site, _f
except OSError:
    pass   # no site.json -> use the defaults above (bench / dev)
