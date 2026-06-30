"""Host tests for the serial-contention guard in oselia_provision.board.

Covers the lsof-based foreign-holder preflight (port_holders / check_port_free) and the
process-wide flock (lock_serial). No real board or lsof needed -- subprocess is stubbed and
the flock runs against a temp lockfile.

Run:  python tests/test_oselia_portguard.py   (needs `pip install -e .`)
"""
import os
import subprocess
import tempfile

import typer

from oselia_provision import board


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _stub_subprocess(monkeyresults):
    """Return a fake subprocess.run that dispatches on argv[0] -> result|exception."""
    def fake_run(cmd, *a, **kw):
        key = cmd[0]
        out = monkeyresults[key]
        if isinstance(out, Exception):
            raise out
        if callable(out):
            return out(cmd)
        return out
    return fake_run


# --- port_holders ----------------------------------------------------------
def test_port_holders_lists_others_excludes_self():
    me = os.getpid()
    def ps_for(cmd):
        pid = cmd[-1]
        return _FakeCompleted(stdout={"4242": "oselia flash\n"}.get(pid, "?\n"))
    orig = subprocess.run
    subprocess.run = _stub_subprocess({
        "lsof": _FakeCompleted(stdout="%d\n4242\n" % me),   # self + a foreign holder
        "ps": ps_for,
    })
    try:
        holders = board.port_holders("/dev/cu.usbmodemTEST")
    finally:
        subprocess.run = orig
    assert holders == ["PID 4242 (oselia flash)"], holders


def test_port_holders_empty_when_none():
    orig = subprocess.run
    subprocess.run = _stub_subprocess({"lsof": _FakeCompleted(stdout="\n")})
    try:
        assert board.port_holders("/dev/cu.usbmodemTEST") == []
    finally:
        subprocess.run = orig


def test_port_holders_no_lsof_is_silent():
    orig = subprocess.run
    subprocess.run = _stub_subprocess({"lsof": FileNotFoundError()})
    try:
        assert board.port_holders("/dev/cu.usbmodemTEST") == []
    finally:
        subprocess.run = orig


def test_port_holders_timeout_means_wedged():
    orig = subprocess.run
    subprocess.run = _stub_subprocess({
        "lsof": subprocess.TimeoutExpired(cmd="lsof", timeout=4)})
    try:
        board.port_holders("/dev/cu.usbmodemTEST")
        assert False, "expected PortWedged"
    except board.PortWedged as e:
        assert e.port == "/dev/cu.usbmodemTEST"
    finally:
        subprocess.run = orig


# --- check_port_free -------------------------------------------------------
def test_check_port_free_passes_when_idle():
    orig = board.port_holders
    board.port_holders = lambda port, **kw: []
    try:
        board.check_port_free("/dev/cu.usbmodemTEST")        # must not raise
    finally:
        board.port_holders = orig


def test_check_port_free_none_is_noop():
    board.check_port_free(None)                               # must not raise / not call lsof


def test_check_port_free_dies_when_busy():
    orig = board.port_holders
    board.port_holders = lambda port, **kw: ["PID 4242 (oselia flash)"]
    try:
        board.check_port_free("/dev/cu.usbmodemTEST")
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass
    finally:
        board.port_holders = orig


def test_check_port_free_force_warns_not_dies():
    orig = board.port_holders
    board.port_holders = lambda port, **kw: ["PID 4242 (oselia flash)"]
    try:
        board.check_port_free("/dev/cu.usbmodemTEST", force=True)   # warns, no Exit
    finally:
        board.port_holders = orig


def test_check_port_free_wedged_dies():
    orig = board.port_holders
    def boom(port, **kw):
        raise board.PortWedged(port)
    board.port_holders = boom
    try:
        board.check_port_free("/dev/cu.usbmodemTEST")
        assert False, "expected typer.Exit"
    except typer.Exit:
        pass
    finally:
        board.port_holders = orig


# --- lock_serial -----------------------------------------------------------
def _with_temp_lockdir(fn):
    if board.fcntl is None:                                   # non-POSIX: locking is a no-op
        return
    tmp = tempfile.mkdtemp(prefix="oselia-lock-")
    orig_dir, orig_fd = board._LOCK_DIR, board._serial_lock
    board._LOCK_DIR = tmp
    board._serial_lock = None
    try:
        fn(tmp)
    finally:
        board._release_serial_lock()
        board._LOCK_DIR = orig_dir
        board._serial_lock = orig_fd


def test_lock_serial_acquires_and_is_idempotent():
    def body(tmp):
        board.lock_serial()
        assert board._serial_lock is not None
        path = os.path.join(tmp, "serial.lock")
        with open(path) as f:
            assert "PID %d" % os.getpid() in f.read()
        first = board._serial_lock
        board.lock_serial()                                  # idempotent: same fd, no error
        assert board._serial_lock is first
    _with_temp_lockdir(body)


def test_lock_serial_blocks_a_second_holder():
    def body(tmp):
        board.lock_serial()
        # An independent open file description must NOT be able to take the lock.
        other = open(os.path.join(tmp, "serial.lock"), "a+")
        try:
            board.fcntl.flock(other, board.fcntl.LOCK_EX | board.fcntl.LOCK_NB)
            assert False, "second flock should have failed while held"
        except OSError:
            pass
        finally:
            other.close()
    _with_temp_lockdir(body)


def test_lock_serial_contention_dies():
    def body(tmp):
        # Pre-lock the file from an external fd, then a fresh lock_serial must die.
        path = os.path.join(tmp, "serial.lock")
        os.makedirs(tmp, exist_ok=True)
        ext = open(path, "a+")
        board.fcntl.flock(ext, board.fcntl.LOCK_EX | board.fcntl.LOCK_NB)
        ext.write("PID 999 (oselia provision)")
        ext.flush()
        try:
            board.lock_serial()
            assert False, "expected typer.Exit on contention"
        except typer.Exit:
            pass
        finally:
            ext.close()
    _with_temp_lockdir(body)


def test_lock_serial_releases():
    def body(tmp):
        board.lock_serial()
        assert board._serial_lock is not None
        board._release_serial_lock()
        assert board._serial_lock is None
        # After release an external holder can take it.
        ext = open(os.path.join(tmp, "serial.lock"), "a+")
        try:
            board.fcntl.flock(ext, board.fcntl.LOCK_EX | board.fcntl.LOCK_NB)   # must succeed
            board.fcntl.flock(ext, board.fcntl.LOCK_UN)
        finally:
            ext.close()
    _with_temp_lockdir(body)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d portguard tests" % len(fns))


if __name__ == "__main__":
    _run()
