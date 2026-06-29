---
name: oselia-provision
description: >-
  Drive the OSELIA Hearth (RP2040-ETH) board from the host with the `oselia` CLI
  (provisioning/oselia_provision, Typer-based) INSTEAD of raw mpremote. Use whenever you
  need to talk to the physical board over USB: flash MicroPython onto a bare/BOOTSEL
  module, reflash one already running MicroPython, provision a unit onto an MQTT broker,
  read/write files on the board (site.json, /slots/a), run MicroPython on it, read its
  device id / version, stream its firmware log, erase it, or render the Home Assistant
  dashboard YAML. If you find yourself about to type `mpremote ...`, reach for `oselia`
  first; if a pattern is missing, ADD it to the tool (see "Extending") rather than
  one-off mpremote.
---

# oselia-provision — host-side board toolbox & provisioning

The tool lives in `provisioning/oselia_provision/` (a Typer package). The console
command is `oselia`. It wraps every mpremote pattern this project needs behind one
retrying, USB-wedge-aware, **non-blocking** interface. Prefer it over raw `mpremote`.

## Setup (once per machine)
Install **editable from the cloned repo** (the tool flashes `provisioning/uf2/` and deploys
`firmware/src/` resolved relative to the repo — a non-editable copy would not find them):
```bash
# Installer / global command (recommended):
pipx install -e ./provisioning && pipx inject oselia-provision zeroconf pyserial
# Dev / local venv:
cd provisioning && python3 -m venv .venv && .venv/bin/python -m pip install -e '.[all]'
#   then activate the venv or call .venv/bin/oselia directly
```
`mpremote` is pulled in as a dependency. The `[all]`/inject extras add zeroconf (mDNS broker
discovery) + pyserial (reliable passive serial). Full installer-facing setup (prereqs, PATH,
picking a firmware version via `git checkout fw-vX`, offline use) is in
`provisioning/README.md` → "Prepare your laptop".

## NON-NEGOTIABLE for automated/agent use
Every interactive prompt is bypassable — **always pass one of these so you never block**:
- `-y` / `--yes` — answer yes to every confirmation.
- `-n` / `--non-interactive` — never prompt; take defaults (use for scripted runs).
These are GLOBAL options, before the subcommand: `oselia -n board info`, `oselia -y erase`.
For provision, also pass creds as flags to skip prompts: `--broker IP[:PORT] --user U
--password P` (or `--user ''` for anonymous). Add `--no-stream` to skip the live boot-log.

## Board toolbox (use these instead of mpremote)
| Need | `oselia` command | (raw mpremote it replaces) |
|------|------------------|----------------------------|
| List boards | `oselia board list` | `mpremote connect list` |
| Version + id + site.json | `oselia board info` | several execs |
| MicroPython version | `oselia board version` | `exec os.uname().release` |
| Device id (6-hex) | `oselia board id` | `exec unique_id()` |
| List a dir | `oselia board ls /slots/a` | `fs ls` |
| Read a file | `oselia board cat site.json` | `fs cat :site.json` |
| Write a file | `oselia board push f :dest` | `fs cp f :dest` |
| Read off a file | `oselia board pull :f local` | `fs cp :f local` |
| Delete a file | `oselia board rm :f` | `fs rm :f` |
| Run MicroPython | `oselia board exec 'import os; print(os.listdir())'` | `exec ...` |
| Reset (resume autorun) | `oselia board reset` | `reset` |
| Interactive REPL | `oselia board repl` | `repl` |

All accept `--port` to target a specific board; otherwise the first MicroPython board is used.

> Caution: the board toolbox execs break into the raw REPL. On a unit actively running
> the firmware the watchdog can reset it mid-exec (the wrapper retries the flake). For a
> sustained session, the high-level flows quiesce the firmware first; for ad-hoc pokes,
> if you see "could not enter raw repl", reset/replug or use `oselia provision` which
> quiesces up front.

## High-level flows
- **Provision a unit** (flash MicroPython if needed → write site.json → deploy firmware in
  the OTA slot layout → confirm `online` on the broker):
  ```bash
  oselia provision                                   # interactive wizard (installer)
  oselia -n provision --broker 192.168.1.104 --user '' --no-stream   # scripted
  oselia provision --dry-run --broker IP             # preview site.json + deploy plan
  ```
  Flags: `--port --broker --user --password --static IP/GW/MASK --boards N --names f.csv
  --no-diag --skip-mpy-check --mpy-uf2 PATH --no-flash --no-stream --dry-run`. Units are
  always provisioned for the **OSELIA integration** (`site.json` gets
  `"ha_integration": "oselia"`, written explicitly so the unit is unambiguous and any older
  `"mqtt"`-default firmware is overridden): the firmware skips MQTT discovery, and the HACS
  custom integration + the rendered dashboard own the entities. Legacy MQTT-discovery mode
  was removed from the tool, and current firmware also defaults to `"oselia"`.
