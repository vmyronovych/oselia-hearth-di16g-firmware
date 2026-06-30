"""Host tests for the config.py site.json overlay (CPython, no board).

Run:  python3 tests/test_config_overlay.py
The firmware's src/config.py overlays a machine-owned site.json (written by the
`oselia` provisioning tool) on top of its hardware defaults. These tests import config with a
crafted site.json in the cwd and assert the overrides land in the right shapes
(notably BROKER_IP as a 4-tuple, which main._validate_config still guards).
"""
import json
import os
import sys
import tempfile

SRC = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "firmware", "src"))
sys.path.insert(0, SRC)


def _load_config_with(site):
    """Import a fresh `config` with the given site dict as ./site.json (or, if
    site is None, with no site.json present). Returns the module."""
    sys.modules.pop("config", None)
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        if site is not None:
            with open("site.json", "w") as f:
                json.dump(site, f)
        import config
        return config
    finally:
        os.chdir(cwd)


def test_overlay_applies_broker_and_dhcp():
    cfg = _load_config_with({
        "broker_ip": "192.168.1.10",
        "broker_port": 8883,
        "mqtt_user": "ha",
        "mqtt_pass": "secret",
        "use_dhcp": True,
        "board_count": 3,
    })
    assert cfg.BROKER_IP == (192, 168, 1, 10), cfg.BROKER_IP
    assert cfg.BROKER_PORT == 8883
    assert cfg.MQTT_USER == "ha" and cfg.MQTT_PASS == "secret"
    assert cfg.USE_DHCP is True
    # board_count present -> explicit list AND autodiscovery disabled.
    assert cfg.MCP_ADDRESSES == [0x20, 0x21, 0x22], cfg.MCP_ADDRESSES
    assert cfg.MCP_AUTODISCOVER is False


def test_overlay_no_board_count_keeps_autodiscover():
    cfg = _load_config_with({"broker_ip": "10.0.0.1"})
    assert cfg.MCP_AUTODISCOVER is True


def test_overlay_diag_defaults_on():
    # No "diag" key -> firmware default DIAG_ENABLE stands (True).
    cfg = _load_config_with({"broker_ip": "10.0.0.1"})
    assert cfg.DIAG_ENABLE is True


def test_overlay_diag_disable():
    cfg = _load_config_with({"broker_ip": "10.0.0.1", "diag": False})
    assert cfg.DIAG_ENABLE is False


def test_overlay_ha_integration_defaults_oselia():
    # No "ha_integration" key -> firmware default ("oselia": skip MQTT discovery).
    cfg = _load_config_with({"broker_ip": "10.0.0.1"})
    assert cfg.HA_INTEGRATION == "oselia", cfg.HA_INTEGRATION


def test_overlay_ha_integration_mqtt_legacy():
    # A hand-set legacy "mqtt" override still works (publish HA discovery).
    cfg = _load_config_with({"broker_ip": "10.0.0.1", "ha_integration": "mqtt"})
    assert cfg.HA_INTEGRATION == "mqtt"


def test_overlay_persisted_tunables():
    # Board-persisted live-tune values (written by the firmware on an HA command)
    # override the hardware defaults at boot.
    cfg = _load_config_with({"broker_ip": "10.0.0.1", "long_ms": 750,
                             "double_gap_ms": 250, "debounce_ms": 15,
                             "log_level": 3})
    assert cfg.LONG_MS == 750
    assert cfg.DOUBLE_GAP_MS == 250
    assert cfg.DEBOUNCE_MS == 15
    assert cfg.LOG_LEVEL == 3


def test_overlay_blank_creds_become_none():
    cfg = _load_config_with({"broker_ip": "10.0.0.1", "mqtt_user": "",
                             "mqtt_pass": ""})
    assert cfg.MQTT_USER is None and cfg.MQTT_PASS is None


def test_overlay_static_forces_static_ip():
    cfg = _load_config_with({
        "broker_ip": "10.0.0.1",
        "use_dhcp": True,
        "static": {"ip": "10.0.0.50", "gateway": "10.0.0.1",
                   "mask": "255.255.255.0"},
    })
    assert cfg.USE_DHCP is False
    assert cfg.LOCAL_IP == (10, 0, 0, 50)
    assert cfg.GATEWAY == (10, 0, 0, 1)
    assert cfg.SUBNET_MASK == (255, 255, 255, 0)


def test_overlay_name_rows_become_tuple_keyed_dict():
    cfg = _load_config_with({
        "broker_ip": "10.0.0.1",
        "names": [[1, 1, "kitchen_main"], [2, 5, "garage_door"]],
    })
    assert cfg.INPUT_NAME_OVERRIDES[(1, 1)] == "kitchen_main"
    assert cfg.INPUT_NAME_OVERRIDES[(2, 5)] == "garage_door"


def test_no_site_json_keeps_defaults():
    cfg = _load_config_with(None)
    # Hardware defaults stand: BROKER_IP is the 4-tuple from the file.
    assert isinstance(cfg.BROKER_IP, tuple) and len(cfg.BROKER_IP) == 4
    assert 1 <= len(cfg.MCP_ADDRESSES) <= 8


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


if __name__ == "__main__":
    failures = 0
    for t in _all_tests():
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failures += 1
            print("FAIL", t.__name__, "-", e)
    print("\n{} passed, {} failed".format(len(_all_tests()) - failures, failures))
    sys.exit(1 if failures else 0)
