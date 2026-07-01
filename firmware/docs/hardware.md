# RP2040-ETH pinout — as used by the `dib-monolith` board

The stock Waveshare RP2040-ETH pinout is in [`rp2040-eth-pinout.png`](rp2040-eth-pinout.png).
This page overlays **which of those pins the manufactured `dib-monolith` PCB actually
uses**, so you can probe/trace the board against the module. Values come from the PCB
netlist (`hardware/dib-monolith.eprj`) and match `config.py`; see `spec.md` §2.2 and the
[Provenance](#provenance--poc-confirmed-facts) section below for the derivation.

This is the single home for how the board is wired **and** how its devices are
initialized — pin map + powering (below), then the confirmed CH9120 / MCP23017 /
press-detection facts, then their POC provenance.

```
                              ┌──── USB-C ────┐
        dib-monolith use      │   RP2040-ETH  │   dib-monolith use
        ──────────────────────┤   (Waveshare) ├──────────────────────
                         VBUS ─┤ ●           ● ├─ GP0
             3V3 rail ──► VSYS ─┤ ●           ● ├─ GP1
                  GND ──►  GND ─┤ ●           ● ├─ GND  ◄── GND
                       3V3_EN ─┤ ●           ● ├─ GP2
                      3V3(OUT) ─┤ ●           ● ├─ GP3
                      ADC_VREF ─┤ ●           ● ├─ GP4
                         GP28 ─┤ ●           ● ├─ GP5
                  GND ──► AGND ─┤ ●           ● ├─ GND  ◄── GND
             I²C1 SCL ◄── GP27 ─┤ ●           ● ├─ GP6
             I²C1 SDA ◄── GP26 ─┤ ●           ● ├─ GP7
                          RUN ─┤ ●           ● ├─ GP8
              MCP INT ◄── GP22 ─┤ ●           ● ├─ GP9  ──► MCP /RESET
        ──────────────────────┤               ├──────────────────────
                              └──── RJ45 ─────┘
   (left column = power/ADC side · right column = GP0–GP9 side · USB at top)
```

## Pins the board connects

| RP2040 pin | Module label | Board net | Role | `config.py` |
|------------|--------------|-----------|------|-------------|
| **GP26**   | ADC0 / I²C1 SDA | `SDA`  | MCP23017 I²C data  | `PIN_I2C_SDA=26`, `I2C_ID=1` |
| **GP27**   | ADC1 / I²C1 SCL | `SCK`  | MCP23017 I²C clock | `PIN_I2C_SCL=27`, `I2C_ID=1` |
| **GP22**   | GP22 (GPIO/PIO) | `INTA` | MCP shared wired-OR INT — **not used** (firmware polls) | — |
| **GP9**    | SPI1 CSn / I²C0 SCL | `RESET` | MCP `/RESET` (active-low, MCU-driven) | `PIN_MCP_RESET=9` |
| **VSYS**   | Vsys (power in) | `VCC_3V3` | Board 3V3 rail powers the module via VSYS | — |
| **GND ×4** | G / AGND | `GND` | Ground (pads 3 & 8 of **both** header rows) | — |

> GP26/GP27 are the RP2040's **native I²C1** SDA/SCL pair — that hardware alignment is
> what anchors the header orientation. The breadboard POC used different pins (I²C0
> GP0/GP1, INT GP2, RESET tied high); don't mix them up — see [Provenance](#provenance--poc-confirmed-facts).

## Powering the board

> ⚠️ **Do not connect USB-C while the 24 V supply is ON — pick one source at a time.**

The board's regulated **3V3 rail feeds the module's `VSYS`** pin (see the table above).
`VSYS` is also where the module's own USB path delivers power (USB 5 V → module regulator
→ `VSYS`/3V3). So with **both** USB and the 24 V→3V3 supply live, two sources fight on the
`VSYS`/3V3 net — which is why the board can't run on USB and field power simultaneously.

Practical rule for any USB work (flashing, `deploy.sh`, provisioning, serial logs):

1. Switch the **24 V supply OFF**.
2. Connect USB-C and do the work (the module runs fine on USB power alone).
3. **Unplug USB**, then switch the **24 V supply back ON** for normal operation.

Note this means you can't watch USB serial *and* drive the 24 V inputs at the same time —
once field power is on, observe the board over **MQTT + the status LED**, not USB.

## Module-internal pins (not on the breakout header)

These live inside the RP2040-ETH module and are **not** routed by the `dib-monolith` PCB;
the firmware uses them exactly as the Waveshare module wires them:

| RP2040 pin | Function |
|------------|----------|
| GP20 / GP21 | CH9120 UART TXD / RXD (`UART(1, tx=20, rx=21)`) |
| GP17 | CH9120 TCPCS (LOW = TCP connected) — **disabled in firmware** (`PIN_CH9120_TCPCS=None`); liveness is MQTT keepalive instead |
| GP18 | CH9120 CFG0 (LOW = config mode) |
| GP19 | CH9120 RSTI (active-LOW reset) |
| GP25 | onboard WS2812 RGB status LED (**RGB** wire order; driven via PIO) |

