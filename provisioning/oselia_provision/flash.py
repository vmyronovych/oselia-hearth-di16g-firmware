"""Flash the MicroPython interpreter onto the board over BOOTSEL/UF2, and the related
version check + whole-flash erase.

The wipe-vs-no-wipe split, the BOOTSEL mount-race retries, and the "never reflash a board that
merely failed a version read" rule defend against the firmware watchdog hard-resetting the
board during a sustained host raw-REPL session (a version read can fail on a perfectly good
interpreter).
"""
import glob
import os
import shutil
import time

from . import board, console, uf2
from .constants import EXPECTED_MPY_VERSION
from .quiesce import disable_app


# ---------------------------------------------------------------------------
# BOOTSEL / re-enumeration waits
# ---------------------------------------------------------------------------
def find_rpi_rp2_mount():
    """Path to a mounted RP2040 BOOTSEL drive (RPI-RP2), or None (macOS + Linux)."""
    for c in (["/Volumes/RPI-RP2"] + glob.glob("/media/*/RPI-RP2")
              + glob.glob("/run/media/*/RPI-RP2")):
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "INFO_UF2.TXT")):
            return c
    return None


def _wait_for_bootsel(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        m = find_rpi_rp2_mount()
        if m:
            return m
        time.sleep(0.5)
    return None


def _wait_for_micropython(timeout=60):
    """Wait for a board to re-enumerate on USB after a flash. -> port|None. Detection is by
    USB re-enumeration (a board still in BOOTSEL is mass-storage and never listed), NOT a
    REPL exec -- a UF2 flash preserves littlefs, so a board with a prior OTA layout boots
    straight into the watchdog'd firmware and an exec probe would race its reset."""
    end = time.time() + timeout
    while time.time() < end:
        boards = board.find_boards()
        if boards:
            return boards[0][0]
        time.sleep(1.0)
    return None


def _enter_bootsel(port):
    """Ensure the board is in BOOTSEL and return the mounted RPI-RP2 path (or die). Reboots
    a running board via machine.bootloader(); on a bare board, walks the user through the
    BOOT+RESET dance (skipped when non-interactive -- it just waits for the drive)."""
    if find_rpi_rp2_mount():
        pass                                   # already in BOOTSEL (e.g. a bare board)
    elif port and board.has_micropython(port):
        console.info("Rebooting the board into BOOTSEL (machine.bootloader) ...")
        board.exec_(port, "import machine; machine.bootloader()")
    else:
        console.warn("Put the board into BOOTSEL: hold BOOT + RESET, release RESET, then "
                     "release BOOT (see firmware/FLASHING.md).")
        if console.INTERACTIVE:
            input("  Press Enter once the board is in BOOTSEL ...")
        else:
            console.info("  (non-interactive: waiting up to 30s for the RPI-RP2 drive)")
    mount = _wait_for_bootsel(timeout=30)
    if not mount:
        console.die("BOOTSEL drive (RPI-RP2) didn't appear -- see firmware/FLASHING.md.")
    return mount


# ---------------------------------------------------------------------------
# flash MicroPython
# ---------------------------------------------------------------------------
def flash_micropython(mpy_uf2, port, erase_uf2=None, wipe=False):
    """Put the board in BOOTSEL, copy the pinned UF2, wait for it to come back running
    MicroPython. Returns the (re-enumerated) port; dies on failure.

    wipe=True first erases the WHOLE flash (flash_nuke) so the board boots to a clean bare
    REPL -- use it on the BOOTSEL/acquire path (no REPL to park a prior OTA app; a preserved
    old firmware auto-runs and its watchdog fights the tool's break-ins). On the upgrade path
    the caller already quiesced the /main.py loader, so pass wipe=False and littlefs
    (site.json) is preserved."""
    image = uf2.resolve_mpy(mpy_uf2)
    if not image:
        console.die("No MicroPython UF2 available to flash.")
    mount = _enter_bootsel(port)
    if wipe:
        nuke = uf2.resolve_nuke(erase_uf2)
        if not nuke:
            console.die("No flash_nuke UF2 available to wipe the board (pass --erase-uf2 PATH).")
        console.info("Erasing the board's flash first for a clean install -> %s ..." % mount)
        try:
            shutil.copy(nuke, mount)
        except OSError:
            pass        # board wipes + reboots to BOOTSEL as the UF2 lands; tail error normal
        mount = _wait_for_bootsel(timeout=30)
        if not mount:
            console.die("BOOTSEL didn't re-appear after the flash wipe -- re-plug and retry.")
        time.sleep(2.0)     # let the freshly nuked volume settle before the next copy
    console.info("Flashing %s -> %s ..." % (os.path.basename(image), mount))
    # Copy-then-confirm with a retry: on a SUCCESSFUL flash the board reboots out of BOOTSEL
    # mid-copy (the OSError tail is expected), so the real signal is the board leaving BOOTSEL
    # / re-appearing on serial. Still in BOOTSEL => the copy didn't take (mount race); re-copy.
    newport = None
    for _attempt in range(2):
        try:
            shutil.copy(image, mount)
        except OSError:
            pass
        console.info("  waiting for the board to come back online ...")
        newport = _wait_for_micropython(timeout=60)
        if newport:
            break
        mount = find_rpi_rp2_mount()
        if not mount:                          # left BOOTSEL but never re-enumerated
            break
        console.warn("  still in BOOTSEL -- the UF2 copy didn't take; retrying ...")
        time.sleep(2.0)
    if not newport:
        console.die("Board didn't re-appear as MicroPython after flashing -- re-plug and retry.")
    # A non-wipe flash preserves littlefs: a prior OTA layout boots straight into the running
    # firmware, which can't be reliably broken into over USB. Park it to a bare REPL now (a
    # wiped board is already bare, so skip it there).
    if not wipe:
        disable_app(newport)
    console.ok("Flashed MicroPython %s." % (board.read_mpy_version(newport) or "?"))
    return newport


def ensure_micropython(mpy_uf2, port):
    """Verify the board runs the pinned MicroPython; reflash (WIPED) only on a real version
    mismatch. Returns the port to keep using.

    CRITICAL (HW rule): a board that ENUMERATES as MicroPython is never flashed just because
    its version didn't read -- the read fails because the running firmware's watchdog keeps
    resetting the REPL, NOT because MicroPython is wrong, so a reflash would needlessly disrupt
    a healthy interpreter. A None read on a confirmed-MicroPython board => skip the flash and
    continue. A board that dropped off USB entirely => die with BOOTSEL guidance."""
    ver = board.read_mpy_version(port)
    if ver is None and board.port_is_micropython(port, wait_s=10):
        for _ in range(2):
            disable_app(port)
            ver = board.read_mpy_version(port)
            if ver:
                break
    if ver == EXPECTED_MPY_VERSION:
        console.ok("MicroPython %s detected -- matches the pinned build." % ver)
        return port
    if ver:
        console.warn("MicroPython on the board is %s, but this firmware pins %s."
                     % (ver, EXPECTED_MPY_VERSION))
        if not console.confirm("Re-flash MicroPython %s now?" % EXPECTED_MPY_VERSION,
                               default=True):
            console.info("  Keeping %s -- the pinned build carries features/fixes the "
                         "firmware expects (firmware/FLASHING.md)." % ver)
            return port
        return flash_micropython(mpy_uf2, port, wipe=True)
    if board.port_is_micropython(port, wait_s=10):
        console.warn("MicroPython is present but its version couldn't be read -- the running "
                     "firmware keeps resetting the REPL.")
        console.info("  Skipping the interpreter flash -- the version read fails because the "
                     "running firmware keeps resetting the REPL, not because MicroPython is "
                     "wrong. Continuing. If the interpreter is genuinely wrong, run "
                     "`oselia erase` then re-provision for a clean start.")
        return port
    console.die(
        "The board dropped off USB while pausing its firmware. On this hardware the firmware's"
        " watchdog can hard-reset the board during a sustained host REPL session -- so a\n"
        "  RUNNING unit can't always be re-provisioned in place.\n"
        "  Recover it for a CLEAN re-provision: hold the BOOT button while plugging in USB (it\n"
        "  mounts as RPI-RP2), then re-run `oselia provision` -- it does a wiped flash and\n"
        "  provisions. See firmware/FLASHING.md.")


# ---------------------------------------------------------------------------
# whole-flash erase (flash_nuke) -> bare-metal RP2040
# ---------------------------------------------------------------------------
def erase_flash(port, erase_uf2=None):
    """Erase the RP2040's ENTIRE flash -- interpreter AND filesystem -- leaving a bare-metal
    chip in BOOTSEL. Irreversible. Returns an exit code."""
    console.warn("This ERASES THE ENTIRE FLASH: it removes MicroPython and all files, leaving")
    console.warn("a bare-metal RP2040. The board won't run anything until re-flashed.")
    if not console.confirm("Erase everything now?", default=False):
        console.die("Aborted -- nothing was erased.")
    nuke = uf2.resolve_nuke(erase_uf2)
    if not nuke:
        console.die("No flash_nuke UF2 available (pass --erase-uf2 PATH).")
    mount = _enter_bootsel(port)
    console.info("Erasing the entire flash (flash_nuke) -> %s ..." % mount)
    try:
        shutil.copy(nuke, mount)
    except OSError:
        pass            # board erases + reboots to BOOTSEL as the UF2 lands; tail error normal
    console.ok("Flash erased -- the board is now bare-metal RP2040 (empty flash, in BOOTSEL).")
    console.info("To use it again, flash MicroPython: `oselia flash`, or see firmware/FLASHING.md.")
    return 0


def wipe_fs(port):
    """Delete every file from the board's filesystem (the MicroPython interpreter stays).
    Leaves a bare board ready to be re-provisioned. Returns an exit code."""
    console.info("Wiping the board's filesystem (interpreter stays) ...")
    script = ("import os\n"
              "def rmr(d):\n"
              "    for e in os.listdir(d):\n"
              "        p=(d if d=='/' else d+'/')+e\n"
              "        try: os.remove(p)\n"
              "        except OSError: rmr(p); os.rmdir(p)\n"
              "rmr('/'); print('FS:', os.listdir('/'))")
    r = board.exec_(port, script)
    out = (r.stdout or r.stderr or "").strip()
    console.info("  " + (out or "(no output)"))
    board.reset(port)
    if "FS: []" in out:
        console.ok("Board wiped -- bare MicroPython, no app. Re-run `oselia provision` to set up.")
        return 0
    console.warn("WARN: wipe may not be complete (see output above); retry if needed.")
    return 2
