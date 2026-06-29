"""Low-level board operations -- a quirk-aware wrapper around the `mpremote` CLI.

This is the backend for the `oselia board ...` toolbox AND the building block the
high-level flows (provision/flash/monitor) use. The point of centralising it here is
that the wizard, an installer, and an automated caller all talk to the board through
ONE retrying, raw-REPL-flake-tolerant path instead of ad-hoc `mpremote` invocations.

Ported from the original single-file wizard; the retry-on-raw-REPL-flake behaviour exists because a board
running the firmware has a watchdog that can reset it mid raw-REPL entry.
"""
import atexit
import json
import os
import re
import subprocess
import sys
import tempfile
import time

try:
    import fcntl                          # POSIX advisory locks (macOS/Linux only)
except ImportError:                       # pragma: no cover - non-POSIX: locking is skipped
    fcntl = None

from . import console
from .constants import RP2040_VID, SITE_FILE, SITE_TMP
from .paths import CACHE_DIR

_DEVICE_ID_RE = re.compile(r"^[0-9A-F]{6}$")


# ---------------------------------------------------------------------------
# serial contention guard
# ---------------------------------------------------------------------------
# Two oselia commands (or oselia + a serial monitor) hitting the board's USB-CDC at once
# can wedge the device in an uninterruptible kernel read -- unkillable until the cable is
# pulled. macOS makes this easy to trigger: opening /dev/cu.* is NOT exclusive, so the OS
# never rejects the second opener. So we guard ourselves: a process-wide flock serialises
# all oselia serial access, and an lsof preflight catches FOREIGN holders the lock can't see.
_LOCK_DIR = os.path.join(CACHE_DIR, "locks")
_serial_lock = None                       # held fd; kept open for the process lifetime


class PortWedged(Exception):
    """lsof itself hung on the port -- the hallmark of a crashed/wedged serial device."""
    def __init__(self, port):
        super().__init__(port)
        self.port = port


def _self_desc():
    return "oselia " + " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "oselia"


def _lock_path():
    os.makedirs(_LOCK_DIR, exist_ok=True)
    return os.path.join(_LOCK_DIR, "serial.lock")