## Unused header pins

Everything else on the module's headers is unconnected on this board: VBUS, 3V3_EN,
3V3(OUT), ADC_VREF, RUN, GP28, and GP0–GP8 on the right row. They're free for future use.

## CH9120 sequence (confirmed)

- Config UART baud = **9600**; transparent UART baud = **115200**. Configure at
  9600, then re-open `UART(1, baudrate=115200, tx=Pin(20), rx=Pin(21))`.
- `enter_config`: `RST=1`, `CFG0=0`, wait 500 ms.
- Command frame = `b"\x57\xab" + opcode + payload`, ~100 ms between commands:
  - `0x10` mode (1B), `0x11` local IP (4B), `0x12` subnet (4B), `0x13` gateway (4B),
  - `0x14` local port (2B LE), `0x15` target IP (4B), `0x16` target port (2B LE),
  - `0x21` baud (4B LE).
- `exit_config`: write `\x57\xab\x0D`, `\x57\xab\x0E`, `\x57\xab\x5E`, then `CFG0=1`.
- Mode = TCP client (`1`), target port 1883 (broker). No DNS — numeric IP only.

## MCP23017 init (confirmed)

`IODIRA/B=0xFF` (inputs), `GPPUA/B=0xFF` (pull-ups), `INTCONA/B=0x00`
(compare-to-previous), `GPINTENA/B=0xFF`, `IOCON=0x40` (MIRROR + active-low INT).
Clear by reading INTCAP/GPIO. **Active-low**: pin level 0 = pressed.

> **Multi-board extension (beyond the POC):** the POC used a single chip at 0x20.
> The firmware supports multiple chips on the shared bus (0x20..0x27) with a single
> **wired-OR** INT line, which requires `IOCON=0x44` (adds `ODR` open-drain) instead
> of `0x40`. This part is not POC-verified — confirm the shared INT + pull-up on
> hardware. Single-chip behaviour is unchanged if `MCP_ADDRESSES = [0x20]`.

## Press detection (POC values)

`LONG_PRESS_TIME = 400 ms`, `DOUBLE_CLICK_TIME = 300 ms` (the in-code values; a
header comment of 500/350 is stale). Active-low transitions; long emitted on the
threshold while held (and again guarded on release).

## Provenance — POC-confirmed facts

The device-init values above (CH9120 framing, MCP registers, press timings) and the
pin map were extracted from a **working** proof-of-concept the user verified on real
RP2040-ETH + MCP23017 + CH9120 hardware. They override earlier datasheet/family
guesses. Source files were a read-only reference (`dib/app/ch9120.py`,
`RP2040-ETH-MQTT.py`, `rp2040-eth-mqtt-dip-v1.py`, `mqtt_client.py`).

Two migrations from the POC to the manufactured board:

| Signal       | POC (breadboard)         | Manufactured board                |
|--------------|--------------------------|-----------------------------------|
| I2C bus      | I2C0, SDA GP0 / SCL GP1  | I2C1, SDA **GP26** / SCL **GP27** |
| MCP INT      | GP2                      | **GP22**                          |
| MCP /RESET   | tied high (none)         | **GP9** (driven by MCU)           |
| WS2812 LED   | GP25, GRB order          | GP25, **RGB** order (via PIO)     |

`config.py` reflects the manufactured-board column, **not** the POC's breadboard
wiring — don't "restore" POC pin values. GP26/GP27 are the RP2040's native I2C1 pair,
which anchors the mapping; see `hardware/dib-monolith.eprj` for the source netlist.

> **Trust the code, not stale comments.** The POC header carried a wrong
> `# I2C SDA=GP16, SCL=GP17`; the actual code used GP0/GP1. Likewise the LED is
> RGB-order here (GRB shows green-as-red), clocked over the RP2040 PIO — so it works
> regardless of the interpreter build (boards have shipped with 1.15.0, which lacks
> `neopixel`/`bitstream`; reflash to 1.28.0 per `flashing.md`).

> **MQTT & discovery (historical note):** the POC ran a tiny MQTT-over-UART client
> (CONNECT level 4, keepalive 60, PUBLISH QoS0, PINGREQ, SUBSCRIBE) with no CONNACK
> read, LWT, or auth, and published `binary_sensor` + a separate `.../event` topic.
> The shipped firmware keeps that proven wire format (`mqtt_packets.py`) but adds
> LWT/auth/CONNACK + keepalive timing, and uses the device-automation trigger /
> `event`-entity discovery of the current contract (`mqtt-contract.md`, `spec.md §5`).

## Authoritative hardware references

The upstream sources behind the confirmed facts above — consult these to verify the
`spec.md §11` HW-VERIFY items:

- **Waveshare RP2040-ETH wiki** + `RP2040-ETH_CODE.zip` — CH9120 config command bytes,
  exact UART tx/rx pins.
- **CH9120 datasheet** — config-mode command framing.
- **MCP23017 datasheet** — `IOCON.MIRROR`, `INTCAP`, interrupt-on-change.
- **Home Assistant** — MQTT Device Trigger / `event` discovery schema.
