#!/usr/bin/env python3
"""Installer provisioning wizard for the OSELIA Hearth (RP2040-ETH).

Runs on the *installer's laptop* (CPython 3), drives a USB-connected board via the
`mpremote` CLI, and brings a fresh unit online in Home Assistant with a few prompts
and no Python editing. This is the host side of PROVISIONING_SPEC.md.

What it does (happy path -- `python3 provision.py`):
  1. find the RP2040-ETH on USB,
  2. discover the MQTT broker via mDNS (or accept a typed address),
  3. ask for optional credentials and the number of input boards,
  4. validate the broker is reachable (and creds work) BEFORE touching the board,
  5. write a small `site.json` to the board (atomically) + copy `src/*.py`,
  6. reset and confirm the unit reaches "online" in the broker.

The board's own config.py overlays this `site.json` on top of its hardware
defaults, so the firmware needs no edits to consume the installer's answers.

Standalone modes (run, then exit -- they bypass the provisioning flow): `--monitor` streams
the firmware log over USB (it relaunches the firmware over a HELD session -- no flashing --
so USB stays enumerated, since a cold boot wedges USB on this board; `--monitor-passive` just
listens). `--uninstall-*` / `--erase-flash` decommission a unit.

Design split: the pure helpers (IP parsing, site-dict build, bring-up
classification, MQTT packet encode) carry no I/O so they are unit-tested on the
host (tests/test_provision.py). The mpremote / mDNS / socket calls are thin
wrappers around them.

Only optional dependency: `zeroconf` (mDNS auto-discovery). Without it the wizard
falls back to validated manual broker entry. `mpremote` must be on PATH.
"""
import argparse
import csv
import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

RP2040_VID = "2e8a"                 # Raspberry Pi (RP2040) USB vendor id
DEFAULT_BROKER_PORT = 1883
DEFAULT_BASE_TOPIC = "hearth"  # must match cfg.BASE_TOPIC default
MAX_BOARDS = 8                      # MCP23017 strap range 0x20..0x27

# Pinned MicroPython interpreter (the base image, separate from our src/*.py). Must
# match firmware/FLASHING.md -- bump all three together when the pin changes.
EXPECTED_MPY_VERSION = "1.28.0"
MPY_UF2_NAME = "RPI_PICO-20260406-v1.28.0.uf2"
MPY_UF2_URL = "https://micropython.org/resources/firmware/" + MPY_UF2_NAME
# Raspberry Pi's flash_nuke wipes the ENTIRE RP2040 flash (interpreter + littlefs),
# leaving a bare-metal chip in BOOTSEL. Used by --erase-flash.
FLASH_NUKE_NAME = "flash_nuke.uf2"
FLASH_NUKE_URL = "https://datasheets.raspberrypi.com/soft/flash_nuke.uf2"
MCP_BASE_ADDR = 0x20
SITE_FILE = "site.json"
SITE_TMP = "site.json.tmp"
HERE = os.path.dirname(os.path.abspath(__file__))
# UF2 images shipped alongside the wizard so flashing works with no internet (see
# uf2/README.md). The resolver prefers these over the network.
UF2_DIR = os.path.join(HERE, "uf2")
# The firmware lives in the sibling firmware/ folder; provision.py flashes its
# src/*.py to the board. Override with $FIRMWARE_SRC for non-standard layouts.
SRC_DIR = os.environ.get(
    "FIRMWARE_SRC",
    os.path.normpath(os.path.join(HERE, "..", "firmware", "src")))


# ===========================================================================
# Pure helpers (no I/O -- unit-tested on the host)
# ===========================================================================
def is_valid_ipv4(s):
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except ValueError:
        return False


def parse_board_list(stdout):
    """Parse `mpremote connect list` output -> [(port, vid:pid, desc), ...] for
    RP2040 boards only. Each line is `<port> <serial> <vid:pid> <mfr> <product>`;
    the vid:pid is found by token (not fixed column) to tolerate format drift."""
    boards = []
    for line in stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        vidpid = next((p for p in parts[1:]
                       if ":" in p and RP2040_VID in p.lower()), None)
        if vidpid:
            i = parts.index(vidpid)
            boards.append((parts[0], vidpid, " ".join(parts[i + 1:])))
    return boards


def board_count_to_addrs(n):
    """1..MAX_BOARDS -> list of MCP I2C addresses [0x20, 0x21, ...]."""
    if not 1 <= n <= MAX_BOARDS:
        raise ValueError("board count must be 1..%d" % MAX_BOARDS)
    return [MCP_BASE_ADDR + i for i in range(n)]


def parse_names_csv(text):
    """CSV text with rows `board,pin,name` -> [[board, pin, name], ...].

    Lines that are blank or start with '#' are ignored; a header row of
    board,pin,name (any case) is skipped.
    """
    rows = []
    for raw in csv.reader(text.splitlines()):
        if not raw or raw[0].strip().startswith("#"):
            continue
        if len(raw) < 3:
            raise ValueError("names row needs board,pin,name: %r" % (raw,))
        b, p, name = raw[0].strip(), raw[1].strip(), raw[2].strip()
        if b.lower() == "board" and p.lower() == "pin":
            continue                # header
        board, pin = int(b), int(p)
        if not 1 <= board <= MAX_BOARDS or not 1 <= pin <= 16:
            raise ValueError("names row out of range: board=%d pin=%d" % (board, pin))
        rows.append([board, pin, name])
    return rows


def build_site_dict(broker_ip, broker_port, user, password,
                    board_count=None, use_dhcp=True, static=None, names=None,
                    diag=True, ha_integration="mqtt"):
    """Assemble the machine-owned site.json contents from the answers.

    `board_count` is normally None -> the firmware auto-discovers the boards on the
    I2C bus, so it is omitted. Pass an int (wizard --boards) to pin an explicit
    count (the overlay then disables autodiscovery). `static` (if given) =
    {"ip","gateway","mask"} and forces USE_DHCP off. `names` = [board, pin, name] rows.
    `diag` defaults True (firmware default); only written when False (--no-diag) so a
    happy-path site.json stays minimal.
    `ha_integration` is "mqtt" (firmware publishes HA discovery -- the default) or
    "oselia" (the OSELIA custom integration owns the entities; firmware skips
    discovery). Only written when not the "mqtt" default, to keep site.json minimal.
    """
    if not is_valid_ipv4(broker_ip):
        raise ValueError("broker_ip must be numeric IPv4, got %r" % broker_ip)
    site = {
        "broker_ip": broker_ip,
        "broker_port": int(broker_port),
        "mqtt_user": user or None,
        "mqtt_pass": password or None,
        "use_dhcp": bool(use_dhcp) and static is None,
    }
    if board_count is not None:
        site["board_count"] = int(board_count)
    if static is not None:
        for k in ("ip", "gateway", "mask"):
            if not is_valid_ipv4(static[k]):
                raise ValueError("static %s must be IPv4, got %r" % (k, static[k]))
        site["static"] = {k: static[k] for k in ("ip", "gateway", "mask")}
        site["use_dhcp"] = False
    if names:
        site["names"] = names
    if not diag:
        # Default is on (firmware DIAG_ENABLE=True); only record the opt-out.
        site["diag"] = False
    if ha_integration and ha_integration != "mqtt":
        # Default is "mqtt" (firmware HA_INTEGRATION default); only record the switch.
        site["ha_integration"] = ha_integration
    return site


# Bring-up markers, taken verbatim from the firmware's serial log lines so the
# wizard maps a failure to a specific cause instead of a stack trace (spec §3.7).
def classify_bringup(text):
    """Inspect captured board serial -> (status, message).

    status in {"pass","ethernet","mqtt","mcp","unknown"}. Best-effort: the
    authoritative PASS signal is the retained MQTT status topic (see
    mqtt_wait_online); this maps serial to a human cause for the FAIL paths.
    """
    # "published" = MQTT-discovery mode; "skipped" = oselia mode (custom integration
    # owns the entities). Either line means the board reached online + ready.
    if "HA discovery published" in text or "HA discovery skipped" in text:
        return ("pass", "Board reached HA bring-up and is online.")
    if "CH9120 TCP down" in text or "CH9120 re-bringup failed" in text:
        return ("ethernet",
                "Ethernet/TCP to the broker is down -- check the cable and that "
                "the broker IP is reachable from the board's network.")
    if "no MCP chips responding" in text or "MCP@0x" in text and "init failed" in text:
        return ("mcp",
                "No input board is responding -- check the I2C wiring and that the "
                "board count matches the chips actually installed.")
    if "connect failed" in text or "MQTT connect" in text:
        return ("mqtt",
                "Reached the network but the MQTT session didn't complete -- check "
                "the broker IP/port and username/password.")
    return ("unknown", "Could not determine bring-up state from serial output.")


# ---- minimal MQTT 3.1.1 encode (mirrors src/mqtt_packets.py wire format) ----
def _mqtt_str(s):
    if isinstance(s, str):
        s = s.encode()
    return struct.pack(">H", len(s)) + s


def build_connect(client_id, keepalive=15, user=None, password=None):
    flags = 0x02                                # clean session
    if user is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    var = _mqtt_str("MQTT") + bytes([0x04, flags]) + struct.pack(">H", keepalive)
    payload = _mqtt_str(client_id)
    if user is not None:
        payload += _mqtt_str(user)
    if password is not None:
        payload += _mqtt_str(password)
    body = var + payload
    return bytes([0x10]) + _encode_rl(len(body)) + body


def build_subscribe(packet_id, topic):
    body = struct.pack(">H", packet_id) + _mqtt_str(topic) + b"\x00"   # QoS0
    return bytes([0x82]) + _encode_rl(len(body)) + body


def build_publish(topic, payload=b"", retain=False):
    body = _mqtt_str(topic) + payload                  # QoS0 -> no packet id
    header = 0x30 | (0x01 if retain else 0x00)
    return bytes([header]) + _encode_rl(len(body)) + body


def _encode_rl(n):
    out = bytearray()
    while True:
        d = n & 0x7F
        n >>= 7
        if n:
            d |= 0x80
        out.append(d)
        if not n:
            return bytes(out)


# ===========================================================================
# I/O wrappers
# ===========================================================================
def _mqtt_read_packet(sock):
    """Read one MQTT packet -> (type_byte, body_bytes) or None on EOF/timeout."""
    try:
        hdr = sock.recv(1)
        if not hdr:
            return None
        mult, length = 1, 0
        while True:
            b = sock.recv(1)
            if not b:
                return None
            length += (b[0] & 0x7F) * mult
            if not (b[0] & 0x80):
                break
            mult *= 128
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return (hdr[0], body)
    except socket.timeout:
        return None