def lock_serial(force=False):
    """Take the process-wide serial lock so only ONE oselia talks to the USB bus at a time.
    Idempotent. On contention: name the holder and die (or warn when force=True). The fd is
    held open until the process exits; the lock releases automatically on exit."""
    global _serial_lock
    if _serial_lock is not None or fcntl is None:
        return
    path = _lock_path()
    fd = open(path, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fd.seek(0)
            holder = fd.read().strip() or "another oselia process"
        except OSError:
            holder = "another oselia process"
        fd.close()
        msg = ("the board is already in use by %s. Wait for it to finish, or pass --force."
               % holder)
        if not force:
            console.die(msg)
        console.warn("--force: ignoring lock -- " + msg)
        return
    fd.seek(0)
    fd.truncate()
    fd.write("PID %d (%s)" % (os.getpid(), _self_desc()))
    fd.flush()
    _serial_lock = fd
    atexit.register(_release_serial_lock)


def _release_serial_lock():
    global _serial_lock
    if _serial_lock is None:
        return
    try:
        _serial_lock.seek(0)
        _serial_lock.truncate()           # clear the stale holder text
        _serial_lock.flush()
        fcntl.flock(_serial_lock, fcntl.LOCK_UN)
        _serial_lock.close()
    except OSError:
        pass
    _serial_lock = None


def port_holders(port, timeout=4):
    """OTHER processes holding `port` open, as ['PID 123 (cmd)', ...]; [] if none or lsof is
    unavailable. Raises PortWedged if lsof times out -- a hung device hangs lsof too."""
    try:
        r = subprocess.run(["lsof", "-t", "-w", "--", port],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return []                          # no lsof -> skip foreign-holder detection
    except subprocess.TimeoutExpired:
        raise PortWedged(port)
    holders = []
    for pid in r.stdout.split():
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                             capture_output=True, text=True).stdout.strip()
        holders.append("PID %s (%s)" % (pid, cmd or "?"))
    return holders


def check_port_free(port, force=False):
    """Die if `port` is held by another process or appears wedged. force -> warn instead."""
    if not port:
        return
    try:
        holders = port_holders(port)
    except PortWedged:
        msg = ("%s is not responding (lsof hung on it) -- the board likely crashed mid-"
               "session. Unplug and replug it, then retry." % port)
        if not force:
            console.die(msg)
        console.warn("--force: " + msg)
        return
    if holders:
        msg = ("%s is already in use by %s. Close it (or stop that oselia run), or pass "
               "--force." % (port, "; ".join(holders)))
        if not force:
            console.die(msg)
        console.warn("--force: " + msg)


# ---------------------------------------------------------------------------
# board enumeration
# ---------------------------------------------------------------------------
def parse_board_list(stdout):
    """Parse `mpremote connect list` -> [(port, vid:pid, desc), ...] for RP2040 boards
    only. Each line is `<port> <serial> <vid:pid> <mfr> <product>`; the vid:pid is found
    by token (not fixed column) to tolerate format drift."""
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


def find_boards():
    """`mpremote connect list` -> [(port, vid:pid, desc), ...] RP2040 only."""
    return parse_board_list(run(["connect", "list"], check=False).stdout)


# ---------------------------------------------------------------------------
# the mpremote runner
# ---------------------------------------------------------------------------
def _raw_repl_flake(r):
    """True if the failure looks like a transient raw-REPL entry problem -- typically a
    board running the firmware whose watchdog resets it mid-attempt. Worth a retry."""
    blob = ((r.stderr or "") + (r.stdout or "")).lower()
    return ("could not enter raw repl" in blob or "no response" in blob
            or "failed to access" in blob)


def run(args, port=None, timeout=30, check=True, retries=2):
    """Run `mpremote [connect PORT] ARGS...`. Retries the transient raw-REPL flake.
    Raises RuntimeError on a real failure when check=True."""
    cmd = ["mpremote"]
    if port:
        cmd += ["connect", port]
    cmd += args
    r = None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            console.die("`mpremote` not found on PATH. Install it: pipx install mpremote "
                        "(or `pip install -e .` here pulls it in).")
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(cmd, 1, "", "timeout")
        if r.returncode == 0 or not _raw_repl_flake(r) or attempt == retries:
            break
        time.sleep(0.8)                      # let a watchdog-reset board reach a fresh boot
    if check and r.returncode != 0:
        raise RuntimeError("mpremote %s failed: %s"
                           % (" ".join(args), r.stderr.strip() or r.stdout.strip()))
    return r


def exec_(port, code, check=False, timeout=30):
    """Run a line/block of MicroPython on the board (`mpremote exec`)."""
    return run(["exec", code], port=port, check=check, timeout=timeout)


def reset(port):
    run(["reset"], port=port, check=False)


# ---------------------------------------------------------------------------
# interpreter / identity probes
# ---------------------------------------------------------------------------
def has_micropython(port):
    r = exec_(port, "import sys; print(sys.implementation.name)")
    return "micropython" in (r.stdout or "").lower()


def port_is_micropython(port, wait_s=0):
    """True if `port` is ENUMERATED as an RP2040 MicroPython serial device (read from the
    USB descriptor via `mpremote connect list`). Reliable even when a REPL exec flakes:
    a board in BOOTSEL is USB mass-storage and is NOT listed, so a hit means MicroPython
    is genuinely running. Polls up to `wait_s` seconds (the board may be mid
    re-enumeration after a quiesce reset)."""
    end = time.time() + wait_s
    while True:
        if any(p == port for p, _vid, _desc in find_boards()):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


def _parse_uname_release(stdout):
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def read_mpy_version(port):
    """MicroPython version on the board (`os.uname().release`), e.g. '1.28.0'. -> str|None."""
    if not port:
        return None
    r = exec_(port, "import os; print(os.uname().release)")
    return _parse_uname_release(r.stdout) if r.returncode == 0 else None


def read_device_id(port):
    """The device id the firmware uses (last 6 hex of unique_id, upper). -> id|None.

    Format-VALIDATED: a running board emits boot-log lines over the same USB-CDC, so the
    id is printed behind a fixed marker and only a 6-hex match is trusted -- a bad id
    would become an MQTT topic segment and spawn a phantom device."""
    code = ("import machine,ubinascii;"
            "print('OSELIA_ID:'+ubinascii.hexlify(machine.unique_id()).decode()[-6:].upper())")
    r = exec_(port, code)
    for line in (r.stdout or "").splitlines():
        if "OSELIA_ID:" in line:
            cand = line.split("OSELIA_ID:", 1)[1].strip()
            if _DEVICE_ID_RE.match(cand):
                return cand
    return None


# ---------------------------------------------------------------------------
# filesystem operations (the `oselia board ls/cat/push/pull/rm` backend)
# ---------------------------------------------------------------------------
def fs_ls(port, path="/"):
    """List a directory on the board. -> raw `mpremote fs ls` text."""
    return run(["fs", "ls", ":" + path.lstrip(":")], port=port, check=False).stdout


def fs_cat(port, remote):
    """Read a file's bytes-as-text from the board, or None if it doesn't exist."""
    r = run(["fs", "cat", ":" + remote.lstrip(":")], port=port, check=False)
    return r.stdout if r.returncode == 0 else None


def fs_push(port, local, remote, timeout=120):
    """Copy a local file to the board (`mpremote fs cp local :remote`)."""
    run(["fs", "cp", local, ":" + remote.lstrip(":")], port=port, timeout=timeout)


def fs_pull(port, remote, local, timeout=120):
    """Copy a file from the board to the host."""
    run(["fs", "cp", ":" + remote.lstrip(":"), local], port=port, timeout=timeout)


def fs_rm(port, remote):
    run(["fs", "rm", ":" + remote.lstrip(":")], port=port, check=False)


def fs_mkdir(port, remote):
    run(["fs", "mkdir", ":" + remote.lstrip(":")], port=port, check=False)  # ignore 'exists'


# ---------------------------------------------------------------------------
# site.json read / atomic write
# ---------------------------------------------------------------------------
def read_existing_site(port):
    """Parse the board's site.json, or None if absent/invalid."""
    out = fs_cat(port, SITE_FILE)
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def write_site_atomic(port, site, dry_run=False):
    """Write site.json to the board atomically (temp + rename) so an aborted run never
    leaves a half file."""
    blob = json.dumps(site, indent=2)
    if dry_run:
        console.info("\n--- would write %s ---\n%s\n" % (SITE_FILE, blob))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        tf.write(blob)
        local = tf.name
    try:
        fs_push(port, local, SITE_TMP)
        exec_(port, "import os; os.rename('%s','%s')" % (SITE_TMP, SITE_FILE), check=True)
    finally:
        os.unlink(local)
