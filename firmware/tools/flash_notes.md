# Flashing & deployment

## 1. Put MicroPython on the board (once)

See **`../FLASHING.md`** for the pinned version and full steps. In short: enter BOOTSEL
(**BOOT+RESET** dance — hold both, release RESET first, then BOOT — or
`machine.bootloader()`) → a `RPI-RP2` disk appears → copy
`RPI_PICO-20260406-v1.28.0.uf2` onto it → it reboots into MicroPython 1.28.0.

> Don't grab "whatever UF2 is handy" — boards have shipped with an ancient 1.15.0 image
> that breaks the status LED (no `neopixel`/`bitstream`). Use the pinned 1.28.0 build.

## 2. Copy the firmware

Using `mpremote` (recommended):

```bash
mpremote connect /dev/ttyACM0 fs cp config.py :
mpremote connect /dev/ttyACM0 fs cp src/*.py :   # all modules to root
```

> MicroPython resolves `import config`, `import main`, etc. from the filesystem
> root, so copy the `src/*.py` files to `:` (root), not into a `src/` dir on the
> board — or add `sys.path` handling. Keep `config.py` at root.

To auto-run on power-up, name the entry `main.py` at root (it already is) — MicroPython
runs `boot.py` (if present) then `main.py` automatically. This firmware ships **no**
`boot.py`: the root `main.py` is the OTA loader, so the rp2 port initialises USB-CDC
natively before it runs (USB-CDC is inited only between `boot.py` and `main.py`).

## 3. Watch logs

```bash
mpremote connect /dev/ttyACM0 repl     # see print() output; Ctrl-] to exit
```

## 4. Verify the MQTT side from a PC

```bash
mosquitto_sub -h <broker_ip> -t 'homeassistant/device_automation/#' -v   # discovery
mosquitto_sub -h <broker_ip> -t 'hearth/#' -v                         # actions + status
```

Press a switch and confirm exactly one `single` / `double` / `long` per gesture.

## Useful references

- Waveshare RP2040-ETH wiki + `RP2040-ETH_CODE.zip` (CH9120 config command bytes,
  exact UART tx/rx pins) — the authoritative source for §11 HW-VERIFY items.
- CH9120 datasheet — config-mode command framing.
- MCP23017 datasheet — IOCON.MIRROR, INTCAP, interrupt-on-change.
- Home Assistant: MQTT Device Trigger discovery schema.
