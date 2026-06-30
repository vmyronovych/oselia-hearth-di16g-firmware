"""Deploy the firmware app to the board in the OTA A/B slot layout, so a freshly
provisioned unit is OTA-ready out of the box (firmware/OTA_SPEC.md).

Layout written:
    /main.py            loader (installed LAST; never part of an OTA bundle)
    /ota/state          fresh boot-confirm state {active:a, pending:false, ...}
    /slots/a/  <app>    all firmware src/*.py except main.py (incl. the app entry app.py)

The loader is main.py (not boot.py) by design.
"""
import os

from . import board, console
from .paths import SRC_DIR

# A fresh boot-confirm state: app in slot a, active, not pending (firmware ota.py).
_FRESH_OTA_STATE = ('{"active": "a", "previous": "a", "pending": false, '
                    '"tries": 0, "crashes": 0}')
# Clear EVERY root .py before the loader is (re)pushed last: a pre-OTA flat install left
# app modules + a main.py app entry at root, and an OTA board has a root main.py loader --
# both shadow the slot via sys.path, so wipe them all and let the final fs_push lay down
# the fresh loader. An interrupt after this (before the push) leaves NO root entry -> a
# bare REPL, never a boot loop. Never touches /site.json or anything outside root.
_CLEAN_FLAT_ROOT = ("import os\n"
                    "for _f in os.listdir('/'):\n"
                    "    if _f.endswith('.py'):\n"
                    "        try:\n"
                    "            os.remove('/' + _f)\n"
                    "        except OSError:\n"
                    "            pass\n")


def deploy(port, dry_run=False, src_dir=SRC_DIR):
    """Deploy the OTA slot layout: app .py -> /slots/a, fresh /ota/state, clear old root
    modules, then install /main.py (the loader) LAST (an interrupted copy leaves a bare
    REPL, not a boot loop)."""
    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".py"))
    app = [f for f in files if f != "main.py"]   # main.py is the loader, installed separately
    if dry_run:
        console.info("--- would deploy OTA slot layout: /slots/a/{%d app files} + /main.py "
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
    board.fs_push(port, os.path.join(src_dir, "main.py"), "main.py")
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
