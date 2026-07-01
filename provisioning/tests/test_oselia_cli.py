"""Host smoke tests for the oselia CLI surface (no board, no broker).

Asserts the acceptance-suite subcommands (`mqtt …`, `ota …`) are registered and their
help renders, so the hw-test skill can rely on them existing. Uses Typer's CliRunner.

Run:  python tests/test_oselia_cli.py
"""
from typer.testing import CliRunner

from oselia_provision.cli import app

_r = CliRunner()


def _help(*args):
    res = _r.invoke(app, list(args) + ["--help"])
    assert res.exit_code == 0, (args, res.exit_code, res.stdout)
    return res.stdout


def test_mqtt_subcommands_registered():
    top = _help("mqtt")
    for cmd in ("watch", "pub", "cmd", "bounce"):
        assert cmd in top, (cmd, top)


def test_ota_subcommands_registered():
    top = _help("ota")
    for cmd in ("build", "publish"):
        assert cmd in top, (cmd, top)


def test_mqtt_watch_exposes_expect_absent_and_for():
    h = _help("mqtt", "watch")
    assert "--expect-absent" in h and "--for" in h


def test_mqtt_bounce_exposes_container():
    assert "--container" in _help("mqtt", "bounce")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d cli tests" % len(fns))


if __name__ == "__main__":
    _run()