def mqtt_validate(ip, port, user, password, timeout=5.0):
    """TCP-connect and (if creds given) MQTT CONNECT. -> (ok, detail)."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        return (False, "cannot reach %s:%d (%s)" % (ip, port, e))
    try:
        sock.settimeout(timeout)
        sock.sendall(build_connect("provision-check", user=user, password=password))
        pkt = _mqtt_read_packet(sock)
        if pkt is None or pkt[0] != 0x20 or len(pkt[1]) < 2:
            return (False, "no/invalid CONNACK from broker (is it MQTT?)")
        code = pkt[1][1]
        if code == 0:
            return (True, "broker accepted the connection")
        reasons = {1: "unacceptable protocol version", 2: "client id rejected",
                   3: "server unavailable", 4: "bad username or password",
                   5: "not authorized"}
        return (False, "broker refused: %s" % reasons.get(code, "code %d" % code))
    finally:
        try:
            sock.sendall(b"\xE0\x00")           # DISCONNECT
        except OSError:
            pass
        sock.close()


def mqtt_wait_online(ip, port, user, password, base_topic, timeout=45.0):
    """Subscribe to <base>/+/status and wait for a retained/published 'online'.

    This is the authoritative PASS signal: it is broker-side truth and does not
    depend on parsing the board's serial. -> (ok, device_id_or_None).

    keepalive=0 disables the MQTT keepalive timeout: this watcher only reads and
    never sends, so a non-zero keepalive would have the broker drop it for
    inactivity (~1.5x) long before a slow board (DHCP lease + discovery burst +
    first-connect reconnect) publishes 'online'. With 0 the broker keeps it open.
    """
    topic = base_topic + "/+/status"
    deadline = time.time() + timeout
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return (False, None)
    try:
        sock.settimeout(2.0)
        sock.sendall(build_connect("provision-watch", keepalive=0,
                                   user=user, password=password))
        pkt = _mqtt_read_packet(sock)
        if pkt is None or pkt[0] != 0x20:
            return (False, None)
        sock.sendall(build_subscribe(1, topic))
        while time.time() < deadline:
            pkt = _mqtt_read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30:            # PUBLISH
                body = pkt[1]
                tlen = struct.unpack(">H", body[:2])[0]
                ptopic = body[2:2 + tlen].decode("utf-8", "replace")
                payload = body[2 + tlen:]
                if payload == b"online" and ptopic.endswith("/status"):
                    parts = ptopic.split("/")
                    dev = parts[1] if len(parts) >= 3 else None
                    return (True, dev)
        return (False, None)
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()


def mqtt_list_online(ip, port, user, password, base_topic, timeout=5.0):
    """Subscribe to <base>/+/status and return the device ids currently 'online' (collected
    over a short window). NETWORK-only -- it does NOT touch the board over USB, so a running
    unit's MQTT session stays alive (required so it can still receive the maintenance command
    we send next). -> list[str] (may be empty)."""
    topic = base_topic + "/+/status"
    deadline = time.time() + timeout
    state = {}
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return []
    try:
        sock.settimeout(2.0)
        sock.sendall(build_connect("provision-scan", keepalive=0,
                                   user=user, password=password))
        pkt = _mqtt_read_packet(sock)
        if pkt is None or pkt[0] != 0x20:
            return []
        sock.sendall(build_subscribe(1, topic))
        while time.time() < deadline:
            pkt = _mqtt_read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30:                # PUBLISH
                body = pkt[1]
                tlen = struct.unpack(">H", body[:2])[0]
                ptopic = body[2:2 + tlen].decode("utf-8", "replace")
                payload = body[2 + tlen:]
                parts = ptopic.split("/")
                if ptopic.endswith("/status") and len(parts) >= 3:
                    state[parts[1]] = (payload == b"online")
        return [d for d, up in state.items() if up]
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()


def mqtt_send_command(ip, port, user, password, base_topic, device_id, name, payload=b""):
    """Publish <base>/<id>/cmd/<name> (QoS0, not retained) -> ok bool. Used to send the
    cooperative maintenance/quiesce command to a running unit over the broker."""
    topic = "%s/%s/cmd/%s" % (base_topic, device_id, name)
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return False
    try:
        sock.settimeout(5.0)
        sock.sendall(build_connect("provision-cmd", user=user, password=password))
        if _mqtt_read_packet(sock) is None:
            return False
        sock.sendall(build_publish(topic, payload))
        time.sleep(0.3)
        return True
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()


def resolve_to_ipv4(host):
    """A typed hostname is resolved on the laptop; the numeric result is what we
    write to the board (the CH9120 does no DNS). -> ip string or None."""
    if is_valid_ipv4(host):
        return host
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


# ---- mpremote ----
def _raw_repl_flake(r):
    """True if the failure looks like a transient raw-REPL entry problem -- typically a
    board running the firmware whose watchdog resets it mid-attempt. Worth a retry."""
    blob = ((r.stderr or "") + (r.stdout or "")).lower()
    return ("could not enter raw repl" in blob or "no response" in blob
            or "failed to access" in blob)


def _mpremote(args, port=None, timeout=30, check=True, retries=2):
    cmd = ["mpremote"]
    if port:
        cmd += ["connect", port]
    cmd += args
    r = None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            die("`mpremote` not found on PATH. Install it: pipx install mpremote")
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(cmd, 1, "", "timeout")
        if r.returncode == 0 or not _raw_repl_flake(r) or attempt == retries:
            break
        time.sleep(0.8)                      # let a watchdog-reset board reach a fresh boot
    if check and r.returncode != 0:
        raise RuntimeError("mpremote %s failed: %s" % (" ".join(args),
                                                        r.stderr.strip() or r.stdout.strip()))
    return r


def find_boards():
    """`mpremote connect list` -> [(port, vid:pid, desc), ...] RP2040 only."""
    return parse_board_list(_mpremote(["connect", "list"], check=False).stdout)


def board_has_micropython(port):
    r = _mpremote(["exec", "import sys; print(sys.implementation.name)"],
                  port=port, check=False)
    return "micropython" in r.stdout.lower()


def port_is_micropython(port, wait_s=0):
    """True if `port` is ENUMERATED as an RP2040 MicroPython serial device, read from the USB
    descriptor via `mpremote connect list`. Reliable even when a REPL exec flakes (the
    watchdog firmware resetting the port): a board in BOOTSEL is USB mass-storage and is NOT
    listed, so a hit means MicroPython is genuinely running. Polls up to `wait_s` seconds --
    the board may be mid-re-enumeration after a quiesce reset, and a single snapshot can
    falsely read 'absent' and trigger a needless (USB-wedging) reflash. Use this -- not a
    None from read_mpy_version -- to decide 'is MicroPython present'."""
    end = time.time() + wait_s
    while True:
        if any(p == port for p, _vid, _desc in find_boards()):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


def read_existing_site(port):
    r = _mpremote(["fs", "cat", ":" + SITE_FILE], port=port, check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def write_site_atomic(port, site, dry_run):
    blob = json.dumps(site, indent=2)
    if dry_run:
        print("\n--- would write %s ---\n%s\n" % (SITE_FILE, blob))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        tf.write(blob)
        local = tf.name
    try:
        _mpremote(["fs", "cp", local, ":" + SITE_TMP], port=port)
        # Atomic swap on the board so an aborted run never leaves a half file.
        _mpremote(["exec",
                   "import os; os.rename('%s','%s')" % (SITE_TMP, SITE_FILE)],
                  port=port)
    finally:
        os.unlink(local)


# A fresh boot-confirm state: app in slot a, active, not pending (firmware ota.py).
_FRESH_OTA_STATE = ('{"active": "a", "previous": "a", "pending": false, '
                    '"tries": 0, "crashes": 0}')
# Clear flat-layout app modules a pre-OTA install left at root (the slot shadows them
# via sys.path, but clear them so a re-provisioned unit migrates cleanly onto slots).
# Never touches /boot.py, /site.json, or anything outside root (e.g. /slots, /ota).
_CLEAN_FLAT_ROOT = ("import os\n"
                    "for _f in os.listdir('/'):\n"
                    "    if _f.endswith('.py') and _f != 'boot.py':\n"
                    "        try:\n"
                    "            os.remove('/' + _f)\n"
                    "        except OSError:\n"
                    "            pass\n")


def copy_firmware(port, dry_run):
    """Deploy the OTA A/B slot layout (firmware/OTA_SPEC.md), so a freshly provisioned
    unit is OTA-ready out of the box: the app .py go into /slots/a, a fresh /ota/state
    is written, old flat-layout root modules are cleared, and the /boot.py loader is
    installed LAST -- an interrupted copy then leaves a stable REPL, not a boot.py reset
    loop."""
    files = sorted(f for f in os.listdir(SRC_DIR) if f.endswith(".py"))
    app = [f for f in files if f != "boot.py"]   # the loader is installed separately
    if dry_run:
        print("--- would deploy OTA slot layout: /slots/a/{%d app files} + /boot.py "
              "loader + /ota/state" % len(app))
        return
    for d in ("/slots", "/slots/a", "/ota"):
        _mpremote(["fs", "mkdir", ":" + d], port=port, check=False)   # ignore 'exists'
    # One mpremote invocation for all app files (like tools/deploy.sh): far faster and
    # avoids the WDT rebooting the board between per-file calls.
    paths = [os.path.join(SRC_DIR, f) for f in app]
    _mpremote(["fs", "cp"] + paths + [":slots/a/"], port=port, timeout=120)
    _mpremote(["exec", "open('/ota/state', 'w').write(%r)" % _FRESH_OTA_STATE],
              port=port)
    _mpremote(["exec", _CLEAN_FLAT_ROOT], port=port, check=False)
    # Loader LAST: only now does the board have a runnable entry point.
    _mpremote(["fs", "cp", os.path.join(SRC_DIR, "boot.py"), ":boot.py"], port=port)
    print("Deployed OTA slot layout: %d app files in /slots/a + loader." % len(app))


def reset_board(port):
    _mpremote(["reset"], port=port, check=False)


def _disable_app(port):
    """Quiesce a running unit before filesystem writes. The firmware owns a watchdog
    (core 0), which resets the board mid-raw-REPL when mpremote interrupts it -- so
    `fs cp` flakes with 'could not enter raw repl'. Park the auto-run entry (/boot.py on
    an OTA-layout board, else /main.py) and hard-reset in ONE exec; the board reboots to
    a bare REPL (no app, no watchdog) where writes are reliable.

    Self-verifying: after each attempt we read the FS back -- on a bare REPL no auto-run
    entry remains; if the firmware is still running the read itself flakes, so we retry.
    Returns True once quiesced (restore drops/renames the parked backup safely).

    First, though, check whether there's anything to park. A freshly flashed bare board
    (no boot.py and no main.py) has no firmware to quiesce, so resetting it is pointless
    AND harmful: machine.reset() drops the USB-CDC, and the 3x retry loop below would fire
    three rapid resets that can wedge USB enumeration entirely (see the dual-core boot-wedge
    investigation). On a bare board return immediately with nothing parked."""
    probe = _mpremote(["exec", "import os; _l = os.listdir(); "
                               "print('boot.py' in _l or 'main.py' in _l)"],
                      port=port, check=False)
    pout = (probe.stdout or "").strip().splitlines()
    if probe.returncode == 0 and pout and pout[-1].strip() == "False":
        return False                         # bare board: nothing to park, do NOT reset
    rename = ("import os, machine\n"
              "entry = 'boot.py' if 'boot.py' in os.listdir() else 'main.py'\n"
              "bak = entry + '.provbak'\n"
              "try:\n"
              "    os.remove(bak)\n"
              "except OSError:\n"
              "    pass\n"
              "try:\n"
              "    os.rename(entry, bak)\n"
              "except OSError:\n"
              "    pass\n"
              "machine.reset()\n")
    for _ in range(3):
        _mpremote(["exec", rename], port=port, check=False, timeout=10)
        time.sleep(2.0)                      # board hard-resets toward a bare REPL
        r = _mpremote(["exec", "import os; _l = os.listdir(); "
                               "print('boot.py' not in _l and 'main.py' not in _l)"],
                      port=port, check=False)
        out = (r.stdout or "").strip().splitlines()
        if out and out[-1].strip() == "True":
            return True                      # quiesced: no auto-run entry -> bare REPL
    print("  (note: could not fully quiesce the firmware; writes may need a retry)")
    return True


def _restore_app(port, quiesced):
    """Undo _disable_app. If a loader is present (copy_firmware reinstalled /boot.py) the
    parked backup is obsolete -> drop it. Otherwise (e.g. --no-flash) rename the parked
    entry back so the board still boots its existing app. Only one entry is ever parked."""
    if not quiesced:
        return
    script = ("import os\n"
              "_l = os.listdir()\n"
              "_loader = 'boot.py' in _l\n"
              "for _bak in ('boot.py.provbak', 'main.py.provbak'):\n"
              "    if _bak not in _l:\n"
              "        continue\n"
              "    try:\n"
              "        if _loader:\n"
              "            os.remove(_bak)\n"
              "        else:\n"
              "            os.rename(_bak, _bak[:-8])\n"   # _bak[:-8] strips '.provbak'
              "    except OSError:\n"
              "        pass\n")
    _mpremote(["exec", script], port=port, check=False)


def _wait_for_bare_repl(port, timeout=30):
    """After a cooperative maintenance reset, wait for the board to re-enumerate and confirm
    it is at a BARE REPL (no boot.py/main.py auto-run -> no watchdog). -> port (possibly a new
    path) or None. A bare board responds to an exec instantly (no WDT to fight)."""
    end = time.time() + timeout
    while time.time() < end:
        boards = find_boards()
        cand = (port if any(b[0] == port for b in boards)
                else (boards[0][0] if boards else None))
        if cand:
            r = _mpremote(["exec", "import os; _l = os.listdir(); "
                                   "print('boot.py' not in _l and 'main.py' not in _l)"],
                          port=cand, check=False)
            out = (r.stdout or "").strip().splitlines()
            if out and out[-1].strip() == "True":
                return cand
        time.sleep(1.0)
    return None


def _cooperative_quiesce(port):
    """Quiesce a RUNNING firmware unit the SAFE way: ask it over MQTT to park its loader and
    reset ITSELF (firmware-driven -- no host REPL break-in, so no hardware-watchdog race and
    no USB wedge -- see PROVISIONING_SPEC.md). -> the (bare-REPL) port on success, else None
    (caller falls back to the USB-driven _disable_app).

    Finds the unit on the network WITHOUT touching USB (so its MQTT session stays alive to
    receive the command): discover brokers (mDNS, then a LAN scan), and on each look for an
    'online' device on <base>/+/status. Acts only when EXACTLY ONE unit is online
    (unambiguous) on a no-auth broker -- we have no broker creds yet at quiesce time, so an
    auth broker falls through to _disable_app. On a match it publishes the maintenance command
    and waits for the board to come back as a bare REPL."""
    brokers = (discover_brokers_mdns() or []) or _scan_lan(_probe_mqtt)
    for bip, bport in brokers:
        devs = mqtt_list_online(bip, bport, None, None, DEFAULT_BASE_TOPIC, timeout=5.0)
        if len(devs) != 1:
            continue                              # zero or ambiguous -> can't target safely
        dev = devs[0]
        print("  unit %s is online -- asking it to enter maintenance mode "
              "(cooperative quiesce, no watchdog fight) ..." % dev)
        if not mqtt_send_command(bip, bport, None, None, DEFAULT_BASE_TOPIC,
                                 dev, "maintenance"):
            continue
        newport = _wait_for_bare_repl(port, timeout=30)
        if newport:
            print("  unit parked itself -> bare REPL on %s (clean; no USB wedge)." % newport)
            return newport
    return None


# ===========================================================================
# MicroPython interpreter: version check + (optional) auto-flash
# ===========================================================================
def _parse_uname_release(stdout):
    """Last non-empty line of `os.uname().release` output, e.g. '1.28.0'. -> str|None."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def read_mpy_version(port):
    """MicroPython version on the board (`os.uname().release`), e.g. '1.28.0'. -> str|None."""
    if not port:
        return None
    r = _mpremote(["exec", "import os; print(os.uname().release)"], port=port, check=False)
    return _parse_uname_release(r.stdout) if r.returncode == 0 else None


