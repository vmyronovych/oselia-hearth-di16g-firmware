"""Host smoke tests for the oselia CLI surface (no board, no broker).

Asserts the acceptance-suite subcommands (`mqtt …`, `ota …`) and their key options are
registered, so the hw-test skill can rely on them existing. Introspects the Click command
tree directly rather than scraping `--help` text — help rendering is width-dependent (Rich
wraps long option names on a narrow/no-TTY terminal, e.g. CI), which made substring checks
flaky; the command tree is deterministic everywhere.

Run:  python tests/test_oselia_cli.py
"""
import typer

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


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("PASS %d cli tests" % len(fns))


if __name__ == "__main__":
    _run()
