"""Deploy the firmware app to the board in the OTA A/B slot layout, so a freshly
provisioned unit is OTA-ready out of the box (firmware/OTA_SPEC.md).

Layout written:
    /boot.py            loader (installed LAST; never part of an OTA bundle)
    /ota/state          fresh boot-confirm state {active:a, pending:false, ...}
    /slots/a/  <app>    all firmware src/*.py except boot.py

Ported from the original wizard's copy_firmware().
"""
import os

from . import board, console
from .paths import SRC_DIR

# A fresh boot-confirm state: app in slot a, active, not pending (firmware ota.py).
_FRESH_OTA_STATE = ('{"active": "a", "previous": "a", "pending": false, '
                    '"tries": 0, "crashes": 0}')
# Clear flat-layout app modules a pre-OTA install left at root (the slot shadows them via
# sys.path, but clear them so a re-provisioned unit migrates cleanly onto slots). Never
# touches /boot.py, /site.json, or anything outside root.
_CLEAN_FLAT_ROOT = ("import os\n"
                    "for _f in os.listdir('/'):\n"
                    "    if _f.endswith('.py') and _f != 'boot.py':\n"
                    "        try:\n"
                    "            os.remove('/' + _f)\n"
                    "        except OSError:\n"
                    "            pass\n")


def deploy(port, dry_run=False, src_dir=SRC_DIR):
    """Deploy the OTA slot layout: app .py -> /slots/a, fresh /ota/state, clear old flat
    root modules, then install /boot.py LAST (an interrupted copy leaves a stable REPL, not
    a boot.py reset loop)."""
    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".py"))
    app = [f for f in files if f != "boot.py"]   # the loader is installed separately
    if dry_run:
        console.info("--- would deploy OTA slot layout: /slots/a/{%d app files} + /boot.py "
                     "loader + /ota/state" % len(app))
        return
    for d in ("/slots", "/slots/a", "/ota"):
        board.fs_mkdir(port, d)
    # One mpremote invocation for all app files: far faster, and avoids the WDT rebooting
    # the board between per-file calls.
    paths = [os.path.join(src_dir, f) for f in app]
    board.run(["fs", "cp"] + paths + [":slots/a/"], port=port, timeout=120)
    board.exec_(port, "open('/ota/state', 'w').write(%r)" % _FRESH_OTA_STATE, check=True)
    board.exec_(port, _CLEAN_FLAT_ROOT)
    # Loader LAST: only now does the board have a runnable entry point.
    board.fs_push(port, os.path.join(src_dir, "boot.py"), "boot.py")
    console.ok("Deployed OTA slot layout: %d app files in /slots/a + loader." % len(app))


def fw_version(src_dir=SRC_DIR):
    """Read SW_VERSION from the firmware config so the banner can't drift. -> str."""
    try:
        with open(os.path.join(src_dir, "config.py")) as f:
            for line in f:
                if line.strip().startswith("SW_VERSION"):
                    return line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
    except OSError:
        pass
    return "?"
