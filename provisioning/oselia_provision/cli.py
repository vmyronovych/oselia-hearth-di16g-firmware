"""The `oselia` command -- a Typer CLI over the provisioning flows and the board toolbox.

Command map:
    oselia provision         full bring-up: flash MicroPython if needed -> write site.json
                             -> deploy firmware -> confirm online on the broker
    oselia flash             (re)flash the pinned MicroPython UF2 (bare-metal or in place)
    oselia erase             erase the WHOLE flash -> bare-metal RP2040 (flash_nuke)
    oselia wipe-fs           erase the filesystem only (interpreter stays)
    oselia monitor           stream the firmware log over USB (--passive to only listen)
    oselia dashboard render  emit the Lovelace dashboard YAML for manual upload to HA
    oselia board ...         low-level toolbox: list/info/ls/cat/push/pull/rm/exec/
                             reset/id/version/repl  (use these instead of raw mpremote)

Every interactive prompt is bypassable: `--yes` answers yes to all, `--non-interactive`
takes defaults and never blocks (see console.py).
"""
import json
import os
import subprocess
import sys

import typer

from . import (__version__, board, console, dashboard, discovery, firmware, flash,
               monitor, mqtt, quiesce, siteconfig)
from .constants import (DEFAULT_BASE_TOPIC, EXPECTED_MPY_VERSION, MAX_BOARDS, MPY_UF2_NAME)

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="OSELIA Hearth (RP2040-ETH) provisioning + board toolbox.")
board_app = typer.Typer(no_args_is_help=True, help="Low-level board operations "
                        "(quirk-aware mpremote wrappers -- use these instead of raw mpremote).")
dashboard_app = typer.Typer(no_args_is_help=True, help="Render Home Assistant assets.")
app.add_typer(board_app, name="board")
app.add_typer(dashboard_app, name="dashboard")


# ---------------------------------------------------------------------------
# global options
# ---------------------------------------------------------------------------
def _version_cb(value: bool):
    if value:
        typer.echo("oselia-provision %s (pins MicroPython %s)"
                   % (__version__, EXPECTED_MPY_VERSION))
        raise typer.Exit()


@app.callback()
def main(
    yes: bool = typer.Option(False, "--yes", "-y",
                             help="Answer yes to every confirmation (non-blocking)."),
    non_interactive: bool = typer.Option(False, "--non-interactive", "-n",
                                          help="Never prompt; take defaults. For scripts/CI."),
    force: bool = typer.Option(False, "--force",
                               help="Override the serial-busy guard (talk to the board even "
                                    "if another process holds it). Use with care."),
    version: bool = typer.Option(False, "--version", callback=_version_cb,
                                 is_eager=True, help="Show version and exit."),
):
    console.configure(yes, non_interactive, force)


# ---------------------------------------------------------------------------
# board selection helpers
# ---------------------------------------------------------------------------
def _pick_board(port):
    board.lock_serial(console.FORCE)        # serialise USB access before we even enumerate
    if port:
        board.check_port_free(port, console.FORCE)
        return port
    boards = board.find_boards()
    if not boards:
        if flash.find_rpi_rp2_mount():
            console.die("The board is in BOOTSEL (bare-metal, the RPI-RP2 drive) -- it has no "
                        "MicroPython yet. Run `oselia flash` (or `oselia provision`) first.")
        console.die("No RP2040-ETH detected over USB -- is it plugged in? (or pass --port)")
    if len(boards) == 1:
        console.info("Found board: %s (%s %s)" % boards[0])
        target = boards[0][0]
    else:
        target = console.pick_one(boards, "board", lambda b: "%s (%s %s)" % b)[0]
    board.check_port_free(target, console.FORCE)
    return target


def _acquire_board(port, mpy_uf2=None, allow_bootsel=True):
    """Find the board to provision. If none on serial but a BOOTSEL drive is mounted, offer
    a wiped MicroPython flash onto that bare board and re-detect."""
    board.lock_serial(console.FORCE)        # serialise USB access before we even enumerate
    if port:
        board.check_port_free(port, console.FORCE)
        return port
    if board.find_boards():
        return _pick_board(port)
    if allow_bootsel and flash.find_rpi_rp2_mount():
        console.warn("No MicroPython board on USB, but a BOOTSEL drive (RPI-RP2) is mounted.")
        if console.confirm("Flash a clean MicroPython %s onto it now? (erases the board's "
                           "flash for a fresh install)" % EXPECTED_MPY_VERSION, default=True):
            return flash.flash_micropython(mpy_uf2, None, wipe=True)
    console.die("No RP2040-ETH detected over USB -- is it plugged in? (or pass --port)")