def _find_rpi_rp2_mount():
    """Path to a mounted RP2040 BOOTSEL drive (RPI-RP2), or None (macOS + Linux)."""
    import glob
    for c in (["/Volumes/RPI-RP2"] + glob.glob("/media/*/RPI-RP2")
              + glob.glob("/run/media/*/RPI-RP2")):
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "INFO_UF2.TXT")):
            return c
    return None


def _wait_for_bootsel(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        m = _find_rpi_rp2_mount()
        if m:
            return m
        time.sleep(0.5)
    return None


def _wait_for_micropython(timeout=60):
    """Wait for a board to re-enumerate on USB after a flash. -> port|None.

    Detection is by USB re-enumeration: a serial port at the RP2040 vendor id appears in
    `mpremote connect list` (a board still in BOOTSEL is USB mass-storage and is never
    listed, so a hit means MicroPython is running). We deliberately do NOT probe with a
    REPL exec here -- a UF2 flash preserves littlefs, so a board carrying a prior OTA
    layout (/boot.py + /slots/a) boots straight into the watchdog-guarded firmware, whose
    hardware WDT resets the board the instant mpremote breaks in. An exec probe would race
    that reset and spuriously report "no board"; the caller quiesces the firmware
    (_disable_app) once the port is back."""
    end = time.time() + timeout
    while time.time() < end:
        boards = find_boards()
        if boards:
            return boards[0][0]
        time.sleep(1.0)
    return None


def _cached_uf2(url, name, override, min_size=1000):
    """Resolve a UF2 to a local path, in order of preference:
      1. `override` (an explicit --*-uf2 path),
      2. a copy bundled with the wizard in uf2/ (offline, repo-shipped),
      3. the per-user cache (~/.cache/oselia),
      4. download from `url` (and cache it).
    Returns the path, or None on failure."""
    if override:
        if not os.path.isfile(override):
            die("UF2 file not found: %s" % override)
        return override
    bundled = os.path.join(UF2_DIR, name)
    if os.path.isfile(bundled) and os.path.getsize(bundled) > min_size:
        return bundled                         # shipped with the wizard -> no network
    cache = os.path.expanduser("~/.cache/oselia")
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > min_size:
        return dest                            # already cached
    print("  downloading %s ..." % name)
    try:
        import urllib.request
        tmp = dest + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
        return dest
    except Exception as e:
        print("  download failed: %s" % e)
        return None


def _resolve_uf2(args):
    """The pinned MicroPython UF2 (--mpy-uf2 or cached download), or None."""
    uf2 = _cached_uf2(MPY_UF2_URL, MPY_UF2_NAME, args.mpy_uf2, min_size=100000)
    if not uf2:
        print("  Fetch it manually and pass --mpy-uf2 PATH (see firmware/FLASHING.md):")
        print("    " + MPY_UF2_URL)
    return uf2


def _enter_bootsel(port):
    """Ensure the board is in BOOTSEL and return the mounted RPI-RP2 path (or die).
    Reboots a running board via machine.bootloader(); on a bare board, walks the
    installer through the BOOT+RESET dance."""
    if _find_rpi_rp2_mount():
        pass                                   # already in BOOTSEL (e.g. a bare board)
    elif port and board_has_micropython(port):
        print("Rebooting the board into BOOTSEL (machine.bootloader) ...")
        _mpremote(["exec", "import machine; machine.bootloader()"], port=port, check=False)
    else:
        print("Put the board into BOOTSEL: hold BOOT + RESET, release RESET, then "
              "release BOOT (see firmware/FLASHING.md).")
        input("  Press Enter once the board is in BOOTSEL ...")
    mount = _wait_for_bootsel(timeout=30)
    if not mount:
        die("BOOTSEL drive (RPI-RP2) didn't appear -- see firmware/FLASHING.md.")
    return mount


def flash_micropython(args, port, wipe=False):
    """Put the board in BOOTSEL, copy the pinned UF2, and wait for it to come back
    running MicroPython. Returns the (re-enumerated) port; dies on failure.

    `wipe=True` first erases the WHOLE flash (flash_nuke) so the board boots to a clean
    bare REPL. Use it on the BOOTSEL / `acquire_board` path -- a board we can NOT reach
    over the REPL to park its app first. A plain UF2 flash preserves littlefs, so a prior
    OTA layout (/boot.py + /slots/a) would auto-run on the next boot; if that firmware boot
    wedges USB enumeration (a watchdog/reset interaction observed on hardware), the board
    never re-appears and provisioning is stuck. Wiping guarantees a bare boot with stable
    USB. On the *upgrade* path the caller has already quiesced /boot.py, so the board boots
    bare WITHOUT a wipe and littlefs (site.json) is preserved -- pass wipe=False.

    The non-wipe UF2 flash preserves the board's littlefs, so a prior site.json survives an
    interpreter upgrade (see firmware/FLASHING.md)."""
    uf2 = _resolve_uf2(args)
    if not uf2:
        die("No MicroPython UF2 available to flash.")
    mount = _enter_bootsel(port)
    if wipe:
        nuke = _cached_uf2(FLASH_NUKE_URL, FLASH_NUKE_NAME, args.erase_uf2)
        if not nuke:
            die("No flash_nuke UF2 available to wipe the board (pass --erase-uf2 PATH).")
        print("Erasing the board's flash first for a clean install -> %s ..." % mount)
        try:
            shutil.copy(nuke, mount)
        except OSError:
            pass        # board wipes + reboots to BOOTSEL as the UF2 lands; tail error normal
        mount = _wait_for_bootsel(timeout=30)
        if not mount:
            die("BOOTSEL didn't re-appear after the flash wipe -- re-plug and retry.")
        # The freshly nuked BOOTSEL volume has only just re-enumerated; copying the
        # next UF2 the instant it mounts races macOS finishing the mount and the copy
        # silently fails (the except below swallows it), leaving the board in BOOTSEL
        # with no valid image. Let the mount settle before the MicroPython flash.
        time.sleep(2.0)
    print("Flashing %s -> %s ..." % (os.path.basename(uf2), mount))
    # Copy-then-confirm with a retry: on a SUCCESSFUL flash the board reboots out of
    # BOOTSEL mid-copy (the OSError tail is expected), so we can't trust the copy's
    # return -- the real signal is the board leaving BOOTSEL / re-appearing on serial.
    # If it's still sitting in BOOTSEL, the copy didn't take (mount race); re-copy.
    newport = None
    for attempt in range(2):
        try:
            shutil.copy(uf2, mount)
        except OSError:
            pass        # board reboots itself as the UF2 lands; a copy-tail error is normal
        print("  waiting for the board to come back online ...")
        newport = _wait_for_micropython(timeout=60)
        if newport:
            break
        mount = _find_rpi_rp2_mount()
        if not mount:                          # left BOOTSEL but never re-enumerated
            break
        print("  still in BOOTSEL -- the UF2 copy didn't take; retrying ...")
        time.sleep(2.0)
    if not newport:
        die("Board didn't re-appear as MicroPython after flashing -- re-plug and retry.")
    # A non-wipe UF2 flash preserves littlefs: if a prior OTA layout is on the board
    # (/boot.py + /slots/a) it boots STRAIGHT into the watchdog-guarded firmware, whose
    # hardware WDT resets the board whenever mpremote interrupts it -- defeating the version
    # read and file writes that follow. Park the app to a bare, watchdog-free REPL now (same
    # mechanism the write path uses). A wiped board is already bare, so skip it there.
    if not wipe:
        _disable_app(newport)
    print("Flashed MicroPython %s." % (read_mpy_version(newport) or "?"))
    return newport


def ensure_micropython(args, port):
    """Verify the board runs the expected MicroPython; reflash only on a real version
    mismatch (WIPED, so it can't wedge). Returns the port to keep using.

    CRITICAL: a board that ENUMERATES as a MicroPython device is never flashed just because
    the version didn't read. On this board a running unit can't always be quiesced (the
    watchdog hard-resets it when the wizard breaks in), so read_mpy_version flakes to None --
    and a needless non-wipe reflash then preserves the auto-running firmware and WEDGES USB on
    the cold boot (the board vanishes; see firmware boot-wedge notes). So: a None read on a
    confirmed-MicroPython board (polled, since it may be mid-re-enumeration) => skip the flash
    and continue. If the board has dropped off USB entirely (the pause wedged it) we DIE with
    BOOTSEL recovery guidance rather than a wedge-prone flash. Any flash we DO perform wipes
    first (clean bare boot, stable USB)."""
    ver = read_mpy_version(port)
    if ver is None and port_is_micropython(port, wait_s=10):
        # Version unreadable but the board IS a MicroPython device -> the REPL flaked (running
        # firmware), or the up-front quiesce just reset it and it's back. Re-quiesce + retry;
        # never fall through to a flash on this path.
        for _ in range(2):
            _disable_app(port)
            ver = read_mpy_version(port)
            if ver:
                break
    if ver == EXPECTED_MPY_VERSION:
        print("MicroPython %s detected -- matches the pinned build." % ver)
        return port
    if ver:
        print("MicroPython on the board is %s, but this firmware pins %s."
              % (ver, EXPECTED_MPY_VERSION))
        if not confirm("Re-flash MicroPython %s now?" % EXPECTED_MPY_VERSION, default=True):
            print("  Keeping %s -- the pinned build carries features/fixes the firmware "
                  "expects (firmware/FLASHING.md)." % ver)
            return port
        # WIPE: a non-wipe flash preserves the auto-running firmware, which cold-boot-wedges
        # USB on this board. A clean wipe boots bare -> stable USB. (site.json is re-entered.)
        return flash_micropython(args, port, wipe=True)
    if port_is_micropython(port, wait_s=10):
        # MicroPython present (enumerates) but its version never read -- the firmware keeps
        # resetting the REPL. Do NOT flash (a needless reflash wedges USB here); continue and
        # let provisioning's writes retry against the firmware.
        print("MicroPython is present on the board, but its version couldn't be read -- the "
              "running firmware keeps resetting the REPL.")
        print("  Skipping the interpreter flash to avoid a needless reflash that can wedge "
              "USB. Continuing. If the interpreter is genuinely wrong, run "
              "`python3 provision.py --erase-flash` then re-provision for a clean start.")
        return port
    # The board is NOT on USB anymore. It was a MicroPython board when we acquired it, so it
    # didn't "lose" MicroPython -- pausing the watchdog firmware most likely hard-reset it and
    # the cold boot wedged USB enumeration. Do NOT offer a (non-wipe) flash here: we can't
    # even reach the board, and a non-wipe flash would just re-wedge. Recover cleanly via
    # BOOTSEL, where acquire_board does a WIPED flash.
    die("The board dropped off USB while pausing its firmware. On this hardware the firmware's"
        " watchdog can hard-reset the board when the wizard breaks in, and a cold boot can\n"
        "  wedge USB enumeration -- so a RUNNING unit can't always be re-provisioned in place.\n"
        "  Recover it for a CLEAN re-provision: hold the BOOT button while plugging in USB (it\n"
        "  mounts as RPI-RP2), then re-run `python3 provision.py` -- it does a wiped flash and\n"
        "  provisions. See firmware/FLASHING.md.")


_DEVICE_ID_RE = re.compile(r"^[0-9A-F]{6}$")


def read_device_id(port):
    """Compute the device id the firmware will use (last 6 hex of unique_id,
    upper) so we can target its retained status topic. -> id str or None.

    The id is printed behind a fixed marker and the result is format-VALIDATED: a
    running board emits boot-log lines over the same USB-CDC, so blindly trusting the
    REPL output (e.g. the last line) can capture serial noise. A bad id must never be
    returned -- it would be published as an MQTT topic segment (clear_retained_status)
    and spawn a phantom gateway/device in the OSELIA integration."""
    code = ("import machine,ubinascii;"
            "print('OSELIA_ID:'+ubinascii.hexlify(machine.unique_id()).decode()[-6:].upper())")
    r = _mpremote(["exec", code], port=port, check=False)
    for line in (r.stdout or "").splitlines():
        if "OSELIA_ID:" in line:
            cand = line.split("OSELIA_ID:", 1)[1].strip()
            if _DEVICE_ID_RE.match(cand):
                return cand
    return None


def clear_retained_status(ip, port, user, password, base_topic, device_id):
    """Delete the retained status message for this device (empty retained payload)
    so the post-reset wait can't false-pass on a stale 'online' from a prior run."""
    topic = "%s/%s/status" % (base_topic, device_id)
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return
    try:
        sock.settimeout(5.0)
        sock.sendall(build_connect("provision-clear", user=user, password=password))
        if _mqtt_read_packet(sock) is None:
            return
        sock.sendall(build_publish(topic, b"", retain=True))   # empty retained = clear
        time.sleep(0.3)
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()


# ANSI colour per firmware log level prefix ([E]/[W]/[I]/[D] from src/log.py). INFO is
# left uncoloured (the common case) so only WARN/ERROR/DEBUG stand out.
_LOG_COLORS = {"E": "\x1b[31m", "W": "\x1b[33m", "D": "\x1b[2m"}


def colorize_log_line(line):
    """Colour a firmware serial log line by its level prefix ('[E] ...', '[W] ...',
    '[D] ...'). INFO lines and anything without a recognised prefix (boot banners,
    tracebacks) pass through unchanged. Pure -- the caller decides whether colour is
    wanted (a TTY, honouring NO_COLOR); see _supports_color."""
    if len(line) >= 3 and line[0] == "[" and line[2] == "]":
        col = _LOG_COLORS.get(line[1])
        if col:
            return col + line + "\x1b[0m"
    return line


def _has_pyserial():
    try:
        import serial  # noqa: F401
        return True
    except ImportError:
        return False


def _read_serial_until_closed(port, on_chunk):
    """Open `port` and feed decoded text chunks to on_chunk(text) until the port errors
    (the board reset/unplugged) or KeyboardInterrupt. PASSIVE read -- it never enters the
    raw REPL, so the running firmware (and its hardware watchdog) is undisturbed, unlike
    `mpremote exec/run`. pyserial preferred; raw POSIX read of the USB-CDC tty as a
    fallback (macOS/Linux; Windows COM ports need pyserial)."""
    if _has_pyserial():
        import serial                              # type: ignore
        try:
            with serial.Serial(port, 115200, timeout=0.5) as s:
                while True:
                    data = s.read(256)
                    if data:
                        on_chunk(data.decode("utf-8", "replace"))
        except serial.SerialException:
            return                                 # board rebooted / port went away
        return
    try:
        with open(port, "rb", buffering=0) as f:
            os.set_blocking(f.fileno(), False)
            while True:
                try:
                    chunk = f.read(256)
                except BlockingIOError:
                    time.sleep(0.05)               # no data yet -- not a disconnect
                    continue
                except OSError:
                    return                         # device gone -> let caller reconnect
                if chunk:
                    on_chunk(chunk.decode("utf-8", "replace"))
                else:
                    time.sleep(0.05)
    except OSError:
        return


def stream_serial(port, colorize=True):
    """Stream the board's USB-CDC serial to the terminal until Ctrl-C, line by line.

    Resilient to the board rebooting: a reset/OTA drops USB-CDC and it re-enumerates, so
    when the read ends we re-detect the port (its path may change) and reconnect -- a
    fresh boot log then streams in too. Read-only throughout (see
    _read_serial_until_closed)."""
    pending = [""]                                 # carry a partial last line across chunks

    def emit(text):
        pending[0] += text
        while "\n" in pending[0]:
            line, pending[0] = pending[0].split("\n", 1)
            line = line.rstrip("\r")
            print(colorize_log_line(line) if colorize else line, flush=True)

    try:
        while True:
            _read_serial_until_closed(port, emit)
            if pending[0]:                         # flush a trailing partial line
                print(colorize_log_line(pending[0]) if colorize else pending[0], flush=True)
                pending[0] = ""
            print("  [monitor] link dropped (reset/unplug?) -- waiting for the board ...",
                  flush=True)
            newport = _wait_for_micropython(timeout=30)
            if not newport:
                print("  [monitor] board didn't re-appear -- stopping.", flush=True)
                return
            if newport != port:
                print("  [monitor] reconnected on %s" % newport, flush=True)
                port = newport
            else:
                print("  [monitor] reconnected.", flush=True)
    except KeyboardInterrupt:
        print("\n  [monitor] stopped.", flush=True)


def _stream_subprocess(cmd, colorize):
    """Run `cmd` and echo its stdout to the terminal line-by-line (colourising firmware log
    levels) until it exits or Ctrl-C. Used to relay a held `mpremote ... resume exec`
    session (resume = NO soft reset: mpremote's default Ctrl-D soft reset would auto-run
    /boot.py, and the firmware's non-returning main() then blocks raw-REPL entry -- 'could
    not enter raw repl'. resume enters raw REPL on the idle board and runs the loader once,
    under our control, streaming)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(colorize_log_line(line) if colorize else line, flush=True)
        proc.wait()
    except KeyboardInterrupt:
        # The terminal already delivered SIGINT to mpremote (same foreground group); let it
        # clean up (it Ctrl-Cs the board and exits, leaving it at the REPL).
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        print("\n  [monitor] stopped -- the board is left at the REPL "
              "(`mpremote reset`, or re-provision, to resume normal autorun).", flush=True)


# Launch the app exactly as the OTA loader does (honouring slot selection / boot-confirm)
# but FROM a held mpremote session, so USB stays enumerated through net_task's boot. A cold
# hard-reset wedges USB on this board -- core 1 (net_task) starves core 0 (USB/TinyUSB)
# during enumeration -- so we never let the firmware cold-boot here; we run it ourselves
# over a session that is already enumerated. Falls back to a flat-layout main at root.
_MONITOR_LAUNCH = (
    "import os\n"
    "_r = os.listdir('/')\n"
    "if 'boot.py' in _r:\n"
    "    exec(open('/boot.py').read())\n"
    "elif 'main.py' in _r:\n"
    "    __import__('main').main()\n"
    "else:\n"
    "    print('monitor: no boot.py/main.py on the board -- provision it first')\n"
)


def _stream_bringup(port, colorize, timeout):
    """Run the firmware over a HELD `mpremote ... resume exec` session and stream its boot
    log live to the terminal, returning once bring-up is confirmed, the session ends,
    `timeout` elapses, or Ctrl-C. For VISIBILITY during provisioning (a cold reset wedges
    USB and would show nothing). `resume` => NO soft reset, so the idle board's /boot.py is
    not auto-run during raw-REPL entry; we run it ourselves and follow the output, and USB
    stays enumerated through net_task's boot. Returns (status, text): 'pass' the moment the
    firmware logs it reached HA bring-up, else classify_bringup() of the captured text, or
    'interrupted' on Ctrl-C / 'error' if the session couldn't run. FULLY non-fatal -- the
    caller still confirms via the broker, so a flaky read never fails an otherwise-good
    provision."""
    import threading
    try:
        # stderr -> DEVNULL: keep the BOARD's boot log (mpremote relays it on stdout) but
        # drop mpremote's own diagnostics (e.g. "could not exec command (response: ...)")
        # so a failed attach doesn't spew raw-REPL noise into the boot log.
        proc = subprocess.Popen(
            ["mpremote", "connect", port, "resume", "exec", _MONITOR_LAUNCH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except Exception as e:
        return ("error", "could not start stream: %s" % e)
    lines = []
    status = {"v": None}
    done = threading.Event()

    def reader():
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("mpremote:"):
                    continue                       # defensive: skip any mpremote-self line
                print(colorize_log_line(line) if colorize else line, flush=True)
                lines.append(line)
                if "HA discovery published" in line or "HA discovery skipped" in line:
                    status["v"] = "pass"
                    return
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        done.wait(timeout)
    except KeyboardInterrupt:
        status["v"] = "interrupted"
        print("\n  [bring-up] log skipped -- checking the broker ...", flush=True)
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    text = "\n".join(lines)
    return (status["v"] or classify_bringup(text)[0], text)


def _monitor_find_board(args):
    """Find the board to monitor, with a USB-wedge-aware message when none enumerates.
    The monitor never flashes -- it needs the board already running MicroPython on USB."""
    if args.port:
        return args.port
    if find_boards():
        return pick_board(args)
    if _find_rpi_rp2_mount():
        die("The board is in BOOTSEL (the RPI-RP2 *drive*) -- that's USB mass-storage, not a\n"
            "  serial device, so there's nothing to stream. The monitor doesn't flash; get\n"
            "  MicroPython + firmware back on it first with `python3 provision.py`, then run\n"
            "  the monitor. See firmware/BRINGUP.md.")
    die("No RP2040-ETH on USB.\n"
        "  The monitor streams from a board that's already running MicroPython -- it doesn't\n"
        "  flash. If the board vanished from USB right after provisioning, that's the known\n"
        "  cold-boot USB wedge (core-1 net_task starves core-0 USB enumeration); recover it\n"
        "  via BOOTSEL (hold BOOT while plugging in) and re-flash with `python3 provision.py`.\n"
        "  See firmware/BRINGUP.md. (Or pass --port for a non-standard path.)")


def monitor_logs(args):
    """Stream the board's firmware log + boot diagnostics over USB, then exit. Never flashes.

    Default mode RELAUNCHES the firmware over a HELD `mpremote` session and relays its log
    live: USB is already enumerated by that session, so it survives net_task's boot and you
    get a clean log from the banner on (CH9120 link, MCP discovery, leased IP, MQTT connect,
    'HA discovery published/skipped') plus runtime lines. This avoids a cold hard-reset, which
    wedges USB on this board (core-1 net_task starves core-0 USB enumeration -- see firmware
    boot-wedge notes / BRINGUP.md). The board is first quiesced to a bare, watchdog-free REPL
    (`_disable_app` -- a reset to a *bare* board, NOT a flash; USB re-enumerates cleanly) and
    its loader restored, then the firmware is run over the held session.

    `--monitor-passive` instead only LISTENS to the board's current serial without
    interrupting it -- for an already-running unit you must not restart. Lines are leveled
    ([E]/[W]/[I]/[D], src/log.py); WARN/ERROR/DEBUG are colourised. Ctrl-C stops."""
    colorize = _supports_color()

    if args.monitor_passive:
        if os.name == "nt" and not _has_pyserial():
            die("Passive serial on Windows needs pyserial: pip install pyserial")
        port = _monitor_find_board(args)
        print("Listening to %s (passive -- the board is NOT interrupted). Ctrl-C to stop."
              % port)
        if not _has_pyserial():
            print("  (install pyserial for the most reliable capture: pip install pyserial)")
        stream_serial(port, colorize=colorize)
        return 0

    # Default: relaunch the firmware over a held session so USB stays up through the boot.
    port = _monitor_find_board(args)
    print("Pausing the firmware so it can be relaunched cleanly over USB (no flashing) ...")
    parked = _disable_app(port)                    # reset to a bare, watchdog-free REPL
    boards = find_boards()
    port = boards[0][0] if boards else port        # the reset may re-enumerate at a new path
    _restore_app(port, parked)                     # put the loader back (no reset) so it runs
    print("Starting the firmware over a held USB session and streaming its log from %s."
          % port)
    print("  (the held session keeps USB enumerated -- a cold reset would wedge it; Ctrl-C "
          "to stop)")
    # `resume` so mpremote does NOT soft-reset on connect: a soft reset auto-runs /boot.py,
    # and the firmware's non-returning main() then blocks raw-REPL entry. With resume we
    # enter the raw REPL on the idle (quiesced) board and run the loader ourselves.
    _stream_subprocess(["mpremote", "connect", port, "resume", "exec", _MONITOR_LAUNCH],
                       colorize)
    return 0


# ===========================================================================
# Prompts
# ===========================================================================
def die(msg, code=1):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


# ---- branded startup banner ----
PRODUCT = "OSELIA Hearth · DI16-G"
TAGLINE = "16-channel 24 V discrete-input gateway"


def _supports_color():
    return (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") not in (None, "dumb"))


def _fw_version():
    """Read SW_VERSION from the firmware config so the banner can't drift."""
    try:
        with open(os.path.join(SRC_DIR, "config.py")) as f:
            for line in f:
                if line.strip().startswith("SW_VERSION"):
                    return line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
    except OSError:
        pass
    return "?"


def print_banner():
    """OSELIA-branded header: the hearth mark + wordmark + product line. Colour only
    on a real terminal (honours NO_COLOR)."""
    c = _supports_color()
    T = "\x1b[38;2;13;148;136m" if c else ""   # OSELIA teal #0d9488
    B = "\x1b[1m" if c else ""
    D = "\x1b[2m" if c else ""
    R = "\x1b[0m" if c else ""
    icon = ["     {T}▟█▙{R}     ",
            "   {T}▟█████▙{R}   ",
            "   {T}███████{R}   ",
            "   {T}██{R} ◉ {T}██{R}   ",
            "   {T}███████{R}   "]
    right = ["", "{B}O S E L I A{R}", "{D}smart automation{R}", "", ""]
    lines = [""]
    for ic, rt in zip(icon, right):
        lines.append(("  " + ic + "  " + rt).format(T=T, R=R, B=B, D=D))
    lines.append("")
    lines.append("  {B}{P}{R}  {D}—  {TL}{R}".format(B=B, R=R, D=D, P=PRODUCT, TL=TAGLINE))
    lines.append("  {D}Provisioning wizard{R}  ·  firmware {T}v{V}{R}".format(
        D=D, R=R, T=T, V=_fw_version()))
    lines.append("  {D}{ln}{R}".format(D=D, R=R, ln="─" * 58))
    print("\n".join(lines))


HA_TOKEN_PATH = os.path.expanduser("~/.config/oselia/ha_token")
GH_TOKEN_PATH = os.path.expanduser("~/.config/oselia/gh_token")
# Default firmware release feed for the OSELIA integration. The GitHub Releases API URL
# works for a PRIVATE repo (with a token); override with --release-url.
DEFAULT_RELEASE_URL = "https://api.github.com/repos/vmyronovych/oselia-hearth-di16g-firmware/releases/latest"


def _resolve_ha_token(args):
    """Long-lived token from --ha-token, $OSELIA_HA_TOKEN, or ~/.config/oselia/ha_token;
    if none of those, PROMPT the installer (with instructions). Kept out of site.json
    (it is HA-side, not board-side) and out of git."""
    if args.ha_token:
        return args.ha_token
    if os.environ.get("OSELIA_HA_TOKEN"):
        return os.environ["OSELIA_HA_TOKEN"]
    if os.path.exists(HA_TOKEN_PATH):
        with open(HA_TOKEN_PATH) as f:
            tok = f.read().strip()
        if tok:
            return tok
    return _prompt_ha_token()


def _resolve_gh_token(args):
    """GitHub token for a PRIVATE release feed, from --github-token, $OSELIA_GH_TOKEN, or
    ~/.config/oselia/gh_token. Returns None for a public repo (no token needed). Stored
    HA-side via the integration options; never on the board, never in git."""
    if args.github_token:
        return args.github_token
    if os.environ.get("OSELIA_GH_TOKEN"):
        return os.environ["OSELIA_GH_TOKEN"]
    if os.path.exists(GH_TOKEN_PATH):
        with open(GH_TOKEN_PATH) as f:
            tok = f.read().strip()
        if tok:
            return tok
    return None


def _prompt_ha_token():
    """Ask the installer for the HA token, explaining how to create one. Offers to
    save it for next time. Blank input -> None (HA setup is then skipped)."""
    print("\nHome Assistant needs a long-lived access token to set up the dashboard")
    print("and blueprint. To create one (takes ~20 seconds):")
    print("  1. Open Home Assistant in your browser.")
    print("  2. Click your user profile — your name at the BOTTOM-LEFT of the sidebar.")
    print("  3. Open the 'Security' tab and scroll to 'Long-Lived Access Tokens'.")
    print("  4. Click 'Create Token', name it (e.g. 'oselia-provision'), and copy it.")
    # Visible input (not getpass): pasting a long token into a no-echo prompt is
    # unreliable -- some terminals wrap pastes in bracketed-paste escapes that corrupt
    # it. Showing it lets you confirm the full token landed; strip those markers + ws.
    raw = input("Paste the HA token here (or leave blank to skip): ")
    token = raw.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
    if not token:
        return None
    if confirm("Save this token to %s for next time?" % HA_TOKEN_PATH, default=True):
        try:
            os.makedirs(os.path.dirname(HA_TOKEN_PATH), exist_ok=True)
            with open(HA_TOKEN_PATH, "w") as f:
                f.write(token + "\n")
            os.chmod(HA_TOKEN_PATH, 0o600)
            print("  saved (readable only by you).")
        except OSError as e:
            print("  could not save (%s) — continuing with the token for this run." % e)
    return token


def _prompt_gh_token():
    """Ask the installer for a GitHub token for the PRIVATE release feed, explaining how
    to create one. Offers to save it for next time. Blank input -> None (the integration
    then can't read a private repo, so the dashboard shows a 'feed not configured /
    access denied' note until a token is set)."""
    print("\nThe firmware release repo is PRIVATE, so Home Assistant needs a GitHub token")
    print("to read the release feed (stored HA-side via the integration; never on the board).")
    print("To create a fine-grained token (takes ~30 seconds):")
    print("  1. Open https://github.com/settings/tokens?type=beta")
    print("  2. 'Generate new token'; under 'Repository access' pick the oselia repo.")
    print("  3. Under 'Repository permissions' set 'Contents' to 'Read-only'.")
    print("  4. Generate, then copy the token (starts with 'github_pat_').")
    raw = input("Paste the GitHub token here (or leave blank to skip): ")
    token = raw.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
    if not token:
        return None
    if confirm("Save this token to %s for next time?" % GH_TOKEN_PATH, default=True):
        try:
            os.makedirs(os.path.dirname(GH_TOKEN_PATH), exist_ok=True)
            with open(GH_TOKEN_PATH, "w") as f:
                f.write(token + "\n")
            os.chmod(GH_TOKEN_PATH, 0o600)
            print("  saved (readable only by you).")
        except OSError as e:
            print("  could not save (%s) — continuing with the token for this run." % e)
    return token


def _discover_ha_url(broker_ip):
    """HA base URL for --ha-setup: mDNS first, then a LAN port scan (verified HA on
    8123); one -> auto, several -> ask. Falls back to HA co-located with the broker."""
    has = discover_ha_instances_mdns() or []
    if not has:
        print("  Scanning the local network for Home Assistant (port 8123) ...")
        has = _scan_lan(_probe_ha)
    hit = _pick_one(has, "Home Assistant", lambda h: "%s:%d" % h)
    if hit:
        return "http://%s:%d" % hit
    return "http://%s:8123" % broker_ip


def _token_valid(ha_url, token):
    """True if the token authenticates against HA (GET /api/ -> 200)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(ha_url.rstrip("/") + "/api/",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False                          # 401 -> rejected
    except Exception:
        return True                           # network/other -> don't block on it


def _ha_setup(args, broker_ip, broker_port, user, password, device_id):
    """Legacy MQTT-discovery HA setup (only when provisioned with --mqtt): ensure HA has
    the MQTT integration + push the switch blueprint. No curated dashboard -- that is
    OSELIA-mode only now (/oselia-hearth); MQTT-discovery devices appear under the MQTT
    integration. Best-effort: a failure here is reported but does NOT fail provisioning
    (the unit is online)."""
    import ha_setup
    ha_url = args.ha_url or _discover_ha_url(broker_ip)
    token = _resolve_ha_token(args)
    if token and not _token_valid(ha_url, token):
        print("  The configured HA token was rejected by %s -- it may be stale." % ha_url)
        token = _prompt_ha_token()            # ask for a fresh one (with instructions)
    if not token:
        print("  HA setup skipped (no valid token). Re-run to set up the MQTT "
              "integration + blueprint later.")
        return False
    print("Setting up Home Assistant at %s ..." % ha_url)
    try:
        done = ha_setup.run_setup(
            ha_url, token, HERE, broker_ip=broker_ip, broker_port=broker_port,
            mqtt_user=user, mqtt_pass=password)
        for d in done:
            print("  installed: %s" % d)
        print("\nOpen Home Assistant -> Settings -> Devices & Services -> MQTT to find "
              "the device (entities are auto-created from its discovery).")
        return True
    except Exception as e:
        print("  HA setup FAILED (unit is still online): %s" % e)
        return False


def _add_ha_hearth_dashboard(args):
    """Standalone, NO device/flash: do the OSELIA HA-side setup against --ha-url, then exit.
    Configures the integration's firmware release feed (defaults to DEFAULT_RELEASE_URL --
    override with --release-url) and (re)builds the OSELIA Hearth dashboard (/oselia-hearth)
    from the gateways the integration has created. The integration itself installs via HACS
    (github.com/vmyronovych/oselia-hearth-di16g-ha) and its broker is set in Add Integration.
    Returns an exit code.

    Broker details aren't needed when the OSELIA config entry already exists (the common
    case after a HACS install + Add Integration) -- ensure_oselia then just updates its
    options. If no entry exists yet, add it in HA first (HACS) and re-run."""
    import ha_setup
    ha_url = args.ha_url or _discover_ha_url(getattr(args, "broker_ip", None))
    token = _resolve_ha_token(args)
    if token and not _token_valid(ha_url, token):
        print("  The configured HA token was rejected by %s -- it may be stale." % ha_url)
        token = _prompt_ha_token()
    if not token:
        print("  Skipped (no valid HA token). Pass --ha-token / $OSELIA_HA_TOKEN / "
              "~/.config/oselia/ha_token.")
        return 1
    # Release feed: default silently (no prompt) -- override with --release-url. The GitHub
    # token (private repo) comes from the flag/env/file; offer to enter one only if missing.
    release_url = args.release_url or DEFAULT_RELEASE_URL
    gh_token = _resolve_gh_token(args)
    if not gh_token and sys.stdin.isatty():
        gh_token = _prompt_gh_token()
    print("Setting up OSELIA in Home Assistant at %s ..." % ha_url)
    try:
        done = ha_setup.ensure_oselia(
            ha_url, token, getattr(args, "broker_ip", None), DEFAULT_BROKER_PORT,
            None, None, DEFAULT_BASE_TOPIC, release_url, gh_token)
        for d in done:
            print("  %s" % d)
        print("  release feed: %s" % release_url)
        if not gh_token:
            print("  (no GitHub token -- fine for a PUBLIC repo; for the private repo set "
                  "--github-token / $OSELIA_GH_TOKEN / ~/.config/oselia/gh_token)")
        dash, gw_ids = ha_setup.ensure_oselia_dashboard(ha_url, token, HERE)
        for d in dash:
            print("  %s" % d)
        if gw_ids:
            print("\nOpen in Home Assistant:  %s/%s"
                  % (ha_url.rstrip("/"), ha_setup.DASHBOARD_URL_PATH))
        else:
            print("  No OSELIA gateways visible yet -- install the integration (HACS) and "
                  "point it at your broker first, then re-run.")
        return 0
    except Exception as e:
        print("  FAILED: %s" % e)
        return 1


def _ha_setup_oselia(args, broker_ip, broker_port, user, password, device_id):
    """OSELIA-mode HA setup: ensure the OSELIA custom integration exists against this
    broker, set its firmware release-feed (+ GitHub token for a private repo), and
    (re)build the OSELIA Hearth dashboard so this gateway gets its `gw-<id>` view. The
    device appears under OSELIA and is OTA-ready from the HA UI. Best-effort."""
    import ha_setup
    ha_url = args.ha_url or _discover_ha_url(broker_ip)
    token = _resolve_ha_token(args)
    if token and not _token_valid(ha_url, token):
        print("  The configured HA token was rejected by %s -- it may be stale." % ha_url)
        token = _prompt_ha_token()
    if not token:
        print("  HA setup skipped (no valid token). Add the OSELIA integration manually "
              "later (Settings -> Devices & Services -> Add integration -> OSELIA).")
        return False
    # Release feed URL + GitHub token. If either wasn't supplied (as a flag / env var /
    # saved file), stop and ask the installer rather than silently defaulting -- on a
    # PRIVATE repo a silent default just 404s and the dashboard shows a "feed not
    # configured / access denied" note. Automated (non-TTY) runs keep defaulting silently.
    release_url = args.release_url
    gh_token = _resolve_gh_token(args)
    interactive = sys.stdin.isatty()
    if not release_url and interactive and confirm(
            "No firmware release feed URL was provided. Set one now?", default=True):
        release_url = ask("Release feed URL", default=DEFAULT_RELEASE_URL)
    if not release_url:
        release_url = DEFAULT_RELEASE_URL          # non-interactive / declined -> default
    if not gh_token and interactive and confirm(
            "No GitHub token was provided (needed for the PRIVATE release repo). "
            "Provide one now?", default=True):
        gh_token = _prompt_gh_token()
    print("Setting up the OSELIA integration at %s ..." % ha_url)
    if not gh_token:
        print("  (no GitHub token -- fine for a PUBLIC release repo; for a private repo "
              "pass --github-token / $OSELIA_GH_TOKEN / ~/.config/oselia/gh_token)")
    try:
        done = ha_setup.ensure_oselia(
            ha_url, token, broker_ip, broker_port, user, password,
            DEFAULT_BASE_TOPIC, release_url, gh_token)
        for d in done:
            print("  installed: %s" % d)
        print("  release feed: %s" % release_url)
        # (Re)build the shared OSELIA Hearth dashboard; the new gateway joins the views.
        dash, gw_ids = ha_setup.ensure_oselia_dashboard(ha_url, token, HERE)
        for d in dash:
            print("  installed: %s" % d)
        base = ha_url.rstrip("/")
        print("\nOpen in Home Assistant (Cmd/Ctrl-click):")
        if device_id and any(g.lower() == device_id.lower() for g in gw_ids):
            print("  Dashboard:  %s/%s/gw-%s  (name your switches on the device page)"
                  % (base, ha_setup.DASHBOARD_URL_PATH, device_id.lower()))
        elif gw_ids:
            print("  Dashboard:  %s/%s" % (base, ha_setup.DASHBOARD_URL_PATH))
        return True
    except Exception as e:
        print("  OSELIA HA setup FAILED (unit is still online): %s" % e)
        return False


# ===========================================================================
# Uninstall / decommission
# ===========================================================================
def _wipe_board(port):
    """Delete every file from the board's filesystem (the MicroPython interpreter
    stays). Leaves a bare board ready to be re-provisioned."""
    print("Wiping the board's filesystem (interpreter stays) ...")
    script = ("import os\n"
              "def rmr(d):\n"
              "    for e in os.listdir(d):\n"
              "        p=(d if d=='/' else d+'/')+e\n"
              "        try: os.remove(p)\n"
              "        except OSError: rmr(p); os.rmdir(p)\n"
              "rmr('/'); print('FS:', os.listdir('/'))")
    r = _mpremote(["exec", script], port=port, check=False)
    out = (r.stdout or r.stderr or "").strip()
    print("  " + (out or "(no output)"))
    reset_board(port)
    if "FS: []" in out:
        print("Board wiped -- bare MicroPython, no app. Re-run provisioning to set up.")
        return 0
    print("WARN: wipe may not be complete (see output above); retry if needed.")
    return 2


def erase_flash(args):
    """Erase the RP2040's ENTIRE flash -- MicroPython interpreter AND filesystem --
    leaving a bare-metal chip sitting in BOOTSEL. Uses Raspberry Pi's flash_nuke.uf2.
    Irreversible: the board has no interpreter afterwards (re-flash to reuse it)."""
    print("This ERASES THE ENTIRE FLASH: it removes MicroPython and all files, leaving")
    print("a bare-metal RP2040. The board won't run anything until re-flashed.")
    if not confirm("Erase everything now?", default=False):
        die("Aborted -- nothing was erased.")
    nuke = _cached_uf2(FLASH_NUKE_URL, FLASH_NUKE_NAME, args.erase_uf2)
    if not nuke:
        print("  Fetch it manually and pass --erase-uf2 PATH:")
        print("    " + FLASH_NUKE_URL)
        die("No flash_nuke UF2 available.")
    boards = find_boards()
    port = args.port or (boards[0][0] if boards else None)
    mount = _enter_bootsel(port)
    print("Erasing the entire flash (flash_nuke) -> %s ..." % mount)
    try:
        shutil.copy(nuke, mount)
    except OSError:
        pass            # board erases + reboots to BOOTSEL as the UF2 lands; tail error is normal
    print("Flash erased -- the board is now bare-metal RP2040 (empty flash, in BOOTSEL).")
    print("To use it again, flash MicroPython: re-run provision.py (it offers to), or "
          "see firmware/FLASHING.md.")
    return 0


def _clear_device_retained(ip, port, user, password, base_topic, device_id):
    """Subscribe, collect retained topics that belong to <device_id>, and clear them
    (empty retained payload). Clearing the retained HA-discovery configs is what makes
    the device disappear from Home Assistant. Returns the count cleared."""
    try:
        sock = socket.create_connection((ip, port), timeout=5.0)
    except OSError:
        return 0
    try:
        sock.settimeout(2.0)
        sock.sendall(build_connect("oselia-uninstall", user=user, password=password))
        if _mqtt_read_packet(sock) is None:                 # CONNACK
            return 0
        sock.sendall(build_subscribe(1, "homeassistant/#"))
        sock.sendall(build_subscribe(2, base_topic + "/#"))
        marker = "/%s/" % device_id
        topics = set()
        deadline = time.time() + 4.0
        while time.time() < deadline:
            pkt = _mqtt_read_packet(sock)
            if pkt is None:
                continue
            if pkt[0] & 0xF0 == 0x30 and (pkt[0] & 0x01):   # PUBLISH + retain flag
                body = pkt[1]
                tlen = (body[0] << 8) | body[1]
                topic = body[2:2 + tlen].decode("utf-8", "replace")
                if marker in topic or topic.startswith(base_topic + "/" + device_id):
                    topics.add(topic)
        for t in topics:
            sock.sendall(build_publish(t, b"", retain=True))   # empty retained = delete
        time.sleep(0.3)
        return len(topics)
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()


def _uninstall_ha(args):
    """Remove a unit's HA presence: clear its retained discovery/state on the broker
    (device disappears), rebuild the shared /oselia-hearth dashboard without its view
    (deleting the dashboard only if no gateways remain) and remove the blueprint (+ MQTT
    integration with --uninstall-ha-mqtt). Device id / broker come from --device-id /
    --broker, else the
    connected board's site.json; if neither has the broker (e.g. an already-wiped board)
    we discover it on the network."""
    device_id, broker_ip = args.device_id, None
    broker_port, user, password = DEFAULT_BROKER_PORT, None, None
    boards = [] if args.device_id and args.broker else find_boards()
    port = args.port or (boards[0][0] if boards else None)
    if port and board_has_micropython(port):
        device_id = device_id or read_device_id(port)
        site = read_existing_site(port) or {}
        broker_ip = site.get("broker_ip")
        broker_port = site.get("broker_port", DEFAULT_BROKER_PORT)
        user, password = site.get("mqtt_user"), site.get("mqtt_pass")
    if not device_id:
        die("uninstall-ha: need the device id -- connect the board or pass --device-id")
    if args.broker or not broker_ip:
        # --broker overrides; or the board carried no broker (no site.json) -> find it
        if not broker_ip and not args.broker:
            print("No broker recorded on the board -- locating it on the network ...")
        broker_ip, broker_port = prompt_broker(args, None)

    print("Uninstalling device %s ..." % device_id)
    n = _clear_device_retained(broker_ip, broker_port, user, password,
                               DEFAULT_BASE_TOPIC, device_id)
    print("  cleared %d retained topic(s) on %s -- HA will drop the device" % (n, broker_ip))

    import ha_setup
    ha_url = args.ha_url or _discover_ha_url(broker_ip)
    token = _resolve_ha_token(args)
    if token and not _token_valid(ha_url, token):
        print("  HA token rejected -- need a fresh one.")
        token = _prompt_ha_token()
    if not token:
        print("  No HA token -- left the dashboard/blueprint (device already cleared).")
        return 0
    try:
        removed = ha_setup.teardown(ha_url, token, HERE,
                                    remove_mqtt=args.uninstall_ha_mqtt)
        for d in removed:
            print("  removed: %s" % d)
    except Exception as e:
        print("  HA asset removal failed: %s" % e)
    return 0


def _announce_online(args, broker_ip, broker_port, user, password, device_id_str,
                     oselia_mode):
    """Report a successful bring-up and (optionally) push the HA assets. Used by both
    the broker-confirmed path and the serial-confirmed fallback so --ha-setup runs in
    either case.

    `oselia_mode` is the unit's ACTUAL integration (from site.json -- the default, an
    explicit --oselia/--mqtt, or a prior choice preserved across a re-provision), NOT
    just this run's CLI flag. So re-running the wizard on a legacy --mqtt unit keeps the
    MQTT-discovery path instead of switching it to the OSELIA integration + dashboard."""
    print("\nPASS: device%s is online in Home Assistant." %
          (" " + device_id_str if device_id_str else ""))
    # --ha-setup forces it; --no-ha-setup skips it; otherwise ask (default yes).
    if args.no_ha_setup:
        do_ha = False
    elif args.ha_setup:
        do_ha = True
    elif oselia_mode:
        do_ha = confirm("Set up the OSELIA integration in Home Assistant now "
                        "(+ firmware release feed + Hearth dashboard)?", default=True)
    else:
        do_ha = confirm("Set up Home Assistant now "
                        "(MQTT integration + blueprint)?", default=True)
    linked = False
    if do_ha and oselia_mode:
        linked = _ha_setup_oselia(args, broker_ip, broker_port, user, password,
                                  device_id_str)
    elif do_ha:
        linked = _ha_setup(args, broker_ip, broker_port, user, password, device_id_str)
    if not linked:
        # HA setup skipped/failed -> no clickable links; give the manual path.
        print("Open Home Assistant -> Settings -> Devices -> "
              "'Hearth' to name your switches.")
    return 0


def ask(prompt, default=None):
    suffix = " [%s]" % default if default else ""
    val = input("%s%s: " % (prompt, suffix)).strip()
    return val or (default or "")


def confirm(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        v = input("%s [%s] " % (prompt, d)).strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def pick_board(args):
    if args.port:
        return args.port
    boards = find_boards()
    if not boards:
        die("No RP2040-ETH detected over USB -- is it plugged in? "
            "(or pass --port)")
    if len(boards) == 1:
        print("Found board: %s (%s %s)" % boards[0])
        return boards[0][0]
    print("Multiple boards found:")
    for i, b in enumerate(boards):
        print("  [%d] %s (%s %s)" % (i, b[0], b[1], b[2]))
    while True:
        idx = ask("Which board number")
        if idx.isdigit() and 0 <= int(idx) < len(boards):
            return boards[int(idx)][0]


def acquire_board(args):
    """Find the board to provision. If none is on serial but a BOOTSEL drive (RPI-RP2)
    is mounted, offer to flash MicroPython onto it (a bare board) and re-detect."""
    if args.port:
        return args.port
    if find_boards():
        return pick_board(args)
    if not args.skip_mpy_check and _find_rpi_rp2_mount():
        print("No MicroPython board on USB, but a BOOTSEL drive (RPI-RP2) is mounted.")
        # Wipe-then-flash: we have no REPL to park a prior OTA app first, and a preserved
        # old firmware that wedges USB on boot would leave the board un-detectable. A clean
        # erase guarantees a bare REPL; provisioning then writes site.json + firmware fresh.
        if confirm("Flash a clean MicroPython %s onto it now? (erases the board's flash "
                   "for a fresh install)" % EXPECTED_MPY_VERSION, default=True):
            return flash_micropython(args, None, wipe=True)
    die("No RP2040-ETH detected over USB -- is it plugged in? (or pass --port)")


def _pick_one(items, label, fmt):
    """Auto-select when exactly one was found, prompt a numbered menu when several,
    return None when zero/unavailable. `fmt` renders an item for display."""
    if not items:
        return None
    if len(items) == 1:
        print("  Using the only %s found on the network: %s" % (label, fmt(items[0])))
        return items[0]
    print("Multiple %ss found on the network:" % label)
    for i, it in enumerate(items):
        print("  [%d] %s" % (i, fmt(it)))
    while True:
        idx = ask("Which %s number" % label)
        if idx.isdigit() and 0 <= int(idx) < len(items):
            return items[int(idx)]


_ZEROCONF_OK = None   # cache: None unknown, True available, False unavailable/declined


def _ensure_zeroconf():
    """True if `zeroconf` is importable. If missing, offer to install it (pip into the
    running interpreter) and retry. Cached so the offer appears at most once per run."""
    global _ZEROCONF_OK
    if _ZEROCONF_OK is not None:
        return _ZEROCONF_OK
    try:
        import zeroconf  # noqa: F401
        _ZEROCONF_OK = True
        return True
    except ImportError:
        pass
    print("Network auto-discovery needs the 'zeroconf' package, which isn't installed.")
    if not confirm("Install it now (pip install zeroconf)?", default=True):
        _ZEROCONF_OK = False
        return False
    import importlib
    import site
    print("  Installing zeroconf ...")
    # Try a plain install first; on an externally-managed Python (Homebrew, PEP 668)
    # fall back to a user-site install that bypasses the system-package guard.
    attempts = [["install", "zeroconf"],
                ["install", "--user", "--break-system-packages", "zeroconf"]]
    for extra in attempts:
        try:
            r = subprocess.run([sys.executable, "-m", "pip"] + extra,
                               capture_output=True, text=True)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        try:                                  # make a fresh --user site importable now
            site.addsitedir(site.getusersitepackages())
        except Exception:
            pass
        importlib.invalidate_caches()
        try:
            import zeroconf  # noqa: F401
            _ZEROCONF_OK = True
            print("  zeroconf installed.")
            return True
        except ImportError:
            continue
    _ZEROCONF_OK = False
    print("  Couldn't auto-install zeroconf (looks like an externally-managed Python"
          " -- PEP 668). Install it once yourself, then re-run:")
    print("    pip install --user --break-system-packages zeroconf")
    print("  Continuing with manual broker entry for now.")
    return False


def _browse_mdns(services, timeout=4.0, default_port=None):
    """Browse the whole timeout window and return a deduped, ordered list of
    (ip, port) for the given mDNS service type(s). Returns None if zeroconf is
    unavailable (caller distinguishes "unavailable" from [] = "none answered")."""
    if not _ensure_zeroconf():
        return None
    from zeroconf import Zeroconf, ServiceBrowser
    order = []
    seen = set()

    class _L:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info and info.parsed_addresses():
                port = info.port or default_port
                for a in info.parsed_addresses():
                    if is_valid_ipv4(a) and (a, port) not in seen:
                        seen.add((a, port))
                        order.append((a, port))
        def update_service(self, *a):
            pass
        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, list(services), _L())
        end = time.time() + timeout          # full window: collect ALL responders
        while time.time() < end:
            time.sleep(0.2)
    finally:
        zc.close()
    return order


def discover_brokers_mdns(timeout=4.0):
    """All MQTT brokers advertised on the LAN (`_mqtt._tcp`). -> list[(ip,port)] | None."""
    return _browse_mdns(["_mqtt._tcp.local."], timeout, default_port=DEFAULT_BROKER_PORT)


def discover_ha_instances_mdns(timeout=4.0):
    """All Home Assistant instances on the LAN (`_home-assistant._tcp`).
    -> list[(ip,port)] | None."""
    return _browse_mdns(["_home-assistant._tcp.local."], timeout, default_port=8123)


# ---- port-scan fallback (when nothing advertises mDNS) ----
def _primary_ipv4():
    """The laptop's primary LAN IPv4 (no packets sent), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _probe_mqtt(host):
    """(host, 1883) if it speaks MQTT (TCP open + a CONNACK to our CONNECT), else None."""
    try:
        sock = socket.create_connection((host, DEFAULT_BROKER_PORT), timeout=0.5)
    except OSError:
        return None
    try:
        sock.settimeout(1.0)
        sock.sendall(build_connect("oselia-scan"))
        hdr = sock.recv(1)
        if hdr and hdr[0] == 0x20:           # CONNACK -> confirmed broker
            return (host, DEFAULT_BROKER_PORT)
    except OSError:
        pass
    finally:
        try:
            sock.sendall(b"\xE0\x00")
        except OSError:
            pass
        sock.close()
    return None


