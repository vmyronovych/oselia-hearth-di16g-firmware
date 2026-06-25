# POC-confirmed hardware facts

These values were extracted from a **working** proof-of-concept that the user
verified runs on the real RP2040-ETH + MCP23017 + CH9120 hardware. They override
the earlier datasheet/family guesses in the scaffold. The POC is a read-only
reference — do not modify it.

Source files (reference only):
- `dib/app/ch9120.py` — CH9120 config command framing
- `dib/app/RP2040-ETH-MQTT.py` — UART/I2C pin setup, MCP init
- `dib/app/rp2040-eth-mqtt-dip-v1.py` — INT pin, LED, press detection, HA discovery
- `dib/app/mqtt_client.py` — minimal MQTT-over-UART client

## Pins (confirmed)

| Signal              | GPIO | Evidence (POC)                                   |
|---------------------|------|--------------------------------------------------|
| CH9120 UART TX (MCU)| GP20 | `UART(1, ..., tx=Pin(20), rx=Pin(21))`           |
| CH9120 UART RX (MCU)| GP21 | same                                             |
| CH9120 CFG0         | GP18 | `self.CFG = Pin(18, ...)`                         |
| CH9120 RST          | GP19 | `self.RST = Pin(19, ...)`                         |
| CH9120 TCPCS        | GP17 | NOT used by POC; firmware now also DISABLES it (`=None`) — unverified status pin caused a reconnect flap; liveness is MQTT keepalive |
| I2C0 SDA            | GP0  | `I2C(0, sda=Pin(0), scl=Pin(1))`                 |
| I2C0 SCL            | GP1  | same                                             |
| MCP23017 INT        | GP2  | `INT_PIN = 2`, `IRQ_FALLING`, `PULL_UP`          |
| MCP23017 address    | 0x20 | `MCP_ADDR = 0x20`                                |
| WS2812 status LED   | GP25 | `LED_PIN = 25` (fed in **GRB**: `led[0]=(g,r,b)`)|

> The stale comment `# I2C SDA=GP16, SCL=GP17` in the POC header is wrong — the
> actual code uses GP0/GP1. Trust the code, not the comment.

> **Manufactured board (`hardware/dib-monolith`) re-routes the MCP-side pins.**
> The CH9120 pins are internal to the RP2040-ETH module and unchanged, but the I2C bus,
> INT and RESET move to different module pads. The values below were read out of the PCB
> netlist and cross-checked against the official RP2040-ETH pinout; `config.py` reflects
> them, not the POC's breadboard wiring:
>
> | Signal       | POC (breadboard) | Manufactured board        |
> |--------------|------------------|---------------------------|
> | I2C bus      | I2C0, SDA GP0 / SCL GP1 | I2C1, SDA **GP26** / SCL **GP27** |
> | MCP INT      | GP2              | **GP22**                  |
> | MCP /RESET   | tied high (none) | **GP9** (driven by MCU)   |
> | WS2812 LED   | GP25, GRB order  | GP25, **RGB** order (driven via PIO) |
>
> GP26/GP27 are the RP2040's native I2C1 SDA/SCL pair, which is what anchors the
> mapping. See `hardware/dib-monolith.eprj` (PCB doc) for the source netlist, and
> `PINOUT.md` for a visual map of these pins on the RP2040-ETH module.
>
> **Interpreter:** boards have shipped with MicroPython **1.15.0** (no `neopixel` /
> `machine.bitstream`); reflash to **1.28.0** per `FLASHING.md`. The status LED is
> RGB-order on this board (GRB shows green-as-red) and is clocked over the RP2040 PIO,
> so it works regardless of which interpreter build is on the board.

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
> The firmware supports up to 5 chips on the shared bus (0x20..0x24) with a single
> **wired-OR** INT line, which requires `IOCON=0x44` (adds `ODR` open-drain) instead
> of `0x40`. This part is not POC-verified — confirm the shared INT + pull-up on
> hardware. Single-chip behaviour is unchanged if `MCP_ADDRESSES = [0x20]`.

## Press detection (POC values)

`LONG_PRESS_TIME = 400 ms`, `DOUBLE_CLICK_TIME = 300 ms` (the in-code values; a
header comment of 500/350 is stale). Active-low transitions; long emitted on the
threshold while held (and again guarded on release).

## MQTT (confirmed approach, not the final contract)

The POC implements a tiny MQTT-over-UART client: CONNECT (level 4, clean session,
keepalive 60), PUBLISH QoS0, PINGREQ heartbeat, SUBSCRIBE. It does **not** read
CONNACK, set LWT, or authenticate. Our `mqtt_client.py` keeps this proven wire
format and adds LWT/auth/CONNACK + keepalive timing.

## HA discovery difference (note)

The POC publishes **binary_sensor** discovery + a separate `.../event` topic with
`single|double|long`. Per the agreed design we instead use **device_automation
triggers** (SPEC.md sec.5). The press-classification logic is equivalent; only the
discovery schema differs.
