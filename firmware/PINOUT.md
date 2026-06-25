# RP2040-ETH pinout — as used by the `dib-monolith` board

The stock Waveshare RP2040-ETH pinout is in [`docs/rp2040-eth-pinout.png`](docs/rp2040-eth-pinout.png).
This page overlays **which of those pins the manufactured `dib-monolith` PCB actually
uses**, so you can probe/trace the board against the module. Values come from the PCB
netlist (`hardware/dib-monolith.eprj`) and match `config.py`; see SPEC.md §2.2 and
POC_NOTES.md for the derivation.

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
| **GP22**   | GP22 (GPIO/PIO) | `INTA` | MCP shared wired-OR INT (active-low, IRQ) | `PIN_MCP_INT=22` |
| **GP9**    | SPI1 CSn / I²C0 SCL | `RESET` | MCP `/RESET` (active-low, MCU-driven) | `PIN_MCP_RESET=9` |
| **VSYS**   | Vsys (power in) | `VCC_3V3` | Board 3V3 rail powers the module via VSYS | — |
| **GND ×4** | G / AGND | `GND` | Ground (pads 3 & 8 of **both** header rows) | — |

> GP26/GP27 are the RP2040's **native I²C1** SDA/SCL pair — that hardware alignment is
> what anchors the header orientation. The breadboard POC used different pins (I²C0
> GP0/GP1, INT GP2, RESET tied high); don't mix them up — see POC_NOTES.md.

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