def _probe_ha(host):
    """(host, 8123) if it looks like Home Assistant (HTTP on 8123; /api/ -> 401
    without a token), else None."""
    try:
        sock = socket.create_connection((host, 8123), timeout=0.5)
    except OSError:
        return None
    try:
        sock.settimeout(1.5)
        sock.sendall(("GET /api/ HTTP/1.0\r\nHost: %s\r\n\r\n" % host).encode())
        line = sock.recv(128).split(b"\r\n", 1)[0]
        if any(code in line for code in (b" 401", b" 200", b" 403")):
            return (host, 8123)
    except OSError:
        pass
    finally:
        sock.close()
    return None


def _scan_lan(probe, workers=128):
    """Probe every host in the laptop's /24 with `probe(host) -> (ip,port)|None`.
    Returns the sorted matches; [] if the subnet can't be determined."""
    ip = _primary_ipv4()
    if not ip or ip.startswith("127."):
        return []
    base = ip.rsplit(".", 1)[0] + "."
    import concurrent.futures as cf
    hosts = [base + str(i) for i in range(1, 255)]
    found = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(probe, hosts):
            if r:
                found.append(r)
    return sorted(found)


def prompt_broker(args, existing):
    if args.broker:
        host, _, port = args.broker.partition(":")
        ip = resolve_to_ipv4(host) or die("could not resolve --broker host %r" % host)
        return ip, int(port or DEFAULT_BROKER_PORT)

    default_ip = (existing or {}).get("broker_ip")
    default_port = (existing or {}).get("broker_port", DEFAULT_BROKER_PORT)

    print("Searching the network for MQTT brokers (mDNS) ...")
    brokers = discover_brokers_mdns() or []
    if not brokers:
        # nothing advertised mDNS -> fall back to a LAN port scan (verified MQTT)
        print("  None advertised -- scanning the local network for MQTT (port 1883) ...")
        brokers = _scan_lan(_probe_mqtt)
    hit = _pick_one(brokers, "MQTT broker", lambda b: "%s:%d" % b)
    if hit:                                  # one -> auto, several -> the chosen one
        return hit
    print("  No broker found automatically -- enter it manually.")

    while True:
        host = ask("Broker IP or hostname", default_ip)
        ip = resolve_to_ipv4(host)
        if not ip:
            print("  Not a valid IP / resolvable hostname; try again.")
            continue
        port = ask("Broker port", str(default_port))
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            print("  Port must be 1..65535; try again.")
            continue
        return ip, int(port)


