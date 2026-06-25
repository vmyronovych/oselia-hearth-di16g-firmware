# Installer Provisioning Spec — Host-Side USB Wizard

Status: **implemented** (`provision.py`). This is the contract for how an
installer brings a fresh Hearth online in a new house. It
sits alongside `SPEC.md` (the firmware contract) and `BRINGUP.md` (the bench
checklist). Where this document and `SPEC.md` disagree, `SPEC.md` wins for
firmware behaviour; this document owns the *installer experience*.

**Implementation decisions (resolved from the open §4 / network questions):**
- **§4 storage = option (b), `site.json` overlay.** The wizard writes a small
  machine-owned `site.json`; `../firmware/src/config.py` overlays it on top of fixed
  hardware defaults at import (see the overlay block at the end of that file).
  `site.json` is gitignored (it carries the broker password).
- **DHCP by default required a firmware change.** `../firmware/src/ch9120.py` previously always
  programmed a static IP. It now sends the DHCP command `0x57 0xab 0x33 0x01`
  (`_OP_DHCP = 0x33`, param 1 = on / 0 = off) so `USE_DHCP=True` works and the
  installer skips IP planning. Opcode + param are confirmed against the Waveshare
  "CH9120 Serial Control Instruction Set" and CONFIRMED on hardware (board leased an
  IP and reached the broker online with `USE_DHCP=True`). `src/config.py` now
  defaults to `USE_DHCP=True`. The static path (`--static`) is the fallback.
- Host helpers are unit-tested (`tests/test_provision.py`,
  `tests/test_config_overlay.py`); on-hardware bring-up remains to be confirmed.

---

## 1. Goal and audience

**Goal:** an electrician / smart-home installer with no Python knowledge gets a
unit from "just wired, never powered for network" to "device + all inputs
visible in Home Assistant" in a few minutes, using one command and a few
prompts — no text editor, no Python-tuple syntax, no static-IP planning.

**Audience:** the *installer* runs the wizard on their own laptop. They are
assumed to be able to: plug a USB cable into the board, run one command in a
terminal, and answer prompts. They are **not** assumed to know Python, MQTT
wire format, or this board's pin map.

**Precondition:** Home Assistant (with an MQTT broker reachable on the LAN) is
already installed and running. The board is physically wired (24 V inputs,
Ethernet, USB).

### 1.1 Why USB, not a captive portal

This board has **no radio**. The WiFi "join an AP, open a web portal" flow that
Tasmota/ESPHome installers expect is impossible here. The only channels into a
fresh unit are USB serial (laptop) or the wired network *after* it is already
configured (chicken-and-egg). Provisioning therefore happens over **USB**, with
the installer's laptop acting as the smart side (it has DNS/mDNS; the board does
not).

---

## 2. Scope — what the installer actually has to supply

The firmware already auto-registers every input in Home Assistant via MQTT
discovery (`SPEC.md §5`). So the only genuinely site-specific data is a small
kernel:

| Item | Source | Notes |
|------|--------|-------|
| Broker IP (numeric) | mDNS: auto if one, choose if several, manual if none | CH9120 does **no DNS** — must end up numeric (`SPEC.md §4`) |
| Broker port | default `1883`, override on prompt | |
| MQTT username / password | prompt (optional) | many HA installs require auth |

Everything else is defaulted and **not** asked:

- **The board's own IP → DHCP by default.** This is an MQTT *client*; nothing
  connects *to* it, so it needs no stable address. `USE_DHCP=True` removes
  `LOCAL_IP` / `GATEWAY` / `SUBNET_MASK` from the installer's job. Static IP
  stays available as an advanced flag (see §6) for sites that require it.
- **Board count → auto-discovered, not asked.** The firmware scans the I²C bus at
  boot (`MCP_AUTODISCOVER`) and drives exactly the MCP23017 chips that respond, so
  there is no count to enter and an unwired board never causes a fault. An explicit
  count stays available via `--boards` / `board_count` (see §6) to pin it.
- **Hardware pins, timings, robustness knobs** → keep `config.example.py`
  defaults; never prompted.
- **Switch-to-room names** → handled in Home Assistant, not here (see §5).

> Design rule: the wizard asks **at most 3 questions** on the happy path
> (broker confirm, optional creds). Anything else is a flag.

---

