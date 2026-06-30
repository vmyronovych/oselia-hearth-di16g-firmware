# OSELIA Hearth provisioning — host tool

**`oselia`** (Typer-based, in [`oselia_provision/`](oselia_provision/)) is the host tool for
the OSELIA Hearth (RP2040-ETH). It flashes MicroPython onto a bare or running board,
provisions a unit onto an MQTT broker, wraps every day-to-day **board operation** as a
subcommand (so you never reach for raw `mpremote`), surveys the network (`oselia discover`),
and renders the Home Assistant dashboard as YAML for manual upload. See
[`PROVISIONING_SPEC.md`](PROVISIONING_SPEC.md) for the design contract.

## Prepare your laptop (one-time setup)

This is the full setup for an **installer running `oselia` on their own laptop** — no
editor, no AI, just a terminal. The tool deploys firmware **from this repo**, so it must be
installed *from a cloned repo*; that clone is also where you pick which firmware version
goes on a unit (see "Choosing the firmware version" below).

### 1. Prerequisites
You need **Python 3.9+**, **git**, and **pipx**. Install whichever you're missing.

- **Python 3.9+** — check with `python3 --version`. Usually already present; if not, install it:
  - macOS (needs [Homebrew](https://brew.sh)):

    ```bash
    brew install python
    ```
  - Raspberry Pi OS / Debian:

    ```bash
    sudo apt update && sudo apt install -y python3 python3-venv python3-pip
    ```
- **git** — install it:
  - macOS:

    ```bash
    xcode-select --install
    ```
  - Raspberry Pi OS / Debian:

    ```bash
    sudo apt install -y git
    ```
- **Clone repo** — clone this repo and enter it (the tool is installed and run from this folder):

  ```bash
  git clone https://github.com/vmyronovych/oselia-hearth-di16g-firmware.git
  cd oselia-hearth-di16g-firmware
  ```
- **pipx** — installs a CLI tool in its own isolated environment (`mpremote`, which talks to
  the board over USB, comes in automatically):
  - macOS:

    ```bash
    brew install pipx
    pipx ensurepath
    ```
  - Raspberry Pi OS / Debian:

    ```bash
    sudo apt update && sudo apt install -y pipx
    pipx ensurepath
    ```

`pipx ensurepath` puts pipx's app dir (`~/.local/bin`) on your PATH — reopen the terminal
afterwards so the `oselia` command is found.

### 2. Install the `oselia` command (recommended: pipx, editable from the repo)
Run from the repo root you just cloned into:
```bash
pipx install -e ./provisioning       # gives a global `oselia` command
```
`-e` (editable) keeps `oselia` linked to this repo folder, so it always flashes the UF2 in
`provisioning/uf2/` and deploys the firmware in `firmware/src/` from **your checkout**. Keep
the repo folder where it is (don't delete it); `oselia` reads from it every run.

After installing, optionally add extras to the tool you just installed (`zeroconf` = mDNS
broker discovery, `pyserial` = more reliable passive serial) — the tool runs fine without
them:
```bash
pipx inject oselia-provision zeroconf pyserial
```

> **Alternative (no pipx):** a virtualenv inside the repo —
> ```bash
> cd provisioning && python3 -m venv .venv && .venv/bin/pip install -e '.[all]'
> ```
> then run it as `.venv/bin/oselia …`, or `source .venv/bin/activate` and use `oselia …`.

### 3. Verify it's ready
```bash
oselia --version                 # e.g. "oselia-provision 0.1.0 (pins MicroPython 1.28.0)"
oselia discover                  # finds MQTT brokers on the LAN (proves the network + tool work)
oselia board list                # with a board plugged in: lists it (empty otherwise)
```
If `oselia` is "command not found" after a pipx install, run `pipx ensurepath` and open a new
terminal (it adds `~/.local/bin` to your PATH).

### Choosing the firmware version a unit gets
Because the install is editable, `oselia provision`/`oselia flash` deploy **whatever firmware
is checked out** in your clone. To install a specific released version on a house, check out
that tag first:
```bash
git fetch --tags
git checkout fw-v0.8.0           # then run oselia provision — it deploys 0.8.0
```
`oselia --version` shows the pinned **MicroPython** version; the `oselia provision` banner
shows the **firmware** version it's about to deploy; `oselia board info` shows a board's
MicroPython version, device id, and broker config.

### Keeping the tool up to date
```bash
cd oselia-hearth-di16g-firmware && git pull   # editable install picks up changes immediately
# if pyproject deps changed: pipx install -e ./provisioning --force
```

### Offline installs
The MicroPython UF2 and `flash_nuke.uf2` ship in [`uf2/`](uf2/README.md), so **flashing works
with no internet**. Only the one-time tool install (step 2) needs to download the Python
dependencies; do it once on a connected network, then it runs offline on site.

## Common commands

```bash
oselia discover                      # survey: host network, USB boards, brokers (+auth), HA, online units
oselia discover --network --usb      # instant local check (no network scan)
oselia discover --ha --json          # scope to Home Assistant, machine-readable output
oselia provision                     # wizard: flash if needed → site.json → firmware → online
oselia -n provision --broker IP --user ''   # scripted/non-blocking
oselia flash                         # (re)flash the pinned MicroPython UF2
oselia erase                         # whole flash → bare-metal RP2040 (use -y to skip the prompt)
oselia monitor                       # stream the firmware log over USB
oselia board list|info|ls|cat|push|pull|rm|exec|reset|id|version|repl   # board toolbox
oselia dashboard render --id <6hex> > oselia-hearth.yaml   # HA dashboard YAML (manual upload)
```

**Automation note:** every interactive prompt is bypassable. Pass `--yes`/`-y` (yes to all)
or `--non-interactive`/`-n` (take defaults, never block) as a **global** option before the
subcommand. For provision, pass `--broker/--user/--password` to skip the credential prompts.

See the [`oselia-provision` skill](../.claude/skills/oselia-provision/SKILL.md) for the full
command map and the hardware quirks the tool handles.

## Tests

```bash
cd provisioning
for t in tests/test_oselia_*.py; do .venv/bin/python "$t"; done   # new package tests
for t in tests/test_*.py; do .venv/bin/python "$t"; done          # all host tests
```

The MQTT wire encoders are cross-checked against `firmware/src/mqtt_packets.py`, so the
host tool and the board stay byte-compatible.
