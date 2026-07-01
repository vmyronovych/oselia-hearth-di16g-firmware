# Bench bring-up checklist

> ⚠️ **USB-C and the 24 V supply are mutually exclusive on this board — never both on.**
> Do all USB work (deploy, serial logs) with the **24 V OFF**; to exercise the 24 V inputs,
> **unplug USB first**, then power 24 V and observe over **MQTT + the status LED** (you
> can't watch USB serial and drive inputs at the same time). See `hardware.md` →
> "Powering the board".

Tick top-to-bottom. Each stage gates the next — don't move on until the
"expected" line is true. LED reference (status_led; one LED shows the **highest-
priority** fault — root cause first): **blue** solid=booting, **orange** med-blink=MQTT/link
down, **yellow** fast-blink=an MCP not responding, **green** solid=all healthy, brief
**white** flash=a gesture published.
Full table + caveats in `README.md` → "Status LED".

Handy terminal (run on a PC on the same LAN as the broker):
```
mosquitto_sub -h <BROKER_IP> -t 'hearth/#' -v
mosquitto_sub -h <BROKER_IP> -t 'homeassistant/device_automation/#' -v
```

**Automation:** the deterministic steps below are scripted in `tools/` (see
`tools/README.md`) and orchestrated by the `hw-test` skill:
```
tools/deploy.sh                 # flash src/*.py, verify sizes, reset (stages 2)
tools/watch.sh status 10        # availability online/offline      (stages 5, 8)
tools/watch.sh discovery 4      # retained HA discovery configs    (stage 7)
tools/watch.sh actions 45       # press switches; single/double/long (stage 6)
tools/bounce-test.sh            # broker-outage self-heal regression (stage 8)
tools/serial.sh 25              # capture a fresh boot's serial logs
```
Use the scripts for the mechanics; keep ticking this checklist for the physical
and HA steps the scripts can't do (wiring, switch presses, HA UI).

---

## 0. Prep (single board first)
- [ ] Bench PSU on the 24 V side current-limited (for the input-test stages — keep it
      **OFF while USB is connected**; see the warning at the top).
- [ ] Only **one** MCP board on the I²C bus for now (address 0x20, A0–A2 = 000).
- [ ] External pull-up (~4.7 kΩ) on SDA and SCL present.
- [ ] For deploy/serial stages: **24 V OFF**, then USB serial to the RP2040-ETH open
      (`mpremote ... repl`) to watch logs. For input stages: unplug USB, 24 V ON, and
      watch over MQTT/LED instead (can't do both at once).

## 1. Configure
- [ ] `cp config.example.py config.py`.
- [ ] Set `BROKER_IP` (numeric), `LOCAL_IP`, `GATEWAY`, `SUBNET_MASK`.
- [ ] Set `MCP_ADDRESSES = [0x20]` for now.
- [ ] (If broker needs auth) set `MQTT_USER` / `MQTT_PASS`.

## 2. Flash + deploy
- [ ] MicroPython UF2 on the board — pinned to **v1.28.0** (RPI_PICO build); see
      `flashing.md` for the exact file and steps (BOOTSEL via the **BOOT+RESET** button
      dance or `machine.bootloader()`, drag the UF2, it reboots). The littlefs
      filesystem survives a UF2 flash.
- [ ] `mpremote connect <port> fs cp config.py :`
- [ ] `mpremote connect <port> fs cp src/*.py :`
- [ ] Reset. **Expected:** REPL prints version + `id=…`, then config/bring-up logs;
      no traceback. Config errors print an assertion from `_validate_config`.

## 3. I²C / MCP23017 (one chip)
- [ ] Log shows `board1 MCP@0x20 ready` (no `init failed`).
- [ ] Press an input → REPL logs `gesture idx… ` for that pin.
- [ ] **Expected:** every physical input maps to the pin you think it is
      (note any swaps; fix wiring, not code).

## 4. Shared INT line  ← *not proven by the POC; verify carefully*
- [ ] Confirm INT (GP22) idles **high** and pulses **low** on a press (scope/meter).
- [ ] Presses are caught without polling lag (IRQ working, not just health poll).
- [ ] **Expected:** no "stuck" INT — after a burst of presses the line returns high
      and new presses still register.

## 5. CH9120 → broker
- [ ] LED leaves blue; reaches **green** (or red/orange if not connected).
- [ ] `hearth/<id>/status` shows `online` (retained) in `mosquitto_sub`.
- [ ] Press inputs → `hearth/<id>/board1/input<p>/action` shows `single` /
      `double` / `long`.
- [ ] **Expected:** quick tap=`single`, two quick taps=`double` (no stray single),
      hold=`long` (no single/double). White LED flash on each publish.

## 6. Gesture tuning
- [ ] Adjust `DEBOUNCE_MS` if chatter / missed taps.
- [ ] Adjust `LONG_MS` and `DOUBLE_GAP_MS` to feel; redeploy `config.py`.

## 7. Home Assistant
- [ ] Discovery topics appear under `homeassistant/device_automation/#` (retained).
- [ ] Device shows in HA: Settings → Devices → "Hearth".
- [ ] Creating an automation lists single/double/long triggers per input
      (subtype `board<b>_input<p>`).
- [ ] Wire one test automation (e.g. board1 input1 double → toggle a light); fire it.

## 8. Robustness paths
- [x] **Broker bounce:** stop broker → `status` (LWT) → `offline`; restart →
      reconnects with backoff, `online`, discovery republished.
      *(verified via `tools/bounce-test.sh`; LED colour not visually checked)*
- [ ] **Offline buffering:** press while broker down, bring broker back → buffered
      gestures flush (within `EVENT_QUEUE_SIZE`).
- [x] **MCP pull (I²C line):** disconnect SDA/SCL → read fails `EIO`, LED **yellow**
      fast-blink, board stays **online** (no reboot); reconnect → auto re-init within
      `MCP_HEALTHCHECK_MS`, back to **green**, presses resume. *(verified on hardware)*
- [x] **Ethernet pull / sustained outage:** unplug cable (or stop broker) → LED **orange
      (MQTT/link down)**, board stays up, reconnects via CH9120 re-bring-up + exponential
      backoff (cap `RECONNECT_BACKOFF_MAX_MS`); replug → back to **green**, unit re-online on
      the broker. The blocking reconnect keeps core1's heartbeat ticking, so a long outage
      does **not** starve the watchdog. *(verified on hardware: no reboot; no `core1 stalled`;
      unit re-online after replug.)*
- [x] **Watchdog:** suspend core1's WDT feed (host raw-REPL session) → board hard-resets at
      ~8 s; `machine.reset_cause()` reports `WDT_RESET`. *(verified on hardware)*

> **Known limitation — MCP *power* loss can reboot the board.** Disconnecting an
> I²C *line* degrades gracefully (above). But cutting an MCP's *power* while it stays
> wired to the bus rebooted the board (observed: `status` → `offline` → `online`,
> USB re-enumerated). Likely cause, to confirm by **timing**: a half-powered chip
> can hold the bus **low** → core 0 stalls → ~8 s **watchdog** reset (≈8 s after the
> cut); or, if the MCP shares the RP2040's 3.3 V rail, a **brownout** (instant). The
> watchdog reboot is the intended failsafe, so this is acceptable. To make even this
> case degrade gracefully, add **I²C bus-recovery** (bounded SCL clock-out + I²C
> re-init on a bus error) and/or give each MCP board its **own 3.3 V feed**. Narrow
> edge case (not normal operation); left as-is for now.

## 9. Scale to multiple boards
- [ ] Add board 2 (MCP at **0x21**, A0–A2 = 001); tie its INT to the shared net.
- [ ] Set `MCP_ADDRESSES = [0x20, 0x21]`; redeploy `config.py`; reset.
- [ ] Log shows both `board1…ready` and `board2…ready`.
- [ ] Presses on board 2 publish `…/board2/input<p>/action`.
- [ ] Pull board 2 → board 1 keeps working and **board 1 numbering is unchanged**.
- [ ] Repeat for boards 3–5 (0x22, 0x23, 0x24), watching I²C integrity (pull-ups,
      bus length). Add/keep the external ~4.7 kΩ pull-up on the INT net.

## Sign-off
- [ ] All inputs across all boards classify correctly and reach HA.
- [ ] Reconnect, MCP-recovery, and watchdog behaviours verified.
- [ ] Timings tuned and `config.py` saved/backed up.

Cross-references: pins/wiring → spec.md §2; MCP regs → §7; MQTT topics → §5;
robustness → §12; open hardware items → §11; confirmed POC facts → hardware.md.
