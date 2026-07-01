"""Host tests for oselia_provision.siteconfig (pure, no board, no typer).

Run:  python tests/test_oselia_siteconfig.py   (needs `pip install -e .` for the package)
"""
from oselia_provision import siteconfig as s


def test_is_valid_ipv4():
    assert s.is_valid_ipv4("192.168.1.10")
    assert not s.is_valid_ipv4("192.168.1.256")
    assert not s.is_valid_ipv4("broker.local")
    assert not s.is_valid_ipv4("::1")


def test_board_count_to_addrs():
    assert s.board_count_to_addrs(1) == [0x20]
    assert s.board_count_to_addrs(3) == [0x20, 0x21, 0x22]
    assert s.board_count_to_addrs(8) == [0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27]
    for bad in (0, 9, -1):
        try:
            s.board_count_to_addrs(bad)
            assert False, "expected ValueError for %d" % bad
        except ValueError:
            pass


def test_build_site_dict_minimal():
    site = s.build_site_dict("192.168.1.5", 1883, None, None)
    # OSELIA is the only integration mode, always written explicitly. Current firmware
    # ignores the key; it's kept to force OLDER firmware (default "mqtt") out of discovery.
    # diag-on stays omitted to keep the file minimal.
    assert site == {"broker_ip": "192.168.1.5", "broker_port": 1883,
                    "mqtt_user": None, "mqtt_pass": None, "use_dhcp": True,
                    "ha_integration": "oselia"}
    assert "diag" not in site


def test_build_site_dict_full():
    site = s.build_site_dict(
        "10.0.0.2", 8883, "u", "p", board_count=2,
        static={"ip": "10.0.0.50", "gateway": "10.0.0.1", "mask": "255.255.255.0"},
        diag=False)
    assert site["use_dhcp"] is False            # static forces DHCP off
    assert site["static"]["ip"] == "10.0.0.50"
    assert site["board_count"] == 2
    assert site["diag"] is False
    assert site["ha_integration"] == "oselia"   # always oselia


def test_build_site_dict_rejects_non_numeric_broker():
    try:
        s.build_site_dict("broker.local", 1883, None, None)
        assert False, "expected ValueError for hostname broker"
    except ValueError:
        pass


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d siteconfig tests" % len(fns))


if __name__ == "__main__":
    _run()
