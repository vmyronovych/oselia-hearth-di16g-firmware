"""Copy this file to config.py and edit for your installation.

config.py is the ONLY place pins, timings, broker, and input names are defined.
Nothing else in the firmware should hard-code these values.

Note: the per-install values (broker IP/port, MQTT credentials, DHCP, board
count, optional name overrides) are normally NOT hand-edited. The host-side
wizard (../provisioning/provision.py) writes them into a machine-owned
`site.json` on the board, which config.py overlays on top of these defaults at
import (see the overlay block at the bottom of src/config.py). Treat the values
here as hardware defaults; let the wizard own the site-specific kernel. See
../provisioning/PROVISIONING_SPEC.md.
"""

# ---------------------------------------------------------------------------
# Network / MQTT broker  (CH9120 does NO DNS -> use a numeric IP)
# ---------------------------------------------------------------------------
BROKER_IP = (192, 168, 1, 10)   # MQTT broker, numeric IP only
BROKER_PORT = 1883
MQTT_USER = None                # or "username"
MQTT_PASS = None                # or "password"
MQTT_KEEPALIVE_S = 30

# CH9120 own network identity
USE_DHCP = False
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
# CH9120 TCP-status pin. DISABLED (None): never HW-validated (POC didn't use it), and
# trusting it caused a false-"down" reconnect FLAP. Liveness comes from MQTT keepalive/
# PINGRESP + CONNACK instead (HW-independent). Re-enable (17) only after verifying on HW.
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
# With MCP_AUTODISCOVER the firmware scans the bus at boot and drives the chips
# that respond; MCP_ADDRESSES is then just the fallback. Set False to pin a list.
MCP_AUTODISCOVER = True
MCP_ADDRESSES = [0x20, 0x21, 0x22, 0x23,
                 0x24, 0x25, 0x26, 0x27]         # fallback / explicit (1..8 chips)
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

DEBOUNCE_MS = 0                # software debounce, ms. 0 leans on the hardware RC
                               # debounce for minimal latency; raise (10-25) if a
                               # noisy input produces spurious gestures.
LONG_MS = 400                  # hold >= this -> long   (POC-proven value)
DOUBLE_GAP_MS = 0              # double-tap window, ms. >0 = single-press latency
                               # (e.g. 250); 0 = double off, single fires instantly
                               # on release (long still works). Set >0 to use double.

# ---------------------------------------------------------------------------
# Identity & topics
# ---------------------------------------------------------------------------
# DEVICE_ID is normally derived from machine.unique_id() at runtime; this is a
# fallback / override.
DEVICE_ID = None               # None -> derive from unique_id() last 6 hex
DEVICE_NAME = "Hearth"
DEVICE_MODEL = "Hearth (DI16-G)"
DEVICE_MANUFACTURER = "OSELIA"
SW_VERSION = "0.6.0"
HW_VERSION = "DI16-G"                   # board model (shown as Hardware in HA)
PROJECT_URL = "https://github.com/vmyronovych/oselia-hearth-di16g-firmware"  # HA discovery origin

BASE_TOPIC = "hearth"             # -> hearth/<device_id>/...
DISCOVERY_PREFIX = "homeassistant"     # HA default

# How inputs appear in HA: "event" (modern entity per input; shows in dashboards &
# logbook, and what the shipped blueprint targets), "trigger" (device_automation
# triggers, the original), or "both". "both" doubles discovery traffic on connect.
INPUT_DISCOVERY = "both"

# Which HA integration consumes this device: "mqtt" (firmware publishes HA MQTT
# discovery; device shows under the MQTT integration -- the default) or "oselia" (the
# first-party OSELIA custom integration owns the entities; firmware skips publishing
# discovery). Data/command topics are identical either way. Set via `provision.py
# --oselia`. See homeassistant/INTEGRATION_SPEC.md.
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
EVENT_QUEUE_SIZE = 128         # gestures buffered while offline; drop-oldest beyond
                               # (sized for up to 5 chips x 16 inputs)

# MQTT connection management
MQTT_CONNECT_TIMEOUT_MS = 4000     # wait for CONNACK
PING_RESPONSE_TIMEOUT_MS = 5000    # no PINGRESP within this -> treat link dead
RECONNECT_BACKOFF_MIN_MS = 1000    # first retry delay
RECONNECT_BACKOFF_MAX_MS = 30000   # cap
DISCOVERY_REPUBLISH_ON_RECONNECT = True

# I2C resilience
I2C_RETRIES = 3                # retry count for MCP reads/writes
MCP_HEALTHCHECK_MS = 2000      # how often core0 re-verifies the MCP responds

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
# Two-way control: subscribe to <base>/<id>/cmd/# and expose HA `button` entities
# (Restart, Identify). Commands are handled on core1 after the gesture queue is
# drained, so they never delay a button publish.
CONTROL_ENABLE = True
# With DHCP the MCU doesn't know its leased IP; net_task reads it once from the
# CH9120 at boot (read-only config round-trip) after this settle for the lease.
# Only happens on the initial bring-up, never on reconnect. 0 disables the read.
DHCP_LEASE_SETTLE_MS = 4000

# OTA application updates over MQTT (A/B slots + boot-confirm/auto-revert). The
# interpreter is not updated OTA; the loader (/boot.py) + /site.json are never bundled
# so OTA can't brick the boot path. See OTA_SPEC.md.
OTA_ENABLE = True
OTA_MAX_BOOT_TRIES = 2          # boots to prove a new build (MUST match boot.py)
OTA_BOOT_CONFIRM_MS = 20000    # MQTT-online + healthy this long -> confirm the build
OTA_CHUNK_SIZE = 1024          # bytes per ota/data chunk (must match the publisher)
OTA_NAK_STALL_MS = 1500        # no chunk for this long -> NAK still-missing chunks
OTA_DOWNLOAD_TIMEOUT_MS = 60000
SLOTS_DIR = "/slots"
OTA_STATE_PATH = "/ota/state"
OTA_STAGING_PATH = "/ota/staging.bin"
