"""Host tests for the installer wizard's pure helpers (CPython, no board).

Run:  python3 tests/test_provision.py
These cover the I/O-free logic in provision.py: IP validation, board-count
mapping, names CSV parsing, site-dict assembly, bring-up classification, and the
MQTT CONNECT wire encode (checked against src/mqtt_packets.py).
"""
import os
import sys

PROV = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
FW_SRC = os.path.normpath(os.path.join(PROV, "..", "firmware", "src"))
sys.path.insert(0, PROV)            # provision.py
sys.path.insert(0, FW_SRC)          # firmware mqtt_packets (wire-format check)

import provision as p                              # noqa: E402
import mqtt_packets                                # noqa: E402
import ha_setup                                    # noqa: E402


def test_is_valid_ipv4():
    assert p.is_valid_ipv4("192.168.1.10")
    assert not p.is_valid_ipv4("192.168.1.256")
    assert not p.is_valid_ipv4("broker.local")
    assert not p.is_valid_ipv4("::1")


def test_parse_board_list():
    # Real `mpremote connect list` format: port serial vid:pid mfr product.
    out = (
        "/dev/cu.Bluetooth-Incoming-Port None 0000:0000 None None\n"
        "/dev/cu.usbmodem111201 de646c76db893922 2e8a:0005 MicroPython "
        "Board in FS mode\n"
    )
    boards = p.parse_board_list(out)
    assert boards == [("/dev/cu.usbmodem111201", "2e8a:0005",
                       "MicroPython Board in FS mode")], boards


def test_parse_board_list_none():
    out = "/dev/cu.Bluetooth-Incoming-Port None 0000:0000 None None\n"
    assert p.parse_board_list(out) == []


def test_parse_board_list_multiple():
    out = ("/dev/ttyACM0 aaa 2e8a:0005 MicroPython x\n"
           "/dev/ttyACM1 bbb 2e8a:0005 MicroPython y\n")
    assert len(p.parse_board_list(out)) == 2


def test_board_count_to_addrs():
    assert p.board_count_to_addrs(1) == [0x20]
    assert p.board_count_to_addrs(3) == [0x20, 0x21, 0x22]
    # Full MCP23017 strap range: 8 boards 0x20..0x27.
    assert p.board_count_to_addrs(8) == [0x20, 0x21, 0x22, 0x23,
                                         0x24, 0x25, 0x26, 0x27]
    for bad in (0, 9, -1):
        try:
            p.board_count_to_addrs(bad)
            assert False, "expected ValueError for %d" % bad
        except ValueError:
            pass


def test_parse_names_csv():
    text = ("board,pin,name\n"
            "1,1,kitchen_main\n"
            "# a comment\n"
            "\n"
            "2,5,garage_door\n")
    assert p.parse_names_csv(text) == [[1, 1, "kitchen_main"], [2, 5, "garage_door"]]


def test_parse_names_csv_out_of_range():
    try:
        p.parse_names_csv("1,17,nope\n")
        assert False, "expected ValueError for pin 17"
    except ValueError:
        pass


def test_build_site_dict_dhcp():
    s = p.build_site_dict("192.168.1.10", 1883, "ha", "secret")
    assert s["broker_ip"] == "192.168.1.10"
    assert s["broker_port"] == 1883
    assert s["mqtt_user"] == "ha" and s["mqtt_pass"] == "secret"
    assert s["use_dhcp"] is True
    assert "static" not in s


def test_build_site_dict_omits_board_count_by_default():
    # Default = autodiscover: no board_count key written.
    s = p.build_site_dict("192.168.1.10", 1883, None, None)
    assert "board_count" not in s


def test_build_site_dict_board_count_when_pinned():
    s = p.build_site_dict("192.168.1.10", 1883, None, None, board_count=3)
    assert s["board_count"] == 3


def test_build_site_dict_anon():
    s = p.build_site_dict("10.0.0.5", 1883, "", "")
    assert s["mqtt_user"] is None and s["mqtt_pass"] is None


def test_build_site_dict_static_forces_dhcp_off():
    static = {"ip": "192.168.1.50", "gateway": "192.168.1.1", "mask": "255.255.255.0"}
    s = p.build_site_dict("192.168.1.10", 1883, None, None,
                          use_dhcp=True, static=static)
    assert s["use_dhcp"] is False
    assert s["static"] == static


def test_build_site_dict_rejects_bad_ip():
    try:
        p.build_site_dict("not-an-ip", 1883, None, None, 1)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_site_dict_names():
    s = p.build_site_dict("192.168.1.10", 1883, None, None, 1,
                          names=[[1, 1, "foo"]])
    assert s["names"] == [[1, 1, "foo"]]


