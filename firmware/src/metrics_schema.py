"""Metrics wire schema -- the single source of truth for the compact telemetry format.

The diag/state blob uses SHORT, nested keys (e.g. `c.br` = counters.bus_recoveries) to keep
the retained MQTT payload tiny on a constrained MCU, while staying plain JSON so Home
Assistant `value_json` templates can read it natively (HA cannot parse CBOR/MessagePack).

Every payload carries `v` = SCHEMA_VERSION; consumers branch on it. NEVER repurpose a short
key within a major version -- add a key and bump the version. This module is PURE (no
`machine`/`os`) so the whole metrics core is host-testable under CPython.

The KEY_DICTIONARY at the bottom is the human-readable short->long map; it must stay in sync
with the constants and is the contract mirrored into the HA integration + INTEGRATION_SPEC.md.
"""

SCHEMA_VERSION = 1

# ---- top-level keys ----
K_V = "v"               # schema version
K_SEQ = "q"             # publish sequence (per-boot monotonic)
K_FW = "fw"             # firmware version
K_HW = "hw"             # hardware version
K_MODEL = "md"          # device model
K_RESET = "rc"          # reset cause: power_on|wdt|soft|hard|deepsleep|unknown
K_BOOT = "bc"           # boot count (persists across reboot)
K_UPTIME = "up"         # uptime seconds (this boot)
K_MEM = "mf"            # free heap bytes (instant)
K_MEM_MIN = "ml"        # free heap low-water mark (this boot)
K_TEMP = "tc"           # die temperature C (uncalibrated trend)
K_ETH = "e"             # ethernet link up (bool)
K_MQTT = "m"            # broker connected (bool)
K_IP = "ip"             # ip (redact before egress)
K_HEALTH = "h"          # ok|degraded|mcp_fault|net_fault
K_BTOTAL = "bt"         # boards total
K_BOK = "bo"            # boards responding
K_BADDRS = "ba"         # board addresses
K_BOARDS = "bn"         # resolved board count (legacy `boards`)
K_COUNTERS = "c"        # counters object
K_BOARDS_ARR = "b"      # per-board array
K_RING = "r"            # recent faults ring
K_LASTFAULT = "lf"      # latest fault record
K_CRASH = "cr"          # last crash record
K_LAST = "g"            # last gesture (human string)

# ---- counter sub-keys (under K_COUNTERS); all distinct from each other ----
C_RECONNECTS = "re"
C_MQTT_DISC = "mq"
C_ETH_LOSS = "el"
C_DROPPED = "dr"
C_BUS_REC = "br"
C_MCP_RESET = "mr"
# NOTE: no int_stuck counter. The firmware is PURE POLLING -- the MCP INT/wired-OR line is
# deliberately unused (it was the original freeze/dropped-press cause; see input_task docstring),
# so a "stuck INT" cannot occur. The legacy CODE_INT_STUCK taxonomy entry is dead.

# Fixed counter set (no runtime metric creation -> bounded serialized size).
COUNTER_KEYS = (C_RECONNECTS, C_MQTT_DISC, C_ETH_LOSS, C_DROPPED,
                C_BUS_REC, C_MCP_RESET)

# ---- per-board record keys ----
B_BOARD = "n"           # board index (1-based)
B_ADDR = "a"            # i2c addr "0x20"
B_OK = "ok"
B_CODE = "c"
B_DETAIL = "d"
B_FAILS = "f"           # consecutive fails (this boot)
B_LASTOK = "lo"         # seconds since last ok
B_RECOV = "rv"          # recoveries (this boot)
B_FAILTOTAL = "ft"      # cumulative fails (persists across reboot)

# ---- fault record keys (ring items + last fault) ----
F_UP = "up"             # uptime seconds at the fault
F_BOOT = "bc"           # boot_count at the fault (anchors across reboots)
F_COMP = "cp"           # component: mcp|net|mqtt|sys
F_CODE = "c"
F_DETAIL = "d"
F_BOARD = "b"           # optional board index

# ---- crash record keys ----
CR_BOOT = "bc"
CR_UP = "up"
CR_CAUSE = "rc"
CR_EXC = "x"            # traceback excerpt (capped)

# Bound free-text so a hostile/huge detail or traceback can't bloat the payload.
DETAIL_MAX = 80


def cap(s, n=DETAIL_MAX):
    """Length-cap a free-text string (detail/exc). None -> ''. Pure."""
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def board_to_wire(b):
    """Map a semantic BoardStatus.as_dict() (+ optional fail_total) to the short board
    record. Producers (mcp_health.BoardStatus) stay schema-agnostic; mapping lives here."""
    return {
        B_BOARD: b.get("board"),
        B_ADDR: b.get("addr"),
        B_OK: b.get("ok"),
        B_CODE: b.get("code", ""),
        B_DETAIL: cap(b.get("detail", "")),
        B_FAILS: b.get("fails", 0),
        B_LASTOK: b.get("last_ok_s"),
        B_RECOV: b.get("recoveries", 0),
        B_FAILTOTAL: b.get("fail_total", 0),
    }


def fault_to_wire(rec):
    """Map a semantic fault record {up,boot,component,code,detail[,board]} to short keys."""
    out = {
        F_UP: rec.get("up", 0),
        F_BOOT: rec.get("boot", 0),
        F_COMP: rec.get("component", ""),
        F_CODE: rec.get("code", ""),
        F_DETAIL: cap(rec.get("detail", "")),
    }
    if rec.get("board") is not None:
        out[F_BOARD] = rec["board"]
    return out


# Human-readable short->long dictionary. Source of truth for the HA side + docs.
KEY_DICTIONARY = {
    "v": "schema_version", "q": "publish_seq", "fw": "firmware", "hw": "hardware",
    "md": "model", "rc": "reset_cause", "bc": "boot_count", "up": "uptime_s",
    "mf": "mem_free", "ml": "mem_free_min", "tc": "temp_c (uncalibrated trend)",
    "e": "eth", "m": "mqtt", "ip": "ip", "h": "health", "bt": "boards_total",
    "bo": "boards_ok", "ba": "board_addrs", "bn": "boards",
    "c": "counters{re:reconnects,mq:mqtt_disconnects,el:eth_link_losses,"
         "dr:dropped,br:bus_recoveries,mr:mcp_resets}",
    "b": "boards[]{n:board,a:addr,ok,c:code,d:detail,f:fails,lo:last_ok_s,"
         "rv:recoveries,ft:fail_total}",
    "r": "recent[]{up,bc:boot,cp:component,c:code,d:detail,b:board}",
    "lf": "last_fault (same shape as recent[] item)",
    "cr": "last_crash{bc:boot,up,rc:cause,x:exc}", "g": "last_gesture",
}
