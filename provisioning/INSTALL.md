# Installing an OSELIA Hearth — step-by-step

This guide is for the **installer** bringing a new Hearth
online. No programming needed: you plug in a USB cable, run one command, and
answer a few questions. The wizard does the rest.

> If something here disagrees with `PROVISIONING_SPEC.md`, the spec is the source
> of truth — but you shouldn't need it for a normal install.

---

## Before you start

> ⚠️ **Never connect USB-C while the board's 24 V supply is ON.** This board can't be
> powered from USB and the 24 V supply at the same time. **Switch the 24 V supply OFF
> before plugging in USB**, and only switch it back on after you've unplugged USB. (USB
> alone powers the gateway fine for provisioning — see the sequence below.)

You need:

- The **Hearth**, already wired (24 V inputs + **Ethernet plugged in**), **24 V supply
  switched OFF** for now. MicroPython does **not** need to be pre-flashed — the wizard
  checks the interpreter version and offers to flash the pinned build for you if it's
  missing or wrong. The UF2 **ships with the wizard** (`uf2/`), so this works **offline**
  (no internet needed); see `../firmware/FLASHING.md`.
- A **USB cable** from the gateway to your laptop. USB powers the gateway during
  provisioning; Ethernet must stay connected so it can reach the broker.
- **Home Assistant** already running on the LAN with its MQTT broker, on the
  **same network** the gateway's Ethernet is plugged into.
- Your **broker username / password**, if your Home Assistant requires MQTT login.

## One-time laptop setup

Install Python 3, then the tool the wizard uses to talk to the board:

```bash
pip install mpremote
# optional: improves serial diagnostics if a board fails to come up, and is required for
# `--monitor-passive` (listening to an already-running unit) on Windows:
pip install pyserial
```

`zeroconf` (used to auto-find your broker / Home Assistant) is **not** required up
front — if it's missing the wizard **offers to install it for you** on first run.
(`mpremote` talks to the board over USB.)

---

## Provisioning a gateway

