# Flashing MicroPython on the RP2040-ETH

The base interpreter is a one-time, physical (BOOTSEL) step — separate from deploying
our app (`tools/deploy.sh` copies the `.py` files onto the already-flashed interpreter).
Do this when you bring up a **new** RP2040-ETH board, or to update the interpreter.

> 💡 **The `oselia` tool automates this.** `oselia flash` (or `oselia provision`, which
> checks the version first) reboots the board to BOOTSEL → copies the pinned UF2 → waits for
> reboot, on a bare/BOOTSEL board or one already running MicroPython. The UF2 ships bundled
> (offline), or use `--mpy-uf2 PATH`. This page is the manual reference / what the tool does
> under the hood — keep the version here in step with `EXPECTED_MPY_VERSION` / `MPY_UF2_NAME`
> in `provisioning/oselia_provision/constants.py`.

> ⚠️ **Power:** on the `dib-monolith` board you must **never** have USB-C connected and
> the **24 V supply on at the same time**. Switch the 24 V supply **OFF before** plugging
> in USB (for flashing *and* for `deploy.sh`), and only switch it back on after USB is
> unplugged. See `PINOUT.md` → "Powering the board".

## Which UF2

| | |
|---|---|
| **Version** | MicroPython **1.28.0** |
| **Build** | `RPI_PICO` (generic Raspberry Pi Pico / RP2040 image) |
| **File** | `RPI_PICO-20260406-v1.28.0.uf2` |
| **Download page** | https://micropython.org/download/RPI_PICO/ |
| **Direct URL** | https://micropython.org/resources/firmware/RPI_PICO-20260406-v1.28.0.uf2 |

**Why the generic `RPI_PICO` build?** The RP2040-ETH is a stock RP2040 — the Ethernet
is a **CH9120 driven over UART by our own code**, not MicroPython networking — so no
board-specific port is needed. The generic Pico image is correct.

**Why pin 1.28.0?** Boards have shipped with an ancient **1.15.0** image that lacks the
`neopixel` module and `machine.bitstream` (it silently breaks the status LED) and is
missing years of dual-core / stability fixes. 1.28.0 (2026-04-06) was the current stable
when this firmware was validated. A newer stable is fine, but re-run the host gate +
`BRINGUP.md` after changing it. (Our WS2812 driver uses PIO and does **not** depend on
`neopixel`, so it works on any RP2040 build.)

## How to flash

You need the board in **BOOTSEL** mode — it then mounts as a USB drive named `RPI-RP2`.

**Option A — software trigger (board already running MicroPython):**
```bash
PORT=$(mpremote connect list | awk '/MicroPython/{print $1; exit}')
mpremote connect "$PORT" exec "import machine; machine.bootloader()"   # reboots to BOOTSEL
```

**Option B — physical buttons (bare board, or when Option A can't break in):** the
RP2040-ETH has **two** buttons, **BOOT** and **RESET**. The reliable sequence (per the
[Waveshare wiki](https://www.waveshare.com/wiki/RP2040-ETH#MicroPython_Series)) is a
two-button dance — you must let **RESET go before BOOT**:

- If the board is **already plugged into USB**: press and **hold BOOT + RESET together**
  → **release RESET first** → then **release BOOT**.
- If starting from a **bare / unpowered board**: hold **BOOT + RESET**, plug in USB while
  holding both, then **release RESET first**, then **release BOOT**.

The `RPI-RP2` drive should appear. (Just "hold BOOT while plugging in" is **not** reliable
on this board — use the BOOT+RESET sequence above. This is the only method that worked on
our unit.)

Then copy the UF2 onto the mounted drive; the board flashes and reboots automatically:
```bash
curl -L -o /tmp/RPI_PICO-v1.28.0.uf2 \
  https://micropython.org/resources/firmware/RPI_PICO-20260406-v1.28.0.uf2
cp /tmp/RPI_PICO-v1.28.0.uf2 /Volumes/RPI-RP2/      # macOS; Linux: /media/<user>/RPI-RP2
```

> **The littlefs filesystem survives a UF2 flash** — flashing only rewrites the
> interpreter region, so existing `.py` files remain. Re-deploy anyway to be sure
> everything is current.

## After flashing

```bash
./tools/deploy.sh                                   # re-copy src/*.py + config.py, reset

# verify the interpreter + that the LED modules are present
PORT=$(mpremote connect list | awk '/MicroPython/{print $1; exit}')
mpremote connect "$PORT" exec "import sys; print(sys.implementation.version)"   # (1, 28, 0, '')
```

Then follow `BRINGUP.md` from §2. The USB port re-enumerates after the flash (the serial
device name may change); the `tools/` scripts auto-detect it.

### Re-flashing a board that already has firmware (watchdog interplay)

A UF2 flash **preserves the board's littlefs**, so re-flashing a previously provisioned
unit keeps `/main.py` (the loader) + `/slots/a`: the board reboots **straight into the firmware**,
which arms the hardware watchdog once the network is up. From then on, any `mpremote`
break-in stops the watchdog feed and the board resets within ~8 s. The wizard handles
this — after a re-flash it waits for the board to **re-enumerate on USB** (it no longer
relies on a REPL exec that the watchdog would interrupt) and then quiesces the firmware
to a bare REPL before writing files. If you ever flash by hand and see the port appear
then vanish on a loop, that's the watchdog: park the app first
(`mpremote exec "import os; os.rename('main.py','main.py.bak')"` then reset) or re-plug
to retry.
