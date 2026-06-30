"""Pause a RUNNING firmware unit so the host can rewrite it over USB.

Two paths, ported verbatim from the original single-file wizard (HW-confirmed behaviour):

  * cooperative_quiesce()  -- the SAFE way: find the unit on the network and ask it over
    MQTT (<base>/<id>/cmd/maintenance) to park its loader and reset ITSELF. No host
    REPL break-in, so no hardware-watchdog race. Only acts when exactly
    ONE unit is online on a no-auth broker (we have no creds pre-quiesce).

  * disable_app() / restore_app()  -- the USB fallback: break into the REPL, rename the
    auto-run entry (/boot.py or /main.py) to *.provbak, hard-reset to a bare REPL. Works
    for a bare/idle board; fragile on a running watchdog unit, hence the cooperative path
    is tried first. restore_app() undoes it.

See PROVISIONING_SPEC.md sec.3.1 / firmware/SPEC.md sec.5.3.
"""
import time

from . import board, console, mqtt
from .constants import DEFAULT_BASE_TOPIC
from .discovery import discover_brokers_mdns, scan_lan


def disable_app(port):
    """Park the auto-run entry and hard-reset to a bare, watchdog-free REPL. Returns True
    if something was parked (restore_app then has work to do), False on a bare board.

    On a freshly flashed bare board (no main.py) this returns immediately WITHOUT resetting:
    a needless machine.reset() just churns USB-CDC (drop + ~1 s re-enumerate) for no reason.
    Self-verifying: it reads the FS back after each attempt; on a bare REPL no auto-run entry
    remains."""
    probe = board.exec_(port, "import os; _l = os.listdir(); "
                               "print('boot.py' in _l or 'main.py' in _l)")
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
        board.exec_(port, rename, timeout=10)
        time.sleep(2.0)                      # board hard-resets toward a bare REPL
        r = board.exec_(port, "import os; _l = os.listdir(); "
                               "print('boot.py' not in _l and 'main.py' not in _l)")
        out = (r.stdout or "").strip().splitlines()
        if out and out[-1].strip() == "True":
            return True
    console.warn("  (note: could not fully quiesce the firmware; writes may need a retry)")
    return True


def restore_app(port, quiesced):
    """Undo disable_app. If a current entry is present (firmware.deploy reinstalled the
    /main.py loader) the parked backup is obsolete -> drop it. Otherwise rename the parked
    entry back so the board still boots its existing app.

    The loader is now /main.py, so a freshly deployed board has main.py present and the
    backup should be dropped; a board where deploy did NOT run (early exit) has main.py
    parked as main.py.provbak -> restore it. Both legacy boot.py.provbak and main.py.provbak
    are handled so a unit mid-migration off the old boot.py layout recovers cleanly."""
    if not quiesced:
        return
    script = ("import os\n"
              "_l = os.listdir()\n"
              "_have_entry = 'main.py' in _l\n"  # a fresh loader is present -> backups obsolete
              "for _bak in ('boot.py.provbak', 'main.py.provbak'):\n"
              "    if _bak not in _l:\n"
              "        continue\n"
              "    try:\n"
              "        if _have_entry:\n"
              "            os.remove(_bak)\n"
              "        else:\n"
              "            os.rename(_bak, _bak[:-8])\n"   # _bak[:-8] strips '.provbak'
              "    except OSError:\n"
              "        pass\n")
    board.exec_(port, script)


def wait_for_bare_repl(port, timeout=30):
    """After a cooperative reset, wait for the board to re-enumerate at a BARE REPL (no
    boot.py/main.py -> no watchdog). -> port (possibly a new path) or None."""
    end = time.time() + timeout
    while time.time() < end:
        boards = board.find_boards()
        cand = (port if any(b[0] == port for b in boards)
                else (boards[0][0] if boards else None))
        if cand:
            r = board.exec_(cand, "import os; _l = os.listdir(); "
                                   "print('boot.py' not in _l and 'main.py' not in _l)")
            out = (r.stdout or "").strip().splitlines()
            if out and out[-1].strip() == "True":
                return cand
        time.sleep(1.0)
    return None


def cooperative_quiesce(port):
    """Quiesce a RUNNING unit the safe (firmware-driven) way. -> bare-REPL port on
    success, else None (caller falls back to disable_app).

    Finds the unit on the network WITHOUT touching USB (so its MQTT session stays alive):
    discover brokers (mDNS, then a LAN scan), find the single 'online' device, publish the
    maintenance command, and wait for it to come back as a bare REPL. Acts only on EXACTLY
    ONE online unit on a no-auth broker (no creds known yet)."""
    brokers = (discover_brokers_mdns() or []) or scan_lan(mqtt.probe_broker)
    for bip, bport in brokers:
        devs = mqtt.list_online(bip, bport, None, None, DEFAULT_BASE_TOPIC, timeout=5.0)
        if len(devs) != 1:
            continue                              # zero or ambiguous -> can't target safely
        dev = devs[0]
        console.info("  unit %s is online -- asking it to enter maintenance mode "
                     "(cooperative quiesce, no watchdog fight) ..." % dev)
        if not mqtt.send_command(bip, bport, None, None, DEFAULT_BASE_TOPIC,
                                 dev, "maintenance"):
            continue
        newport = wait_for_bare_repl(port, timeout=30)
        if newport:
            console.ok("  unit parked itself -> bare REPL on %s (clean; no watchdog fight)."
                       % newport)
            return newport
    return None
