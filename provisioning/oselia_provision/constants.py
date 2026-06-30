"""Pinned constants shared across the tool. Bump the MicroPython pin in lockstep with
firmware/FLASHING.md and provisioning/uf2/."""

RP2040_VID = "2e8a"                 # Raspberry Pi (RP2040) USB vendor id
DEFAULT_BROKER_PORT = 1883
DEFAULT_BASE_TOPIC = "hearth"       # must match firmware cfg.BASE_TOPIC default
MAX_BOARDS = 8                      # MCP23017 strap range 0x20..0x27
MCP_BASE_ADDR = 0x20

# Pinned MicroPython interpreter (the base image, separate from our firmware src/*.py).
# Must match firmware/FLASHING.md -- bump all three together when the pin changes.
EXPECTED_MPY_VERSION = "1.28.0"
MPY_UF2_NAME = "RPI_PICO-20260406-v1.28.0.uf2"
MPY_UF2_URL = "https://micropython.org/resources/firmware/" + MPY_UF2_NAME
# Raspberry Pi's flash_nuke wipes the ENTIRE RP2040 flash (interpreter + littlefs),
# leaving a bare-metal chip in BOOTSEL. Used by `erase`.
FLASH_NUKE_NAME = "flash_nuke.uf2"
FLASH_NUKE_URL = "https://datasheets.raspberrypi.com/soft/flash_nuke.uf2"

SITE_FILE = "site.json"
SITE_TMP = "site.json.tmp"