# ===========================================================================
# Orchestration
# ===========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Provision an OSELIA Hearth.")
    ap.add_argument("--port", help="serial port of the board (skip USB auto-detect)")
    ap.add_argument("--broker", metavar="IP[:PORT]",
                    help="skip mDNS discovery; use this broker")
    ap.add_argument("--static", metavar="IP/GW/MASK",
                    help="force a static address instead of DHCP")
    ap.add_argument("--boards", type=int, metavar="N", choices=range(1, MAX_BOARDS + 1),
                    help="pin an explicit board count (1-%d) instead of "
                         "auto-discovering MCP chips on the bus" % MAX_BOARDS)
    ap.add_argument("--names", metavar="FILE",
                    help="CSV of board,pin,name for on-device name overrides")
    ap.add_argument("--no-diag", action="store_true",
                    help="disable diagnostics telemetry on the unit (no diag/state "
                         "publishes or HA diagnostic entities)")
    ap.add_argument("--oselia", action="store_true",
                    help="use the first-party OSELIA Home Assistant integration (the "
                         "DEFAULT): the firmware skips publishing MQTT discovery (the "
                         "integration owns the entities, so the device appears under "
                         "OSELIA, not MQTT). The wizard sets up the OSELIA integration "
                         "in HA, the firmware release feed, and the /oselia-hearth "
                         "dashboard. This flag is now implicit; see INTEGRATION_SPEC.md")
    ap.add_argument("--mqtt", action="store_true",
                    help="LEGACY: provision for MQTT discovery instead of the OSELIA "
                         "integration -- the firmware publishes HA discovery and the "
                         "device appears under the MQTT integration (no curated "
                         "dashboard). Mutually exclusive with --oselia")
    ap.add_argument("--release-url", metavar="URL",
                    help="firmware release feed for the OSELIA integration's update "
                         "entity (if omitted you're prompted; default offered is the "
                         "GitHub Releases API for this repo)")
    ap.add_argument("--github-token", metavar="TOKEN",
                    help="GitHub token for a PRIVATE release repo (else $OSELIA_GH_TOKEN "
                         "or ~/.config/oselia/gh_token; if none, you're prompted). Stored "
                         "HA-side, not on the board")
    ap.add_argument("--ha-setup", action="store_true",
                    help="set up Home Assistant without asking (OSELIA: add the "
                         "integration + release feed + /oselia-hearth dashboard; --mqtt: "
                         "MQTT integration + blueprint); otherwise the wizard asks once "
                         "the unit is online")
    ap.add_argument("--no-ha-setup", action="store_true",
                    help="skip the Home Assistant setup step without asking")
    ap.add_argument("--add-ha-hearth-dashboard", action="store_true",
                    help="NO device/flash: do the OSELIA HA-side setup against --ha-url, "
                         "then exit -- set the firmware release feed (defaults to the "
                         "vmyronovych/oselia-hearth-di16g-firmware releases API; override with --release-url) and "
                         "(re)build the /oselia-hearth dashboard. The integration installs "
                         "via HACS (broker set in Add Integration). Use it to set up / "
                         "refresh the dashboard + OTA feed without re-provisioning a gateway")
    ap.add_argument("--ha-url", metavar="URL",
                    help="Home Assistant base URL for --ha-setup (default: auto-detect "
                         "via mDNS, else http://<broker-ip>:8123)")
    ap.add_argument("--ha-token", metavar="TOKEN",
                    help="HA long-lived token for --ha-setup (else $OSELIA_HA_TOKEN, "
                         "else ~/.config/oselia/ha_token, else you're prompted)")
    ap.add_argument("--skip-mpy-check", action="store_true",
                    help="don't check the MicroPython version or offer to flash it")
    ap.add_argument("--mpy-uf2", metavar="PATH",
                    help="MicroPython UF2 to flash (offline installs); default downloads "
                         "the pinned build %s" % MPY_UF2_NAME)
    ap.add_argument("--no-flash", action="store_true",
                    help="write config only; assume the slot layout (/slots/a + "
                         "/boot.py) is already on the board")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be written without touching the board")
    # diagnostics
    ap.add_argument("--monitor", action="store_true",
                    help="stream the firmware's log + boot diagnostics over USB, then exit "
                         "(Ctrl-C to stop). Does NOT flash or provision -- it relaunches the "
                         "firmware over a held USB session (keeping USB enumerated; a cold "
                         "boot wedges USB on this board) and relays the log live")
    ap.add_argument("--monitor-passive", action="store_true",
                    help="with --monitor, only LISTEN to the board's current serial without "
                         "interrupting it (for an already-running unit you must not restart)")
    # uninstall / decommission
    ap.add_argument("--uninstall-all", action="store_true",
                    help="UNINSTALL: full decommission -- remove from Home Assistant "
                         "(incl. the MQTT integration) AND erase the board firmware, "
                         "then exit")
    ap.add_argument("--uninstall-firmware", action="store_true",
                    help="UNINSTALL: erase the board's filesystem (all app files; the "
                         "MicroPython interpreter stays), then exit")
    ap.add_argument("--erase-flash", action="store_true",
                    help="UNINSTALL: erase the ENTIRE flash -- removes MicroPython too, "
                         "leaving a bare-metal RP2040 in BOOTSEL (flash_nuke), then exit")
    ap.add_argument("--erase-uf2", metavar="PATH",
                    help="flash_nuke UF2 for --erase-flash (offline); default downloads it")
    ap.add_argument("--uninstall-ha", action="store_true",
                    help="UNINSTALL: remove the unit from Home Assistant (clear retained "
                         "discovery so the device drops, rebuild the /oselia-hearth "
                         "dashboard without it + remove the blueprint), then exit")
    ap.add_argument("--uninstall-ha-mqtt", action="store_true",
                    help="with --uninstall-ha, also remove the HA MQTT integration")
    ap.add_argument("--device-id", metavar="ID",
                    help="device id for --uninstall-ha (else read from the board)")
    args = ap.parse_args(argv)
    print_banner()

    if args.monitor or args.monitor_passive:
        return monitor_logs(args)               # standalone: stream logs (no flash/provision)
    if args.uninstall_all:
        # HA first (it reads device id/broker from the board's site.json), then wipe.
        args.uninstall_ha_mqtt = True
        rc_ha = _uninstall_ha(args)
        rc_fw = _wipe_board(pick_board(args))
        return rc_ha or rc_fw
    if args.uninstall_firmware:
        return _wipe_board(pick_board(args))
    if args.erase_flash:
        return erase_flash(args)
    if args.uninstall_ha:
        return _uninstall_ha(args)
    if args.add_ha_hearth_dashboard:
        return _add_ha_hearth_dashboard(args)   # standalone: push the dashboard, no board

    static = None
    if args.static:
        try:
            ip, gw, mask = args.static.split("/")
            static = {"ip": ip, "gateway": gw, "mask": mask}
        except ValueError:
            die("--static must be IP/GW/MASK, e.g. 192.168.1.50/192.168.1.1/255.255.255.0")

    names = None
    if args.names:
        with open(args.names) as f:
            names = parse_names_csv(f.read())
        print("Loaded %d name override(s)." % len(names))

    # 1. board + interpreter. Quiesce the firmware ONCE, up front, BEFORE any REPL read
    # or write. The firmware's hardware watchdog (core 0) resets the board whenever
    # mpremote breaks in, which otherwise corrupts the version check, the site.json
    # read-back, AND `fs cp`. Parking the auto-run entry drops the board to a bare,
    # watchdog-free REPL for the whole run, so re-provisioning an already-running unit is
    # as robust as provisioning a bare board. The try/finally below is the safety net the
    # early-quiesce needs: it guarantees the app is restored (and the board reset) even if
    # the installer aborts at a prompt, so a parked unit is never stranded at the REPL.
    port = None if args.dry_run and not args.port else acquire_board(args)
    app_bak = None
    restored = False
    if port and not args.dry_run:
        print("Pausing the firmware so the board can be read and written ...")
        # Prefer the COOPERATIVE quiesce: if the unit is online, ask it over MQTT to park its
        # loader and reset itself -- firmware-driven, so no host REPL break-in, no
        # hardware-watchdog race, and no USB wedge (the reliable way to re-provision a RUNNING
        # unit in place; see PROVISIONING_SPEC.md sec.3.1). Falls back to the USB-driven
        # _disable_app (works for a bare/idle board; fragile on a running watchdog unit).
        coop_port = _cooperative_quiesce(port)
        if coop_port:
            port = coop_port
            app_bak = True              # the firmware parked its loader -> boot.py.provbak
        else:
            app_bak = _disable_app(port)
    try:
        if port and not args.skip_mpy_check:
            port = ensure_micropython(args, port)  # check version; offer to flash if needed
        existing = read_existing_site(port) if port else None
        if existing:
            print("Existing site.json found on the board; offering its values as defaults.")

        # 2-3. answers (board count is NOT asked -- the firmware auto-discovers the
        # MCP boards on the I2C bus at boot; --boards pins an explicit count instead.)
        broker_ip, broker_port = prompt_broker(args, existing)
        ex_user = (existing or {}).get("mqtt_user") or ""
        user = ask("Broker username (blank for none)", ex_user) or None
        password = getpass.getpass("Broker password (blank for none): ") or None
        if user and password is None and existing and existing.get("mqtt_pass"):
            password = existing["mqtt_pass"]            # keep prior password on re-run

        board_count = args.boards           # None -> autodiscover; int -> pinned

        # 4. validate broker before writing
        print("Validating broker %s:%d ..." % (broker_ip, broker_port))
        ok, detail = mqtt_validate(broker_ip, broker_port, user, password)
        print(("  OK -- " if ok else "  PROBLEM -- ") + detail)
        if not ok and not confirm("Write config to the board anyway?", default=False):
            die("Aborted at broker validation; re-run with corrected details.")

        # Decide this unit's HA integration. Default is the OSELIA custom integration
        # (it owns the entities; firmware skips MQTT discovery). --mqtt opts into legacy
        # MQTT discovery. An explicit flag always wins; otherwise a prior choice recorded
        # on the board is preserved across a re-provision (so a re-run doesn't silently
        # flip the unit); otherwise the default (OSELIA) applies.
        if args.mqtt and args.oselia:
            die("--mqtt and --oselia are mutually exclusive")
        if args.mqtt:
            ha_integration = "mqtt"
        elif args.oselia:
            ha_integration = "oselia"
        elif existing and existing.get("ha_integration"):
            ha_integration = existing["ha_integration"]
        else:
            ha_integration = "oselia"
        if ha_integration == "oselia":
            # _announce_online routes HA setup to _ha_setup_oselia, which adds the
            # integration, sets the firmware release feed + builds the Hearth dashboard.
            print("HA integration: OSELIA custom integration (firmware skips MQTT "
                  "discovery; the wizard sets up the OSELIA integration + Hearth "
                  "dashboard in HA).")
        else:
            print("HA integration: legacy MQTT discovery (firmware publishes HA "
                  "discovery; pass --oselia for the OSELIA integration).")
        site = build_site_dict(broker_ip, broker_port, user, password,
                               board_count=board_count, use_dhcp=True,
                               static=static, names=names, diag=not args.no_diag,
                               ha_integration=ha_integration)
        # Preserve values the firmware itself writes to site.json (live tunables changed
        # from HA -- SPEC.md sec.5.4) so re-provisioning doesn't reset them. (The HA
        # integration choice is already resolved above, including prior-choice carry-over.)
        if existing:
            for k in ("long_ms", "double_gap_ms", "debounce_ms", "log_level"):
                if k in existing and k not in site:
                    site[k] = existing[k]
        if args.no_diag:
            print("Diagnostics telemetry: DISABLED for this unit.")

        # 6. write + flash. The firmware was already quiesced up front, so writes land on
        # a bare REPL; copy_firmware reinstalls /boot.py, after which the parked backup is
        # obsolete and _restore_app drops it.
        write_site_atomic(port, site, args.dry_run)
        if not args.no_flash:
            copy_firmware(port, args.dry_run)
        if port and not args.dry_run:
            _restore_app(port, app_bak)
        restored = True                  # normal restore done -> the finally is a no-op

        if args.dry_run:
            print("\nDry run complete -- nothing was written to the board.")
            return 0

        # 7. bring-up + confirm.
        oselia_mode = site.get("ha_integration") == "oselia"
        device_id = read_device_id(port)

        # 7a. VISIBILITY (best-effort): stream the boot log over a held USB session (resume
        # = no cold reset, so USB stays enumerated and you can watch the bring-up -- a cold
        # reset wedges USB on this board). Fully wrapped: any failure here NEVER affects the
        # provision; the broker below is the authoritative check. It only attaches cleanly
        # when the board is at an idle REPL; if the firmware is already running it can't, and
        # we just say so and move on (use `--monitor` on a running unit for live logs).
        print("\nStreaming the boot log over USB (Ctrl-C to skip to the broker check) ...")
        try:
            stream_status, stream_text = _stream_bringup(port, _supports_color(), timeout=60.0)
        except Exception as e:
            stream_status, stream_text = ("error", "")
            print("  (boot-log stream unavailable: %s)" % e)
        if not any(m in stream_text for m in ("boot:", "[I]", "[W]", "[E]", "OSELIA")):
            print("  (no live boot log captured over USB -- confirming via the broker; "
                  "use `--monitor` to watch a running unit)")

        # 7b. CONFIRM: leave the board running on its own and confirm via the broker. Clear
        # any stale retained status first so the wait can only succeed on a genuine post-reset
        # 'online' (matters on re-provision). The broker is network truth, independent of the
        # USB serial (which a cold boot can wedge on this board).
        if device_id:
            clear_retained_status(broker_ip, broker_port, user, password,
                                  DEFAULT_BASE_TOPIC, device_id)
        print("\nResetting board and waiting for it to come online (broker) ...")
        reset_board(port)
        online, dev = mqtt_wait_online(broker_ip, broker_port, user, password,
                                       DEFAULT_BASE_TOPIC, timeout=90.0)
        if online:
            return _announce_online(args, broker_ip, broker_port, user, password,
                                    dev or device_id, oselia_mode)

        # Not confirmed on the broker. The streamed boot above may still have shown a clean
        # bring-up (e.g. a slow broker-watch) -- treat 'pass' as success; otherwise FAIL with
        # the cause classified from the streamed log (which the installer just saw live).
        if stream_status == "pass":
            print("\n(broker wait timed out, but the streamed boot confirmed bring-up)")
            return _announce_online(args, broker_ip, broker_port, user, password,
                                    device_id, oselia_mode)
        status, msg = classify_bringup(stream_text)
        print("\nFAIL (%s): %s" % (status, msg))
        if not stream_text:
            print("(no boot log captured over USB -- if the board keeps failing, "
                  "`--erase-flash` then re-provision for a clean start)")
        return 2
    finally:
        # Safety net for an early exit (abort at a prompt, the validation die(), an
        # exception/KeyboardInterrupt): if we parked the app but never reached the normal
        # restore above, put it back and reset so the unit resumes running its firmware
        # instead of sitting at the bare REPL.
        if port and not args.dry_run and not restored:
            _restore_app(port, app_bak)
            reset_board(port)


if __name__ == "__main__":
    sys.exit(main())