def _banner():
    if not console.supports_color() and not console.INTERACTIVE:
        return
    v = firmware.fw_version()
    console.step("\n  OSELIA Hearth · DI16-G  —  provisioning  ·  firmware v%s" % v)
    console.info("  " + "─" * 56)


# ===========================================================================
# provision
# ===========================================================================
@app.command()
def provision(
    port: str = typer.Option(None, help="Serial port of the board (skip USB auto-detect)."),
    broker: str = typer.Option(None, metavar="IP[:PORT]",
                               help="Skip mDNS discovery; use this broker."),
    user: str = typer.Option(None, "--user", help="MQTT username (skip the prompt)."),
    password: str = typer.Option(None, "--password",
                                 help="MQTT password (skip the prompt; insecure on shared hosts)."),
    static: str = typer.Option(None, metavar="IP/GW/MASK",
                               help="Force a static address instead of DHCP."),
    boards: int = typer.Option(None, min=1, max=MAX_BOARDS,
                               help="Pin an explicit board count (1-%d) instead of "
                                    "auto-discovering MCP chips." % MAX_BOARDS),
    names: str = typer.Option(None, metavar="FILE",
                              help="CSV of board,pin,name for on-device name overrides."),
    no_diag: bool = typer.Option(False, "--no-diag",
                                 help="Disable diagnostics telemetry on the unit."),
    skip_mpy_check: bool = typer.Option(False, "--skip-mpy-check",
                                        help="Don't check the MicroPython version / offer to flash."),
    mpy_uf2: str = typer.Option(None, metavar="PATH",
                                help="MicroPython UF2 to flash (offline); default is the "
                                     "bundled pinned build %s." % MPY_UF2_NAME),
    no_flash: bool = typer.Option(False, "--no-flash",
                                  help="Write config only; assume the firmware is on the board."),
    stream: bool = typer.Option(True, "--stream/--no-stream",
                                help="Stream the boot log over USB during bring-up."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show what would be written without touching the board."),
):
    """Bring a fresh (or relocated) Hearth online: optionally flash MicroPython, write the
    site.json broker config, deploy the firmware in the OTA slot layout, and confirm the
    unit reports 'online' on the broker.

    The unit is provisioned for the OSELIA Home Assistant integration (the firmware skips
    MQTT discovery; the HACS integration + the rendered dashboard own the entities)."""
    _banner()
    static_d = None
    if static:
        try:
            ip, gw, mask = static.split("/")
            static_d = {"ip": ip, "gateway": gw, "mask": mask}
        except ValueError:
            console.die("--static must be IP/GW/MASK, e.g. "
                        "192.168.1.50/192.168.1.1/255.255.255.0")

    name_rows = None
    if names:
        with open(names) as f:
            name_rows = siteconfig.parse_names_csv(f.read())
        console.info("Loaded %d name override(s)." % len(name_rows))

    # 1. board + up-front quiesce. The firmware watchdog resets the board whenever mpremote
    # breaks in, corrupting the version check, the site.json read-back AND fs cp. Quiesce
    # ONCE up front to a bare REPL; the try/finally restores it on any early exit.
    target = None if (dry_run and not port) else _acquire_board(port, mpy_uf2)
    app_bak = None
    restored = False
    if target and not dry_run:
        console.step("Pausing the firmware so the board can be read and written ...")
        coop = quiesce.cooperative_quiesce(target)
        if coop:
            target, app_bak = coop, True       # firmware parked its own loader
        else:
            app_bak = quiesce.disable_app(target)

    try:
        if target and not skip_mpy_check:
            target = flash.ensure_micropython(mpy_uf2, target)
        existing = board.read_existing_site(target) if target else None
        if existing:
            console.info("Existing site.json found; offering its values as defaults.")

        broker_ip, broker_port = discovery.prompt_broker(broker, existing)
        ex_user = (existing or {}).get("mqtt_user") or ""
        muser = user if user is not None else (
            console.ask("Broker username (blank for none)", ex_user) or None)
        mpass = password if password is not None else console.prompt_secret(
            "Broker password (blank for none)")
        if muser and mpass is None and existing and existing.get("mqtt_pass"):
            mpass = existing["mqtt_pass"]      # keep prior password on re-run

        console.step("Validating broker %s:%d ..." % (broker_ip, broker_port))
        ok, detail = mqtt.validate(broker_ip, broker_port, muser, mpass)
        (console.ok if ok else console.warn)(("  OK -- " if ok else "  PROBLEM -- ") + detail)
        if not ok and not dry_run and not console.confirm(
                "Write config to the board anyway?", default=False):
            console.die("Aborted at broker validation; re-run with corrected details.")

        console.info("HA integration: OSELIA custom integration (firmware skips MQTT "
                     "discovery; the integration + dashboard own the entities).")

        site = siteconfig.build_site_dict(
            broker_ip, broker_port, muser, mpass, board_count=boards, use_dhcp=True,
            static=static_d, names=name_rows, diag=not no_diag)
        if existing:                           # preserve board-written live tunables
            for k in ("long_ms", "double_gap_ms", "debounce_ms", "log_level"):
                if k in existing and k not in site:
                    site[k] = existing[k]
        if no_diag:
            console.info("Diagnostics telemetry: DISABLED for this unit.")

        board.write_site_atomic(target, site, dry_run)
        if not no_flash:
            firmware.deploy(target, dry_run)
        if target and not dry_run:
            quiesce.restore_app(target, app_bak)
        restored = True

        if dry_run:
            console.ok("\nDry run complete -- nothing was written to the board.")
            return

        # bring-up + confirm via the broker (network truth, independent of USB serial).
        device_id = board.read_device_id(target)
        stream_status, stream_text = ("skipped", "")
        if stream:
            console.step("\nStreaming the boot log over USB (Ctrl-C to skip to the broker "
                         "check) ...")
            try:
                stream_status, stream_text = monitor.stream_bringup(
                    target, console.supports_color(), timeout=60.0)
            except Exception as e:
                stream_status, stream_text = ("error", "")
                console.warn("  (boot-log stream unavailable: %s)" % e)

        if device_id:
            mqtt.clear_retained_status(broker_ip, broker_port, muser, mpass,
                                       DEFAULT_BASE_TOPIC, device_id)
        console.step("\nResetting board and waiting for it to come online (broker) ...")
        board.reset(target)
        online, dev = mqtt.wait_online(broker_ip, broker_port, muser, mpass,
                                       DEFAULT_BASE_TOPIC, timeout=90.0)
        dev = dev or device_id
        if online or stream_status == "pass":
            if not online:
                console.info("(broker wait timed out, but the streamed boot confirmed bring-up)")
            _report_online(dev)
            return
        status, msg = monitor.classify_bringup(stream_text)
        console.err("\nFAIL (%s): %s" % (status, msg))
        if not stream_text:
            console.info("(no boot log captured over USB -- if the board keeps failing, "
                         "`oselia erase` then re-provision for a clean start)")
        raise typer.Exit(2)
    finally:
        # Safety net: if we parked the app but never reached the normal restore, put it back
        # and reset so the unit resumes its firmware instead of sitting at the bare REPL.
        if target and not dry_run and not restored:
            quiesce.restore_app(target, app_bak)
            board.reset(target)


def _report_online(device_id):
    console.ok("\nPASS: device%s is online in Home Assistant." %
               (" " + device_id if device_id else ""))
    console.info("\nNext steps:")
    console.info("  • Make sure the OSELIA integration is installed in HA (HACS) and "
                 "pointed at this broker.")
    if device_id:
        console.info("  • Build the dashboard YAML to paste into HA:")
        console.info("      oselia dashboard render --id %s > oselia-hearth-%s.yaml"
                     % (device_id, device_id.lower()))
    console.info("  • Name your switches in HA -> Settings -> Devices -> the Hearth device.")


# ===========================================================================
# discover (network brokers)
# ===========================================================================
def select_sections(flags):
    """Resolve which sections to run from the scope flags (dict name->bool). When none are
    selected, run them all. -> dict name->bool."""
    any_sel = any(flags.values())
    return {k: (v or not any_sel) for k, v in flags.items()}


def _collect(mdns_fn, probe_fn, timeout, scan):
    """mDNS browse, then a verified LAN-scan fallback. -> (list[(ip,port)], zeroconf_missing)."""
    m = mdns_fn(timeout=timeout)
    if m is None:
        found, zc_missing = [], True            # zeroconf unavailable
    else:
        found, zc_missing = list(m), False
    if not found and scan:
        found = discovery.scan_lan(probe_fn)
    return found, zc_missing


def _broker_auth(ip, port, user, password):
    """Classify a broker's auth: 'anonymous' (accepts no creds), 'auth-ok' (the given creds
    work), 'auth-rejected' (creds wrong), or 'auth-required' (creds needed, none given)."""
    ok, _ = mqtt.validate(ip, port, None, None)
    if ok:
        return "anonymous"
    if user is not None:
        ok2, _ = mqtt.validate(ip, port, user, password)
        return "auth-ok" if ok2 else "auth-rejected"
    return "auth-required"


@app.command()
def discover(
    network: bool = typer.Option(False, "--network", help="Only this host's network info."),
    usb: bool = typer.Option(False, "--usb",
                             help="Only connected USB boards (incl. a bare-metal board in BOOTSEL)."),
    brokers: bool = typer.Option(False, "--brokers", help="Only MQTT brokers."),
    ha: bool = typer.Option(False, "--ha", help="Only Home Assistant instances."),
    units: bool = typer.Option(False, "--units",
                               help="Only online Hearth units (per reachable broker)."),
    user: str = typer.Option(None, "--user",
                             help="MQTT username (to list units on an auth broker)."),
    password: str = typer.Option(None, "--password", help="MQTT password (with --user)."),
    timeout: float = typer.Option(3.0, "--timeout", help="mDNS browse window (seconds)."),
    scan: bool = typer.Option(True, "--scan/--no-scan",
                              help="Fall back to a verified LAN port-scan if mDNS finds nothing."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
):
    """Survey the local setup for OSELIA: this host's network, connected USB boards, MQTT
    brokers (with auth status), Home Assistant instances, and the online Hearth units on each
    reachable broker. mDNS first, then a protocol-verified LAN port-scan. Scope with
    --network/--usb/--brokers/--ha/--units (default: all). Exits non-zero if no board, broker,
    HA, or unit is found."""
    sel = select_sections({"network": network, "usb": usb, "brokers": brokers,
                           "ha": ha, "units": units})
    do_network, do_usb = sel["network"], sel["usb"]
    do_brokers, do_ha, do_units = sel["brokers"], sel["ha"], sel["units"]
    speak = not json_out
    result = {}
    zc_missing = False
    any_found = False

    # this host's network context (always available, so it only counts toward "found" when
    # it was the explicitly requested scope -- otherwise the default run would never exit 1)
    host_ip = subnet = None
    if do_network:
        host_ip = discovery._primary_ipv4()
        subnet = (host_ip.rsplit(".", 1)[0] + ".0/24") if host_ip else None
        result["network"] = {"host_ip": host_ip, "subnet": subnet}
        if network:
            any_found = any_found or bool(host_ip)

    usb_boards = []
    bootsel = None
    if do_usb:
        usb_boards = board.find_boards()        # MicroPython serial boards
        bootsel = flash.find_rpi_rp2_mount()    # a bare-metal board in BOOTSEL (mass-storage)
        result["usb_boards"] = [dict(port=p, id=vp, desc=d) for p, vp, d in usb_boards]
        result["bootsel"] = bootsel
        any_found = any_found or bool(usb_boards) or bool(bootsel)

    broker_rows = []                            # (ip, port, auth, [device_ids])
    if do_brokers or do_units:                  # units live on brokers -> need them either way
        if speak:
            console.step("Searching for MQTT brokers (mDNS, then LAN scan) ...")
        bl, miss = _collect(discovery.discover_brokers_mdns, mqtt.probe_broker, timeout, scan)
        zc_missing = zc_missing or miss
        for ip, port in bl:
            auth = _broker_auth(ip, port, user, password)
            devs = []
            if do_units and auth in ("anonymous", "auth-ok"):
                cu, cp = (user, password) if auth == "auth-ok" else (None, None)
                devs = sorted(mqtt.list_online(ip, port, cu, cp, DEFAULT_BASE_TOPIC,
                                               timeout=4.0))
            broker_rows.append((ip, port, auth, devs))
        result["brokers"] = [
            dict(ip=ip, port=port, auth=auth, **({"units": devs} if do_units else {}))
            for ip, port, auth, devs in broker_rows]
        any_found = any_found or bool(broker_rows)

    ha_rows = []
    if do_ha:
        if speak:
            console.step("Searching for Home Assistant (mDNS, then LAN scan) ...")
        ha_rows, miss = _collect(discovery.discover_ha_instances_mdns, discovery.probe_ha,
                                 timeout, scan)
        zc_missing = zc_missing or miss
        result["home_assistant"] = [dict(ip=ip, port=port) for ip, port in ha_rows]
        any_found = any_found or bool(ha_rows)

    if json_out:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(0 if any_found else 1)

    if zc_missing:
        console.warn("  (zeroconf not installed -- mDNS skipped, used the LAN scan. Enable "
                     "with: pipx inject oselia-provision zeroconf)")
    if do_network:
        console.info("\nThis host:")
        if host_ip:
            console.ok("  %s   (LAN scans %s)" % (host_ip, subnet))
        else:
            console.info("  (could not determine local IPv4 -- not on a LAN?)")
    if do_usb:
        console.info("\nUSB boards:")
        for p, vp, d in usb_boards:
            console.ok("  %s  %s  %s" % (p, vp, d))
        if bootsel:
            console.ok("  BOOTSEL: bare-metal RP2040 (RPI-RP2 drive at %s) -- run "
                       "`oselia flash` to install MicroPython" % bootsel)
        if not usb_boards and not bootsel:
            console.info("  (none connected)")
    if do_brokers or do_units:
        console.info("\nMQTT brokers:")
        for ip, port, auth, devs in broker_rows or []:
            console.ok("  %s:%d  [%s]" % (ip, port, auth))
            if do_units:
                if devs:
                    console.info("      Hearth units online: %s" % ", ".join(devs))
                elif auth == "auth-required":
                    console.info("      units: broker needs auth -- pass --user/--password")
                else:
                    console.info("      Hearth units online: (none)")
        if not broker_rows:
            console.info("  (none found)")
    if do_ha:
        console.info("\nHome Assistant:")
        for ip, port in ha_rows:
            console.ok("  http://%s:%d" % (ip, port))
        if not ha_rows:
            console.info("  (none found)")
    if broker_rows:
        console.info("\nProvision against a broker with: oselia provision --broker %s"
                     % ("%s:%d" % (broker_rows[0][0], broker_rows[0][1])))
    raise typer.Exit(0 if any_found else 1)


# ===========================================================================
# flash / erase / wipe-fs
# ===========================================================================
@app.command(name="flash")
def flash_cmd(
    port: str = typer.Option(None, "--port", help="Serial port of the board."),
    mpy_uf2: str = typer.Option(None, "--mpy-uf2", metavar="PATH",
                                help="UF2 to flash; default is the bundled pinned build."),
    wipe: bool = typer.Option(None, "--wipe/--no-wipe",
                              help="Erase the whole flash first (clean install). Default: "
                                   "wipe a bare/BOOTSEL board, keep littlefs when reflashing "
                                   "a running unit."),
):
    """Flash the pinned MicroPython interpreter. Works on a bare-metal board in BOOTSEL or
    one already running MicroPython (it reboots into BOOTSEL for you)."""
    board.lock_serial(console.FORCE)
    if board.find_boards() and not flash.find_rpi_rp2_mount():
        # reflash a running unit in place: pause its firmware first, keep littlefs by default
        target = _pick_board(port)
        if console.confirm("Pause the running firmware before reflashing?", default=True):
            quiesce.disable_app(target)
        flash.flash_micropython(mpy_uf2, target, wipe=bool(wipe))
    else:
        # bare board / BOOTSEL: default to a wiped clean install
        flash.flash_micropython(mpy_uf2, port, wipe=True if wipe is None else wipe)


@app.command()
def erase(
    port: str = typer.Option(None, "--port", help="Serial port of the board."),
    erase_uf2: str = typer.Option(None, "--erase-uf2", metavar="PATH",
                                  help="flash_nuke UF2 (offline); default downloads it."),
):
    """Erase the ENTIRE flash (MicroPython + filesystem) -> bare-metal RP2040 in BOOTSEL.
    Irreversible; confirms first (use --yes to skip the prompt)."""
    board.lock_serial(console.FORCE)
    boards = board.find_boards()
    target = port or (boards[0][0] if boards else None)
    board.check_port_free(target, console.FORCE)
    raise typer.Exit(flash.erase_flash(target, erase_uf2))


@app.command(name="wipe-fs")
def wipe_fs(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Erase the board's filesystem (all app files; the MicroPython interpreter stays)."""
    target = _pick_board(port)
    quiesce.disable_app(target)
    raise typer.Exit(flash.wipe_fs(target))


# ===========================================================================
# monitor
# ===========================================================================
@app.command(name="monitor")
def monitor_cmd(
    port: str = typer.Option(None, "--port", help="Serial port of the board."),
    passive: bool = typer.Option(False, "--passive",
                                 help="Only LISTEN to the running unit's serial (don't "
                                      "restart it)."),
):
    """Stream the firmware's log over USB (Ctrl-C to stop). Never flashes or provisions.

    Default relaunches the firmware over a held USB session (keeping USB enumerated -- a
    cold boot wedges USB on this board). --passive only listens to a running unit."""
    colorize = console.supports_color()
    target = _monitor_find_board(port)
    if passive:
        if os.name == "nt" and not monitor.has_pyserial():
            console.die("Passive serial on Windows needs pyserial: pip install pyserial")
        console.info("Listening to %s (passive -- the board is NOT interrupted). Ctrl-C to "
                     "stop." % target)
        monitor.stream_passive(target, colorize=colorize)
        return
    console.step("Pausing the firmware so it can be relaunched cleanly over USB (no flashing) ...")
    parked = quiesce.disable_app(target)
    boards = board.find_boards()
    target = boards[0][0] if boards else target
    quiesce.restore_app(target, parked)
    console.info("Starting the firmware over a held USB session and streaming its log from %s."
                 % target)
    monitor.stream_held(target, colorize=colorize)


def _monitor_find_board(port):
    board.lock_serial(console.FORCE)
    if port:
        board.check_port_free(port, console.FORCE)
        return port
    if board.find_boards():
        return _pick_board(port)
    if flash.find_rpi_rp2_mount():
        console.die("The board is in BOOTSEL (the RPI-RP2 drive) -- that's USB mass-storage, "
                    "not serial. The monitor doesn't flash; run `oselia provision` first.")
    console.die("No RP2040-ETH on USB. The monitor streams from a board already running "
                "MicroPython. If it vanished after provisioning, that's the cold-boot USB "
                "wedge: recover via BOOTSEL (hold BOOT while plugging in) and re-flash.")


# ===========================================================================
# board toolbox
# ===========================================================================
@board_app.command("list")
def board_list():
    """List connected RP2040 boards (MicroPython, and a bare-metal board in BOOTSEL)."""
    board.lock_serial(console.FORCE)        # enumeration touches the USB bus too
    boards = board.find_boards()
    for p, vidpid, desc in boards:
        console.info("%s  %s  %s" % (p, vidpid, desc))
    bootsel = flash.find_rpi_rp2_mount()
    if bootsel:
        console.warn("BOOTSEL: bare-metal RP2040 (RPI-RP2 drive at %s) -- run `oselia flash` "
                     "to install MicroPython" % bootsel)
    if not boards and not bootsel:
        console.warn("No RP2040 boards found on USB.")
        raise typer.Exit(1)


@board_app.command("info")
def board_info(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Show the board's MicroPython version, device id, and site.json summary."""
    target = _pick_board(port)
    console.info("port:        %s" % target)
    console.info("micropython: %s" % (board.read_mpy_version(target) or "?"))
    console.info("device id:   %s" % (board.read_device_id(target) or "?"))
    site = board.read_existing_site(target)
    if site:
        console.info("site.json:   broker=%s:%s user=%s dhcp=%s integration=%s" % (
            site.get("broker_ip"), site.get("broker_port"), site.get("mqtt_user"),
            site.get("use_dhcp"), site.get("ha_integration", "oselia")))
    else:
        console.info("site.json:   (none -- not provisioned)")


@board_app.command("ls")
def board_ls(path: str = typer.Argument("/"),
             port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """List a directory on the board."""
    target = _pick_board(port)
    out = board.fs_ls(target, path)
    console.info(out.rstrip() if out else "(empty)")


@board_app.command("cat")
def board_cat(remote: str = typer.Argument(..., help="Path on the board, e.g. site.json"),
              port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Print a file from the board."""
    target = _pick_board(port)
    out = board.fs_cat(target, remote)
    if out is None:
        console.die("No such file on the board: %s" % remote)
    sys.stdout.write(out)


@board_app.command("push")
def board_push(local: str = typer.Argument(..., help="Local file."),
               remote: str = typer.Argument(..., help="Destination path on the board."),
               port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Copy a local file to the board."""
    target = _pick_board(port)
    board.fs_push(target, local, remote)
    console.ok("pushed %s -> :%s" % (local, remote.lstrip(":")))


@board_app.command("pull")
def board_pull(remote: str = typer.Argument(..., help="Path on the board."),
               local: str = typer.Argument(..., help="Local destination."),
               port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Copy a file from the board to the host."""
    target = _pick_board(port)
    board.fs_pull(target, remote, local)
    console.ok("pulled :%s -> %s" % (remote.lstrip(":"), local))


@board_app.command("rm")
def board_rm(remote: str = typer.Argument(..., help="Path on the board."),
             port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Delete a file on the board."""
    target = _pick_board(port)
    board.fs_rm(target, remote)
    console.ok("removed :%s" % remote.lstrip(":"))


@board_app.command("exec")
def board_exec(code: str = typer.Argument(..., help="MicroPython to run, e.g. "
                                          "'import os; print(os.listdir())'"),
               port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Run a line/block of MicroPython on the board and print its output."""
    target = _pick_board(port)
    r = board.exec_(target, code)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.returncode != 0:
        if r.stderr:
            sys.stderr.write(r.stderr)
        raise typer.Exit(r.returncode)


@board_app.command("reset")
def board_reset(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Soft-reset the board (resume normal autorun)."""
    target = _pick_board(port)
    board.reset(target)
    console.ok("reset %s" % target)


@board_app.command("id")
def board_id(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Print the board's 6-hex device id."""
    target = _pick_board(port)
    did = board.read_device_id(target)
    if not did:
        console.die("Could not read the device id (is the firmware resetting the REPL?).")
    console.info(did)


@board_app.command("version")
def board_version(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Print the board's MicroPython version."""
    target = _pick_board(port)
    console.info(board.read_mpy_version(target) or "?")


@board_app.command("repl")
def board_repl(port: str = typer.Option(None, "--port", help="Serial port of the board.")):
    """Open an interactive REPL (passthrough to `mpremote repl`)."""
    target = _pick_board(port)
    cmd = ["mpremote", "connect", target, "repl"]
    raise typer.Exit(subprocess.call(cmd))


# ===========================================================================
# dashboard
# ===========================================================================
@dashboard_app.command("render")
def dashboard_render(
    device_id: str = typer.Option(None, "--id", help="6-hex device id (else read from a "
                                  "connected board)."),
    port: str = typer.Option(None, "--port", help="Serial port (to read the id from)."),
    name: str = typer.Option(None, "--name", help="Friendly title for the gateway view."),
    boards: int = typer.Option(1, "--boards", min=1, max=MAX_BOARDS,
                               help="Number of input boards to lay out."),
    inputs: int = typer.Option(16, "--inputs", min=1, max=16,
                               help="Inputs per board."),
    logo: bool = typer.Option(True, "--logo/--no-logo",
                              help="Embed the OSELIA logo as a data URI."),
    out: str = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout."),
):
    """Render the OSELIA Hearth dashboard as YAML to paste into Home Assistant (no live HA
    connection needed)."""
    did = device_id
    if not did:
        target = _pick_board(port)
        did = board.read_device_id(target)
        if not did:
            console.die("Could not read a device id from the board; pass --id.")
    text = dashboard.render_yaml(did, friendly=name, boards=boards,
                                 inputs_per_board=inputs, logo=logo)
    if out:
        with open(out, "w") as f:
            f.write(text)
        console.ok("wrote %s" % out)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    app()