def test_build_site_dict_diag_default_omitted():
    # Diagnostics on by default -> no "diag" key written (keeps site.json minimal).
    s = p.build_site_dict("192.168.1.10", 1883, None, None)
    assert "diag" not in s


def test_build_site_dict_no_diag():
    s = p.build_site_dict("192.168.1.10", 1883, None, None, diag=False)
    assert s["diag"] is False


def test_build_site_dict_ha_integration_default_omitted():
    # Default "mqtt" -> no key written (keeps site.json minimal).
    s = p.build_site_dict("192.168.1.10", 1883, None, None)
    assert "ha_integration" not in s


def test_build_site_dict_ha_integration_oselia():
    s = p.build_site_dict("192.168.1.10", 1883, None, None, ha_integration="oselia")
    assert s["ha_integration"] == "oselia"


def test_classify_bringup():
    assert p.classify_bringup("[I] HA discovery published (16 inputs)")[0] == "pass"
    assert p.classify_bringup("[W] CH9120 TCP down; re-bringing up link")[0] == "ethernet"
    assert p.classify_bringup("[E] no MCP chips responding at boot")[0] == "mcp"
    assert p.classify_bringup("[E] board1 MCP@0x20 init failed: x")[0] == "mcp"
    assert p.classify_bringup("[E] connect failed: ETIMEDOUT")[0] == "mqtt"
    assert p.classify_bringup("nothing useful")[0] == "unknown"


def test_colorize_log_line():
    RED, YEL, DIM, RST = "\x1b[31m", "\x1b[33m", "\x1b[2m", "\x1b[0m"
    # WARN/ERROR/DEBUG get wrapped; the level prefix is preserved inside.
    assert p.colorize_log_line("[E] connect failed") == RED + "[E] connect failed" + RST
    assert p.colorize_log_line("[W] CH9120 TCP down") == YEL + "[W] CH9120 TCP down" + RST
    assert p.colorize_log_line("[D] tick") == DIM + "[D] tick" + RST
    # INFO and unrecognised lines pass through unchanged (no escape codes).
    assert p.colorize_log_line("[I] HA discovery published") == "[I] HA discovery published"
    assert p.colorize_log_line("=== OSELIA boot ===") == "=== OSELIA boot ==="
    assert p.colorize_log_line("") == ""


def test_connect_encode_matches_firmware():
    # The wizard's CONNECT bytes must match the firmware's known-good builder so
    # validating against a broker exercises the same wire format.
    mine = p.build_connect("cid", keepalive=15, user="u", password="pw")
    ref = mqtt_packets.build_connect("cid", keepalive=15, clean=True,
                                     user="u", password="pw")
    assert mine == ref, "CONNECT wire mismatch:\n %r\n %r" % (mine, ref)


def test_connect_encode_anon():
    mine = p.build_connect("cid", keepalive=15)
    ref = mqtt_packets.build_connect("cid", keepalive=15, clean=True)
    assert mine == ref


def test_build_publish_retain_clear():
    # Empty retained payload to a topic = "clear retained" (used before re-checking
    # online). Retain bit must be set; body is just the length-prefixed topic.
    pkt = p.build_publish("hearth/ABC123/status", b"", retain=True)
    assert pkt[0] == 0x31, hex(pkt[0])          # PUBLISH | retain
    ref = mqtt_packets.build_publish("hearth/ABC123/status", b"", retain=True)
    assert pkt == ref


def test_encode_rl():
    assert p._encode_rl(0) == b"\x00"
    assert p._encode_rl(127) == b"\x7f"
    assert p._encode_rl(128) == b"\x80\x01"


def test_pick_one_auto_single_menu_multi():
    import builtins
    fmt = lambda x: "%s:%d" % x
    assert p._pick_one([], "MQTT broker", fmt) is None          # zero -> None
    assert p._pick_one([("10.0.0.1", 1883)], "MQTT broker", fmt) \
        == ("10.0.0.1", 1883)                                   # one -> auto, no prompt
    orig = builtins.input
    builtins.input = lambda *a: "1"                              # several -> menu, pick #1
    try:
        assert p._pick_one([("a", 1), ("b", 2)], "MQTT broker", fmt) == ("b", 2)
    finally:
        builtins.input = orig


def test_parse_uname_release():
    # os.uname().release over mpremote -> the bare version string (last non-empty line).
    assert p._parse_uname_release("1.28.0\n") == "1.28.0"
    assert p._parse_uname_release("\n1.28.0\n\n") == "1.28.0"
    assert p._parse_uname_release("") is None
    assert p._parse_uname_release(None) is None
    # sanity: the pinned constants agree (version embedded in the UF2 file name)
    assert p.EXPECTED_MPY_VERSION.replace(".", "") in p.MPY_UF2_NAME.replace(".", "")


