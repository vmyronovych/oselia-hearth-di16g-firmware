"""Host tests for the acceptance fault-injection plumbing in SharedState (CPython, no board).

Covers the one-shot request/take handshake core1 uses to ask core0 for an injected MCP
fault (§11 proof path). The command handlers themselves live on-device (they call
machine/utime), but this cross-core signal is pure and worth locking down.

Run:  python3 tests/test_acceptance_hooks.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared_state import SharedState                   # noqa: E402


def test_take_is_none_when_nothing_requested():
    s = SharedState()
    assert s.take_debug_fault() is None


def test_request_then_take_returns_pair_once():
    s = SharedState()
    s.request_debug_fault(2, 4)
    assert s.take_debug_fault() == (2, 4)
    # one-shot: a second take sees nothing (core0 already consumed it).
    assert s.take_debug_fault() is None


def test_latest_request_wins():
    s = SharedState()
    s.request_debug_fault(1, 3)
    s.request_debug_fault(3, 9)
    assert s.take_debug_fault() == (3, 9)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d acceptance-hook tests" % len(fns))


if __name__ == "__main__":
    _run()
