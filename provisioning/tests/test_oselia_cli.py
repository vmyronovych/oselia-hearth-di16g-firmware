"""Host smoke tests for the oselia CLI surface (no board, no broker).

Asserts the acceptance-suite subcommands (`mqtt …`, `ota …`) and their key options are
registered, so the hw-test skill can rely on them existing. Introspects the Click command
tree directly rather than scraping `--help` text — help rendering is width-dependent (Rich
wraps long option names on a narrow/no-TTY terminal, e.g. CI), which made substring checks
flaky; the command tree is deterministic everywhere.

Run:  python tests/test_oselia_cli.py
"""
import subprocess

import typer

from oselia_provision import board
from oselia_provision.cli import app

_cli = typer.main.get_command(app)


def _cmd(*path):
    cmd = _cli
    for name in path:
        cmd = cmd.commands[name]
    return cmd


def _subcommands(*path):
    return set(_cmd(*path).commands.keys())


def _opts(*path):
    opts = set()
    for param in _cmd(*path).params:
        opts.update(getattr(param, "opts", []))
        opts.update(getattr(param, "secondary_opts", []))
    return opts


def test_mqtt_subcommands_registered():
    subs = _subcommands("mqtt")
    for cmd in ("watch", "pub", "cmd", "bounce", "clear-retained"):
        assert cmd in subs, (cmd, subs)


def test_ota_subcommands_registered():
    subs = _subcommands("ota")
    for cmd in ("build", "publish"):
        assert cmd in subs, (cmd, subs)


def test_mqtt_watch_exposes_expect_absent_and_for():
    opts = _opts("mqtt", "watch")
    assert "--expect-absent" in opts and "--for" in opts, opts


def test_mqtt_bounce_exposes_container():
    assert "--container" in _opts("mqtt", "bounce")


def test_board_version_exposes_mpy_toggle():
    # `board version` now reports the firmware version by default; --mpy falls back to the
    # MicroPython runtime version.
    assert "--mpy" in _opts("board", "version")


def _fake_exec(monkeypatch, stdout, returncode=0):
    def _exec(port, code, check=False, timeout=30):
        return subprocess.CompletedProcess(["mpremote"], returncode, stdout, "")
    monkeypatch.setattr(board, "exec_", _exec)


class _MP:
    """Tiny stand-in for pytest's monkeypatch so this runs under bare `python tests/…`."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


def test_read_fw_version_parses_active_slot():
    mp = _MP()
    try:
        _fake_exec(mp, "boot noise\nOSELIA_FW:0.9.5:b\n")
        assert board.read_fw_version("/dev/ttyX") == ("0.9.5", "b")
    finally:
        mp.undo()


def test_read_fw_version_handles_unresolvable():
    mp = _MP()
    try:
        # A board whose active slot couldn't be imported reports the sentinel -> (None, None),
        # never the repo placeholder.
        _fake_exec(mp, "OSELIA_FW_ERR:ImportError('config')\n")
        assert board.read_fw_version("/dev/ttyX") == (None, None)
        # A blank/'?' version is also treated as unknown.
        mp.undo()
        _fake_exec(mp, "OSELIA_FW:?:a\n")
        assert board.read_fw_version("/dev/ttyX") == (None, None)
    finally:
        mp.undo()


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d cli tests" % len(fns))


if __name__ == "__main__":
    _run()