## 3. The wizard (`provision.py`) — happy path

Runs on the installer's laptop, drives the board over `mpremote`. One command:

```
python3 provision.py
```

Step by step:

1. **Find the board.** Auto-detect the RP2040 USB serial port (filter by
   USB VID/PID; if more than one candidate, list them and ask). Fail clearly if
   none found ("No RP2040-ETH detected over USB — is it plugged in?"). If no
   MicroPython board is on USB but a **BOOTSEL drive (RPI-RP2) is mounted**, offer to
   flash the interpreter onto that bare board (`acquire_board`). This path **wipes the
   flash first** (`flash_nuke`, then the MicroPython UF2): we have no REPL to park a prior
   OTA app, and a preserved old firmware that wedges USB on boot would leave the board
   un-detectable — a clean erase guarantees a bare REPL with stable USB. (HW-verified: a
   bare-MicroPython re-flash that *keeps* littlefs boots the old firmware, which wedged USB
   enumeration so the board never re-appeared.) Provisioning then writes `site.json` +
   firmware fresh, so nothing is lost by the wipe.

1a. **Check the MicroPython interpreter.** Read `os.uname().release` and compare to the
   pinned `EXPECTED_MPY_VERSION` (`1.28.0`, kept in step with `firmware/FLASHING.md`).
   On a mismatch or no MicroPython, **inform the installer and offer to flash it
   automatically** (`ensure_micropython`): reboot into BOOTSEL (`machine.bootloader()`,
   or the BOOT+RESET dance on a bare board), copy the pinned UF2 to the RPI-RP2 drive,
   and wait for the board to **re-enumerate on USB** (detected by the port re-appearing,
   *not* a REPL exec — a re-flash keeps littlefs, so a board with a prior OTA layout boots
   straight into the watchdog'd firmware and an exec probe would race its reset). The
   wizard then quiesces that firmware (see "Quiescing", below) before reading the version.
   The UF2 comes from the
   **bundled `uf2/`** (offline), then `~/.cache/oselia`, then a download (`MPY_UF2_URL`);
   `--mpy-uf2 PATH` overrides. The flash preserves littlefs,
   so a prior `site.json` survives. `--skip-mpy-check` bypasses the whole step; a missing
   interpreter the installer declines to flash is fatal (can't copy `src/*.py`), but a
   *version mismatch* they decline is a warning and provisioning continues.

2. **Find the broker.** A two-stage discovery, then `_pick_one` (auto when one,
   numbered menu when several, manual when none):
   - **mDNS** — browse `_mqtt._tcp` for the full window, dedupe to numeric `(ip,port)`.
   - **LAN port scan (fallback when mDNS finds nothing — e.g. a plain broker that
     doesn't advertise)** — scan the laptop's /24 for **port 1883** and *verify the
     protocol* (`_probe_mqtt`: TCP open + a CONNACK to our CONNECT), concurrently
     (~1 s for a /24). Only confirmed brokers are offered, so it won't match random
     open ports.
   - **Manual entry** (validated IPv4 / port) if both find nothing.

   Resolving a hostname the installer types is allowed *here* (the laptop has DNS)
   as long as the value written to the board is the resolved numeric IP.
   **Home Assistant discovery for `--ha-setup` works the same way** (`_home-assistant.
   _tcp`, then a port-8123 scan verified by `GET /api/` → 401; one → auto, several →
   choose, none → `broker-ip:8123`). `--broker` / `--ha-url` skip discovery.

3. **Credentials.** `Broker username (blank for none):` then password
   (masked input). Blank → anonymous.

   (No board-count question: the firmware auto-discovers the MCP chips on the bus
   at boot. `--boards N` pins an explicit count for sites that want it.)

5. **Validate before writing.** Optionally open a TCP connection from the laptop
   to `broker_ip:port` (and, if creds given, a real MQTT CONNECT) and report
   reachability. This catches "wrong IP / broker not running / bad password"
   *before* touching the board, while the installer is still standing there.

6. **Write config to the board.** Generate the per-install config from the
   answers (see §4), copy it to the board over `mpremote`, copy `src/*.py` if
   not already present (so the wizard can flash a blank-but-MicroPython board
   end to end), and reset.

7. **Confirm it came up.** Reset the board and watch the **broker** for the unit's retained
   `online` — this is the authoritative check (network truth, independent of the USB serial,
   which a cold boot can wedge on this board). On a broker-wait timeout, fall back to a
   best-effort serial capture + LED/serial classification. Report **PASS** when the broker
   shows `online` (or the serial confirms HA bring-up); report a specific **FAIL** otherwise.
   (To *watch* the boot log live over USB, the installer runs `--monitor` separately — §6.3.)
   FAIL causes, mapped from the LED / serial:
   - red slow-blink → "Ethernet/TCP to broker is down — check cable / broker IP"
   - orange med-blink → "Reached the broker TCP port but MQTT login failed —
     check username/password"
   - yellow fast-blink → "No input board responding — check I²C wiring / board
     count"
   The wizard exits non-zero on FAIL so it is scriptable.

8. **Point to next step.** On PASS, print: "Device is online. Open Home
   Assistant → Settings → Devices → 'Hearth' to name your
   switches (see §5)."

### 3.1 Idempotency / re-provisioning

Running the wizard again on an already-configured unit must be safe: it reads
the existing config if present, offers current values as defaults, and only
rewrites what changed. Used this way it doubles as the "change the broker / add
a board later" tool.

**Quiescing a RUNNING unit (cooperative maintenance command).** Re-provisioning a unit that
is actively running the firmware means pausing it so the host can rewrite the board over USB.
Doing that host-side (`_disable_app`: break into the REPL, rename the loader, reset) fights
the firmware's **hardware watchdog** — when the host interrupts core 0 the WDT stops being
fed and **hard-resets** the board, and a cold boot can wedge USB enumeration (see the
firmware boot-wedge notes). So the wizard first tries a **cooperative quiesce**: it finds the
unit on the network (mDNS / LAN scan → the single `online` device on `<base>/+/status`,
without touching USB so the unit's MQTT session stays alive) and publishes
`<base>/<id>/cmd/maintenance`. The **firmware** then renames its loader
(`boot.py`→`boot.py.provbak`) and `machine.reset()`s **itself** — no host break-in, no
watchdog race — so the board boots **bare** (no `main`, no WDT) with stable USB, ready to be
rewritten reliably. `_restore_app` reinstates the loader afterwards (same `.provbak` suffix).
If the unit can't be targeted unambiguously (zero/multiple online, or an auth broker — no
creds are known pre-quiesce), the wizard falls back to the USB-driven `_disable_app`, which
is reliable for a bare/idle board but fragile on a running watchdog unit. The cooperative
path is what makes in-place re-provision (no BOOTSEL) reliable. See `firmware/SPEC.md` §5.3.

---

## 4. What the wizard writes to the board

The wizard does **not** ask the installer to edit `config.py`. It generates the
per-install values and writes them. **Chosen: option (b)** —

- **(b) A tiny machine-owned `site.json`** (broker, creds, dhcp, board count,
  optional name rows, optional `diag` opt-out, optional `ha_integration` mode) that `../firmware/src/config.py` reads and overlays onto fixed,
  never-touched hardware defaults. Clean separation of installer data vs. hardware
  facts; idempotent re-provision rewrites only this file. The firmware change is
  the small guarded overlay block at the end of `../firmware/src/config.py`.

(Option (a), templating the whole `config.py`, was the simpler alternative but was
not chosen.)

The installer-facing artifact is **generated, validated, and not hand-edited**.
Secrets (broker password) live only in `site.json`, which is gitignored and stays
out of source control.

### 4a. On-board layout: OTA A/B slots (not flat)

So a freshly provisioned unit is **OTA-ready out of the box**, the wizard lays down the
slot layout from `firmware/OTA_SPEC.md` rather than copying the app flat to root:

```
/boot.py            loader (installed LAST; never part of an OTA bundle)
/site.json          per-unit config (above)
/ota/state          fresh boot-confirm state {active:a, pending:false, ...}
/slots/a/  <app>    all firmware src/*.py except boot.py
```

`copy_firmware()` creates `/slots/a` + `/ota`, copies the app there, writes a fresh
`/ota/state`, clears any old flat-layout root modules (so a re-provision **migrates**
a pre-OTA unit onto slots), then installs `/boot.py` **last** — an interrupted copy
leaves a stable REPL, not a boot.py reset loop. `_disable_app` parks whichever auto-run
entry exists (`/boot.py` on a slot unit, else `/main.py`) before writing. After this,
every firmware update goes over Ethernet from Home Assistant (`OTA_SPEC.md`,
`../homeassistant/INTEGRATION_SPEC.md`) — USB is needed only for this
first install.

> The firmware also **writes** `site.json` itself when a live-tunable is changed
> from HA (`long_ms` / `double_gap_ms` / `debounce_ms` / `log_level`; see
> `SPEC.md §5.4`), merging the key via an atomic temp+rename. Re-running the wizard
> reads the existing `site.json` and only rewrites the keys it owns, so board-set
> tunables are preserved unless explicitly overridden.

---

## 5. Switch naming — Home Assistant first, on-device optional

Decision: **support both, default to Home Assistant.**

- **Default (HA-side).** The firmware publishes generic
  `board<b>_input<p>` triggers; HA auto-creates the device and all
  inputs × gestures. The installer names "which switch is what" in the HA UI —
  a rename, not a redeploy. The wizard does **not** prompt for names.

- **To make HA-side mapping painless:** rely on a *learn affordance* (proposed
  for the firmware): every press is also published to a single retained
  `…/last_input` topic (board / pin / gesture) and echoed on the LED. The
  installer walks the house pressing each switch, watches one topic (or a small
  HA dashboard card), and labels each input as they go — no wiring diagram
  needed. *(This is a firmware feature, specced here only as the reason the
  wizard can skip naming; tracked separately.)*

- **Optional (on-device).** `INPUT_NAME_OVERRIDES` in the config still works for
  installers who want friendly names baked into discovery. The wizard MAY expose
  this behind an advanced flag (e.g. accept a `--names names.csv` file mapping
  `board,pin,name`) but never prompts for it interactively on the happy path.
  Changing an on-device name means re-running the wizard / redeploying.

---

## 6. Advanced / non-happy-path (flags, not prompts)

Kept out of the interactive flow so the common case stays ≤3 questions:

- `--static IP/GW/MASK` — force a static address instead of DHCP (sites that
  disallow DHCP for fixed devices). Writes `USE_DHCP=False` + the three values.
- `--boards <N>` — pin an explicit board count (1–8) instead of auto-discovering;
  writes `board_count`, which disables `MCP_AUTODISCOVER` on the board.
- `--port <serial>` — skip USB auto-detect / disambiguate multiple boards.
- `--broker <ip[:port]>` — skip mDNS discovery (headless / scripted installs).
- `--names <file>` — on-device name overrides (see §5).
- `--skip-mpy-check` — skip the MicroPython version check / auto-flash (step 1a).
- `--mpy-uf2 <path>` — flash this local UF2 instead of downloading (offline installs).
- `--no-diag` — disable diagnostics telemetry on the unit; writes `"diag": false`
  to `site.json` so the firmware publishes no `…/diag/state` and registers no HA
  diagnostic entities (`SPEC.md §5.2`). Default is on.
- **HA integration mode** — the first-party **OSELIA integration** is now the
  **default**; `--mqtt` opts into legacy MQTT discovery, `--oselia` selects OSELIA
  explicitly (implicit; the two flags are mutually exclusive). OSELIA mode writes
  `"ha_integration": "oselia"` to `site.json`, so the firmware skips publishing
  `homeassistant/.../config` (the integration owns the entities; the device appears
  under OSELIA, not the MQTT integration); `--mqtt` omits the key (firmware default).
  The data/command topics are identical, so a unit can switch modes with no other
  change. In OSELIA mode the wizard sets the integration up in HA automatically
  (idempotent: adds it if absent, else updates its options + firmware release feed) and
  (re)builds the `/oselia-hearth` dashboard. An explicit flag wins; otherwise a prior
  choice recorded on the board is preserved across re-provisions; otherwise OSELIA
  applies. HA setup follows the unit's **actual** `ha_integration` (from `site.json`),
  not just this run's flag — so re-running the wizard on a legacy `--mqtt` unit keeps the
  MQTT path rather than switching it. See
  `../homeassistant/INTEGRATION_SPEC.md`.
- HA setup is **prompted by default** once the unit is online (`Set up Home Assistant
  now …? [Y/n]`). `--ha-setup` does it **without asking**; `--no-ha-setup` skips it
  silently. With `--ha-url` (default: mDNS, then port-8123 scan, else
  `http://<broker-ip>:8123`) and a long-lived token (`--ha-token`, else
  `$OSELIA_HA_TOKEN`, else `~/.config/oselia/ha_token`, else prompted). The token is
  **validated** first; a rejected/stale one re-prompts. See §6.1.
- `--no-flash` — write config only, assume `src/*.py` already on the board.
- `--dry-run` — show what would be written without touching the board.

---

### 6.1 Home Assistant auto-setup (`--ha-setup`)

`ha_setup.py` sets HA up so the installer gets a working UI with no manual HA steps.
What it installs depends on the unit's integration mode:

- **Locate HA** (both modes): `--ha-url` if given, else **mDNS** (`_home-assistant._tcp`)
  on the LAN, else fall back to `http://<broker-ip>:8123` (HA co-located with the broker).
- **OSELIA mode (default)** — `ensure_oselia` adds/updates the OSELIA config entry
  (pointed at the same broker) and sets the firmware release feed (config/options flows
  are **REST-only**). The feed URL and GitHub token resolve from `--release-url` /
  `--github-token` (or `$OSELIA_GH_TOKEN` / `~/.config/oselia/gh_token`); when neither was
  supplied and the run is **interactive**, the wizard stops and asks whether to provide
  them (a token is required because the release repo is private), rather than silently
  defaulting to a feed that 404s. Then `ensure_oselia_dashboard` (re)builds the shared
  `/oselia-hearth` dashboard, reusing `dashboards/generate.py`'s `build_config` /
  `push_config` so the wizard and a manual `generate.py` run produce the identical
  dashboard. The freshly provisioned gateway joins as a `gw-<id>` Sections view (logo +
  status + inputs-by-board + controls), built from the live device/entity registry. On
  success the wizard prints a **clickable link** to `<ha>/oselia-hearth/gw-<id>` (where
  the installer also names the switches on the device page). A soft-skip (no link) when
  no OSELIA gateways are visible yet — re-running `--ha-setup` picks them up.
- **Legacy `--mqtt` mode** — `run_setup` ensures HA's **MQTT integration** (check
  `config_entries/get`; if no `mqtt` entry, add one via the config flow pointed at the
  same broker, so a fresh HA ingests the unit's retained discovery and the device
  appears) and installs the **switch blueprint** (`blueprint/save`, overwritten if
  present). No curated dashboard — the device's entities are auto-created under the MQTT
  integration. (The legacy per-unit `/hearth-di16g` dashboard builder has been removed.)

Dashboard/blueprint installs are **WebSocket-API** operations (HA's REST `/api/` cannot
install blueprints or dashboards), while config/options flows are REST-only; auth is the
long-lived token throughout. The client is a minimal stdlib-only
WebSocket implementation in `ha_setup.py` (no new dependency). It runs only after the
unit is confirmed online, and a failure is reported but does **not** fail
provisioning (the unit is already up).

**Token resolution** (`provision.py`): `--ha-token` → `$OSELIA_HA_TOKEN` →
`~/.config/oselia/ha_token` → otherwise the wizard **prompts** the installer (masked
input) with instructions for creating one (profile → Security → Long-Lived Access
Tokens), and offers to save it to `~/.config/oselia/ha_token` (chmod 600) for reuse.
The token is never written to `site.json` (it is HA-side) and stays out of git.

Robustness notes (validated by the clean-room test): the online-wait uses
`keepalive=0` so the broker doesn't drop the watcher before a slow boot publishes
`online`. The OSELIA dashboard is rebuilt from whichever gateways the integration has
registered at that moment (`build_config` reads the live registry); if the just-
provisioned unit hasn't registered yet, the build is **soft-skipped** (no link printed)
and a re-run of `--ha-setup` — or a manual `generate.py` — picks it up.

### 6.2 Uninstall / decommission

Standalone modes (each runs, then exits — they bypass the normal flow):

- **`--uninstall-firmware`** — delete every file from the board's littlefs (the
  MicroPython interpreter stays), leaving a bare board (`_wipe_board`).
- **`--erase-flash`** — erase the **entire** flash (MicroPython interpreter *and*
  filesystem) via Raspberry Pi's `flash_nuke.uf2`, leaving a **bare-metal** RP2040 in
  BOOTSEL (`erase_flash`). Confirms first (irreversible); the UF2 comes from the bundled
  `uf2/` (offline), else cache, else download (`FLASH_NUKE_URL`); `--erase-uf2 PATH`
  overrides. Re-flash MicroPython (step 1a /
  `firmware/FLASHING.md`) to reuse the board. This is the inverse of step 1a.
- **`--uninstall-ha`** — remove the unit's HA presence: **clear the retained MQTT
  discovery + state for the device** on the broker (a subscribe-and-clear over the
  wizard's own socket MQTT, `_clear_device_retained` — empty retained payloads make HA
  drop the device), then **rebuild** the shared `/oselia-hearth` dashboard without this
  gateway's view (deleting it only if no gateways remain) and remove the blueprint
  (`ha_setup.teardown`).
  `--uninstall-ha-mqtt` also removes the HA MQTT integration. Device id / broker come
  from `--device-id` / `--broker`, else the connected board's `site.json`; if the broker
  isn't recorded there (e.g. an already-wiped board), it's **discovered on the network**
  (same mDNS → port-scan → manual flow as step 2) rather than failing. The HA token
  resolves/prompts as for `--ha-setup`.
- **`--uninstall-all`** — full decommission: runs `--uninstall-ha` (forcing
  `--uninstall-ha-mqtt`) **then** `--uninstall-firmware`, in that order — HA cleanup
  must read the board's `site.json` *before* the wipe erases it.

### 6.3 Live log / diagnostics monitor (`--monitor`)

A standalone mode (runs, then exits) that streams the board's firmware log + boot
diagnostics over USB to the installer's terminal, for bring-up debugging and confirming a
healthy boot. It **never flashes or provisions** — it only streams from a board that is
already running MicroPython.

**Why this can't be a naïve passive serial read.** This board has a dual-core USB-wedge
quirk (see `firmware/BRINGUP.md` and the firmware boot-wedge investigation): on a **cold
hard reset** core 1 (`net_task` — CH9120 bring-up + MQTT) starves core 0 (USB/TinyUSB)
through the ~1–2 s enumeration window, so USB enumeration never completes and the board
goes **invisible on USB**. So a board that just cold-booted may present no `…/cu.usbmodem*`
to read; a board in BOOTSEL is USB *mass-storage* (`RPI-RP2` drive), not serial either —
in both cases the monitor can only report this (it doesn't reflash; `provision.py` does
that). Once enumeration *has* completed, USB survives core 1; the proven capture technique
is therefore to **hold a `mpremote` session open** (already enumerated) and launch the
firmware *from* it, so it is never cold-booted.

- **`--monitor`** (default) — **relaunch the firmware over a held `mpremote exec` session**
  and relay its stdout live (`_stream_subprocess`). The board is first quiesced to a bare,
  watchdog-free REPL (`_disable_app` — a reset to a *bare* board, not a flash; no `net_task`,
  so USB re-enumerates cleanly), its loader restored without resetting (`_restore_app`), then
  run via `mpremote connect <port> exec <loader>`. The on-device launch runs the real
  `/boot.py` loader (honouring OTA slot selection / boot-confirm), falling back to a
  flat-layout `main` at root. USB stays enumerated through `net_task`'s boot, so the full
  bring-up is visible. Ctrl-C stops and leaves the board at the REPL (`mpremote reset` /
  power-cycle resumes autorun).
- **`--monitor-passive`** — only **listen** to the board's current USB-CDC serial without
  interrupting it (never enters the raw REPL), for an already-running unit you must not
  restart. Prefers `pyserial`, falling back to a raw tty read on macOS/Linux (Windows
  requires `pyserial`). Reconnecting: if the board reboots it re-detects the port and resumes.

If no board enumerates, the mode prints a **wedge-aware** message (BOOTSEL = mass-storage,
re-flash with `provision.py`) rather than the generic "not plugged in". Lines are colourised
by the firmware's level prefix (`[E]`/`[W]`/`[D]` from `src/log.py`; INFO is left plain) on a
colour-capable TTY (honours `NO_COLOR`). Note: diagnostics *telemetry* proper
(`…/diag/state`) is an **MQTT** feed (§5.2 / `firmware/tools/watch.sh`); this mode surfaces
the **serial** log + boot diagnostics over USB instead.

## 7. Failure modes the wizard must handle gracefully

| Situation | Behaviour |
|-----------|-----------|
| No board on USB | Clear message, exit non-zero; do not hang |
| Multiple boards on USB | List ports, ask which (or require `--port`) |
| mDNS finds no broker | Fall back to validated manual entry |
| Installer types a hostname | Resolve on the laptop, write the numeric IP |
| Broker unreachable at validate | Warn, offer to re-enter or proceed anyway |
| MQTT auth fails at validate | Warn before writing; re-prompt creds |
| Board never reaches green | Map LED/serial to a specific cause (§3 step 7) |
| Re-run on configured unit | Offer existing values as defaults (§3.1) |

The wizard must **never** leave the board in a half-written state: write config
atomically (temp + rename on the board, or write-then-verify) so an aborted run
doesn't brick provisioning.

**Quiescing the firmware (up front).** On a *re-provision* the board is already running
the firmware, whose **watchdog** (core 0) resets it mid-`raw-REPL` — this corrupts not
just `mpremote fs cp` (`could not enter raw repl`) but equally the **version check** and
the **`site.json` read-back**, since all three break into the REPL. So the wizard quiesces
**once, up front** — right after acquiring the board, *before* any read or write: it parks
the auto-run entry (`/boot.py` on an OTA-layout board, else `/main.py`) and hard-resets in
one exec (`_disable_app`) so the board boots to a **bare REPL** — no firmware, no watchdog
— for the whole run. This makes re-provisioning an already-running unit as robust as
provisioning a bare board. It restores the entry after `copy_firmware` (`_restore_app`,
which drops the now-obsolete backup since the loader was reinstalled). A **`try/finally`
safety net** guarantees that if the installer aborts at a prompt (or any exception fires)
the app is restored **and the board reset** so a parked unit is never stranded at the REPL
— this is what lets the quiesce move ahead of broker validation safely. `_mpremote` also
retries the transient raw-REPL flake. (On a first/bare provision there's no app to park,
so this is effectively a no-op.)

---

## 8. Dependencies & assumptions

- Installer laptop has Python 3 + `mpremote`. The mDNS library (`zeroconf`) is
  **not** a hard prerequisite: if it's missing the wizard offers to `pip install`
  it into the running interpreter (`_ensure_zeroconf`, prompted at most once) and
  retries; declining falls back to manual broker entry. Consider shipping as a
  single self-contained script or a `pipx`-installable entry point.
- The MicroPython interpreter need **not** be pre-flashed: the wizard checks the
  version and, on a mismatch / missing interpreter, offers to flash the pinned UF2
  automatically (§3 step 1a). The physical flash is still a BOOTSEL/UF2 operation —
  the wizard just drives it — and is documented in `firmware/FLASHING.md`.
- **No internet required.** Both UF2 images (the pinned MicroPython + `flash_nuke`)
  ship in `provisioning/uf2/`, and the wizard prefers them over any download — so
  step 1a and `--erase-flash` work fully offline (see `uf2/README.md`).
- mDNS works on the installer's LAN segment (same broadcast domain as the
  broker). If the laptop is on a different VLAN, mDNS may fail → manual entry
  path (§3 step 2) covers it.

---

## 9. Acceptance criteria (for when this is built)

1. From a board with MicroPython + `src/*.py` but no `config.py`, one wizard run
   with default answers leaves the device **online** in Home Assistant
   (discovery visible, `…/status` retained `online`).
2. The installer is never shown Python syntax and never types a raw IP tuple;
   the broker IP is offered by discovery and only confirmed.
3. Happy path is ≤ 4 interactive prompts; everything else is a flag with a sane
   default.
4. A wrong broker IP / bad credentials / missing board is reported as a
   **specific** cause before or right after writing, not as a stack trace.
5. Re-running the wizard on a configured unit is safe and uses existing values
   as defaults.
6. An aborted/failed run never leaves the board with a corrupt config.
7. The static-IP, scripted (`--broker`), and `--no-flash` paths all work for
   sites where mDNS or DHCP is unavailable.

---

## 10. Out of scope (tracked elsewhere)

- The `…/last_input` "learn mode" firmware feature (§5) — firmware change,
  specify with `SPEC.md`.
- Flashing the MicroPython UF2 (`BRINGUP.md §2`).
- Any HA-side automation authoring beyond naming inputs.
- Putting the repo under source control / CI (separate task).