- **Survey the setup**: `oselia discover` — reports **this host's network** (IP + the /24 it
  scans), **connected USB boards** (including a **bare-metal board in BOOTSEL** — the RPI-RP2
  mass-storage drive, which a plain serial scan can't see), **MQTT brokers** (with auth status:
  `anonymous`/`auth-required`/`auth-ok`/`auth-rejected`), **Home Assistant** instances, and
  the **online Hearth units** on each reachable broker. mDNS (`_mqtt._tcp` /
  `_home-assistant._tcp`, needs the `[discovery]`/zeroconf extra) first, then a
  protocol-verified LAN scan (MQTT CONNECT→CONNACK on :1883; HA `GET /api/`→401 on :8123).
  Scope with `--network` / `--usb` / `--brokers` / `--ha` / `--units` (default: all; pick
  `--network --usb` for an instant local-only check with no network scan); `--user/--password`
  to list units on an auth broker; `--json` for machine-readable output (use when scripting).
  Exits non-zero if no board/broker/HA/unit is found. Great sanity-check before provisioning,
  and to confirm a unit came online.
- **Flash MicroPython** (bare-metal in BOOTSEL, or reflash a running unit in place):
  `oselia flash` (`--wipe/--no-wipe`, `--mpy-uf2 PATH`). The pinned UF2 ships in
  `provisioning/uf2/` (offline).
- **Erase** the whole flash → bare-metal RP2040: `oselia -y erase`. Filesystem only
  (keep interpreter): `oselia wipe-fs`.
- **Stream the firmware log**: `oselia monitor` (relaunches over a held USB session so a
  cold boot can't wedge USB) or `oselia monitor --passive` (listen only, don't restart).
- **Render the HA dashboard** as YAML to paste into Home Assistant (no live HA):
  `oselia dashboard render --id <6hex> --boards N > oselia-hearth.yaml`
  (or omit `--id` to read it from the connected board). HA integration/dashboard PUSH is
  intentionally NOT done by this tool — render YAML and upload it manually.

## Hardware quirks the tool already handles (don't re-implement by hand)
- **Cold-boot USB wedge** (core-1 net_task starves core-0 USB enumeration): flows never
  cold-reset a running unit; they quiesce to a bare REPL or run over a held session.
- **Cooperative quiesce**: a running unit is asked over MQTT (`…/cmd/maintenance`) to park
  its loader and reset itself — no host REPL break-in. USB fallback otherwise.
- **Wiped vs non-wiped flash**: bare/BOOTSEL boards are wiped (flash_nuke) before the UF2;
  in-place reflash keeps littlefs (site.json survives). A board that merely failed a
  version read is NEVER reflashed (that would wedge USB).
- **Atomic site.json** (temp + rename on the board) and the **OTA A/B slot layout**
  (`/slots/a` + `/boot.py` loader installed last + fresh `/ota/state`).
- **Serial-contention guard** (`board.lock_serial` + `board.check_port_free`): every
  command that touches USB takes a process-wide flock (`~/.cache/oselia/locks/serial.lock`)
  and an `lsof` preflight on the target port, so a second `oselia` (or a stray serial
  monitor) can't open the board concurrently — concurrent opens on macOS `/dev/cu.*` are
  what wedge the device in an unkillable kernel read. Contention prints who holds it and
  exits non-zero; a hung `lsof` is reported as "unplug/replug". Override with the global
  `--force` (downgrades to a warning). Don't run two board commands at once; if you must,
  the lock will stop you rather than wedge the hardware.

## Verify after changes
```bash
cd provisioning && .venv/bin/python -m pip install -e . \
  && for t in tests/test_oselia_*.py; do .venv/bin/python "$t"; done
.venv/bin/oselia --help        # CLI wiring sanity
```
The new pure-helper tests (siteconfig/mqtt/dashboard) run without a board; the MQTT
encoders are cross-checked against `firmware/src/mqtt_packets.py`.

## Extending the tool (do this instead of raw mpremote)
When you hit a board operation the CLI doesn't cover yet:
1. Add the low-level call to `oselia_provision/board.py` (it owns the retrying mpremote
   runner `run()` / `exec_()` and the fs helpers).
2. Surface it as an `oselia board <verb>` subcommand in `cli.py` (or a flag on an existing
   flow). Keep any new prompt bypassable via `console.confirm/ask` (honours `--yes` /
   `--non-interactive`).
3. Add a pure-helper unit test if there's I/O-free logic. Update this skill's table.
Goal: the next session never needs raw mpremote — the pattern is captured here.

## Relationship to other tooling
- `firmware/.claude/skills/hw-test` + `firmware/tools/*.sh` cover **MQTT-side** bring-up
  testing (watch discovery/action topics, broker-bounce regression, gesture tests). This
  skill covers **USB-side** board control + provisioning. They complement each other.
- `oselia` is the single host tool; `provisioning/PROVISIONING_SPEC.md` is its design
  contract. HA integration/dashboard is no longer pushed by the tool — the OSELIA
  integration is installed via HACS and the dashboard is rendered with `oselia dashboard
  render` for manual upload.