def test_scan_lan_enumerates_24_and_aggregates():
    # /24 of the laptop IP, probed concurrently; matches returned sorted. Fake probe
    # so no real network is touched.
    orig = p._primary_ipv4
    p._primary_ipv4 = lambda: "10.0.0.5"
    try:
        hits = {"10.0.0.20", "10.0.0.10"}
        probe = lambda h: (h, 1883) if h in hits else None
        assert p._scan_lan(probe, workers=32) == [("10.0.0.10", 1883),
                                                   ("10.0.0.20", 1883)]
        # no subnet -> empty
        p._primary_ipv4 = lambda: None
        assert p._scan_lan(probe) == []
    finally:
        p._primary_ipv4 = orig


# ---------------- ha_setup (pure helpers) ----------------
def test_ha_parse_url():
    assert ha_setup._parse_url("http://localhost:8123") == ("localhost", 8123, False)
    assert ha_setup._parse_url("http://10.0.0.5") == ("10.0.0.5", 8123, False)
    assert ha_setup._parse_url("https://ha.example.com/") == ("ha.example.com", 443, True)
    assert ha_setup._parse_url("http://ha:9000") == ("ha", 9000, False)


def test_ha_blueprint_source_url():
    txt = "blueprint:\n  name: x\n  source_url: https://example.com/bp.yaml\n"
    assert ha_setup._blueprint_source_url(txt) == "https://example.com/bp.yaml"
    assert ha_setup._blueprint_source_url("blueprint:\n  name: x\n") is None


def test_ha_ensure_mqtt_skips_when_present():
    # If HA already has an MQTT config entry, ensure_mqtt is a no-op (no config flow,
    # no REST call) and returns False.
    class _WS:
        def call(self, t, **kw):
            assert t == "config_entries/get"
            return [{"domain": "sun"}, {"domain": "mqtt"}]
    assert ha_setup.ensure_mqtt(_WS(), "http://ha:8123", "tok",
                                "10.0.0.1", 1883, None, None) is False


def test_ha_dashboard_url_path_is_oselia_hearth():
    # The wizard now builds the shared OSELIA Hearth dashboard, not the legacy per-unit
    # /hearth-di16g one.
    assert ha_setup.DASHBOARD_URL_PATH == "oselia-hearth"


def _load_generate():
    """Import the standalone dashboard generator the wizard reuses (loaded by path -- it
    lives outside the provisioning package). Mirrors ha_setup._load_dashboard_generator."""
    import importlib.util
    path = os.path.normpath(os.path.join(PROV, "..", "homeassistant",
                                         "dashboards", "generate.py"))
    spec = importlib.util.spec_from_file_location("oselia_dashboard_generate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RegistryWS:
    """Fake HA WS serving a one-gateway device + entity registry to build_config."""
    def __init__(self, devices, entities):
        self.devices, self.entities = devices, entities
        self.saved = None
        self.dashboards = []                      # nothing exists yet
    def call(self, t, **kw):
        if t == "config/device_registry/list":
            return self.devices
        if t == "config/entity_registry/list":
            return self.entities
        if t == "lovelace/dashboards/list":
            return self.dashboards
        if t == "lovelace/dashboards/create":
            self.dashboards.append({"url_path": kw["url_path"], "id": "DASH"})
            return {}
        if t == "lovelace/config/save":
            self.saved = kw["config"]
            return {}
        raise AssertionError("unexpected WS call %s" % t)


def test_generate_build_config_one_gateway_view():
    # build_config groups a gateway's entities into a single Sections view at gw-<id>,
    # and a unit with no OSELIA device yields zero views (the wizard's soft-skip case).
    gen = _load_generate()
    devices = [{"id": "D", "name": "Hearth 893922",
                "identifiers": [["oselia", "hearth_893922"]]}]
    entities = [
        {"device_id": "D", "entity_id": "event.in", "unique_id": "hearth_893922_b1_in2_event"},
        {"device_id": "D", "entity_id": "sensor.ip", "unique_id": "hearth_893922_diag_ip"},
        {"device_id": "D", "entity_id": "button.reboot", "unique_id": "hearth_893922_reboot"},
        {"device_id": "H", "entity_id": "binary_sensor.broker", "unique_id": "oselia_broker_1"},
    ]
    config, gw_ids = gen.build_config(_RegistryWS(devices, entities))
    assert gw_ids == ["893922"]
    assert len(config["views"]) == 1
    assert config["views"][0]["path"] == "gw-893922"

    empty, none = gen.build_config(_RegistryWS([], entities))
    assert none == [] and empty["views"] == []


def test_generate_push_config_creates_then_saves():
    gen = _load_generate()
    ws = _RegistryWS([], [])
    gen.push_config(ws, {"title": "x", "views": []})
    assert any(d["url_path"] == gen.URL_PATH for d in ws.dashboards)  # created
    assert ws.saved == {"title": "x", "views": []}                    # then saved


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
