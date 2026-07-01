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
# CH9120 TCP-status pin (GP17) -- DISABLED (None); it caused a reconnect flap. Liveness
# comes from MQTT keepalive/PINGRESP instead (HW-independent).
PIN_CH9120_TCPCS = None
PIN_CH9120_CFG0 = 18           # LOW = config mode (POC: Pin(18))
PIN_CH9120_RST = 19            # active LOW (POC: Pin(19))

# MCP23017 chips on the shared I2C bus (CONFIRMED against POC).
# Up to 8 boards: one "main" (RP2040 + MCP) plus satellite MCP-only boards.
# Each chip needs a distinct address via its A0..A2 strap pins (0x20..0x27 = 8).
# BOARD NUMBER = position in the resolved list (1-based).
I2C_ID = 1                     # manufactured dib-monolith uses I2C1 (GP26/GP27)
PIN_I2C_SDA = 26               # board net SDA -> RP2040-ETH ADC0 pad = GP26 (I2C1 SDA)
PIN_I2C_SCL = 27               # board net SCK -> RP2040-ETH ADC1 pad = GP27 (I2C1 SCL)
I2C_FREQ = 50_000              # 50 kHz: generous rise-time margin for the long ember
                               # satellite runs (cable adds bus capacitance; slower edges
                               # = far fewer NACK/retry/recovery events). POC ran 100k,
                               # 400k was an optimistic "short bus" bump. Even at 8 chips
                               # the per-poll bus time (~8 ms) fits the 20 ms poll window;
                               # imperceptible for wall switches.
# Board discovery: with MCP_AUTODISCOVER the firmware scans the bus at boot and
# drives exactly the chips that respond (0x20..0x27) -- no count to configure, and
# an unwired board simply isn't advertised (no permanent MCP fault). MCP_ADDRESSES
# is then only the fallback used if a scan finds nothing. Set MCP_AUTODISCOVER=False
# (`oselia provision --boards` / site.json board_count does this) to pin an exact list.
MCP_AUTODISCOVER = True
MCP_ADDRESSES = [0x20]         # fallback / explicit list (board1 = 0x20)
PIN_MCP_RESET = 9              # board net RESET -> MCP /RESET (pin 18) on GP9.
                               # Driven HIGH (deasserted) at boot, pulsed LOW to reset
                               # the chips (boot + L2 recovery). None = tied high (POC).

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
SW_VERSION = "0.9.0"
HW_VERSION = "DI16-G"                   # board model (reported in diag/state)

BASE_TOPIC = "hearth"             # -> hearth/<device_id>/...

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

# I2C resilience + MCP recovery (an MCP fault must never freeze inputs or reboot)
I2C_RETRIES = 3                # retry count for MCP reads/writes
I2C_TIMEOUT_US = 50000         # per-transaction hardware timeout (us). Bounds a core0
                               # stall on a dead/floating bus (e.g. an unpowered MCP
                               # board removes the bus pull-ups) so a hung I2C op can
                               # never starve the watchdog. 0/None = port default.
MCP_HEALTHCHECK_MS = 2000      # how often core0 re-verifies/re-inits a down MCP
MCP_POLL_MS = 20               # PERIODIC poll of healthy chips, independent of the
                               # shared INT line. The INT is only a latency
                               # accelerator; input must never depend solely on a
                               # single wired-OR IRQ (a missed/quirky INT would
                               # silently drop presses). ~20 ms = imperceptible for
                               # wall switches, light I2C load.
MCP_RECOVERY_AFTER_FAILS = 3   # consecutive failed health checks on a board before
                               # escalating to a bus/reset recovery
MCP_RESET_SETTLE_MS = 5        # settle delay after a bus reclock / /RESET before
                               # re-init, so the first I2C writes land on a stable bus
                               # (a glitched config write can brick inputs; init() also
                               # read-back-verifies as a backstop)
MCP_RECOVERY_MIN_INTERVAL_MS = 10000  # min spacing between recovery actions (no
                               # thrash); escalates L1 (I2C reclock) -> L2 (/RESET)
MCP_RECOVERY_MAX_INTERVAL_MS = 300000  # backoff cap: the recovery interval doubles
                               # after each action up to this (5 min). A persistently
                               # absent chip must not keep pulsing the SHARED /RESET
                               # line (which would reset the HEALTHY boards too); the
                               # 2 s health-check still auto-recovers a returning chip
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
# via the tool (`oselia provision --no-diag` -> site.json "diag": false).
DIAG_ENABLE = True
DIAG_INTERVAL_S = 10           # how often to refresh the diag/state snapshot
DIAG_FAULT_RING = 16           # recent[] fault-history length in the diag blob (one
                               # retained export then carries the fault timeline)
# Two-way control: subscribe to <base>/<id>/cmd/# and expose HA `button` entities
# (Restart, Identify). Commands are handled on core1 after the gesture queue is
# drained, so they never delay a button publish.
CONTROL_ENABLE = True
# Acceptance test hooks: when True, net_task exposes debug-only fault-injection commands
# (`_debug_stall` -> trips the core1 watchdog for §10; `_debug_mcp_fault <board>` -> forces
# an MCP read fault + recovery for §11) so the hw acceptance suite can PROVE those recovery
# paths over USB+MQTT. MUST stay False in shipped firmware -- production builds compile the
# handlers out. The `oselia provision --acceptance` path flips this on for a bench unit.
ACCEPTANCE_HOOKS = False
# With DHCP the MCU doesn't know its leased IP; net_task reads it once from the
# CH9120 at boot (read-only config round-trip) after this settle for the lease.
# Only happens on the initial bring-up, never on reconnect. 0 disables the read.
DHCP_LEASE_SETTLE_MS = 4000

# ---------------------------------------------------------------------------
# OTA application updates (over MQTT, no CH9120 retarget) -- see docs/ota.md
# ---------------------------------------------------------------------------
# Replaces the app .py files via an A/B slot layout with boot-confirm/auto-revert.
# The interpreter itself is NOT updated over the air (physical BOOTSEL only). The
# loader (/main.py) and /site.json are never part of a bundle, so OTA can't brick the
# boot path or lose identity. Bytes stream as chunks over the live broker session.
OTA_ENABLE = True
OTA_MAX_BOOT_TRIES = 2          # boots a pending build gets to prove itself before
                               # auto-revert (MUST match _MAX_TRIES in main.py loader)
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
# Installer overlay (generated by the `oselia` provisioning tool; do NOT hand-edit)
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
    if "acceptance_hooks" in _site:
        # Bench-only: enable the §10/§11 fault-injection commands for the hw acceptance
        # suite. Production site.json never carries this key -> hooks stay off.
        ACCEPTANCE_HOOKS = bool(_site["acceptance_hooks"])
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
    del _site, _f
except OSError:
    pass   # no site.json -> use the defaults above (bench / dev)
