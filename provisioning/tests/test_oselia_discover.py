"""Host tests for the `discover` command's pure logic (no network).

Run:  python tests/test_oselia_discover.py
"""
from oselia_provision import cli
from oselia_provision import mqtt as m


_SECTIONS = ("network", "usb", "brokers", "ha", "units")


def _flags(*on):
    return {k: (k in on) for k in _SECTIONS}


def test_select_sections_default_is_all():
    # No scope flags -> run every section.
    assert cli.select_sections(_flags()) == {k: True for k in _SECTIONS}


def test_select_sections_scoped():
    # A single flag selects exactly that section.
    out = cli.select_sections(_flags("brokers"))
    assert out["brokers"] is True
    assert all(out[k] is False for k in _SECTIONS if k != "brokers")
    # combinations select exactly the chosen sections
    assert cli.select_sections(_flags("network", "usb")) == _flags("network", "usb")
    assert cli.select_sections(_flags("ha", "units")) == _flags("ha", "units")


def test_broker_auth_classification():
    orig = m.validate
    try:
        m.validate = lambda ip, port, u, p, timeout=5.0: (True, "ok")
        assert cli._broker_auth("h", 1883, None, None) == "anonymous"

        m.validate = lambda ip, port, u, p, timeout=5.0: (False, "refused")
        assert cli._broker_auth("h", 1883, None, None) == "auth-required"

        def v(ip, port, u, p, timeout=5.0):
            if u is None:
                return (False, "refused")           # anonymous rejected
            return (u == "good", "creds")           # creds checked
        m.validate = v
        assert cli._broker_auth("h", 1883, "good", "pw") == "auth-ok"
        assert cli._broker_auth("h", 1883, "bad", "pw") == "auth-rejected"
    finally:
        m.validate = orig


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d discover tests" % len(fns))


if __name__ == "__main__":
    _run()
