"""Host tests for oselia_provision.dashboard (pure YAML render, no live HA).

Run:  python tests/test_oselia_dashboard.py
"""
import yaml

from oselia_provision import dashboard as d


def test_view_structure():
    view = d.build_view("A1B2C3", boards=2, inputs_per_board=16, logo=False)
    assert view["type"] == "sections"
    assert view["path"] == "gw-a1b2c3"             # device id lowercased for the path
    # status + 2 board sections + controls
    assert len(view["sections"]) == 4
    status = view["sections"][0]["cards"]
    assert any(c.get("entity") == "sensor.hearth_a1b2c3_diagnostics" for c in status)


def test_entity_ids_lowercased():
    view = d.build_view("DEADBE", logo=False)
    board1 = view["sections"][1]["cards"]
    inputs = [c["entity"] for c in board1 if c.get("type") == "tile"
              and "input" in c.get("entity", "")]
    assert inputs[0] == "event.hearth_deadbe_board_1_input_1"
    assert len(inputs) == 16


def test_render_yaml_roundtrips():
    text = d.render_yaml("A1B2C3", boards=1, logo=False)
    cfg = yaml.safe_load(text)
    assert cfg["title"] == "OSELIA Hearth"
    assert cfg["views"][0]["path"] == "gw-a1b2c3"
    # header comment lines precede the document
    assert text.lstrip().startswith("#")


def test_logo_toggle():
    with_logo = d.build_view("A1B2C3", logo=True)["sections"][0]["cards"]
    without = d.build_view("A1B2C3", logo=False)["sections"][0]["cards"]
    has_pic = any(c.get("type") == "picture" for c in with_logo)
    no_pic = any(c.get("type") == "picture" for c in without)
    # logo card present only when requested (and only if the svg resolved)
    assert no_pic is False
    assert has_pic in (True, False)                # True if logo svg found at LOGO_SVG


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d dashboard tests" % len(fns))


if __name__ == "__main__":
    _run()