1. **Confirm the board's 24 V supply is OFF**, then plug the gateway into your laptop
   with the USB cable. Leave the **Ethernet** connected; the **24 V supply stays off**
   for the whole provisioning step (USB powers the board, and the inputs aren't needed
   to provision — you'll test them after).

2. From this `provisioning/` folder, run:

   ```bash
   python3 provision.py
   ```

3. Answer the prompts:

   The wizard **finds MQTT brokers on the network automatically** — first via mDNS,
   and if nothing advertises, by **scanning the local network for MQTT brokers**
   (open port 1883, protocol-verified, ~1 s). Then:
   - **one broker found** → it's selected automatically (no prompt);
   - **several found** → you pick from a numbered list;
   - **none found** → you type the broker IP (or hostname) and port.

   (Home Assistant is discovered the same way for `--ha-setup` — mDNS, then a
   port-8123 scan.)

   | Prompt | What to do |
   |--------|------------|
   | `Which MQTT broker number` | Only if several were found — type the number of the one to use. |
   | `Broker IP or hostname` | Only if none were found — type your broker's address. |
   | `Broker username` | Type it, or leave **blank** if your broker allows anonymous access. |
   | `Broker password` | Type it (it won't show on screen), or leave blank. |

   The number of input boards is **auto-detected** — the gateway scans its I²C bus
   at boot and uses whatever boards are connected, so you're not asked for a count.
   (Add a board later? Just wire it and power-cycle the gateway to pick it up.)

4. The wizard checks the broker is reachable **before** writing anything, then
   writes the config to the board, copies the firmware, restarts it, and waits for it to
   report **online via the broker**. (To watch the board's boot log live over USB while it
   comes up, run `--monitor` — see *Watching live logs* below.)

5. Watch the result:
   - **`PASS: device … is online`** — done. The gateway is in Home Assistant.
     If you set up HA (next step), the wizard prints a **clickable link** to this
     gateway's view on the OSELIA Hearth dashboard (`…/oselia-hearth/gw-<id>`, where
     you also name your switches on the device page); Cmd/Ctrl-click it to open HA.
   - **`FAIL (…)`** — the message tells you the cause; see *Troubleshooting* below.

6. On PASS, **unplug the USB cable, then switch the 24 V supply ON.** The gateway reboots
   on field power, reconnects to the broker, and the inputs come alive. (Provisioning ran
   on USB power with the inputs off, so this is when the switches start working.)

7. Name your switches: click the **Device** link the wizard printed (or open
   **Home Assistant → Settings → Devices → "Hearth"**) — see the next section.

---

## Naming your switches (in Home Assistant)

You do **not** name switches in the wizard. Every input shows up in Home
Assistant as `board<N>_input<M>`. To label them:

- Walk the house and press each physical switch.
- In Home Assistant, watch the device's triggers light up as you press, and
  rename each one to the room/function it controls.

This is just renaming in the HA UI — no re-flashing. (If you really want names
baked onto the device instead, see `--names` below.)

---

## Reacting to a press (single / double / long) in Home Assistant

Each input reports **how** it was pressed, so one physical switch can do more than one
thing — e.g. a **single** tap turns a light on, a **long** hold turns it off.

> **Out of the box you get `single` and `long`.** `double` is only emitted if
> `DOUBLE_GAP_MS > 0` in `firmware/config.py` (it's `0` = off by default, which makes a
> single tap fire instantly). Leave it off unless you actually use double-tap.

Each gesture is published to an MQTT topic:

```
hearth/<device_id>/board<B>/input<P>/action      payload: single | double | long
```

- `<device_id>` is the gateway's id — find it with
  `mosquitto_sub -h <BROKER_IP> -t 'hearth/+/status' -v` (prints
  `hearth/<device_id>/status online`), or read it off the device page in HA.
- `<B>` = board number (1-based), `<P>` = input number on that board (1–16).

There are two ways to act on it.

### Option A — the HA UI (no YAML; easiest)

The gateway auto-advertises every input/gesture as a Home Assistant **device trigger**,
so you can build the automation entirely in the UI:

1. **Settings → Automations & scenes → Create automation → Start with an empty automation**.
2. **Add trigger → Device**, pick **"Hearth"**, then choose the input and the press
   type:
   - **"… button short press"** = a **single** tap
   - **"… button long press"** = a **long** hold
   - (**"… button double press"** appears only if you enabled `double`.)
3. **Add action** — e.g. *Light → Toggle*, pick your light. Save.

Make one automation for the single press and a second for the long press if you want each
to do something different.

### Option B — YAML in `automations.yaml` (for REST calls / advanced actions)

Trigger directly on the MQTT topic. Generic template (light on a single tap, off on a
long hold of board 1, input 1):

```yaml
- alias: "Hall light ON — board1 input1 single"
  triggers:
    - trigger: mqtt
      topic: hearth/<device_id>/board1/input1/action
      payload: single
  actions:
    - action: light.turn_on
      target: { entity_id: light.hall }

- alias: "Hall light OFF — board1 input1 long"
  triggers:
    - trigger: mqtt
      topic: hearth/<device_id>/board1/input1/action
      payload: long
  actions:
    - action: light.turn_off
      target: { entity_id: light.hall }
```

**Worked example — drive a Sonoff over its local REST API** (this is the setup running
on the lab HA). First define the REST calls in `configuration.yaml`:

```yaml
rest_command:
  sonoff_switch_on:
    url: "http://192.168.1.222:8081/zeroconf/switch"
    method: POST
    content_type: "application/json"
    payload: '{"deviceid":"10010b0e46","data":{"switch":"on"}}'
  sonoff_switch_off:
    url: "http://192.168.1.222:8081/zeroconf/switch"
    method: POST
    content_type: "application/json"
    payload: '{"deviceid":"10010b0e46","data":{"switch":"off"}}'
```

Then the automations in `automations.yaml` — **single** → on, **long** → off:

```yaml
- id: rp2040_sonoff_switch_on
  alias: RP2040 input2 single -> Sonoff on
  triggers:
    - trigger: mqtt
      topic: hearth/<device_id>/board1/input2/action
      payload: single
  actions:
    - action: rest_command.sonoff_switch_on

- id: rp2040_sonoff_switch_off
  alias: RP2040 input2 long -> Sonoff off
  triggers:
    - trigger: mqtt
      topic: hearth/<device_id>/board1/input2/action
      payload: long
  actions:
    - action: rest_command.sonoff_switch_off
```

> Replace `<device_id>`, the board/input numbers, and the IPs/entity ids with yours.
> After editing YAML, apply it in **Developer Tools → YAML → Reload automations** (a
> `rest_command:` change needs a full **Restart**). Tip: watch the presses arrive live
> with `mosquitto_sub -h <BROKER_IP> -t 'hearth/#' -v` while you tap the switch.

---

## What else shows up in Home Assistant

Beyond the press triggers, each gateway registers these in HA (via the first-party
**OSELIA integration** by default, or MQTT discovery when provisioned with `--mqtt`):

- **Per-input `event` entities** (`event.hearth_board<B>_input<N>`) with
  `single`/`double`/`long` event types — the modern, dashboard- and logbook-friendly
  way to react to presses, and what the **blueprint** below targets.
- **Diagnostics** (on the device page): uptime, IP address, RP2040 die temperature,
  free memory, input boards online + their addresses, reconnect / dropped counters,
  Ethernet link, last input, and a "Last log" sensor.
- **Controls**: **Restart** and **Identify** buttons, and live-editable settings —
  **Long press time**, **Double-tap window**, **Debounce time** (`number`) and
  **Log level** (`select`). Changing these takes effect immediately and survives a
  reboot — no re-flashing.

### Auto-set-up Home Assistant

Once the unit is online the wizard **asks** to set up Home Assistant — press Enter to
do it. (Pass `--ha-setup` to do it without asking, or `--no-ha-setup` to skip silently.)

When you accept, by default (OSELIA integration) it does, straight into Home Assistant:

- **finds your HA** automatically on the network (mDNS; or pass `--ha-url`),
- **adds the OSELIA integration** if it isn't set up yet (pointed at the same broker),
  so the device shows up under OSELIA with no manual "Add integration" step,
  > **One-time prerequisite:** the OSELIA integration *component* must be installed in
  > Home Assistant first — via **HACS** as a custom repository
  > ([vmyronovych/oselia-hearth-di16g](https://github.com/vmyronovych/oselia-hearth-di16g),
  > category: Integration), then restart HA. The wizard *configures* the integration but
  > does not install the component itself.
- sets the **firmware release feed** so the device is OTA-updatable from the HA UI
  (if you didn't pass `--release-url` / `--github-token`, it **stops and asks** whether to
  provide them — the repo is private, so a GitHub token is needed for updates to appear), and
- (re)builds the **OSELIA Hearth dashboard** (`/oselia-hearth`) with a `gw-<id>` view
  for this gateway (logo + status + inputs-by-board + controls).

> **Want the dashboard + OTA feed with no device attached?** Run
> `provision.py --add-ha-hearth-dashboard --ha-url http://<ha>:8123`. It configures the
> release feed (defaulting to `…/vmyronovych/oselia-hearth-di16g-firmware/releases/latest`; override with
> `--release-url`, GitHub token from `--github-token`/`$OSELIA_GH_TOKEN`/`~/.config/oselia/gh_token`)
> and (re)builds the `/oselia-hearth` dashboard against an already-running HA, then exits —
> it never acquires or reflashes a board. (Plain `--ha-setup`, by contrast, is a *modifier
> on a full provision*: it acquires the board first, then does HA setup at the end.)

With the legacy `--mqtt` path it instead adds the **MQTT integration** and installs the
**"Hearth switch → actions"** blueprint (no curated dashboard — the device's entities
are auto-created under the MQTT integration).

It uses HA's API with a **long-lived access token**. You can supply it via
`--ha-token`, `$OSELIA_HA_TOKEN`, or `~/.config/oselia/ha_token` — **or just run
`--ha-setup` and the wizard prompts you for it**, with step-by-step instructions, and
offers to save it for next time. To create the token in Home Assistant: click your
**profile** (your name, bottom-left of the sidebar) → **Security** tab →
**Long-Lived Access Tokens** → **Create Token**. (HA setup is best-effort: if the
token is wrong or HA is unreachable, the unit is still online — just re-run later.)

## Moving the gateway to a different house / network

Everything site-specific — broker IP, MQTT login, DHCP-vs-static, board count — lives in
a small `site.json` on the board. **The gateway does not reconfigure itself**: until you
re-run the wizard at the new site it keeps trying the *old* broker and stays offline (LED
red/orange) on the new network. So when you relocate it:

1. Wire it in at the new site: 24 V inputs, **Ethernet into the new LAN**. Leave the
   **24 V supply OFF** (you connect USB next — never both at once).
2. Connect your laptop to the gateway by USB, with the laptop also on the **new**
   network (so broker auto-discovery can see the new broker).
3. Run `python3 provision.py`. It auto-discovers the **new** broker (or type its IP/port),
   re-validates it's reachable, and writes the new `site.json` — no re-flashing needed.
   - The old site's values are only offered as fallback defaults; choose the **new**
     broker when prompted.
   - DHCP is the default, so the gateway picks up a new IP automatically. Add
     `--static IP/GW/MASK` only if the new site requires a fixed address.
4. On **`PASS: device … is online`**, **unplug USB, then switch the 24 V supply ON** — the
   gateway runs on field power, the inputs come alive, and it's live on the new Home
   Assistant. Re-label the switches there (they appear as `board<N>_input<M>` again — see
   *Naming your switches*).

The MicroPython interpreter and firmware already on the board carry over unchanged — a
move is purely a re-provision, not a re-flash.

## Changing things later / re-provisioning (same site)

Run `python3 provision.py` again on the same unit. It reads the existing settings and
offers them as defaults, so you can change just the broker, swap credentials, add a board
(wire it + power-cycle so it's discovered), etc. Re-running is safe.

---

## Advanced options (flags)

Most installs never need these. Add them after `python3 provision.py`:

| Flag | Use when |
|------|----------|
| `--port /dev/...` | Skip USB auto-detect, or pick between multiple boards. |
| `--broker IP[:PORT]` | Skip auto-discovery (e.g. broker on another VLAN). |
| `--static IP/GW/MASK` | The site requires a fixed IP instead of DHCP. |
| `--boards N` | Pin an explicit board count (1–8) instead of auto-detecting. |
| `--names names.csv` | Bake on-device names. CSV rows: `board,pin,name`. |
| `--no-diag` | Turn off device diagnostics (no `diag/*` topics or HA diagnostic entities). |
| `--mqtt` | LEGACY: provision for MQTT discovery instead of the **default** OSELIA integration (device appears under the MQTT integration; no curated dashboard). |
| `--oselia` | Use the OSELIA integration explicitly — now the default, so this is implicit. |
| `--ha-setup` | Set up HA without asking (OSELIA: integration + release feed + **`/oselia-hearth` dashboard**; `--mqtt`: MQTT integration + blueprint). Without it, the wizard **asks** once the unit is online. |
| `--no-ha-setup` | Skip the Home Assistant setup step without asking. |
| `--add-ha-hearth-dashboard` | **No device/flash:** do the OSELIA HA-side setup against `--ha-url`, then exit — set the firmware **release feed** (defaults to `https://api.github.com/repos/vmyronovych/oselia-hearth-di16g-firmware/releases/latest`; override with `--release-url`) and (re)build the **`/oselia-hearth` dashboard**. The integration installs via **HACS** (broker set in *Add Integration*). Use it to set up / refresh the dashboard + OTA feed without re-provisioning a gateway. |
| `--ha-url URL` | HA address for `--ha-setup` (default: auto-detect via mDNS, else `http://<broker-ip>:8123`). |
| `--ha-token TOKEN` | HA long-lived token for `--ha-setup` (else `$OSELIA_HA_TOKEN`, else `~/.config/oselia/ha_token`, else the wizard prompts you). |
| `--skip-mpy-check` | Don't check the MicroPython version or offer to flash it. |
| `--mpy-uf2 PATH` | Flash this MicroPython UF2 instead of the bundled pinned build (`uf2/`). |
| `--erase-flash` | UNINSTALL: erase the entire flash (incl. MicroPython) → bare-metal RP2040 in BOOTSEL. |
| `--erase-uf2 PATH` | `flash_nuke.uf2` for `--erase-flash` instead of the bundled one (`uf2/`). |
| `--no-flash` | Update config only; firmware already on the board. |
| `--dry-run` | Show what *would* be written without touching the board. |
| `--monitor` | Stream the firmware's log over USB (no flashing; Ctrl-C to stop) — see *Watching live logs* below. |
| `--monitor-passive` | With `--monitor`, only *listen* to a running unit's serial without restarting it. |

---

## Uninstalling / decommissioning

To remove a unit (separate from provisioning — each exits after doing its job):

```bash
# Full decommission: remove from HA (incl. the MQTT integration) AND erase the board.
python3 provision.py --uninstall-all

# ...or the individual steps:
python3 provision.py --uninstall-firmware      # erase app files (keeps MicroPython)
python3 provision.py --erase-flash             # erase EVERYTHING incl. MicroPython -> bare-metal RP2040
python3 provision.py --uninstall-ha            # remove from HA; device id + broker read from the board
python3 provision.py --uninstall-ha --device-id 893922 --broker 192.168.1.104   # board not attached
python3 provision.py --uninstall-ha --uninstall-ha-mqtt   # also remove the HA MQTT integration
```

`--erase-flash` wipes the **whole** flash (MicroPython interpreter *and* files) using
Raspberry Pi's `flash_nuke.uf2`, leaving a bare-metal RP2040 in BOOTSEL — it confirms
first (irreversible) and uses the **bundled** UF2 (`uf2/`, offline; or `--erase-uf2 PATH`).
To reuse the board, re-run `provision.py` (it offers to flash MicroPython) or see
`../firmware/FLASHING.md`.

`--uninstall-ha` clears the unit's retained MQTT discovery (the device disappears from
HA), rebuilds the shared `/oselia-hearth` dashboard without this gateway's view
(deleting the dashboard only if no gateways remain), and removes the blueprint. It reads
the device id and broker from the
connected board's `site.json` when present; otherwise pass `--device-id` / `--broker`.
If the broker isn't on the board (e.g. it was already wiped), the wizard **finds it on
the network** instead of failing. It needs an HA token (same sources as `--ha-setup`,
prompted if missing).
**`--uninstall-all`** does `--uninstall-ha` (with `--uninstall-ha-mqtt`) **then**
`--uninstall-firmware` in that order — HA cleanup first, since it reads the board's
`site.json` before the wipe erases it.

---

## Watching live logs / diagnostics (over USB)

To watch what the gateway does as it boots — useful when a unit won't come online, or just
to confirm a healthy bring-up — run, on a board that's **already running** (plugged into
USB and visible):

```bash
python3 provision.py --monitor
```

This **does not flash or re-provision** anything. It relaunches the firmware over USB and
streams its log to your terminal: you'll see the gateway from the banner on — Ethernet/CH9120
link, the input boards it discovers, its leased IP, MQTT connect, and
`HA discovery published/skipped` — then live runtime lines. Levels are colour-coded:
**errors** red, **warnings** yellow, info plain. Press Ctrl-C to stop.

> **Why it launches the firmware instead of just reading the port.** This board has a quirk:
> after a *cold* reset the firmware can wedge USB enumeration (its two CPU cores contend
> during start-up). `--monitor` sidesteps that by holding a USB session open and starting the
> firmware *from* it — USB is already up, so it stays up, and you get the full log. (It does
> a quick internal reset to a clean state first; that's not a re-flash.) When you Ctrl-C, the
> board is left at its REPL — `mpremote reset`, or power-cycle, to resume normal autorun.

**Listening to a unit without interrupting it** (e.g. a stable field unit you must not
restart) — add `--monitor-passive`, which only reads its serial:

```bash
python3 provision.py --monitor --monitor-passive
```

> **My board doesn't show up on USB at all.** The monitor needs a board that's already
> running MicroPython — it never flashes. If a unit vanished from USB right after
> provisioning, that's the wedge quirk: recover it via **BOOTSEL** (hold the BOOT button
> while plugging in USB — it then appears as an `RPI-RP2` *drive*, not a serial device, so
> there's nothing to stream yet) and re-flash with `python3 provision.py`. Once it's running
> again, `--monitor` can stream it. See `../firmware/BRINGUP.md`.

For passive mode, `pip install pyserial` gives the most reliable capture (it falls back to a
raw read on macOS/Linux; on Windows pyserial is required). The default `--monitor` only needs
`mpremote`.

---

## Troubleshooting

| Message | Likely cause / fix |
|---------|--------------------|
| `No RP2040-ETH detected over USB` | Cable not plugged in, or wrong port — try `--port`. If it vanished *after* provisioning, it's the USB-wedge quirk: recover via **BOOTSEL** (hold BOOT while plugging in), then re-run. See *Watching live logs*. |
| `No MicroPython detected` / version mismatch | The wizard offers to flash the pinned build automatically — accept it (needs internet, or pass `--mpy-uf2 PATH`). Manual steps: `../firmware/FLASHING.md`. |
| `BOOTSEL drive (RPI-RP2) didn't appear` | The board didn't enter BOOTSEL; do the BOOT+RESET dance by hand (`../firmware/FLASHING.md`) and retry. |
| `could not enter raw repl` | A running unit's watchdog fights `mpremote`; the wizard now pauses the firmware before writing and retries. If it persists, unplug/replug the board (or tap RESET) and re-run. |
| `cannot reach <ip>:<port>` | Wrong broker IP/port, broker down, or laptop on a different network. |
| `broker refused: bad username or password` | Fix the credentials and re-run. |
| `FAIL (ethernet)` | Ethernet cable / broker IP unreachable **from the gateway's** network. |
| `FAIL (mqtt)` | Broker reachable but login/session failed — recheck IP/port and credentials. |
| `FAIL (mcp)` | An input board isn't responding — check I²C wiring and that the board count matches the chips actually installed. |
| `zeroconf` package missing | The wizard offers to install it; accept to enable auto-discovery, or decline and type the broker IP manually. |

When a `FAIL (…)` doesn't tell you enough, run `python3 provision.py --monitor` to watch the
board's full boot over USB and see exactly where it stalls (see *Watching live logs* above).

The wizard exits non-zero on failure, so it can be scripted.
