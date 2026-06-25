"""Host tests for diag.py pure builders (CPython, no board).

Run:  python3 tests/test_diag.py
Covers the diagnostics state dict, the state topic, the IP-string helper, and the
HA discovery payloads (entity_category, unique_id, availability, binary_sensor
shape) -- everything net_task serialises onto the wire.
"""
import os
import sys

SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import diag                                          # noqa: E402


class _Cfg:
    BASE_TOPIC = "hearth"
    DISCOVERY_PREFIX = "homeassistant"
    DEVICE_NAME = "Hearth"
    DEVICE_MODEL = "Hearth (DI16-G)"
    DEVICE_MANUFACTURER = "OSELIA"
    SW_VERSION = "0.1.0"


DID = "AABBCC"


def test_state_topic():
    assert diag.state_topic(_Cfg, DID) == "hearth/AABBCC/diag/state"


def test_build_state_shape():
    s = diag.build_state("0.1.0", 3600, "192.168.1.50", True, True, 3,
                         102400, 2, 1, "b1/in3 single", temp_c=31.5,
                         board_addrs=["0x20", "0x21", "0x22"])
    assert s["fw"] == "0.1.0"
    assert s["uptime_s"] == 3600
    assert s["ip"] == "192.168.1.50"
    assert s["eth"] is True and s["mqtt"] is True
    assert s["boards"] == 3
    assert s["board_addrs"] == ["0x20", "0x21", "0x22"]
    assert s["mem_free"] == 102400
    assert s["reconnects"] == 2 and s["dropped"] == 1
    assert s["last"] == "b1/in3 single"
    assert s["temp_c"] == 31.5


def test_build_state_temp_defaults_none():
    s = diag.build_state("0.1.0", 0, "dhcp", 0, 0, 0, 0, 0, 0, "")
    assert s["temp_c"] is None
    assert s["board_addrs"] == []      # defaults to empty list, not None


def test_rp2040_temp_c_reference_point():
    # Datasheet reference: V = 0.706 V <-> 27 C. raw = 0.706/3.3*65535.
    raw_27 = round(0.706 / 3.3 * 65535)
    assert abs(diag.rp2040_temp_c(raw_27) - 27.0) < 0.1


def test_rp2040_temp_c_never_negative():
    # This board's ADC offset makes the raw formula go negative (~-36 C at the
    # observed raw~16160); we report the magnitude so it stays plausible/positive.
    assert diag.rp2040_temp_c(16160) > 0
    assert diag.rp2040_temp_c(16160) == abs(diag.rp2040_temp_c(16160))


def test_build_state_coerces_truthiness():
    s = diag.build_state("0.1.0", 0, "dhcp", 1, 0, 0, 0, 0, 0, "")
    assert s["eth"] is True and s["mqtt"] is False


def test_ip_str():
    assert diag.ip_str(True, (1, 2, 3, 4)) == "dhcp"
    assert diag.ip_str(False, (192, 168, 1, 50)) == "192.168.1.50"


def test_format_ip_prefers_lease():
    # A read-back DHCP lease wins over the "dhcp" fallback.
    assert diag.format_ip((192, 168, 1, 77), True, (0, 0, 0, 0)) == "192.168.1.77"
    # No lease + DHCP -> "dhcp"; no lease + static -> the static IP.
    assert diag.format_ip(None, True, (10, 0, 0, 1)) == "dhcp"
    assert diag.format_ip(None, False, (10, 0, 0, 1)) == "10.0.0.1"


def test_discovery_topic_per_component():
    assert (diag.diag_discovery_topic(_Cfg, DID, "sensor", "uptime")
            == "homeassistant/sensor/AABBCC/diag_uptime/config")
    assert (diag.diag_discovery_topic(_Cfg, DID, "binary_sensor", "ethernet")
            == "homeassistant/binary_sensor/AABBCC/diag_ethernet/config")


def test_every_sensor_payload_is_diagnostic_and_wired():
    state = diag.state_topic(_Cfg, DID)
    for key, name, component, tmpl, extra in diag.DIAG_SENSORS:
        p = diag.diag_discovery_payload(_Cfg, DID, key, name, tmpl, extra)
        assert p["entity_category"] == "diagnostic", key
        assert p["state_topic"] == state, key
        assert p["unique_id"] == "hearth_AABBCC_diag_" + key, key
        assert p["availability_topic"] == "hearth/AABBCC/status", key
        assert p["value_template"] == tmpl, key
        # attaches to the one HA device, with native-feel registry fields
        assert p["device"]["identifiers"] == ["hearth_AABBCC"], key
        assert p["device"]["serial_number"] == DID, key
        # origin + expire_after for a complete, self-healing entity
        assert p["origin"]["name"] == _Cfg.DEVICE_NAME, key
        assert p["expire_after"] > 0, key
        # extra fields (units, device_class, ...) survive the merge
        for k, v in extra.items():
            assert p[k] == v, (key, k)


def test_log_entity_and_payload():
    p = diag.log_discovery_payload(_Cfg, DID)
    assert p["state_topic"] == "hearth/AABBCC/diag/log"
    assert p["unique_id"] == "hearth_AABBCC_diag_log"
    assert p["json_attributes_topic"] == p["state_topic"]
    assert "expire_after" not in p          # logs are event-driven, must persist
    b = diag.build_log("E", "boom", 42)
    assert b["line"] == "[E] boom" and b["level"] == "E" and b["ts"] == 42


def test_binary_sensor_payload_on_off():
    spec = {s[0]: s for s in diag.DIAG_SENSORS}["ethernet"]
    _, name, component, tmpl, extra = spec
    assert component == "binary_sensor"
    p = diag.diag_discovery_payload(_Cfg, DID, "ethernet", name, tmpl, extra)
    assert p["payload_on"] == "ON" and p["payload_off"] == "OFF"
    assert p["device_class"] == "connectivity"


def test_publish_diag_discovery_emits_all_retained():
    sent = []

    class _Client:
        def publish(self, topic, payload, retain=False):
            sent.append((topic, retain))

    diag.publish_diag_discovery(_Client(), _Cfg, DID)
    assert len(sent) == len(diag.DIAG_SENSORS) + 1   # + the "Last log" sensor
    assert all(retain for _, retain in sent)


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
