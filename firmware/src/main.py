"""Root entry / OTA loader -- the board's main.py, run by MicroPython at startup.

Installed ONCE via USB (provisioning); it is NEVER part of an OTA bundle, so no update
can brick the boot path. Deliberately self-contained (no app imports until it launches
the slot) and tiny.

The loader is main.py, not boot.py, by design (see the PR that introduced it).

Responsibilities (docs/ota.md "Boot-confirm / auto-revert"):
  1. read /ota/state, run the boot-confirm gate (revert a build that never proved
     itself after _MAX_TRIES boots),
  2. put the chosen slot first on sys.path,
  3. import + run the app's main() from that slot (/slots/<slot>/app.py).

On an app crash it bumps a failure counter and resets (which advances the boot-confirm
`tries` -> a persistently-broken OTA build auto-reverts to the previous slot). But
after _MAX_CRASHES consecutive failures with NO slot booting cleanly, it stops and
drops to the REPL so the board stays reachable over USB for recovery instead of
reset-looping forever. The app clears the failure counter once it reaches the network
(net_task, first connect). The boot-confirm logic here mirrors ota.boot_decision (kept
dependency-free on purpose); change both together.
"""
import sys

try:
    import ujson as json
except ImportError:
    import json

_STATE = "/ota/state"
_MAX_TRIES = 2          # must match config OTA_MAX_BOOT_TRIES
_MAX_CRASHES = 4        # consecutive boot failures before dropping to REPL (recovery)


def _read_state():
    try:
        with open(_STATE) as f:
            s = json.load(f)
        if isinstance(s, dict) and "active" in s:
            return s
    except (OSError, ValueError):
        pass
    return {"active": "a", "previous": "a", "pending": False, "tries": 0}


def _write_state(s):
    try:
        import os
        with open(_STATE + ".tmp", "w") as f:
            json.dump(s, f)
        os.rename(_STATE + ".tmp", _STATE)
    except OSError as e:
        print("boot: state write failed:", e)


def _select_slot():
    s = _read_state()
    active = s.get("active", "a")
    if s.get("pending"):
        tries = int(s.get("tries", 0)) + 1
        if tries > _MAX_TRIES:
            active = s.get("previous", active)
            s.update(active=active, pending=False, tries=0)
            print("boot: build failed to confirm -> AUTO-REVERT to slot", active)
        else:
            s["tries"] = tries
            print("boot: pending slot %s, try %d/%d" % (active, tries, _MAX_TRIES))
        _write_state(s)
    return active


def _bump_crash():
    s = _read_state()
    s["crashes"] = int(s.get("crashes", 0)) + 1
    _write_state(s)
    return s["crashes"]


try:
    # Safe-mode gate: if the last few boots all failed and nothing has cleared the
    # counter (the app never reached the network), stop reset-looping and stay at the
    # REPL so the board can be recovered over USB.
    if int(_read_state().get("crashes", 0)) >= _MAX_CRASHES:
        print("boot: %d consecutive failures -> SAFE MODE (REPL). Recover over USB; "
              "clear /ota/state 'crashes' to retry." % _MAX_CRASHES)
        raise SystemExit
    _slot = _select_slot()
    sys.path.insert(0, "/slots/" + _slot)
    print("boot: running app from slot", _slot)
    import app
    app.main()
except SystemExit:
    pass                          # intentional drop to the REPL (safe mode)
except Exception as _e:           # noqa: BLE001 - top-level guard for the boot path
    sys.print_exception(_e)
    _n = _bump_crash()
    print("boot: failure %d/%d" % (_n, _MAX_CRASHES))
    # Reset so the boot-confirm `tries` counter advances toward auto-revert; the sleep
    # leaves a window to interrupt over USB and recover by hand.
    import time
    import machine
    time.sleep(3)
    machine.reset()
